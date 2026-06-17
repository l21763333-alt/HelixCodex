from __future__ import annotations

from pathlib import Path
from typing import Any


def _resolve(root: Path, value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve().as_posix()


def _select_one(root: Path, candidates: list[str], override: str | None, label: str) -> tuple[str | None, str, str | None]:
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            return None, "missing", f"{label} override does not exist: {path}"
        return path.resolve().as_posix(), "override", None

    if not candidates:
        return None, "missing", None
    if len(candidates) == 1:
        return _resolve(root, candidates[0]), "high", None
    return None, "ambiguous", f"multiple {label} candidates: {candidates}"


def discover_artifacts(
    scan_result: dict[str, Any],
    ask: str,
    *,
    log: str | None = None,
    prediction: str | None = None,
    actual: str | None = None,
) -> dict[str, Any]:
    root = Path(scan_result["experiment_dir"]).resolve()
    prediction_path, prediction_confidence, prediction_issue = _select_one(
        root, scan_result.get("candidate_prediction_files", []), prediction, "prediction"
    )
    actual_path, actual_confidence, actual_issue = _select_one(
        root, scan_result.get("actual_files", []), actual, "actual"
    )
    log_path, log_confidence, log_issue = _select_one(root, scan_result.get("log_files", []), log, "log")
    metrics_path, metrics_confidence, metrics_issue = _select_one(
        root, scan_result.get("metrics_files", []), None, "metrics"
    )

    ambiguous = {}
    missing = []
    for key, path, confidence, issue in [
        ("prediction_path", prediction_path, prediction_confidence, prediction_issue),
        ("actual_path", actual_path, actual_confidence, actual_issue),
        ("train_log_path", log_path, log_confidence, log_issue),
        ("metrics_path", metrics_path, metrics_confidence, metrics_issue),
    ]:
        if confidence == "ambiguous":
            ambiguous[key] = issue
        elif confidence == "missing":
            missing.append(key)

    mode_ready = bool(prediction_path and actual_path and not ambiguous.get("prediction_path") and not ambiguous.get("actual_path"))
    return {
        "ask": ask,
        "prediction_path": prediction_path,
        "actual_path": actual_path,
        "train_log_path": log_path,
        "metrics_path": metrics_path,
        "confidence": {
            "prediction_path": prediction_confidence,
            "actual_path": actual_confidence,
            "train_log_path": log_confidence,
            "metrics_path": metrics_confidence,
        },
        "missing_artifacts": missing,
        "ambiguous_artifacts": ambiguous,
        "mode_ready": mode_ready,
    }


def main() -> int:
    import argparse
    import json

    from scan_experiment import scan_experiment

    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--ask", required=True)
    parser.add_argument("--log")
    parser.add_argument("--prediction")
    parser.add_argument("--actual")
    args = parser.parse_args()
    scan = scan_experiment(args.experiment)
    artifacts = discover_artifacts(
        scan,
        args.ask,
        log=args.log,
        prediction=args.prediction,
        actual=args.actual,
    )
    print(json.dumps(artifacts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
