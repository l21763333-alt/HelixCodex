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
    root: str = "/data/dataworks_data"
    primary: str = "/data/dataworks_data/dwd_forecast_package_feature_df.csv"
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
class GitRepoSourceConfig:
    path: str = ""
    url: str = ""
    lifecycle: str = "existing_worktree"
    remote: str = ""
    base_branch: str = ""
    sync_strategy: str = "ff_only"


@dataclass
class GitModelOutputContractConfig:
    prediction_path: str = "{trial_id}_package_detail.csv"
    actual_path: str = ""
    split_column: str = "split"
    split_filter: str = "test"
    actual_column: str = ""
    prediction_column: str = ""
    actual_candidates: list[str] = field(default_factory=lambda: [
        "true_pos_cnt",
        "true_real_qty_sum",
        "actual",
        "y_true",
    ])
    prediction_candidates: list[str] = field(default_factory=lambda: [
        "pred_pos_cnt",
        "pred_real_qty_sum",
        "prediction",
        "y_pred",
    ])
    error_column: str = ""
    abs_error_column: str = ""
    baseline_prediction_globs: list[str] = field(default_factory=lambda: [
        "*_package_detail.csv",
        "*.csv",
    ])
    secondary_metric_globs: list[str] = field(default_factory=lambda: [
        "*_store_dish_day.csv",
    ])
    primary_level: str = "package_detail"
    secondary_level: str = "store_dish_day"


@dataclass
class GitModelConfig:
    root: str = "baseline"
    copy_include: list[str] = field(default_factory=lambda: [
        "src/**",
        "requirements.txt",
        "train.py",
    ])
    copy_exclude: list[str] = field(default_factory=lambda: [
        "__pycache__/**",
        "*.pyc",
        ".git/**",
        "data/**",
        "outputs/**",
        "logs/**",
    ])
    publish_paths: list[str] = field(default_factory=list)
    requirements_paths: list[str] = field(default_factory=lambda: [
        "requirements.txt",
    ])
    entrypoint_candidates: list[str] = field(default_factory=lambda: [
        "train.py",
        "src/train.py",
        "main.py",
    ])
    default_train_command: list[str] = field(default_factory=list)
    output_contract: GitModelOutputContractConfig | dict = field(
        default_factory=GitModelOutputContractConfig
    )


@dataclass
class GitPublishConfig:
    mode: str = "direct_branch"
    push_on_keep: bool | None = None
    target_branch: str = ""
    branch_prefix: str = ""
    create_pr: bool | None = None
    pr_draft: bool | None = None


