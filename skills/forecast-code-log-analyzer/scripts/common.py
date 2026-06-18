from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def ensure_dir(path: str | Path) -> Path:
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def write_json(path: str | Path, data: Any) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_text_sample(path: str | Path, max_chars: int = 20000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""


def relpath(path: str | Path, root: str | Path) -> str:
    path_obj = Path(path).resolve()
    root_obj = Path(root).resolve()
    try:
        return path_obj.relative_to(root_obj).as_posix()
    except ValueError:
        return path_obj.as_posix()
