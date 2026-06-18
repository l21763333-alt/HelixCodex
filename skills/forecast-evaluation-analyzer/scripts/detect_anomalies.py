from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _read_metric_csv(path: str | Path | None) -> dict[str, float]:
    if not path or not Path(path).exists():
        return {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        result = {}
        for row in csv.DictReader(handle):
            try:
                result[str(row.get("metric"))] = float(row.get("value", ""))
            except (TypeError, ValueError):
                continue
        return result


def _read_csv_records(path: str | Path | None) -> list[dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def detect_anomalies(metrics: dict[str, float] | None, scene_metrics: list[dict[str, Any]] | None, log_summary: dict[str, Any], output_path: str | Path | None = None) -> dict[str, Any]:
    anomalies: list[dict[str, Any]] = []
    metrics = metrics or {}
    wape = metrics.get("wape")
    if wape is not None and wape >= 0.3:
        anomalies.append({"type": "high_wape", "evidence": wape})
    bias = metrics.get("bias")
    if bias is not None and abs(bias) >= 0.1:
        bias_type = "systematic_overestimate" if bias > 0 else "systematic_underestimate"
        anomalies.append({"type": "systematic_bias", "direction": bias_type, "evidence": bias})
    if scene_metrics:
        sorted_scenes = sorted(scene_metrics, key=lambda row: _to_float(row.get("wape")) or -1, reverse=True)
        high_scene = sorted_scenes[0]
        high_wape = _to_float(high_scene.get("wape"))
        if high_wape is not None and high_wape >= 0.3:
            anomalies.append({"type": "scene_high_error", "evidence": high_scene})
    if log_summary.get("errors"):
        anomalies.append({"type": "train_log_error", "evidence": log_summary["errors"][:3]})
    if log_summary.get("warnings"):
        anomalies.append({"type": "train_log_warning", "evidence": log_summary["warnings"][:3]})
    result = {"anomalies": anomalies}
    if output_path:
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics")
    parser.add_argument("--scene-metrics")
    parser.add_argument("--log-summary")
    parser.add_argument("--output")
    args = parser.parse_args()
    log_summary = json.loads(Path(args.log_summary).read_text(encoding="utf-8")) if args.log_summary else {"errors": [], "warnings": []}
    result = detect_anomalies(_read_metric_csv(args.metrics), _read_csv_records(args.scene_metrics), log_summary, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