@dataclass
class GitRepositoryConfig:
    repo_id: str = "default"
    repo: GitRepoSourceConfig | dict = field(default_factory=GitRepoSourceConfig)
    model: GitModelConfig | dict = field(default_factory=GitModelConfig)
    publish: GitPublishConfig | dict = field(default_factory=GitPublishConfig)
    repo_path: str = ""
    repo_url: str = ""
    repo_lifecycle: str = ""
    sync_strategy: str = ""
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
    repo: GitRepoSourceConfig | dict = field(default_factory=GitRepoSourceConfig)
    model: GitModelConfig | dict = field(default_factory=GitModelConfig)
    publish: GitPublishConfig | dict = field(default_factory=GitPublishConfig)
    repo_path: str = "."
    repo_url: str = ""
    repo_lifecycle: str = "existing_worktree"
    sync_strategy: str = "ff_only"
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

        top_repo = _coerce_dataclass(GitRepoSourceConfig, self.repo)
        repo_source = _coerce_dataclass(GitRepoSourceConfig, repo.repo)
        top_model = _coerce_git_model_config(self.model)
        repo_model = _coerce_git_model_config(repo.model)
        top_publish = _coerce_dataclass(GitPublishConfig, self.publish)
        repo_publish = _coerce_dataclass(GitPublishConfig, repo.publish)

        if _has_explicit_model_root(repo.model):
            baseline_dir = repo_model.root
        elif repo.baseline_dir:
            baseline_dir = repo.baseline_dir
        elif _has_explicit_model_root(self.model):
            baseline_dir = top_model.root
        else:
            baseline_dir = self.baseline_dir

        source_dir = repo.source_dir or self.source_dir or str(Path(baseline_dir) / "src")
        requirements = (
            repo.requirements
            or self.requirements
            or str(Path(baseline_dir) / "requirements.txt")
        )
        repo_has_layout = bool(repo.baseline_dir or repo.source_dir or repo.requirements)

        if _has_explicit_model_field(repo.model, "requirements_paths"):
            requirements_paths = list(repo_model.requirements_paths)
        elif _has_explicit_model_field(self.model, "requirements_paths"):
            requirements_paths = list(top_model.requirements_paths)
        elif requirements:
            requirements_paths = [_relative_to_model_root(requirements, baseline_dir)]
        else:
            requirements_paths = []

        if _has_explicit_model_field(repo.model, "copy_include"):
            copy_include = list(repo_model.copy_include)
        elif _has_explicit_model_field(self.model, "copy_include"):
            copy_include = list(top_model.copy_include)
        else:
            copy_include = _default_copy_include(source_dir, requirements, baseline_dir)

        if _has_explicit_model_field(repo.model, "copy_exclude"):
            copy_exclude = list(repo_model.copy_exclude)
        else:
            copy_exclude = list(top_model.copy_exclude)

        if repo_model.publish_paths:
            allowed_paths = list(repo_model.publish_paths)
        elif repo.allowed_paths:
            allowed_paths = list(repo.allowed_paths)
        elif top_model.publish_paths:
            allowed_paths = list(top_model.publish_paths)
        elif self.repositories and repo_has_layout:
            allowed_paths = [f"{source_dir.rstrip('/')}/**"]
            if requirements:
                allowed_paths.append(requirements)
        else:
            allowed_paths = list(self.allowed_paths)

        output_contract = _merge_dataclass_dict(
            GitModelOutputContractConfig,
            top_model.output_contract,
            repo_model.output_contract,
            prefer_second=_has_explicit_model_field(repo.model, "output_contract"),
        )
        model_cfg = GitModelConfig(
            root=baseline_dir,
            copy_include=copy_include,
            copy_exclude=copy_exclude,
            publish_paths=allowed_paths,
            requirements_paths=requirements_paths,
            entrypoint_candidates=(
                list(repo_model.entrypoint_candidates)
                if _has_explicit_model_field(repo.model, "entrypoint_candidates")
                else list(top_model.entrypoint_candidates)
            ),
            default_train_command=(
                list(repo_model.default_train_command)
                if _has_explicit_model_field(repo.model, "default_train_command")
                else list(top_model.default_train_command)
            ),
            output_contract=output_contract,
        )

        repo_path = repo_source.path or repo.repo_path or top_repo.path or self.repo_path
        repo_url = repo_source.url or repo.repo_url or top_repo.url or self.repo_url
        lifecycle = (
            repo_source.lifecycle
            if _has_explicit_dataclass_field(repo.repo, GitRepoSourceConfig, "lifecycle")
            else repo.repo_lifecycle
            or (top_repo.lifecycle if _has_explicit_dataclass_field(self.repo, GitRepoSourceConfig, "lifecycle") else "")
            or self.repo_lifecycle
            or "existing_worktree"
        )
        remote = repo_source.remote or repo.remote or top_repo.remote or self.remote
        base_branch = repo_source.base_branch or repo.base_branch or top_repo.base_branch or self.base_branch
        sync_strategy = (
            repo_source.sync_strategy
            if _has_explicit_dataclass_field(repo.repo, GitRepoSourceConfig, "sync_strategy")
            else repo.sync_strategy
            or (top_repo.sync_strategy if _has_explicit_dataclass_field(self.repo, GitRepoSourceConfig, "sync_strategy") else "")
            or self.sync_strategy
            or "ff_only"
        )
        repo_source_cfg = GitRepoSourceConfig(
            path=repo_path,
            url=repo_url,
            lifecycle=lifecycle,
            remote=remote,
            base_branch=base_branch,
            sync_strategy=sync_strategy,
        )

        publish_cfg = GitPublishConfig(
            mode=(
                repo_publish.mode
                if _has_explicit_dataclass_field(repo.publish, GitPublishConfig, "mode")
                else top_publish.mode
                if _has_explicit_dataclass_field(self.publish, GitPublishConfig, "mode")
                else "direct_branch"
            ),
            push_on_keep=(
                repo_publish.push_on_keep
                if repo_publish.push_on_keep is not None
                else repo.push_on_keep
                if repo.push_on_keep is not None
                else top_publish.push_on_keep
                if top_publish.push_on_keep is not None
                else self.push_on_keep
            ),
            target_branch=(
                repo_publish.target_branch
                or repo.push_target_branch
                or top_publish.target_branch
                or self.push_target_branch
            ),
            branch_prefix=repo_publish.branch_prefix or repo.branch_prefix or top_publish.branch_prefix or self.branch_prefix,
            create_pr=(
                repo_publish.create_pr
                if repo_publish.create_pr is not None
                else repo.create_pr_on_keep
                if repo.create_pr_on_keep is not None
                else top_publish.create_pr
                if top_publish.create_pr is not None
                else self.create_pr_on_keep
            ),
            pr_draft=(
                repo_publish.pr_draft
                if repo_publish.pr_draft is not None
                else repo.pr_draft
                if repo.pr_draft is not None
                else top_publish.pr_draft
                if top_publish.pr_draft is not None
                else self.pr_draft
            ),
        )
        return GitRepositoryConfig(
            repo_id=target_id,
            repo=repo_source_cfg,
            model=model_cfg,
            publish=publish_cfg,
            repo_path=repo_path,
            repo_url=repo_url,
            repo_lifecycle=lifecycle,
            sync_strategy=sync_strategy,
            baseline_dir=baseline_dir,
            source_dir=source_dir,
            requirements=requirements,
            allowed_paths=allowed_paths,
            trial_code_subdir=repo.trial_code_subdir or self.trial_code_subdir,
            branch_prefix=publish_cfg.branch_prefix,
            remote=remote,
            base_branch=base_branch,
            push_target_branch=publish_cfg.target_branch,
            push_on_keep=bool(publish_cfg.push_on_keep),
            create_pr_on_keep=bool(publish_cfg.create_pr),
            pr_draft=bool(publish_cfg.pr_draft),
        )


