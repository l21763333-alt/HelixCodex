#!/usr/bin/env python3
"""
config.py — Codex Flow 统一配置加载 (flow_config.yaml)

支持环境变量覆盖敏感字段 (避免写入配置文件):
  FEISHU_APP_ID, FEISHU_APP_SECRET, OPENAI_API_KEY, CODEX_API_KEY, CODEX_HOME, CODEX_MODEL
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field

from openai_codex.client import CodexConfig


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "flow_config.yaml"


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
class LarkMcpConfig:
    enabled: bool = True
    backend: str = "http"
    server_name: str = "lark_research"


@dataclass
class GitMcpConfig:
    enabled: bool = True
    scope: str = "baseline_model"
    repo_path: str = "."
    baseline_dir: str = "baseline"
    allowed_paths: list[str] = field(default_factory=lambda: [
        "baseline/src/**",
        "baseline/requirements.txt",
    ])
    trial_code_subdir: str = "agent2/code"
    branch_prefix: str = "model-exp/"
    remote: str = "origin"
    base_branch: str = "main"
    require_human_approval_for_push: bool = True
    allow_force_push: bool = False
    allow_reset_hard: bool = False


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
    paths: PathsConfig = field(default_factory=PathsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)

    @property
    def api_key(self) -> str:
        return self.openai_api_key or self.codex_api_key

    @property
    def resolved_codex_home(self) -> str:
        if not self.codex_home:
            return str(Path.home() / ".codex")
        path = Path(self.codex_home).expanduser()
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
        _apply_section(config, raw, "feishu",
                       ["enabled", "app_id", "app_secret", "chat_id",
                        "poll_interval", "verification_token"])
        _apply_section(config, raw, "loop",
                       ["max_iter", "target_wape", "max_sleep_hours"])
        _apply_section(config.loop, raw.get("loop", {}), "human_review",
                       ["enabled", "timeout", "auto_fallback", "authorized_senders"])
        _apply_section(config.loop, raw.get("loop", {}), "limits",
                       ["max_consecutive_reverses", "max_rollbacks_per_round"])
        _apply_section(config.loop, raw.get("loop", {}), "convergence",
                       ["min_wape_improvement", "max_rounds_without_improvement"])
        _apply_section(config, raw, "paths",
                       ["experiment_dir", "runs_dir", "skills_dir"])
        _apply_section(config.mcp, raw.get("mcp", {}), "lark",
                       ["enabled", "backend", "server_name"])
        _apply_section(config.mcp, raw.get("mcp", {}), "git",
                       ["enabled", "scope", "repo_path", "baseline_dir",
                        "allowed_paths", "trial_code_subdir", "branch_prefix",
                        "remote", "base_branch",
                        "require_human_approval_for_push",
                        "allow_force_push", "allow_reset_hard"])

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


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(path: Path | str | None = None) -> Config:
    global _config
    _config = load_config(path)
    return _config


# 便捷函数
def feishu_enabled() -> bool:
    return get_config().feishu.enabled

def human_review_enabled() -> bool:
    cfg = get_config()
    return cfg.feishu.enabled and cfg.loop.human_review.enabled
