from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


PREDICTION_COLUMNS = ("prediction", "pred", "forecast", "y_pred")
ACTUAL_COLUMNS = ("actual", "label", "truth", "sales", "y_true")


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _find_column(rows: list[dict[str, str]], candidates: tuple[str, ...]) -> str:
    if not rows:
        raise ValueError("CSV file has no rows")
    lower_map = {column.lower(): column for column in rows[0].keys()}
    for candidate in candidates:
        if candidate in lower_map:
            return lower_map[candidate]
    raise ValueError(f"required column not found, expected one of {candidates}")


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def load_prediction_actual(prediction_path: str | Path, actual_path: str | Path) -> list[dict[str, Any]]:
    pred_rows = _read_csv(prediction_path)
    actual_rows = _read_csv(actual_path)
    pred_col = _find_column(pred_rows, PREDICTION_COLUMNS)
    actual_col = _find_column(actual_rows, ACTUAL_COLUMNS)

    pred_columns = set(pred_rows[0].keys()) if pred_rows else set()
    actual_columns = set(actual_rows[0].keys()) if actual_rows else set()
    common_keys = sorted((pred_columns & actual_columns) - {pred_col, actual_col})

    merged: list[dict[str, Any]] = []
    if common_keys:
        pred_by_key: dict[tuple[str, ...], list[dict[str, str]]] = {}
        for row in pred_rows:
            key = tuple(row.get(column, "") for column in common_keys)
            pred_by_key.setdefault(key, []).append(row)
        for actual in actual_rows:
            key = tuple(actual.get(column, "") for column in common_keys)
            matches = pred_by_key.get(key, [])
            for pred in matches:
                record: dict[str, Any] = {column: actual.get(column, pred.get(column, "")) for column in common_keys}
                for column, value in actual.items():
                    if column != actual_col and column not in record:
                        record[column] = value
                for column, value in pred.items():
                    if column != pred_col and column not in record:
                        record[column] = value
                record["actual"] = actual.get(actual_col)
                record["prediction"] = pred.get(pred_col)
                merged.append(record)
    else:
        for actual, pred in zip(actual_rows, pred_rows):
            record = {key: value for key, value in actual.items() if key != actual_col}
            for key, value in pred.items():
                if key != pred_col and key not in record:
                    record[key] = value
            record["actual"] = actual.get(actual_col)
            record["prediction"] = pred.get(pred_col)
            merged.append(record)

    if not merged:
        raise ValueError("prediction and actual files have no matching rows")
    return merged


def calculate_metric_values(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    valid: list[tuple[float, float]] = []
    for row in rows:
        actual = _to_float(row.get("actual"))
        prediction = _to_float(row.get("prediction"))
        if actual is not None and prediction is not None:
            valid.append((actual, prediction))
    if not valid:
        raise ValueError("no numeric prediction/actual rows available")

    errors = [prediction - actual for actual, prediction in valid]
    abs_errors = [abs(error) for error in errors]
    actual_sum = sum(actual for actual, _ in valid)
    non_zero = [(actual, prediction) for actual, prediction in valid if actual != 0]
    mape_values = [abs(prediction - actual) / abs(actual) for actual, prediction in non_zero]
    return {
        "wape": sum(abs_errors) / actual_sum if actual_sum != 0 else float("nan"),
        "mape": sum(mape_values) / len(mape_values) if mape_values else float("nan"),
        "bias": sum(errors) / actual_sum if actual_sum != 0 else float("nan"),
        "mae": sum(abs_errors) / len(abs_errors),
        "rmse": (sum(error ** 2 for error in errors) / len(errors)) ** 0.5,
        "rows": len(valid),
    }


def calculate_metrics(prediction_path: str | Path, actual_path: str | Path, output_path: str | Path | None = None) -> tuple[list[dict[str, Any]], dict[str, float | int]]:
    merged = load_prediction_actual(prediction_path, actual_path)
    metrics = calculate_metric_values(merged)
    if output_path:
        with Path(output_path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
            writer.writeheader()
            for metric, value in metrics.items():
                writer.writerow({"metric": metric, "value": value})
    return merged, metrics


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    _, metrics = calculate_metrics(args.prediction, args.actual, args.output)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
