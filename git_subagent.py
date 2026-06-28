from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from openai_codex import Codex
from openai_codex._approval_mode import ApprovalMode
from openai_codex._sandbox import Sandbox

from config import build_codex_config, get_config


PROJECT_ROOT = Path(__file__).resolve().parent


class GitSubagentError(RuntimeError):
    pass


def _codex_config_path() -> Path:
    return Path(get_config().resolved_codex_home) / "config.toml"


def ensure_git_mcp_registered() -> dict[str, Any]:
    """Register the local Git MCP server in this project's CODEX_HOME config."""
    cfg = get_config().mcp.git
    config_path = _codex_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    command = cfg.server_command or sys.executable
    if command == "python":
        command = sys.executable
    args = cfg.server_args or ["-m", "mcp_servers.git_research_server.server"]
    block = "\n".join([
        "[mcp_servers.git_research]",
        f"command = '{command}'",
        "args = [" + ", ".join(repr(str(arg)) for arg in args) + "]",
        "startup_timeout_sec = 120",
        "",
        "[mcp_servers.git_research.env]",
        f"PYTHONPATH = '{PROJECT_ROOT}'",
        f"CODEX_FLOW_ROOT = '{PROJECT_ROOT}'",
        "",
    ])

    pattern = re.compile(
        r"\n?\[mcp_servers\.git_research\][\s\S]*?"
        r"(?=\n\[(?!mcp_servers\.git_research(?:\.|\]))[^\]]+\]|\Z)"
    )
    if pattern.search(text):
        replacement = "\n" + block.rstrip()
        new_text = pattern.sub(lambda _match: replacement, text).rstrip() + "\n"
    else:
        new_text = text.rstrip() + "\n\n" + block if text.strip() else block
    if new_text != text:
        config_path.write_text(new_text, encoding="utf-8")
    return {
        "registered": True,
        "config_path": str(config_path),
        "command": command,
        "args": args,
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise GitSubagentError("git subagent returned an empty response")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise GitSubagentError(f"git subagent response is not JSON: {text[:500]}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise GitSubagentError("git subagent JSON response is not an object")
    return data


def _run_git_subagent(prompt: str, *, cwd: str | Path | None = None) -> dict[str, Any]:
    ensure_git_mcp_registered()
    with Codex(config=build_codex_config()) as codex:
        thread = codex.thread_start(
            cwd=str(cwd or PROJECT_ROOT),
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=(
                "You are a Git publishing subagent for Codex Flow. "
                "Use only the git_research MCP tools for Git operations. "
                "Do not run shell commands. Return one final JSON object."
            ),
        )
        result = thread.run(prompt)
    return _extract_json(result.final_response or "")


def sync_remote_base_via_subagent(repo_id: str | None = None) -> dict[str, Any]:
    cfg = get_config().mcp.git
    repo_cfg = cfg.resolve_repo(repo_id)
    prompt = f"""
Use git_research MCP tools only.
Task: synchronize the local baseline model from the remote base branch.

Target repo_id: {repo_cfg.repo_id!r}

Required steps:
1. Call sync_remote_base(repo_id={repo_cfg.repo_id!r}, remote={repo_cfg.remote!r}, base_branch={repo_cfg.base_branch!r}).
2. Call get_model_repo_state(repo_id={repo_cfg.repo_id!r}).
3. Return JSON only:
{{
  "ok": true|false,
  "operation": "sync_remote_base",
  "repo_id": {repo_cfg.repo_id!r},
  "sync": <sync_remote_base result>,
  "state": <get_model_repo_state result>,
  "error": null|string
}}

If sync_remote_base reports blocked=true, return ok=false and preserve its reason.
Do not call shell. Do not modify files outside allowed Git MCP tools.
"""
    return _run_git_subagent(prompt)


def publish_keep_via_subagent(
    *,
    trial_code_dir: str | Path,
    trial_id: str,
    metrics: dict[str, Any],
    report_path: str | Path,
    supplement: str | None = None,
    result_path: str | Path | None = None,
    repo_id: str | None = None,
    human_approved: bool = True,
) -> dict[str, Any]:
    cfg = get_config().mcp.git
    repo_cfg = cfg.resolve_repo(repo_id)
    prompt = f"""
Use git_research MCP tools only.
Task: publish the accepted KEEP model update.

Target repo_id: {repo_cfg.repo_id!r}

Inputs:
- trial_id: {trial_id!r}
- trial_code_dir: {str(trial_code_dir)!r}
- report_path: {str(report_path)!r}
- metrics JSON: {json.dumps(metrics, ensure_ascii=False)}
- supplement: {supplement!r}
- push: {bool(repo_cfg.push_on_keep)}
- create_pr: {bool(repo_cfg.create_pr_on_keep)}
- human_approved: {bool(human_approved)}

Required steps:
1. Call publish_keep_result(repo_id={repo_cfg.repo_id!r}, trial_code_dir={str(trial_code_dir)!r}, trial_id={trial_id!r}, metrics=<metrics JSON>, report_path={str(report_path)!r}, supplement={supplement!r}, push={bool(repo_cfg.push_on_keep)}, create_pr={bool(repo_cfg.create_pr_on_keep)}, human_approved={bool(human_approved)}).
2. Call get_model_repo_state(repo_id={repo_cfg.repo_id!r}).
3. Return JSON only:
{{
  "ok": true|false,
  "operation": "publish_keep_result",
  "repo_id": {repo_cfg.repo_id!r},
  "publish": <publish_keep_result result>,
  "state": <get_model_repo_state result>,
  "error": null|string
}}

Do not call shell. Do not push main. Do not force push. Do not push or create a remote PR when human_approved is false.
"""
    data = _run_git_subagent(prompt)
    if result_path:
        path = Path(result_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def publish_existing_keep_via_subagent(
    *,
    trial_id: str,
    branch: str,
    metrics: dict[str, Any],
    report_path: str | Path,
    supplement: str | None = None,
    commit: dict[str, Any] | None = None,
    result_path: str | Path | None = None,
    repo_id: str | None = None,
    human_approved: bool = True,
) -> dict[str, Any]:
    cfg = get_config().mcp.git
    repo_cfg = cfg.resolve_repo(repo_id)
    target_branch = repo_cfg.push_target_branch or branch
    prompt = f"""
Use git_research MCP tools only.
Task: publish an already committed KEEP model update.

Target repo_id: {repo_cfg.repo_id!r}

Inputs:
- trial_id: {trial_id!r}
- branch: {branch!r}
- target_branch: {target_branch!r}
- base_branch: {repo_cfg.base_branch!r}
- report_path: {str(report_path)!r}
- commit JSON: {json.dumps(commit or {}, ensure_ascii=False)}
- metrics JSON: {json.dumps(metrics, ensure_ascii=False)}
- supplement: {supplement!r}
- push: {bool(repo_cfg.push_on_keep)}
- create_pr: {bool(repo_cfg.create_pr_on_keep)}
- human_approved: {bool(human_approved)}

Required steps:
1. If push is true, commit.committed is true, and human_approved is true, call push_model_trial_branch(repo_id={repo_cfg.repo_id!r}, branch=branch, remote={repo_cfg.remote!r}, target_branch={target_branch!r}, human_approved={bool(human_approved)}). If human_approved is false, do not push remotely.
2. If create_pr is true, commit.committed is true, and human_approved is true, call create_model_pr(repo_id={repo_cfg.repo_id!r}, branch=branch, base={repo_cfg.base_branch!r}, draft={bool(repo_cfg.pr_draft)}, title="forecast: keep {trial_id}", body=<metrics summary>).
3. Call get_model_repo_state(repo_id={repo_cfg.repo_id!r}).
4. Return JSON only:
{{
  "ok": true|false,
  "operation": "publish_existing_keep",
  "repo_id": {repo_cfg.repo_id!r},
  "publish": {{
    "trial_id": {trial_id!r},
    "branch": {branch!r},
    "commit": <commit JSON>,
    "push": <push result or null>,
    "pr": <pr result or null>
  }},
  "state": <get_model_repo_state result>,
  "error": null|string
}}

Do not call shell. Do not push main. Do not force push. Do not push or create a remote PR when human_approved is false.
"""
    data = _run_git_subagent(prompt)
    if result_path:
        path = Path(result_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data
