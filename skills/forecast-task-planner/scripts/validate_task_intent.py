from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def validate(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors = []
    if data.get("task_type") != "forecast_evaluation":
        errors.append("task_type must be forecast_evaluation")
    if not data.get("ask"):
        errors.append("ask is required")
    if "prediction_path" in data or "actual_path" in data:
        errors.append("task intent must not contain artifact paths")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    errors = validate(Path(args.input))
    if errors:
        print("\n".join(errors))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
