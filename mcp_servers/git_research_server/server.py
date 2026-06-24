from __future__ import annotations

from .baseline_git_server import (
    apply_trial_to_baseline,
    commit_baseline_model_update,
    create_model_pr,
    create_model_trial_branch,
    diff_trial_model_code,
    discard_unaccepted_model_changes,
    get_model_repo_state,
    publish_keep_result,
    push_model_trial_branch,
    restore_baseline_model_snapshot,
    snapshot_baseline_model,
    sync_remote_base,
)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - direct adapter imports still work
    FastMCP = None  # type: ignore[assignment]


def build_mcp_server():
    """Build the stdio MCP server while keeping imports side-effect free."""
    if FastMCP is None:
        raise RuntimeError("mcp package is not installed; run `pip install mcp`")

    mcp = FastMCP(
        "git_research",
        instructions=(
            "Safe Git tools for Codex Flow. Only baseline/src/** and "
            "baseline/requirements.txt may be staged or restored. "
            "Use these tools instead of shell git commands."
        ),
    )
    mcp.tool()(sync_remote_base)
    mcp.tool()(get_model_repo_state)
    mcp.tool()(snapshot_baseline_model)
    mcp.tool()(create_model_trial_branch)
    mcp.tool()(diff_trial_model_code)
    mcp.tool()(apply_trial_to_baseline)
    mcp.tool()(commit_baseline_model_update)
    mcp.tool()(push_model_trial_branch)
    mcp.tool()(create_model_pr)
    mcp.tool()(publish_keep_result)
    mcp.tool()(discard_unaccepted_model_changes)
    mcp.tool()(restore_baseline_model_snapshot)
    return mcp


def main() -> None:
    build_mcp_server().run(transport="stdio")


__all__ = [
    "apply_trial_to_baseline",
    "build_mcp_server",
    "commit_baseline_model_update",
    "create_model_pr",
    "create_model_trial_branch",
    "diff_trial_model_code",
    "discard_unaccepted_model_changes",
    "get_model_repo_state",
    "main",
    "publish_keep_result",
    "push_model_trial_branch",
    "restore_baseline_model_snapshot",
    "snapshot_baseline_model",
    "sync_remote_base",
]


if __name__ == "__main__":
    main()
