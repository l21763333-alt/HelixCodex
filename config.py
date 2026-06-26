#!/usr/bin/env python3
"""
config.py — Codex Flow 统一配置加载 (flow_config.yaml)

支持环境变量覆盖敏感字段 (避免写入配置文件):
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID,
  FEISHU_VERIFICATION_TOKEN, OPENAI_API_KEY, CODEX_API_KEY,
  CODEX_HOME, CODEX_MODEL
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import asdict, dataclass, field, fields as dataclass_fields

from openai_codex.client import CodexConfig


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "flow_config.yaml"
PATH_CONFIG_PATH = PROJECT_ROOT / "flow_paths.yaml"
LOCAL_PATH_CONFIG_PATH = PROJECT_ROOT / "flow_paths.local.yaml"


# ============================================================
# 配置数据类
# ============================================================

@dataclass
class FeishuConfig:
    enabled: bool = True
    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""
    poll_interval: int = 5
    verification_token: str = ""
    encrypt_key: str = ""


@dataclass
class HumanReviewConfig:
    enabled: bool = False
    timeout: int = 1800
    auto_fallback: bool = True
    authorized_senders: list[str] = field(default_factory=list)


@dataclass
class LoopLimitsConfig:
    max_consecutive_reverses: int = 2
    max_rollbacks_per_round: int = 3


@dataclass
class LoopConvergenceConfig:
    min_wape_improvement: float = 0.005
    max_rounds_without_improvement: int = 2


@dataclass
class RecoveryConfig:
    enabled: bool = True
    codex_max_attempts: int = 2
    retry_delay_seconds: int = 30
    manual_after_attempts: bool = True
    manual_timeout: int = 0
    recoverable_codex_phases: list[str] = field(default_factory=lambda: [
        "evaluate", "plan", "codegen", "report", "feishu_card",
    ])
    degradable_phases: list[str] = field(default_factory=lambda: [
        "report", "feishu_card",
    ])


@dataclass
class LoopConfig:
    max_iter: int = 10
    target_wape: float | None = None
    max_sleep_hours: float = 24.0
    human_review: HumanReviewConfig = field(default_factory=HumanReviewConfig)
    limits: LoopLimitsConfig = field(default_factory=LoopLimitsConfig)
    convergence: LoopConvergenceConfig = field(default_factory=LoopConvergenceConfig)


@dataclass
class PathsConfig:
    experiment_dir: str = "baseline"
    runs_dir: str = "runs"
    skills_dir: str = "skills"


@dataclass
class FlowPathRoots:
    project: str = "."
    baseline: str = "baseline"
    runs: str = "runs"
    skills: str = "skills"
    codex_home: str = ".codex_home"


@dataclass
class FlowPathData:
    mode: str = "reference_only"
    root: str = "baseline/data"
    primary: str = "baseline/data/dish_package_feature_df.csv"
    auxiliary: list[str] = field(default_factory=lambda: [
        "baseline/data/holiday_imformation.csv",
    ])


@dataclass
class FlowPathModel:
    source_dir: str = "baseline/src"
    requirements: str = "baseline/requirements.txt"
    publish_allowed_paths: list[str] = field(default_factory=lambda: [
        "baseline/src/**",
        "baseline/requirements.txt",
    ])


@dataclass
class FlowPathTrial:
    code_dir: str = "candidate/code"
    legacy_code_dir: str = "agent2/code"
    outputs_dir: str = "outputs/real_outputs"
    inputs_dir: str = "inputs"
    evaluation_dir: str = "evaluation"
    logs_dir: str = "logs"
    reports_dir: str = "reports"
    standardized_dir: str = "standardized"
    checkpoint_dir: str = ".checkpoint"


@dataclass
class FlowPathGlobalArtifacts:
    model_snapshots_dir: str = "runs/model_code_snapshots"
    git_action_log: str = "runs/git_action_log.jsonl"
    feishu_action_log: str = "runs/feishu_card_actions.jsonl"
    pr_drafts_dir: str = "runs/pr_drafts"


@dataclass
class FlowPathsConfig:
    roots: FlowPathRoots = field(default_factory=FlowPathRoots)
    data: FlowPathData = field(default_factory=FlowPathData)
    model: FlowPathModel = field(default_factory=FlowPathModel)
    trial: FlowPathTrial = field(default_factory=FlowPathTrial)
    global_artifacts: FlowPathGlobalArtifacts = field(default_factory=FlowPathGlobalArtifacts)


class PathRegistry:
    """Resolve Codex Flow paths from flow_paths.yaml."""

    def __init__(self, cfg: FlowPathsConfig):
        self.cfg = cfg

    def abs(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (PROJECT_ROOT / path).resolve()

    def rel(self, value: str | Path) -> str:
        path = self.abs(value)
        try:
            return path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return path.as_posix()

    def trial_path(self, trial_dir: str | Path, rel_path: str) -> Path:
        path = Path(trial_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return (path / rel_path).resolve()

    def trial_code_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.code_dir)

    def legacy_trial_code_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.legacy_code_dir)

    def existing_trial_code_dir(self, trial_dir: str | Path) -> Path:
        canonical = self.trial_code_dir(trial_dir)
        legacy = self.legacy_trial_code_dir(trial_dir)
        return canonical if canonical.exists() else legacy

    def trial_outputs_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.outputs_dir)

    def trial_inputs_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.inputs_dir)

    def trial_logs_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.logs_dir)

    def trial_evaluation_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.evaluation_dir)

    def trial_standardized_dir(self, trial_dir: str | Path) -> Path:
        return self.trial_path(trial_dir, self.cfg.trial.standardized_dir)

    def data_primary(self) -> Path:
        return self.abs(self.cfg.data.primary)

    def data_auxiliary(self) -> list[Path]:
        return [self.abs(item) for item in self.cfg.data.auxiliary]

    def model_source_dir(self) -> Path:
        return self.abs(self.cfg.model.source_dir)

    def model_requirements(self) -> Path:
        return self.abs(self.cfg.model.requirements)

    def global_artifact(self, name: str) -> Path:
        return self.abs(getattr(self.cfg.global_artifacts, name))

    def manifest_summary(self, trial_dir: str | Path) -> dict:
        return {
            "data": {
                "mode": self.cfg.data.mode,
                "primary": self.rel(self.cfg.data.primary),
                "auxiliary": [self.rel(item) for item in self.cfg.data.auxiliary],
            },
            "trial": {
                "code_dir": self.rel(self.trial_code_dir(trial_dir)),
                "legacy_code_dir": self.rel(self.legacy_trial_code_dir(trial_dir)),
                "outputs_dir": self.rel(self.trial_outputs_dir(trial_dir)),
                "inputs_dir": self.rel(self.trial_inputs_dir(trial_dir)),
            },
            "model": {
                "source_dir": self.rel(self.cfg.model.source_dir),
                "requirements": self.rel(self.cfg.model.requirements),
                "publish_allowed_paths": list(self.cfg.model.publish_allowed_paths),
            },
        }


@dataclass
class LarkMcpConfig:
    enabled: bool = True
    backend: str = "http"
    server_name: str = "lark_research"


@dataclass
class GitRepositoryConfig:
    repo_id: str = "default"
    repo_path: str = ""
    baseline_dir: str = ""
    source_dir: str = ""
    requirements: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    trial_code_subdir: str = ""
    branch_prefix: str = ""
    remote: str = ""
    base_branch: str = ""
    push_target_branch: str = ""
    push_on_keep: bool | None = None
    create_pr_on_keep: bool | None = None
    pr_draft: bool | None = None


@dataclass
class GitMcpConfig:
    enabled: bool = True
    scope: str = "baseline_model"
    active_repo: str = "default"
    repo_path: str = "."
    baseline_dir: str = "baseline"
    source_dir: str = ""
    requirements: str = ""
    allowed_paths: list[str] = field(default_factory=lambda: [
        "baseline/src/**",
        "baseline/requirements.txt",
    ])
    trial_code_subdir: str = "candidate/code"
    branch_prefix: str = "model-exp/"
    remote: str = "origin"
    base_branch: str = "main"
    sync_on_loop_start: bool = True
    sync_before_each_trial: bool = False
    publish_via_subagent: bool = True
    push_on_keep: bool = True
    create_pr_on_keep: bool = True
    pr_draft: bool = True
    push_target_branch: str = ""
    server_transport: str = "stdio"
    server_command: str = "python"
    server_args: list[str] = field(default_factory=lambda: [
        "-m", "mcp_servers.git_research_server.server",
    ])
    require_human_approval_for_push: bool = True
    allow_force_push: bool = False
    allow_reset_hard: bool = False
    repositories: dict[str, GitRepositoryConfig] = field(default_factory=dict)

    def resolve_repo(self, repo_id: str | None = None) -> GitRepositoryConfig:
        target_id = repo_id or self.active_repo or "default"
        repo: GitRepositoryConfig | None = None

        if self.repositories:
            if target_id in self.repositories:
                repo = _coerce_git_repo_config(target_id, self.repositories[target_id])
            elif repo_id is None and len(self.repositories) == 1:
                target_id, raw_repo = next(iter(self.repositories.items()))
                repo = _coerce_git_repo_config(target_id, raw_repo)
            else:
                known = ", ".join(sorted(self.repositories))
                raise ValueError(f"unknown Git MCP repo_id {target_id!r}; known repositories: {known}")
        else:
            repo = GitRepositoryConfig(repo_id=target_id)

        baseline_dir = repo.baseline_dir or self.baseline_dir
        source_dir = repo.source_dir or self.source_dir or str(Path(baseline_dir) / "src")
        requirements = repo.requirements or self.requirements or str(Path(baseline_dir) / "requirements.txt")
        repo_has_layout = bool(repo.baseline_dir or repo.source_dir or repo.requirements)
        if repo.allowed_paths:
            allowed_paths = list(repo.allowed_paths)
        elif self.repositories and repo_has_layout:
            allowed_paths = [f"{source_dir.rstrip('/')}/**", requirements]
        else:
            allowed_paths = list(self.allowed_paths)
        return GitRepositoryConfig(
            repo_id=target_id,
            repo_path=repo.repo_path or self.repo_path,
            baseline_dir=baseline_dir,
            source_dir=source_dir,
            requirements=requirements,
            allowed_paths=allowed_paths,
            trial_code_subdir=repo.trial_code_subdir or self.trial_code_subdir,
            branch_prefix=repo.branch_prefix or self.branch_prefix,
            remote=repo.remote or self.remote,
            base_branch=repo.base_branch or self.base_branch,
            push_target_branch=repo.push_target_branch or self.push_target_branch,
            push_on_keep=repo.push_on_keep if repo.push_on_keep is not None else self.push_on_keep,
            create_pr_on_keep=(
                repo.create_pr_on_keep
                if repo.create_pr_on_keep is not None
                else self.create_pr_on_keep
            ),
            pr_draft=repo.pr_draft if repo.pr_draft is not None else self.pr_draft,
        )


@dataclass
class McpConfig:
    lark: LarkMcpConfig = field(default_factory=LarkMcpConfig)
    git: GitMcpConfig = field(default_factory=GitMcpConfig)


@dataclass
class Config:
    model: str = "gpt-5.5"
    codex_home: str = ""
    openai_api_key: str = ""
    codex_api_key: str = ""
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)

    @property
    def api_key(self) -> str:
        return self.openai_api_key or self.codex_api_key

    @property
    def resolved_codex_home(self) -> str:
        configured = self.codex_home or get_paths().cfg.roots.codex_home
        if not configured:
            return str(Path.home() / ".codex")
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return str(path.resolve())

    def codex_config_overrides(self) -> tuple[str, ...]:
        overrides: list[str] = []
        if self.model:
            overrides.append(f'model="{self.model}"')
        return tuple(overrides)


def build_codex_config() -> CodexConfig:
    cfg = get_config()
    env: dict[str, str] = {
        "RUST_LOG": "info",
        "CODEX_HOME": cfg.resolved_codex_home,
    }
    return CodexConfig(env=env, config_overrides=cfg.codex_config_overrides())


# ============================================================
# 加载逻辑 (YAML only)
# ============================================================

ENV_OVERRIDES = {
    "FEISHU_APP_ID":       ("feishu", "app_id"),
    "FEISHU_APP_SECRET":   ("feishu", "app_secret"),
    "FEISHU_CHAT_ID":      ("feishu", "chat_id"),
    "FEISHU_VERIFICATION_TOKEN": ("feishu", "verification_token"),
    "FEISHU_ENCRYPT_KEY":  ("feishu", "encrypt_key"),
    "OPENAI_API_KEY":      ("_top", "openai_api_key"),
    "CODEX_API_KEY":       ("_top", "codex_api_key"),
    "CODEX_HOME":          ("_top", "codex_home"),
    "CODEX_MODEL":         ("_top", "model"),
}


def _set_nested(obj, path: list[str], value) -> None:
    """按路径设置嵌套 dataclass 属性"""
    target = obj
    for part in path[:-1]:
        target = getattr(target, part)
    setattr(target, path[-1], value)


def _apply_section(config: Config, data: dict, section: str, fields: list[str]) -> None:
    """将 dict 中的字段批量写入 dataclass"""
    sd = data.get(section, {})
    if not isinstance(sd, dict):
        return
    target = getattr(config, section)
    for f in fields:
        if f in sd and sd[f] is not None:
            setattr(target, f, sd[f])


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_dataclass_section(target, data: dict) -> None:
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if hasattr(target, key) and value is not None:
            setattr(target, key, value)


def _coerce_git_repo_config(repo_id: str, data) -> GitRepositoryConfig:
    if isinstance(data, GitRepositoryConfig):
        if data.repo_id == repo_id:
            return data
        values = asdict(data)
        values["repo_id"] = repo_id
        return GitRepositoryConfig(**values)
    if not isinstance(data, dict):
        raise TypeError(f"Git MCP repository {repo_id!r} must be a mapping")
    allowed = {field.name for field in dataclass_fields(GitRepositoryConfig)}
    values = {key: value for key, value in data.items() if key in allowed and value is not None}
    values["repo_id"] = repo_id
    return GitRepositoryConfig(**values)


def _coerce_git_repositories(data) -> dict[str, GitRepositoryConfig]:
    if not isinstance(data, dict):
        return {}
    return {str(repo_id): _coerce_git_repo_config(str(repo_id), repo_cfg) for repo_id, repo_cfg in data.items()}


def _flow_paths_from_dict(raw: dict) -> FlowPathsConfig:
    cfg = FlowPathsConfig()
    _apply_dataclass_section(cfg.roots, raw.get("roots", {}))
    _apply_dataclass_section(cfg.data, raw.get("data", {}))
    _apply_dataclass_section(cfg.model, raw.get("model", {}))
    _apply_dataclass_section(cfg.trial, raw.get("trial", {}))
    _apply_dataclass_section(cfg.global_artifacts, raw.get("global_artifacts", {}))
    return cfg


def load_flow_paths(
    path: Path | str | None = None,
    local_path: Path | str | None = None,
) -> FlowPathsConfig:
    import yaml

    raw = asdict(FlowPathsConfig())
    yaml_path = Path(path) if path else PATH_CONFIG_PATH
    if yaml_path.exists():
        with yaml_path.open("r", encoding="utf-8") as f:
            raw = _deep_merge(raw, yaml.safe_load(f) or {})

    local_yaml = Path(local_path) if local_path else LOCAL_PATH_CONFIG_PATH
    if local_yaml.exists():
        with local_yaml.open("r", encoding="utf-8") as f:
            raw = _deep_merge(raw, yaml.safe_load(f) or {})

    return _flow_paths_from_dict(raw)


def load_config(path: Path | str | None = None) -> Config:
    """从 YAML 加载配置, 环境变量可覆盖敏感字段"""
    config = Config()
    yaml_path = Path(path) if path else CONFIG_PATH

    if yaml_path.exists():
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # 顶层字段
        for key in ("model", "codex_home", "openai_api_key", "codex_api_key"):
            if raw.get(key):
                setattr(config, key, raw[key])

        codex_section = raw.get("codex", {})
        if isinstance(codex_section, dict):
            if codex_section.get("model"):
                config.model = codex_section["model"]
            if codex_section.get("home"):
                config.codex_home = codex_section["home"]

        auth_section = raw.get("auth", {})
        if isinstance(auth_section, dict):
            if auth_section.get("openai_api_key"):
                config.openai_api_key = auth_section["openai_api_key"]
            if auth_section.get("codex_api_key"):
                config.codex_api_key = auth_section["codex_api_key"]

        # 子配置 (target, source_dict, section_name, fields)
        # Feishu credentials are intentionally not loaded from YAML. Keep
        # secrets and chat identifiers in environment variables so they do not
        # get committed with project configuration.
        _apply_section(config, raw, "feishu", ["enabled", "poll_interval"])
        _apply_section(config, raw, "loop",
                       ["max_iter", "target_wape", "max_sleep_hours"])
        _apply_section(config.loop, raw.get("loop", {}), "human_review",
                       ["enabled", "timeout", "auto_fallback", "authorized_senders"])
        _apply_section(config.loop, raw.get("loop", {}), "limits",
                       ["max_consecutive_reverses", "max_rollbacks_per_round"])
        _apply_section(config.loop, raw.get("loop", {}), "convergence",
                       ["min_wape_improvement", "max_rounds_without_improvement"])
        _apply_section(config, raw, "recovery",
                       ["enabled", "codex_max_attempts", "retry_delay_seconds",
                        "manual_after_attempts", "manual_timeout",
                        "recoverable_codex_phases", "degradable_phases"])
        _apply_section(config, raw, "paths",
                       ["experiment_dir", "runs_dir", "skills_dir"])
        _apply_section(config.mcp, raw.get("mcp", {}), "lark",
                       ["enabled", "backend", "server_name"])
        _apply_section(config.mcp, raw.get("mcp", {}), "git",
                       ["enabled", "scope", "active_repo", "repo_path", "baseline_dir",
                        "source_dir", "requirements", "allowed_paths",
                        "trial_code_subdir", "branch_prefix",
                        "remote", "base_branch",
                        "sync_on_loop_start", "sync_before_each_trial",
                        "publish_via_subagent", "push_on_keep",
                        "create_pr_on_keep", "pr_draft", "push_target_branch",
                        "server_transport", "server_command", "server_args",
                        "require_human_approval_for_push",
                        "allow_force_push", "allow_reset_hard", "repositories"])
        config.mcp.git.repositories = _coerce_git_repositories(config.mcp.git.repositories)

    # 环境变量覆盖
    for env_var, (section, field) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var, "")
        if value:
            if section == "_top":
                setattr(config, field, value)
            else:
                _set_nested(config, [section, field], value)

    return config


# ============================================================
# 模块级单例
# ============================================================

_config: Config | None = None
_paths: PathRegistry | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(path: Path | str | None = None) -> Config:
    global _config
    _config = load_config(path)
    return _config


def get_paths() -> PathRegistry:
    global _paths
    if _paths is None:
        _paths = PathRegistry(load_flow_paths())
    return _paths


def reload_paths(
    path: Path | str | None = None,
    local_path: Path | str | None = None,
) -> PathRegistry:
    global _paths
    _paths = PathRegistry(load_flow_paths(path, local_path))
    return _paths


# 便捷函数
def feishu_enabled() -> bool:
    return get_config().feishu.enabled

def human_review_enabled() -> bool:
    cfg = get_config()
    return cfg.feishu.enabled and cfg.loop.human_review.enabled