@dataclass
class McpConfig:
    lark: LarkMcpConfig = field(default_factory=LarkMcpConfig)
    git: GitMcpConfig = field(default_factory=GitMcpConfig)


@dataclass
class CodexGatewayConfig:
    enabled: bool = False
    mode: str = "cloudflared_access_tcp"
    cloudflared_path: str = ".tools/cloudflared"
    hostname: str = ""
    listener: str = "127.0.0.1:18080"
    proxy_url: str = ""
    log_file: str = ".codex_home/cloudflared-codex.log"
    pid_file: str = ".codex_home/cloudflared-codex.pid"
    log_level: str = "info"
    service_token_id: str = ""
    service_token_secret: str = ""


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
    codex_gateway: CodexGatewayConfig = field(default_factory=CodexGatewayConfig)

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


def _existing_proxy_url() -> str:
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        value = os.environ.get(key, "")
        if value:
            return value
    return ""


def _listener_proxy_url(cfg: Config) -> str:
    return f"http://{cfg.codex_gateway.listener}"


def _codex_gateway_proxy_url(cfg: Config) -> str:
    listener_proxy = _listener_proxy_url(cfg)
    if cfg.codex_gateway.mode == "proxy_env_only":
        if cfg.codex_gateway.proxy_url and cfg.codex_gateway.proxy_url != listener_proxy:
            return cfg.codex_gateway.proxy_url
        return _existing_proxy_url() or cfg.codex_gateway.proxy_url
    return cfg.codex_gateway.proxy_url or listener_proxy


def build_codex_config() -> CodexConfig:
    cfg = get_config()
    env: dict[str, str] = {
        "RUST_LOG": os.environ.get("RUST_LOG", "info"),
        "CODEX_HOME": cfg.resolved_codex_home,
    }
    if cfg.codex_gateway.enabled:
        proxy_url = _codex_gateway_proxy_url(cfg)
        if proxy_url:
            env.update({
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "all_proxy": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            })
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "CODEX_ACCESS_TOKEN",
    ):
        value = os.environ.get(key)
        if value and key not in env:
            env[key] = value
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
    "CODEX_GATEWAY_ENABLED": ("codex_gateway", "enabled"),
    "CODEX_GATEWAY_MODE": ("codex_gateway", "mode"),
    "CODEX_GATEWAY_HOSTNAME": ("codex_gateway", "hostname"),
    "CODEX_GATEWAY_LISTENER": ("codex_gateway", "listener"),
    "CODEX_GATEWAY_PROXY_URL": ("codex_gateway", "proxy_url"),
    "CODEX_GATEWAY_SERVICE_TOKEN_ID": ("codex_gateway", "service_token_id"),
    "CODEX_GATEWAY_SERVICE_TOKEN_SECRET": ("codex_gateway", "service_token_secret"),
}


def _set_nested(obj, path: list[str], value) -> None:
    """按路径设置嵌套 dataclass 属性"""
    target = obj
    for part in path[:-1]:
        target = getattr(target, part)
    current = getattr(target, path[-1], None)
    setattr(target, path[-1], _coerce_env_value(current, value))


