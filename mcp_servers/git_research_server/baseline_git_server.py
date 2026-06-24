from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, get_config, get_paths

from .schemas import ModelRepoState

def _cfg():
    return get_config().mcp.git


def _repo_path() -> Path:
    path = Path(_cfg().repo_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _baseline_dir() -> Path:
    return get_paths().abs(get_paths().cfg.roots.baseline)


def _baseline_src() -> Path:
    return get_paths().model_source_dir()


def _baseline_requirements() -> Path:
    return get_paths().model_requirements()


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_repo_path()).as_posix()


def _allowed_pathspecs() -> list[str]:
    specs: list[str] = []
    for item in get_paths().cfg.model.publish_allowed_paths:
        if item.endswith("/**"):
            specs.append(item[:-3])
        else:
            specs.append(item)
    return specs


def _current_branch() -> str:
    return _run_git(["branch", "--show-current"], check=False).stdout.strip()


def _current_head(ref: str = "HEAD") -> str:
    return _run_git(["rev-parse", ref], check=False).stdout.strip()


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    result = subprocess.run(
        cmd,
        cwd=str(_repo_path()),
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


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


def get_model_repo_state() -> dict[str, Any]:
    branch = _run_git(["branch", "--show-current"], check=False).stdout.strip()
    head = _run_git(["rev-parse", "HEAD"], check=False).stdout.strip()
    model_status = _run_git(["status", "--porcelain", "--", *_allowed_pathspecs()], check=False)
    project_status = _run_git(["status", "--porcelain"], check=False)
    model_changes = _parse_porcelain(model_status.stdout)
    state = ModelRepoState(
        branch=branch,
        head=head,
        model_dirty=bool(model_changes),
        model_changes=model_changes,
        project_changes=_parse_porcelain(project_status.stdout),
    )
    payload = state.to_dict()
    _log("get_model_repo_state", payload)
    return payload


def snapshot_baseline_model(label: str) -> dict[str, Any]:
    SNAPSHOT_DIR = get_paths().global_artifact("model_snapshots_dir")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:80]
    snapshot_path = SNAPSHOT_DIR / f"{int(time.time())}_{safe_label}"
    snapshot_path.mkdir(parents=True, exist_ok=False)

    if _baseline_src().exists():
        shutil.copytree(_baseline_src(), snapshot_path / "src")
    if _baseline_requirements().exists():
        shutil.copy2(_baseline_requirements(), snapshot_path / "requirements.txt")

    state = get_model_repo_state()
    metadata = {
        "label": label,
        "branch": state.get("branch"),
        "head": state.get("head"),
        "baseline_dir": _rel(_baseline_dir()),
        "manifest": _file_manifest(snapshot_path / "src"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (snapshot_path / "snapshot.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    payload = {"snapshot_path": str(snapshot_path), **metadata}
    _log("snapshot_baseline_model", payload)
    return payload


def create_model_trial_branch(trial_id: str, base_ref: str | None = None) -> dict[str, Any]:
    branch = f"{_cfg().branch_prefix}{trial_id}"
    args = ["switch", "-c", branch]
    if base_ref:
        args.append(base_ref)
    result = _run_git(args, check=False)
    if result.returncode != 0:
        existing = _run_git(["rev-parse", "--verify", branch], check=False)
        if existing.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        _run_git(["switch", branch])
    payload = {"branch": branch, "base_ref": base_ref}
    _log("create_model_trial_branch", payload)
    return payload


def diff_trial_model_code(trial_code_dir: str | Path) -> dict[str, Any]:
    trial = Path(trial_code_dir)
    if not trial.is_absolute():
        trial = PROJECT_ROOT / trial
    trial_src = trial / "src"
    summary: list[str] = []

    baseline_files = {p.relative_to(_baseline_src()).as_posix(): p for p in _baseline_src().rglob("*") if p.is_file()} if _baseline_src().exists() else {}
    trial_files = {p.relative_to(trial_src).as_posix(): p for p in trial_src.rglob("*") if p.is_file()} if trial_src.exists() else {}
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

    base_req = _baseline_requirements()
    trial_req = trial / "requirements.txt"
    if trial_req.exists():
        if not base_req.exists():
            added += 1
            summary.append("A requirements.txt")
        elif _sha256(base_req) != _sha256(trial_req):
            changed += 1
            summary.append("M requirements.txt")

    text = "\n".join(summary[:80]) or "No model code changes."
    payload = {
        "trial_code_dir": str(trial),
        "changed": changed,
        "added": added,
        "removed": removed,
        "summary": text,
    }
    _log("diff_trial_model_code", payload)
    return payload


def _ensure_model_paths_clean() -> None:
    state = get_model_repo_state()
    if state.get("model_dirty"):
        raise RuntimeError(
            "baseline model code has uncommitted changes: "
            + ", ".join(state.get("model_changes", []))
        )


def sync_remote_base(remote: str | None = None, base_branch: str | None = None) -> dict[str, Any]:
    """Fetch and fast-forward the configured base branch when model paths are clean."""
    cfg = _cfg()
    remote = remote or cfg.remote
    base_branch = base_branch or cfg.base_branch
    before = get_model_repo_state()
    if before.get("model_dirty"):
        payload = {
            "synced": False,
            "blocked": True,
            "reason": "baseline model code has uncommitted changes",
            "model_changes": before.get("model_changes", []),
            "remote": remote,
            "base_branch": base_branch,
            "local_head": before.get("head"),
        }
        _log("sync_remote_base", payload)
        return payload

    _run_git(["fetch", remote, base_branch])
    remote_ref = f"{remote}/{base_branch}"
    remote_head = _current_head(remote_ref)
    local_before = before.get("head", "")
    current_branch = before.get("branch", "")

    if current_branch != base_branch:
        switched = _run_git(["switch", base_branch], check=False)
        if switched.returncode != 0:
            _run_git(["switch", "-c", base_branch, "--track", remote_ref])

    _run_git(["merge", "--ff-only", remote_ref])
    after = get_model_repo_state()
    payload = {
        "synced": True,
        "blocked": False,
        "remote": remote,
        "base_branch": base_branch,
        "remote_head": remote_head,
        "local_head_before": local_before,
        "local_head_after": after.get("head"),
        "branch_before": current_branch,
        "branch_after": after.get("branch"),
    }
    _log("sync_remote_base", payload)
    return payload


def _safe_replace_dir(src: Path, dst: Path) -> None:
    """原子化替换目录内容: 先清空目标再填充, 避免 rmtree+copytree 中间态丢失

    如果 copytree 失败, dst 内容为空但目录存在, git checkout 可恢复。
    """
    # 1. 确保目标目录存在
    dst.mkdir(parents=True, exist_ok=True)

    # 2. 清空目标目录内容 (保留目录本身)
    for item in list(dst.iterdir()):
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # 3. 从源目录复制所有内容 (dirs_exist_ok=True 因为 dst 已存在)
    shutil.copytree(
        src, dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def apply_trial_to_baseline(trial_code_dir: str | Path, trial_id: str) -> dict[str, Any]:
    _ensure_model_paths_clean()
    trial = Path(trial_code_dir)
    if not trial.is_absolute():
        trial = PROJECT_ROOT / trial
    trial_src = trial / "src"
    if not trial_src.exists():
        raise FileNotFoundError(f"trial src directory not found: {trial_src}")

    _safe_replace_dir(trial_src, _baseline_src())

    trial_req = trial / "requirements.txt"
    if trial_req.exists():
        shutil.copy2(trial_req, _baseline_requirements())

    payload = {"trial_id": trial_id, "trial_code_dir": str(trial), "baseline_dir": str(_baseline_dir())}
    _log("apply_trial_to_baseline", payload)
    return payload


def commit_baseline_model_update(
    trial_id: str,
    metrics: dict[str, Any] | None = None,
    report_path: str | None = None,
    supplement: str | None = None,
) -> dict[str, Any]:
    _run_git(["add", "--", *_allowed_pathspecs()])
    staged = _run_git(["diff", "--cached", "--name-only", "--", *_allowed_pathspecs()], check=False).stdout.splitlines()
    if not staged:
        payload = {"committed": False, "reason": "no baseline model changes", "trial_id": trial_id}
        _log("commit_baseline_model_update", payload)
        return payload

    primary = (metrics or {}).get("primary", {})
    body = [
        f"Trial: {trial_id}",
        f"Old WAPE: {primary.get('old_wape')}",
        f"New WAPE: {primary.get('new_wape')}",
        f"Old Bias: {primary.get('old_bias')}",
        f"New Bias: {primary.get('new_bias')}",
        f"Report: {report_path or ''}",
    ]
    if supplement:
        body.append(f"Human supplement: {supplement}")
    message = f"forecast: keep {trial_id}\n\n" + "\n".join(body)
    _run_git(["commit", "-m", message, "--", *_allowed_pathspecs()])
    head = _run_git(["rev-parse", "HEAD"]).stdout.strip()
    payload = {"committed": True, "trial_id": trial_id, "commit": head, "files": staged}
    _log("commit_baseline_model_update", payload)
    return payload


def push_model_trial_branch(
    branch: str | None = None,
    remote: str | None = None,
    target_branch: str | None = None,
    set_upstream: bool = True,
) -> dict[str, Any]:
    cfg = _cfg()
    remote = remote or cfg.remote
    branch = branch or _current_branch()
    target_branch = target_branch or getattr(cfg, "push_target_branch", "") or branch
    if not branch:
        raise RuntimeError("cannot push: current branch is empty")
    args = ["push"]
    if set_upstream and target_branch == branch:
        args.append("-u")
    args.extend([remote, branch if target_branch == branch else f"{branch}:{target_branch}"])
    result = _run_git(args, check=True)
    payload = {
        "pushed": True,
        "remote": remote,
        "branch": branch,
        "target_branch": target_branch,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    _log("push_model_trial_branch", payload)
    return payload


def discard_unaccepted_model_changes(trial_id: str) -> dict[str, Any]:
    _run_git(["checkout", "--", *_allowed_pathspecs()], check=False)
    state = get_model_repo_state()
    payload = {
        "trial_id": trial_id,
        "discarded_tracked": True,
        "remaining_model_changes": state.get("model_changes", []),
        "note": "untracked model files are reported but not deleted automatically",
    }
    _log("discard_unaccepted_model_changes", payload)
    return payload


def restore_baseline_model_snapshot(snapshot_path: str | Path, trial_id: str = "") -> dict[str, Any]:
    snapshot = Path(snapshot_path)
    if not snapshot.is_absolute():
        snapshot = PROJECT_ROOT / snapshot
    src_snapshot = snapshot / "src"
    if not src_snapshot.exists():
        raise FileNotFoundError(f"snapshot src not found: {src_snapshot}")
    _safe_replace_dir(src_snapshot, _baseline_src())
    req_snapshot = snapshot / "requirements.txt"
    if req_snapshot.exists():
        shutil.copy2(req_snapshot, _baseline_requirements())
    payload = {"trial_id": trial_id, "snapshot_path": str(snapshot)}
    _log("restore_baseline_model_snapshot", payload)
    return payload


def create_model_pr(
    branch: str,
    base: str | None = None,
    body: str = "",
    title: str | None = None,
    draft: bool | None = None,
) -> dict[str, Any]:
    cfg = _cfg()
    base = base or cfg.base_branch
    draft = cfg.pr_draft if draft is None and hasattr(cfg, "pr_draft") else bool(draft)
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
            cwd=str(_repo_path()),
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            payload = {
                "created": True,
                "url": url,
                "draft": draft,
                "branch": branch,
                "base": base,
                "title": title,
            }
            _log("create_model_pr", payload)
            return payload

    draft_dir = get_paths().global_artifact("pr_drafts_dir")
    draft_dir.mkdir(parents=True, exist_ok=True)
    safe_branch = branch.replace("/", "_")
    path = draft_dir / f"{safe_branch}.md"
    content = f"# {title}\n\nBranch: {branch}\nBase: {base}\nDraft: {draft}\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    payload = {
        "created": False,
        "draft_path": str(path),
        "branch": branch,
        "base": base,
        "title": title,
        "draft": draft,
        "reason": "gh CLI unavailable or pr create failed",
    }
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
) -> dict[str, Any]:
    cfg = _cfg()
    push = cfg.push_on_keep if push is None and hasattr(cfg, "push_on_keep") else bool(push)
    create_pr = (
        cfg.create_pr_on_keep
        if create_pr is None and hasattr(cfg, "create_pr_on_keep")
        else bool(create_pr)
    )
    branch = _current_branch()
    applied = apply_trial_to_baseline(trial_code_dir, trial_id)
    commit = commit_baseline_model_update(trial_id, metrics, report_path, supplement)
    pushed = None
    pr = None
    if push and commit.get("committed"):
        pushed = push_model_trial_branch(
            branch=branch,
            remote=cfg.remote,
            target_branch=getattr(cfg, "push_target_branch", "") or None,
        )
    if create_pr and commit.get("committed"):
        primary = (metrics or {}).get("primary", {})
        body = "\n".join([
            f"Trial: {trial_id}",
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
            base=cfg.base_branch,
            body=body,
            title=f"forecast: keep {trial_id}",
            draft=cfg.pr_draft if hasattr(cfg, "pr_draft") else True,
        )
    payload = {
        "trial_id": trial_id,
        "branch": branch,
        "applied": applied,
        "commit": commit,
        "push": pushed,
        "pr": pr,
        "state": get_model_repo_state(),
    }
    _log("publish_keep_result", payload)
    return payload
