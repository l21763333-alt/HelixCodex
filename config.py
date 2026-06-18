#!/usr/bin/env python3
"""
config.py — Codex Flow 统一配置加载

从 codex_flow_config.json 读取所有设置，不依赖环境变量。
codex_flow.py 和 lark_notify.py 共用此模块。
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field


CONFIG_PATH = Path(__file__).resolve().parent / "codex_flow_config.json"


@dataclass
class LarkConfig:
    chat_id: str = ""


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""


@dataclass
class LoopConfig:
    max_iter: int = 10
    target_wape: float | None = None
    max_sleep_hours: float = 24.0
    human_review: bool = False
    review_timeout: int = 1800


@dataclass
class Config:
    codex_home: str = ""
    openai_api_key: str = ""
    codex_api_key: str = ""
    model: str = "gpt-5.5"
    lark: LarkConfig = field(default_factory=LarkConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)

    @property
    def api_key(self) -> str:
        """返回第一个可用的 API Key"""
        return self.openai_api_key or self.codex_api_key

    @property
    def resolved_codex_home(self) -> str:
        """返回解析后的 CODEX_HOME (空则用默认 ~/.codex)"""
        return self.codex_home or str(Path.home() / ".codex")


def load_config(path: Path | str | None = None) -> Config:
    """从 JSON 文件加载配置, 缺失字段用默认值"""
    config_path = Path(path) if path else CONFIG_PATH
    config = Config()

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            # 顶层字段
            for key in ("codex_home", "openai_api_key", "codex_api_key", "model"):
                if key in data and data[key]:
                    setattr(config, key, data[key])
            # lark 子配置 (兼容旧配置, 已迁移到 feishu)
            if "lark" in data and isinstance(data["lark"], dict):
                config.lark.chat_id = data["lark"].get("chat_id", "")
            # feishu 子配置 (新增)
            if "feishu" in data and isinstance(data["feishu"], dict):
                fd = data["feishu"]
                for key in ("app_id", "app_secret", "chat_id"):
                    if key in fd and fd[key]:
                        setattr(config.feishu, key, fd[key])
                # 向后兼容: feishu.chat_id 覆盖 lark.chat_id
                if config.feishu.chat_id and not config.lark.chat_id:
                    config.lark.chat_id = config.feishu.chat_id
            # loop 子配置
            if "loop" in data and isinstance(data["loop"], dict):
                loop_data = data["loop"]
                for key in ("max_iter", "target_wape", "max_sleep_hours",
                           "human_review", "review_timeout"):
                    if key in loop_data and loop_data[key] is not None:
                        setattr(config.loop, key, loop_data[key])
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[Config] 配置文件解析失败: {e}, 使用默认值")

    return config


# 模块级单例 — 首次 import 时加载
_config: Config | None = None


def get_config() -> Config:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(path: Path | str | None = None) -> Config:
    """重新加载配置 (用于运行时更新)"""
    global _config
    _config = load_config(path)
    return _config