def _coerce_env_value(current, value: str):
    if isinstance(current, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value


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


def _coerce_dataclass(cls, data):
    if isinstance(data, cls):
        return data
    if not isinstance(data, dict):
        return cls()
    allowed = {field.name for field in dataclass_fields(cls)}
    values = {key: value for key, value in data.items() if key in allowed and value is not None}
    return cls(**values)


def _coerce_git_model_config(data) -> GitModelConfig:
    cfg = _coerce_dataclass(GitModelConfig, data)
    cfg.output_contract = _coerce_dataclass(
        GitModelOutputContractConfig,
        cfg.output_contract,
    )
    return cfg


def _dataclass_dict(cls, data) -> dict:
    return asdict(_coerce_dataclass(cls, data))


def _merge_dataclass_dict(cls, first, second, *, prefer_second: bool) -> dict:
    merged = _dataclass_dict(cls, first)
    if prefer_second:
        for key, value in _dataclass_dict(cls, second).items():
            if value not in (None, "", [], {}):
                merged[key] = value
    return merged


def _has_explicit_model_field(data, field_name: str) -> bool:
    if isinstance(data, GitModelConfig):
        default = GitModelConfig()
        current = getattr(data, field_name, None)
        default_value = getattr(default, field_name, None)
        if field_name == "output_contract":
            return asdict(_coerce_dataclass(GitModelOutputContractConfig, current)) != asdict(default.output_contract)
        return current != default_value
    return isinstance(data, dict) and field_name in data and data[field_name] is not None


def _has_explicit_model_root(data) -> bool:
    return _has_explicit_model_field(data, "root")


def _has_explicit_dataclass_field(data, cls, field_name: str) -> bool:
    if isinstance(data, cls):
        return getattr(data, field_name, None) != getattr(cls(), field_name, None)
    return isinstance(data, dict) and field_name in data and data[field_name] is not None


def _relative_to_model_root(path: str, root: str) -> str:
    candidate = Path(path)
    model_root = Path(root)
    try:
        rel = candidate.relative_to(model_root)
        return rel.as_posix() or "."
    except ValueError:
        return candidate.as_posix()


def _default_copy_include(source_dir: str, requirements: str, root: str) -> list[str]:
    include = [f"{_relative_to_model_root(source_dir, root).rstrip('/')}/**"]
    if requirements:
        include.append(_relative_to_model_root(requirements, root))
    include.append("train.py")
    return include


def _coerce_git_repo_config(repo_id: str, data) -> GitRepositoryConfig:
    if isinstance(data, GitRepositoryConfig):
        values = asdict(data)
        values["repo_id"] = repo_id
        cfg = GitRepositoryConfig(**values)
    else:
        if not isinstance(data, dict):
            raise TypeError(f"Git MCP repository {repo_id!r} must be a mapping")
        allowed = {field.name for field in dataclass_fields(GitRepositoryConfig)}
        values = {key: value for key, value in data.items() if key in allowed and value is not None}
        values["repo_id"] = repo_id
        cfg = GitRepositoryConfig(**values)
    cfg.repo = _coerce_dataclass(GitRepoSourceConfig, cfg.repo)
    cfg.model = _coerce_git_model_config(cfg.model)
    cfg.publish = _coerce_dataclass(GitPublishConfig, cfg.publish)
    return cfg


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
        _apply_section(config, raw, "codex_gateway",
                       ["enabled", "mode", "cloudflared_path", "hostname",
                        "listener", "proxy_url", "log_file", "pid_file",
                        "log_level", "service_token_id", "service_token_secret"])
        _apply_section(config.mcp, raw.get("mcp", {}), "lark",
                       ["enabled", "backend", "server_name"])
        _apply_section(config.mcp, raw.get("mcp", {}), "git",
                       ["enabled", "scope", "active_repo",
                        "repo", "model", "publish",
                        "repo_path", "repo_url", "repo_lifecycle",
                        "baseline_dir", "source_dir", "requirements", "allowed_paths",
                        "trial_code_subdir", "branch_prefix",
                        "remote", "base_branch", "sync_strategy",
                        "sync_on_loop_start", "sync_before_each_trial",
                        "publish_via_subagent", "push_on_keep",
                        "create_pr_on_keep", "pr_draft", "push_target_branch",
                        "server_transport", "server_command", "server_args",
                        "require_human_approval_for_push",
                        "allow_force_push", "allow_reset_hard", "repositories"])
        config.mcp.git.repo = _coerce_dataclass(GitRepoSourceConfig, config.mcp.git.repo)
        config.mcp.git.model = _coerce_git_model_config(config.mcp.git.model)
        config.mcp.git.publish = _coerce_dataclass(GitPublishConfig, config.mcp.git.publish)
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


def override_data_primary(
    data_path: Path | str,
    *,
    data_root: Path | str | None = None,
) -> PathRegistry:
    """Override the primary training data path for the current process."""
    value = str(data_path).strip()
    if not value:
        raise ValueError("data_path must not be empty")

    paths = get_paths()
    primary = Path(value).expanduser()
    paths.cfg.data.primary = str(primary)

    root = Path(data_root).expanduser() if data_root is not None else primary.parent
    if str(root) not in ("", "."):
        paths.cfg.data.root = str(root)

    return paths


# 便捷函数
def feishu_enabled() -> bool:
    return get_config().feishu.enabled

def human_review_enabled() -> bool:
    cfg = get_config()
    return cfg.feishu.enabled and cfg.loop.human_review.enabled
