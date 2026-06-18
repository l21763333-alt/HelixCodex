from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from calculate_metrics import load_prediction_actual


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def _boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "holiday"}


def _weekday_label(value: Any) -> str:
    try:
        day = datetime.fromisoformat(str(value)).weekday()
    except ValueError:
        day = 0
    return "weekend" if day >= 5 else "weekday"


def _scene_for(row: dict[str, Any], q25: float, q75: float) -> str:
    actual = _to_float(row.get("actual"))
    error = _to_float(row.get("prediction")) - actual
    parts: list[str] = []
    if "date" in row:
        parts.append(_weekday_label(row.get("date")))
    parts.append("high_target" if actual >= q75 else "low_target" if actual <= q25 else "normal_target")
    parts.append("overestimate" if error > 0 else "underestimate" if error < 0 else "exact")
    for optional, label in [("is_holiday", "holiday"), ("is_peak_day", "peak_day"), ("is_long_tail", "long_tail")]:
        if optional in row:
            parts.append(label if _boolish(row.get(optional)) else f"not_{label}")
    if "history_days" in row:
        parts.append("short_history" if _to_float(row.get("history_days")) < 30 else "enough_history")
    return ";".join(parts)


def tag_scenes(rows: list[dict[str, Any]], output_path: str | Path | None = None) -> list[dict[str, Any]]:
    actuals = [_to_float(row.get("actual")) for row in rows]
    q25 = _quantile(actuals, 0.25)
    q75 = _quantile(actuals, 0.75)
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"rows": 0, "actual_sum": 0.0, "abs_error_sum": 0.0, "bias_sum": 0.0})
    for row in rows:
        actual = _to_float(row.get("actual"))
        prediction = _to_float(row.get("prediction"))
        error = prediction - actual
        scene = _scene_for(row, q25, q75)
        agg = grouped[scene]
        agg["rows"] += 1
        agg["actual_sum"] += actual
        agg["abs_error_sum"] += abs(error)
        agg["bias_sum"] += error

    result = []
    for scene, agg in grouped.items():
        actual_sum = agg["actual_sum"]
        result.append({
            "scene": scene,
            "rows": agg["rows"],
            "actual_sum": actual_sum,
            "abs_error_sum": agg["abs_error_sum"],
            "bias_sum": agg["bias_sum"],
            "wape": agg["abs_error_sum"] / actual_sum if actual_sum else "",
            "bias": agg["bias_sum"] / actual_sum if actual_sum else "",
        })
    result.sort(key=lambda row: (float(row["wape"]) if row["wape"] != "" else -1), reverse=True)
    if output_path:
        with Path(output_path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["scene", "rows", "actual_sum", "abs_error_sum", "bias_sum", "wape", "bias"])
            writer.writeheader()
            writer.writerows(result)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    merged = load_prediction_actual(args.prediction, args.actual)
    tag_scenes(merged, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
