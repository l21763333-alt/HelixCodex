from __future__ import annotations

import fnmatch
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
    model_root: Path
    baseline_dir: Path
    source_dir: Path
    requirements: Path
    requirements_paths: list[Path]
    copy_include: list[str]
    copy_exclude: list[str]
    publish_paths: list[str]
    allowed_pathspecs: list[str]
    entrypoint_candidates: list[str]
    default_train_command: list[str]
    output_contract: dict[str, Any]
    repo_url: str
    lifecycle: str
    sync_strategy: str
    publish_mode: str
    branch_prefix: str
    remote: str
    base_branch: str
    push_target_branch: str
    push_on_keep: bool
    create_pr_on_keep: bool
    pr_draft: bool
    require_human_approval_for_push: bool


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


def _resolve_model_path(model_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (model_root / path).resolve()
    try:
        resolved.relative_to(model_root)
    except ValueError as exc:
        raise ValueError(f"Git MCP model path must stay inside model.root: {resolved}") from exc
    return resolved


def _repo_context(repo_id: str | None = None) -> ModelRepoContext:
    git_cfg = _cfg()
    repo_cfg = git_cfg.resolve_repo(repo_id)
    repo_path = Path(repo_cfg.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = PROJECT_ROOT / repo_path
    repo_path = repo_path.resolve()

    model_cfg = repo_cfg.model
    publish_cfg = repo_cfg.publish
    repo_source = repo_cfg.repo
    model_root = _resolve_in_repo(repo_path, model_cfg.root, "model.root")
    baseline_dir = model_root
    source_dir = _resolve_in_repo(repo_path, repo_cfg.source_dir, "source_dir")
    requirements = _resolve_in_repo(repo_path, repo_cfg.requirements, "requirements")
    requirements_paths = [
        _resolve_model_path(model_root, item)
        for item in model_cfg.requirements_paths
    ]
    publish_paths = list(model_cfg.publish_paths or repo_cfg.allowed_paths)
    allowed = _pathspecs(publish_paths)
    if not allowed:
        allowed = _pathspecs([model_root.relative_to(repo_path).as_posix() + "/**"])
    return ModelRepoContext(
        repo_id=repo_cfg.repo_id,
        repo_path=repo_path,
        model_root=model_root,
        baseline_dir=baseline_dir,
        source_dir=source_dir,
        requirements=requirements,
        requirements_paths=requirements_paths,
        copy_include=list(model_cfg.copy_include),
        copy_exclude=list(model_cfg.copy_exclude),
        publish_paths=publish_paths,
        allowed_pathspecs=allowed,
        entrypoint_candidates=list(model_cfg.entrypoint_candidates),
        default_train_command=list(model_cfg.default_train_command),
        output_contract=dict(model_cfg.output_contract),
        repo_url=repo_source.url or repo_cfg.repo_url,
        lifecycle=repo_source.lifecycle or repo_cfg.repo_lifecycle or "existing_worktree",
        sync_strategy=repo_source.sync_strategy or repo_cfg.sync_strategy or "ff_only",
        publish_mode=publish_cfg.mode,
        branch_prefix=repo_cfg.branch_prefix,
        remote=repo_cfg.remote,
        base_branch=repo_cfg.base_branch,
        push_target_branch=repo_cfg.push_target_branch,
        push_on_keep=bool(repo_cfg.push_on_keep),
        create_pr_on_keep=bool(repo_cfg.create_pr_on_keep),
        pr_draft=bool(repo_cfg.pr_draft),
        require_human_approval_for_push=bool(git_cfg.require_human_approval_for_push),
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


def _ensure_repo_available(ctx: ModelRepoContext) -> None:
    git_dir = ctx.repo_path / ".git"
    if git_dir.exists():
        return
    if ctx.repo_path.exists() and any(ctx.repo_path.iterdir()):
        raise FileNotFoundError(f"Git MCP repo_path is not a git worktree: {ctx.repo_path}")
    if ctx.lifecycle != "clone_if_missing":
        raise FileNotFoundError(
            f"Git MCP repo_path does not exist: {ctx.repo_path}; "
            "set repo.lifecycle=clone_if_missing and repo.url to enable automatic clone"
        )
    if not ctx.repo_url:
        raise ValueError("Git MCP repo.lifecycle=clone_if_missing requires repo.url")
    ctx.repo_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if ctx.base_branch:
        cmd.extend(["--branch", ctx.base_branch, "--single-branch"])
    cmd.extend([ctx.repo_url, str(ctx.repo_path)])
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "git clone failed for Git MCP repo; check network/authentication: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def _matches_pathspec(rel: str, specs: list[str]) -> bool:
    rel = rel.strip("/")
    for raw in specs:
        spec = str(raw).strip().strip("/")
        if not spec:
            continue
        if spec.endswith("/**"):
            prefix = spec[:-3].strip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
            continue
        if any(ch in spec for ch in "*?[") and fnmatch.fnmatch(rel, spec):
            return True
        if rel == spec or rel.startswith(spec + "/"):
            return True
    return False


def _is_ignored_model_file(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _iter_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if not _is_ignored_model_file(root) else []
    return sorted(p for p in root.rglob("*") if p.is_file() and not _is_ignored_model_file(p))


def _repo_rel(path: Path, ctx: ModelRepoContext) -> str:
    return path.resolve().relative_to(ctx.repo_path).as_posix()


def _model_rel(path: Path, ctx: ModelRepoContext) -> str:
    return path.resolve().relative_to(ctx.model_root).as_posix()


def _publish_model_rel_roots(ctx: ModelRepoContext) -> list[Path]:
    roots: list[Path] = []
    model_root_rel = ctx.model_root.relative_to(ctx.repo_path).as_posix()
    for raw in ctx.publish_paths or [model_root_rel + "/**"]:
        spec = str(raw).strip()
        if spec.endswith("/**"):
            spec = spec[:-3]
        spec_path = Path(spec)
        if spec_path.is_absolute() or ".." in spec_path.parts:
            continue
        repo_abs = (ctx.repo_path / spec_path).resolve()
        try:
            rel = repo_abs.relative_to(ctx.model_root)
        except ValueError:
            continue
        roots.append(rel if rel.as_posix() else Path("."))
    if not roots:
        roots.append(Path("."))
    return roots


def _model_file_manifest(ctx: ModelRepoContext, root: Path | None = None) -> list[dict[str, Any]]:
    base = root or ctx.model_root
    files: list[dict[str, Any]] = []
    for path in _iter_files_under(base):
        rel = path.relative_to(base).as_posix()
        if not _matches_pathspec(rel, ctx.copy_include or ["**"]):
            continue
        if _matches_pathspec(rel, ctx.copy_exclude):
            continue
        files.append({
            "path": rel,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return files


def _publish_file_maps(ctx: ModelRepoContext, trial: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    repo_files: dict[str, Path] = {}
    trial_files: dict[str, Path] = {}
    for rel_root in _publish_model_rel_roots(ctx):
        repo_root = ctx.model_root if rel_root.as_posix() == "." else ctx.model_root / rel_root
        trial_root = trial if rel_root.as_posix() == "." else trial / rel_root
        for path in _iter_files_under(repo_root):
            repo_files[path.relative_to(ctx.model_root).as_posix()] = path
        for path in _iter_files_under(trial_root):
            trial_files[path.relative_to(trial).as_posix()] = path
    return repo_files, trial_files


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
    _ensure_repo_available(ctx)
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
    _ensure_repo_available(ctx)
    SNAPSHOT_DIR = get_paths().global_artifact("model_snapshots_dir")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:80]
    snapshot_path = SNAPSHOT_DIR / f"{int(time.time())}_{ctx.repo_id}_{safe_label}"
    snapshot_path.mkdir(parents=True, exist_ok=False)

    repo_snapshot = snapshot_path / "repo"
    for rel, source in _publish_file_maps(ctx, ctx.model_root)[0].items():
        target = repo_snapshot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    if ctx.source_dir.exists():
        shutil.copytree(ctx.source_dir, snapshot_path / "src", dirs_exist_ok=True)
    for req in ctx.requirements_paths:
        if req.exists():
            target = snapshot_path / "requirements" / req.relative_to(ctx.model_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(req, target)

    state = get_model_repo_state(repo_id=ctx.repo_id)
    metadata = _with_repo({
        "label": label,
        "branch": state.get("branch"),
        "head": state.get("head"),
        "model_root": _rel(ctx.model_root, ctx),
        "baseline_dir": _rel(ctx.baseline_dir, ctx),
        "source_dir": _rel(ctx.source_dir, ctx),
        "requirements_paths": [_model_rel(path, ctx) for path in ctx.requirements_paths],
        "publish_paths": list(ctx.publish_paths),
        "manifest": _model_file_manifest(ctx, repo_snapshot),
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
    trial = trial.resolve()
    summary: list[str] = []

    baseline_files, trial_files = _publish_file_maps(ctx, trial)
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
            summary.append(f"A {name}")
        elif left is not None and right is None:
            removed += 1
            summary.append(f"D {name}")
        elif left and right and _sha256(left) != _sha256(right):
            changed += 1
            summary.append(f"M {name}")

    text = "\n".join(summary[:80]) or "No model code changes."
    payload = _with_repo({
        "trial_code_dir": str(trial),
        "model_root": str(ctx.model_root),
        "publish_paths": list(ctx.publish_paths),
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
    if ctx.sync_strategy != "ff_only":
        raise ValueError(f"unsupported Git MCP sync_strategy={ctx.sync_strategy!r}; only ff_only is allowed")
    _ensure_repo_available(ctx)
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


def _copy_file_or_delete(src: Path, dst: Path) -> str:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return "copied"
    if dst.exists():
        dst.unlink()
        return "deleted"
    return "missing"


def _replace_publish_paths_from_model_tree(ctx: ModelRepoContext, source_root: Path) -> list[dict[str, str]]:
    operations: list[dict[str, str]] = []
    for rel_root in _publish_model_rel_roots(ctx):
        src = source_root if rel_root.as_posix() == "." else source_root / rel_root
        dst = ctx.model_root if rel_root.as_posix() == "." else ctx.model_root / rel_root
        if src.exists() and src.is_dir():
            _safe_replace_dir(src, dst)
            action = "replace_dir"
        elif src.exists() and src.is_file():
            action = _copy_file_or_delete(src, dst)
        elif dst.exists() and dst.is_file():
            dst.unlink()
            action = "delete_file"
        elif dst.exists() and dst.is_dir():
            shutil.rmtree(dst)
            action = "delete_dir"
        else:
            action = "missing"
        operations.append({
            "path": rel_root.as_posix(),
            "source": str(src),
            "target": str(dst),
            "action": action,
        })
    return operations


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
    trial = trial.resolve()
    if not trial.exists():
        raise FileNotFoundError(f"trial code directory not found: {trial}")

    operations = _replace_publish_paths_from_model_tree(ctx, trial)

    payload = _with_repo({
        "trial_id": trial_id,
        "trial_code_dir": str(trial),
        "model_root": str(ctx.model_root),
        "publish_paths": list(ctx.publish_paths),
        "operations": operations,
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
    human_approved: bool = True,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    if ctx.require_human_approval_for_push and not human_approved:
        payload = _with_repo({
            "pushed": False,
            "blocked": True,
            "reason": "remote push requires an explicit human KEEP approval",
        }, ctx)
        _log("push_model_trial_branch", payload)
        return payload
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
    repo_snapshot = snapshot / "repo"
    if repo_snapshot.exists():
        operations = _replace_publish_paths_from_model_tree(ctx, repo_snapshot)
    else:
        src_snapshot = snapshot / "src"
        if not src_snapshot.exists():
            raise FileNotFoundError(f"snapshot repo model files not found: {repo_snapshot}")
        _safe_replace_dir(src_snapshot, ctx.source_dir)
        operations = [{"path": "src", "source": str(src_snapshot), "target": str(ctx.source_dir), "action": "replace_dir"}]
        req_root = snapshot / "requirements"
        if req_root.exists():
            for req in _iter_files_under(req_root):
                rel = req.relative_to(req_root)
                target = ctx.model_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(req, target)
                operations.append({"path": rel.as_posix(), "source": str(req), "target": str(target), "action": "copied"})
    payload = _with_repo({
        "trial_id": trial_id,
        "snapshot_path": str(snapshot),
        "operations": operations,
    }, ctx)
    _log("restore_baseline_model_snapshot", payload)
    return payload


def restore_model_snapshot(
    snapshot_path: str | Path,
    trial_id: str = "",
    repo_id: str | None = None,
) -> dict[str, Any]:
    return restore_baseline_model_snapshot(snapshot_path, trial_id, repo_id=repo_id)


def _write_pr_draft(
    ctx: ModelRepoContext,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    reason: str,
) -> dict[str, Any]:
    draft_dir = get_paths().global_artifact("pr_drafts_dir")
    draft_dir.mkdir(parents=True, exist_ok=True)
    safe_branch = branch.replace("/", "_")
    path = draft_dir / f"{ctx.repo_id}_{safe_branch}.md"
    content = f"# {title}\n\nRepo: {ctx.repo_id}\nBranch: {branch}\nBase: {base}\nDraft: {draft}\nReason: {reason}\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return _with_repo({
        "created": False,
        "draft_path": str(path),
        "branch": branch,
        "base": base,
        "title": title,
        "draft": draft,
        "reason": reason,
    }, ctx)


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

    payload = _write_pr_draft(
        ctx,
        branch=branch,
        base=base,
        title=title,
        body=body,
        draft=draft,
        reason="gh CLI unavailable or pr create failed",
    )
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
    human_approved: bool = True,
) -> dict[str, Any]:
    ctx = _repo_context(repo_id)
    push = ctx.push_on_keep if push is None else bool(push)
    create_pr = ctx.create_pr_on_keep if create_pr is None else bool(create_pr)
    branch = _current_branch(ctx)
    applied = apply_trial_to_baseline(trial_code_dir, trial_id, repo_id=ctx.repo_id)
    commit = commit_baseline_model_update(trial_id, metrics, report_path, supplement, repo_id=ctx.repo_id)
    pushed = None
    pr = None
    pr_body = ""
    if commit.get("committed"):
        primary = (metrics or {}).get("primary", {})
        pr_body = "\n".join([
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

    remote_allowed = bool(human_approved) or not ctx.require_human_approval_for_push
    effective_create_pr = create_pr or ctx.publish_mode == "branch_pr"
    target_branch = branch if ctx.publish_mode == "branch_pr" else (ctx.push_target_branch or None)

    if push and commit.get("committed"):
        pushed = push_model_trial_branch(
            branch=branch,
            remote=ctx.remote,
            target_branch=target_branch,
            repo_id=ctx.repo_id,
            human_approved=human_approved,
        )
    if effective_create_pr and commit.get("committed"):
        if remote_allowed:
            pr = create_model_pr(
                branch=branch,
                base=ctx.base_branch,
                body=pr_body,
                title=f"forecast: keep {trial_id}",
                draft=ctx.pr_draft,
                repo_id=ctx.repo_id,
            )
        else:
            pr = _write_pr_draft(
                ctx,
                branch=branch,
                base=ctx.base_branch,
                title=f"forecast: keep {trial_id}",
                body=pr_body,
                draft=True,
                reason="remote PR creation requires an explicit human KEEP approval",
            )
    payload = _with_repo({
        "trial_id": trial_id,
        "branch": branch,
        "publish_mode": ctx.publish_mode,
        "human_approved": human_approved,
        "applied": applied,
        "commit": commit,
        "push": pushed,
        "pr": pr,
        "state": get_model_repo_state(repo_id=ctx.repo_id),
    }, ctx)
    _log("publish_keep_result", payload)
    return payload
