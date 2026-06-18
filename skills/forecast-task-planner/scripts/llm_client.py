from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


if load_dotenv:
    load_dotenv()


def _expand_env(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        return os.getenv(name, default)

    return re.sub(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}", replace, value)


def load_llm_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path or "config/llm.yaml")
    if not path.exists():
        example = Path("config/llm.yaml.example")
        path = example if example.exists() else path
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: _expand_env(value) for key, value in data.items()}


class LLMClient:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_llm_config()
        self.provider = self.config.get("provider", "openai")
        self.model = self.config.get("model") or os.getenv("OPENAI_MODEL") or "gpt-5"
        self.api_key_env = self.config.get("api_key_env", "OPENAI_API_KEY")
        self.enabled = bool(self.config.get("enabled", True))

    @property
    def available(self) -> bool:
        return self.enabled and self.provider == "openai" and bool(os.getenv(self.api_key_env))

    def complete_text(self, *, instructions: str, input_text: str) -> str | None:
        if not self.available:
            return None
        try:
            from openai import OpenAI

            client = OpenAI(api_key=os.getenv(self.api_key_env), timeout=float(self.config.get("timeout_seconds", 60)))
            response = client.responses.create(
                model=self.model,
                instructions=instructions,
                input=input_text,
            )
            return getattr(response, "output_text", None) or str(response)
        except Exception as exc:  # pragma: no cover - network/provider failure fallback
            return json.dumps({"llm_error": str(exc)}, ensure_ascii=False)


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
