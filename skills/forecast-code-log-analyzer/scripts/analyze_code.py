from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import read_text_sample


METRIC_PATTERNS = ("wape", "mape", "bias", "mae", "rmse")
MODEL_PATTERNS = ("fit(", "train(", "xgboost", "lightgbm", "prophet", "sklearn")
FEATURE_PATTERNS = ("feature", "build_feature", "transform")
OUTPUT_PATTERNS = ("to_csv", "save", "prediction", "forecast")


def analyze_code(experiment_dir: str | Path, code_files: list[str]) -> dict[str, Any]:
    root = Path(experiment_dir)
    analysis: dict[str, Any] = {
        "entrypoints": [],
        "evaluation_entrypoints": [],
        "metric_functions": [],
        "function_defs": [],
        "code_identifiers": [],
        "argparse_args": [],
        "feature_modules": [],
        "model_modules": [],
        "output_patterns": [],
        "possible_issues": [],
    }

    for rel in code_files:
        path = root / rel
        name = path.name.lower()
        text = read_text_sample(path).lower()
        if name in {"train.py", "main.py", "run_train.py", "run.py"}:
            analysis["entrypoints"].append(rel)
        if name in {"evaluate.py", "eval.py", "metrics.py"}:
            analysis["evaluation_entrypoints"].append(rel)
        for function_name in re.findall(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text, flags=re.MULTILINE):
            if function_name not in analysis["function_defs"]:
                analysis["function_defs"].append(function_name)
        for identifier in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text):
            if identifier not in analysis["code_identifiers"]:
                analysis["code_identifiers"].append(identifier)
            if len(analysis["code_identifiers"]) >= 300:
                break
        for arg in _argparse_flags(text):
            if arg not in analysis["argparse_args"]:
                analysis["argparse_args"].append(arg)
        for metric in METRIC_PATTERNS:
            if re.search(rf"\b{metric}\b", text) and metric not in analysis["metric_functions"]:
                analysis["metric_functions"].append(metric)
        if any(pattern in name or pattern in text for pattern in FEATURE_PATTERNS):
            analysis["feature_modules"].append(rel)
        if any(pattern in text for pattern in MODEL_PATTERNS):
            analysis["model_modules"].append(rel)
        if any(pattern in text for pattern in OUTPUT_PATTERNS):
            analysis["output_patterns"].append(rel)

    if not code_files:
        analysis["possible_issues"].append("code analysis unavailable: no code files found")
    return analysis


def _argparse_flags(text: str) -> list[str]:
    flags: list[str] = []
    pattern = re.compile(r"add_argument\((?P<body>.*?)\)", re.DOTALL)
    for match in pattern.finditer(_strip_comment_lines(text)):
        body = match.group("body")
        for flag in re.findall(r"['\"](--[A-Za-z0-9][A-Za-z0-9_-]*)['\"]", body):
            flags.append(flag)
    return flags


def _strip_comment_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def main() -> int:
    import argparse
    import json

    from scan_experiment import scan_experiment

    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    scan = scan_experiment(args.experiment)
    print(json.dumps(analyze_code(args.experiment, scan["code_files"]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
