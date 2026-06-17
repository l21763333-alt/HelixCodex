from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import read_text_sample


def parse_log(log_path: str | Path | None) -> dict[str, Any]:
    if not log_path:
        return {
            "available": False,
            "errors": [],
            "warnings": [],
            "loss_values": [],
            "metric_values": {},
            "row_counts": [],
            "status": "unavailable",
            "notes": ["日志分析不可用"],
        }

    path = Path(log_path)
    text = read_text_sample(path, max_chars=80000)
    lines = text.splitlines()
    errors = [line for line in lines if re.search(r"\berror\b|failed|exception|nan", line, re.I)]
    warnings = [line for line in lines if re.search(r"\bwarning\b|warn|missing", line, re.I)]
    loss_values = [float(v) for v in re.findall(r"loss\s*=\s*([0-9.]+)", text, re.I)]
    metric_values = {
        name.lower(): float(value)
        for name, value in re.findall(r"\b(wape|mape|bias|mae|rmse)\s*=\s*([0-9.]+)", text, re.I)
    }
    row_counts = [int(v) for v in re.findall(r"rows?\s*=\s*(\d+)", text, re.I)]
    if re.search(r"success|completed|finished", text, re.I) and not re.search(r"failed", text, re.I):
        status = "success"
    elif re.search(r"failed|error|exception|nan", text, re.I):
        status = "failed_or_risky"
    else:
        status = "unknown"
    return {
        "available": True,
        "path": path.resolve().as_posix(),
        "errors": errors[:20],
        "warnings": warnings[:20],
        "loss_values": loss_values,
        "metric_values": metric_values,
        "row_counts": row_counts,
        "status": status,
        "notes": [],
    }


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--log")
    args = parser.parse_args()
    print(json.dumps(parse_log(args.log), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
