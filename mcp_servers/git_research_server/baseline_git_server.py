from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import PROJECT_ROOT, get_config, get_paths

from .schemas import ModelRepoState


def _cfg():
    return get_config().mcp.git


@dataclass(frozen=True)
class ModelRepoContext:
    repo_id: str
    repo_path: Path
    baseline_dir: Path
    source_dir: Path
    requirements: Path
    allowed_pathspecs: list[str]
    branch_prefix: str
    remote: str
    base_branch: str
    push_target_branch: str
    push_on_keep: bool
    create_pr_on_keep: bool
    pr_draft: bool


def _resolve_in_repo(repo_path: Path, value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_path / path
    path = path.resolve()
    try:
        path.relative_to(repo_path)
    except ValueError as exc:
        raise ValueError(f"Git MCP {label} must stay inside repo_path: {path}") from exc
    return path


def _pathspecs(items: list[str]) -> list[str]:
    specs: list[str] = []
    for item in items:
        spec = str(item).strip()
        if not spec:
            continue
        if spec.endswith("/**"):
            spec = spec[:-3]
        path = Path(spec)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Git MCP allowed path must be relative and safe: {item}")
        specs.append(spec)
    return specs


def _repo_context(repo_id: str | None = None) -> ModelRepoContext:
    git_cfg = _cfg()
    repo_cfg = git_cfg.resolve_repo(repo_id)
    repo_path = Path(repo_cfg.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = PROJECT_ROOT / repo_path
    repo_path = repo_path.resolve()
    baseline_dir = _resolve_in_repo(repo_path, repo_cfg.baseline_dir, "baseline_dir")
    source_dir = _resolve_in_repo(repo_path, repo_cfg.source_dir, "source_dir")
    requirements = _resolve_in_repo(repo_path, repo_cfg.requirements, "requirements")
    allowed = _pathspecs(repo_cfg.allowed_paths)
    if not allowed:
        allowed = _pathspecs([
            f"{source_dir.relative_to(repo_path).as_posix()}/**",
            requirements.relative_to(repo_path).as_posix(),
        ])
    return ModelRepoContext(
        repo_id=repo_cfg.repo_id,
        repo_path=repo_path,
        baseline_dir=baseline_dir,
        source_dir=source_dir,
        requirements=requirements,
        allowed_pathspecs=allowed,
        branch_prefix=repo_cfg.branch_prefix,
        remote=repo_cfg.remote,
        base_branch=repo_cfg.base_branch,
        push_target_branch=repo_cfg.push_target_branch,
        push_on_keep=bool(repo_cfg.push_on_keep),
        create_pr_on_keep=bool(repo_cfg.create_pr_on_keep),
        pr_draft=bool(repo_cfg.pr_draft),
    )


def _repo_path(repo_id: str | None = None) -> Path:
    return _repo_context(repo_id).repo_path


def _baseline_dir(repo_id: str | None = None) -> Path:
    return _repo_context(repo_id).baseline_dir


def _baseline_src(repo_id: str | None = None) -> Path:
    return _repo_context(repo_id).source_dir


def _baseline_requirements(repo_id: str | None = None) -> Path:
    return _repo_context(repo_id).requirements


def _call_path_provider(provider: Callable[..., Path], repo_id: str | None) -> Path:
    try:
        return provider(repo_id)
    except TypeError:
        return provider()


def _rel(path: Path, ctx: ModelRepoContext | None = None) -> str:
    ctx = ctx or _repo_context()
    return path.resolve().relative_to(ctx.repo_path).as_posix()


def _allowed_pathspecs(repo_id: str | None = None) -> list[str]:
    return list(_repo_context(repo_id).allowed_pathspecs)


def _current_branch(ctx: ModelRepoContext | None = None) -> str:
    return _run_git(["branch", "--show-current"], check=False, ctx=ctx).stdout.strip()


def _current_head(ref: str = "HEAD", ctx: ModelRepoContext | None = None) -> str:
    return _run_git(["rev-parse", ref], check=False, ctx=ctx).stdout.strip()


def _run_git(
    args: list[str],
    *,
    check: bool = True,
    ctx: ModelRepoContext | None = None,
    repo_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    ctx = ctx or _repo_context(repo_id)
    cmd = ["git", *args]
    result = subprocess.run(
        cmd,
        cwd=str(ctx.repo_path),
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _with_repo(payload: dict[str, Any], ctx: ModelRepoContext) -> dict[str, Any]:
    return {"repo_id": ctx.repo_id, "repo_path": str(ctx.repo_path), **payload}


def _log(action: str, payload: dict[str, Any]) -> None:
    ACTION_LOG = get_paths().global_artifact("git_action_log")
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "action": action,
        "payload": payload,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with ACTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return files


def _parse_porcelain(text: str) -> list[str]:
    changes: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        changes.append(line[3:] if len(line) > 3 else line)
    return changes


def get_model_repo_state(repo_id: str | None = None) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    branch = _run_git(["branch", "--show-current"], check=False, ctx=ctx).stdout.strip()
    head = _run_git(["rev-parse", "HEAD"], check=False, ctx=ctx).stdout.strip()
    model_status = _run_git(["status", "--porcelain", "--", *ctx.allowed_pathspecs], check=False, ctx=ctx)
    project_status = _run_git(["status", "--porcelain"], check=False, ctx=ctx)
    model_changes = _parse_porcelain(model_status.stdout)
    state = ModelRepoState(
        branch=branch,
        head=head,
        model_dirty=bool(model_changes),
        model_changes=model_changes,
        project_changes=_parse_porcelain(project_status.stdout),
    )
    payload = _with_repo(state.to_dict(), ctx)
    _log("get_model_repo_state", payload)
    return payload


def snapshot_baseline_model(label: str, repo_id: str | None = None) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    SNAPSHOT_DIR = get_paths().global_artifact("model_snapshots_dir")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:80]
    snapshot_path = SNAPSHOT_DIR / f"{int(time.time())}_{ctx.repo_id}_{safe_label}"
    snapshot_path.mkdir(parents=True, exist_ok=False)

    if ctx.source_dir.exists():
        shutil.copytree(ctx.source_dir, snapshot_path / "src")
    if ctx.requirements.exists():
        shutil.copy2(ctx.requirements, snapshot_path / "requirements.txt")

    state = get_model_repo_state(repo_id=ctx.repo_id)
    metadata = _with_repo({
        "label": label,
        "branch": state.get("branch"),
        "head": state.get("head"),
        "baseline_dir": _rel(ctx.baseline_dir, ctx),
        "source_dir": _rel(ctx.source_dir, ctx),
        "requirements": _rel(ctx.requirements, ctx),
        "manifest": _file_manifest(snapshot_path / "src"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ctx)
    (snapshot_path / "snapshot.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    payload = {"snapshot_path": str(snapshot_path), **metadata}
    _log("snapshot_baseline_model", payload)
    return payload


def snapshot_model(label: str, repo_id: str | None = None) -> dict[str, Any]:
    return snapshot_baseline_model(label, repo_id=repo_id)


def create_model_trial_branch(
    trial_id: str,
    base_ref: str | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    branch = f"{ctx.branch_prefix}{trial_id}"
    args = ["switch", "-c", branch]
    if base_ref:
        args.append(base_ref)
    result = _run_git(args, check=False, ctx=ctx)
    if result.returncode != 0:
        existing = _run_git(["rev-parse", "--verify", branch], check=False, ctx=ctx)
        if existing.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        _run_git(["switch", branch], ctx=ctx)
    payload = _with_repo({"branch": branch, "base_ref": base_ref}, ctx)
    _log("create_model_trial_branch", payload)
    return payload


def diff_trial_model_code(trial_code_dir: str | Path, repo_id: str | None = None) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    trial = Path(trial_code_dir)
    if not trial.is_absolute():
        trial = PROJECT_ROOT / trial
    trial_src = trial / "src"
    summary: list[str] = []

    baseline_src = _call_path_provider(_baseline_src, ctx.repo_id)
    baseline_req = _call_path_provider(_baseline_requirements, ctx.repo_id)
    baseline_files = {
        p.relative_to(baseline_src).as_posix(): p
        for p in baseline_src.rglob("*")
        if p.is_file()
    } if baseline_src.exists() else {}
    trial_files = {
        p.relative_to(trial_src).as_posix(): p
        for p in trial_src.rglob("*")
        if p.is_file()
    } if trial_src.exists() else {}
    all_names = sorted(set(baseline_files) | set(trial_files))
    changed = 0
    added = 0
    removed = 0

    for name in all_names:
        if "__pycache__" in name or name.endswith(".pyc"):
            continue
        left = baseline_files.get(name)
        right = trial_files.get(name)
        if left is None and right is not None:
            added += 1
            summary.append(f"A src/{name}")
        elif left is not None and right is None:
            removed += 1
            summary.append(f"D src/{name}")
        elif left and right and _sha256(left) != _sha256(right):
            changed += 1
            summary.append(f"M src/{name}")

    trial_req = trial / "requirements.txt"
    if trial_req.exists():
        if not baseline_req.exists():
            added += 1
            summary.append("A requirements.txt")
        elif _sha256(baseline_req) != _sha256(trial_req):
            changed += 1
            summary.append("M requirements.txt")

    text = "\n".join(summary[:80]) or "No model code changes."
    payload = _with_repo({
        "trial_code_dir": str(trial),
        "changed": changed,
        "added": added,
        "removed": removed,
        "summary": text,
    }, ctx)
    _log("diff_trial_model_code", payload)
    return payload


def _ensure_model_paths_clean(repo_id: str | None = None) -> None:
    state = get_model_repo_state(repo_id=repo_id)
    if state.get("model_dirty"):
        raise RuntimeError(
            "baseline model code has uncommitted changes: "
            + ", ".join(state.get("model_changes", []))
        )


def sync_remote_base(
    remote: str | None = None,
    base_branch: str | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    """Fetch and fast-forward the configured base branch when model paths are clean."""
    ctx = _repo_context(repo_id)
    remote = remote or ctx.remote
    base_branch = base_branch or ctx.base_branch
    before = get_model_repo_state(repo_id=ctx.repo_id)
    if before.get("model_dirty"):
        payload = _with_repo({
            "synced": False,
            "blocked": True,
            "reason": "baseline model code has uncommitted changes",
            "model_changes": before.get("model_changes", []),
            "remote": remote,
            "base_branch": base_branch,
            "local_head": before.get("head"),
        }, ctx)
        _log("sync_remote_base", payload)
        return payload

    _run_git(["fetch", remote, base_branch], ctx=ctx)
    remote_ref = f"{remote}/{base_branch}"
    remote_head = _current_head(remote_ref, ctx)
    local_before = before.get("head", "")
    current_branch = before.get("branch", "")

    if current_branch != base_branch:
        switched = _run_git(["switch", base_branch], check=False, ctx=ctx)
        if switched.returncode != 0:
            _run_git(["switch", "-c", base_branch, "--track", remote_ref], ctx=ctx)

    _run_git(["merge", "--ff-only", remote_ref], ctx=ctx)
    after = get_model_repo_state(repo_id=ctx.repo_id)
    payload = _with_repo({
        "synced": True,
        "blocked": False,
        "remote": remote,
        "base_branch": base_branch,
        "remote_head": remote_head,
        "local_head_before": local_before,
        "local_head_after": after.get("head"),
        "branch_before": current_branch,
        "branch_after": after.get("branch"),
    }, ctx)
    _log("sync_remote_base", payload)
    return payload


def _safe_replace_dir(src: Path, dst: Path) -> None:
    """Replace directory contents while keeping the destination path stable."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in list(dst.iterdir()):
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def apply_trial_to_baseline(
    trial_code_dir: str | Path,
    trial_id: str,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    _ensure_model_paths_clean(repo_id=ctx.repo_id)
    trial = Path(trial_code_dir)
    if not trial.is_absolute():
        trial = PROJECT_ROOT / trial
    trial_src = trial / "src"
    if not trial_src.exists():
        raise FileNotFoundError(f"trial src directory not found: {trial_src}")

    _safe_replace_dir(trial_src, ctx.source_dir)

    trial_req = trial / "requirements.txt"
    if trial_req.exists():
        ctx.requirements.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(trial_req, ctx.requirements)

    payload = _with_repo({
        "trial_id": trial_id,
        "trial_code_dir": str(trial),
        "baseline_dir": str(ctx.baseline_dir),
        "source_dir": str(ctx.source_dir),
        "requirements": str(ctx.requirements),
    }, ctx)
    _log("apply_trial_to_baseline", payload)
    return payload


def apply_trial_to_model(
    trial_code_dir: str | Path,
    trial_id: str,
    repo_id: str | None = None,
) -> dict[str, Any]:
    return apply_trial_to_baseline(trial_code_dir, trial_id, repo_id=repo_id)


def commit_baseline_model_update(
    trial_id: str,
    metrics: dict[str, Any] | None = None,
    report_path: str | None = None,
    supplement: str | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    _run_git(["add", "--", *ctx.allowed_pathspecs], ctx=ctx)
    staged = _run_git(
        ["diff", "--cached", "--name-only", "--", *ctx.allowed_pathspecs],
        check=False,
        ctx=ctx,
    ).stdout.splitlines()
    if not staged:
        payload = _with_repo({"committed": False, "reason": "no baseline model changes", "trial_id": trial_id}, ctx)
        _log("commit_baseline_model_update", payload)
        return payload

    primary = (metrics or {}).get("primary", {})
    body = [
        f"Trial: {trial_id}",
        f"Repo: {ctx.repo_id}",
        f"Old WAPE: {primary.get('old_wape')}",
        f"New WAPE: {primary.get('new_wape')}",
        f"Old Bias: {primary.get('old_bias')}",
        f"New Bias: {primary.get('new_bias')}",
        f"Report: {report_path or ''}",
    ]
    if supplement:
        body.append(f"Human supplement: {supplement}")
    message = f"forecast: keep {trial_id}\n\n" + "\n".join(body)
    _run_git(["commit", "-m", message, "--", *ctx.allowed_pathspecs], ctx=ctx)
    head = _run_git(["rev-parse", "HEAD"], ctx=ctx).stdout.strip()
    payload = _with_repo({"committed": True, "trial_id": trial_id, "commit": head, "files": staged}, ctx)
    _log("commit_baseline_model_update", payload)
    return payload


def commit_model_update(
    trial_id: str,
    metrics: dict[str, Any] | None = None,
    report_path: str | None = None,
    supplement: str | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    return commit_baseline_model_update(trial_id, metrics, report_path, supplement, repo_id=repo_id)


def push_model_trial_branch(
    branch: str | None = None,
    remote: str | None = None,
    target_branch: str | None = None,
    set_upstream: bool = True,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    remote = remote or ctx.remote
    branch = branch or _current_branch(ctx)
    target_branch = target_branch or ctx.push_target_branch or branch
    if not branch:
        raise RuntimeError("cannot push: current branch is empty")
    args = ["push"]
    if set_upstream and target_branch == branch:
        args.append("-u")
    args.extend([remote, branch if target_branch == branch else f"{branch}:{target_branch}"])
    result = _run_git(args, check=True, ctx=ctx)
    payload = _with_repo({
        "pushed": True,
        "remote": remote,
        "branch": branch,
        "target_branch": target_branch,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }, ctx)
    _log("push_model_trial_branch", payload)
    return payload


def discard_unaccepted_model_changes(trial_id: str, repo_id: str | None = None) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    _run_git(["checkout", "--", *ctx.allowed_pathspecs], check=False, ctx=ctx)
    state = get_model_repo_state(repo_id=ctx.repo_id)
    payload = _with_repo({
        "trial_id": trial_id,
        "discarded_tracked": True,
        "remaining_model_changes": state.get("model_changes", []),
        "note": "untracked model files are reported but not deleted automatically",
    }, ctx)
    _log("discard_unaccepted_model_changes", payload)
    return payload


def restore_baseline_model_snapshot(
    snapshot_path: str | Path,
    trial_id: str = "",
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    snapshot = Path(snapshot_path)
    if not snapshot.is_absolute():
        snapshot = PROJECT_ROOT / snapshot
    src_snapshot = snapshot / "src"
    if not src_snapshot.exists():
        raise FileNotFoundError(f"snapshot src not found: {src_snapshot}")
    _safe_replace_dir(src_snapshot, ctx.source_dir)
    req_snapshot = snapshot / "requirements.txt"
    if req_snapshot.exists():
        ctx.requirements.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(req_snapshot, ctx.requirements)
    payload = _with_repo({"trial_id": trial_id, "snapshot_path": str(snapshot)}, ctx)
    _log("restore_baseline_model_snapshot", payload)
    return payload


def restore_model_snapshot(
    snapshot_path: str | Path,
    trial_id: str = "",
    repo_id: str | None = None,
) -> dict[str, Any]:
    return restore_baseline_model_snapshot(snapshot_path, trial_id, repo_id=repo_id)


def create_model_pr(
    branch: str,
    base: str | None = None,
    body: str = "",
    title: str | None = None,
    draft: bool | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    base = base or ctx.base_branch
    draft = ctx.pr_draft if draft is None else bool(draft)
    title = title or f"forecast: keep {branch}"
    gh = shutil.which("gh")
    if gh:
        args = [
            "pr", "create",
            "--base", base,
            "--head", branch,
            "--title", title,
            "--body", body or f"Automated model update from {branch}.",
        ]
        if draft:
            args.append("--draft")
        result = subprocess.run(
            [gh, *args],
            cwd=str(ctx.repo_path),
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            payload = _with_repo({
                "created": True,
                "url": url,
                "draft": draft,
                "branch": branch,
                "base": base,
                "title": title,
            }, ctx)
            _log("create_model_pr", payload)
            return payload

    draft_dir = get_paths().global_artifact("pr_drafts_dir")
    draft_dir.mkdir(parents=True, exist_ok=True)
    safe_branch = branch.replace("/", "_")
    path = draft_dir / f"{ctx.repo_id}_{safe_branch}.md"
    content = f"# {title}\n\nRepo: {ctx.repo_id}\nBranch: {branch}\nBase: {base}\nDraft: {draft}\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    payload = _with_repo({
        "created": False,
        "draft_path": str(path),
        "branch": branch,
        "base": base,
        "title": title,
        "draft": draft,
        "reason": "gh CLI unavailable or pr create failed",
    }, ctx)
    _log("create_model_pr", payload)
    return payload


def publish_keep_result(
    trial_code_dir: str | Path,
    trial_id: str,
    metrics: dict[str, Any] | None = None,
    report_path: str | None = None,
    supplement: str | None = None,
    push: bool | None = None,
    create_pr: bool | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    push = ctx.push_on_keep if push is None else bool(push)
    create_pr = ctx.create_pr_on_keep if create_pr is None else bool(create_pr)
    branch = _current_branch(ctx)
    applied = apply_trial_to_baseline(trial_code_dir, trial_id, repo_id=ctx.repo_id)
    commit = commit_baseline_model_update(trial_id, metrics, report_path, supplement, repo_id=ctx.repo_id)
    pushed = None
    pr = None
    if push and commit.get("committed"):
        pushed = push_model_trial_branch(
            branch=branch,
            remote=ctx.remote,
            target_branch=ctx.push_target_branch or None,
            repo_id=ctx.repo_id,
        )
    if create_pr and commit.get("committed"):
        primary = (metrics or {}).get("primary", {})
        body = "\n".join([
            f"Trial: {trial_id}",
            f"Repo: {ctx.repo_id}",
            f"Branch: {branch}",
            f"Commit: {commit.get('commit')}",
            f"Old WAPE: {primary.get('old_wape')}",
            f"New WAPE: {primary.get('new_wape')}",
            f"Old Bias: {primary.get('old_bias')}",
            f"New Bias: {primary.get('new_bias')}",
            f"Report: {report_path or ''}",
            f"Supplement: {supplement or ''}",
        ])
        pr = create_model_pr(
            branch=branch,
            base=ctx.base_branch,
            body=body,
            title=f"forecast: keep {trial_id}",
            draft=ctx.pr_draft,
            repo_id=ctx.repo_id,
        )
    payload = _with_repo({
        "trial_id": trial_id,
        "branch": branch,
        "applied": applied,
        "commit": commit,
        "push": pushed,
        "pr": pr,
        "state": get_model_repo_state(repo_id=ctx.repo_id),
    }, ctx)
    _log("publish_keep_result", payload)
    return payload
