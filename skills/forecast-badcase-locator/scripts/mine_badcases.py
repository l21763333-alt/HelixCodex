from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from calculate_metrics import load_prediction_actual


DATE_COLUMNS = ("date", "dt", "ds", "day", "biz_date", "business_date", "sale_date", "sales_date")
METRIC_COLUMNS = {
    "prediction",
    "actual",
    "error",
    "abs_error",
    "ape",
    "badcase_type",
    "key_columns",
    "streak_length",
    "streak_start_date",
    "streak_end_date",
    "streak_abs_error_sum",
}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _with_errors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        actual = _to_float(row.get("actual"))
        prediction = _to_float(row.get("prediction"))
        if actual is None or prediction is None:
            continue
        enriched = dict(row)
        error = prediction - actual
        enriched["prediction"] = prediction
        enriched["actual"] = actual
        enriched["error"] = error
        enriched["abs_error"] = abs(error)
        enriched["ape"] = abs(error) / abs(actual) if actual != 0 else ""
        key_columns = [key for key in row.keys() if key not in {"prediction", "actual", "error", "abs_error", "ape"}]
        enriched["key_columns"] = ",".join(key_columns)
        result.append(enriched)
    return result


def _pick(rows: list[dict[str, Any]], badcase_type: str, sort_column: str, reverse: bool, top_n: int) -> list[dict[str, Any]]:
    sortable = [row for row in rows if row.get(sort_column) != ""]
    picked = sorted(sortable, key=lambda row: float(row[sort_column]), reverse=reverse)[:top_n]
    result = []
    for row in picked:
        enriched = dict(row)
        enriched["badcase_type"] = badcase_type
        result.append(enriched)
    return result


def _pick_extreme(
    rows: list[dict[str, Any]],
    *,
    ape_threshold: float,
    abs_error_threshold: float,
    top_n: int,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if row.get("ape") != ""
        and float(row["ape"]) > ape_threshold
        and float(row["abs_error"]) > abs_error_threshold
    ]
    result = []
    result.extend(_pick([row for row in candidates if row["error"] < 0], "extreme_underestimate", "abs_error", True, top_n))
    result.extend(_pick([row for row in candidates if row["error"] > 0], "extreme_overestimate", "abs_error", True, top_n))
    return result


def _find_date_column(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    lower_map = {column.lower(): column for column in rows[0].keys()}
    for candidate in DATE_COLUMNS:
        if candidate in lower_map:
            return lower_map[candidate]
    return None


def _dimension_columns(row: dict[str, Any], date_column: str) -> list[str]:
    return sorted(column for column in row.keys() if column not in METRIC_COLUMNS and column != date_column)


def _direction(error: float) -> str | None:
    if error < 0:
        return "underestimate"
    if error > 0:
        return "overestimate"
    return None


def _streak_record(run: list[dict[str, Any]], direction: str, date_column: str) -> dict[str, Any]:
    worst_row = max(run, key=lambda row: float(row["abs_error"]))
    result = dict(worst_row)
    result["badcase_type"] = f"consecutive_{direction}"
    result["streak_length"] = len(run)
    result["streak_start_date"] = run[0].get(date_column, "")
    result["streak_end_date"] = run[-1].get(date_column, "")
    result["streak_abs_error_sum"] = sum(float(row["abs_error"]) for row in run)
    return result


def _pick_consecutive_bias(rows: list[dict[str, Any]], *, min_streak: int, top_n: int) -> list[dict[str, Any]]:
    date_column = _find_date_column(rows)
    if not date_column or min_streak <= 1:
        return []

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(column, "") for column in _dimension_columns(row, date_column))
        grouped.setdefault(key, []).append(row)

    streaks: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        ordered = sorted(group_rows, key=lambda row: str(row.get(date_column, "")))
        run: list[dict[str, Any]] = []
        run_direction: str | None = None
        for row in ordered:
            direction = _direction(float(row["error"]))
            if direction is None:
                if run_direction and len(run) >= min_streak:
                    streaks.append(_streak_record(run, run_direction, date_column))
                run = []
                run_direction = None
                continue
            if direction != run_direction:
                if run_direction and len(run) >= min_streak:
                    streaks.append(_streak_record(run, run_direction, date_column))
                run = [row]
                run_direction = direction
            else:
                run.append(row)
        if run_direction and len(run) >= min_streak:
            streaks.append(_streak_record(run, run_direction, date_column))

    result = []
    for badcase_type in ("consecutive_underestimate", "consecutive_overestimate"):
        typed = [row for row in streaks if row["badcase_type"] == badcase_type]
        result.extend(sorted(typed, key=lambda row: float(row["streak_abs_error_sum"]), reverse=True)[:top_n])
    return result


def mine_badcases(
    rows: list[dict[str, Any]],
    top_n: int = 10,
    output_path: str | Path | None = None,
    extreme_ape_threshold: float = 0.2,
    extreme_abs_error_threshold: float = 10,
    min_streak: int = 3,
) -> list[dict[str, Any]]:
    data = _with_errors(rows)
    result = []
    result.extend(_pick(data, "top_abs_error", "abs_error", True, top_n))
    result.extend(_pick(data, "top_ape", "ape", True, top_n))
    result.extend(_pick([row for row in data if row["error"] < 0], "top_underestimate", "error", False, top_n))
    result.extend(_pick([row for row in data if row["error"] > 0], "top_overestimate", "error", True, top_n))
    result.extend(
        _pick_extreme(
            data,
            ape_threshold=extreme_ape_threshold,
            abs_error_threshold=extreme_abs_error_threshold,
            top_n=top_n,
        )
    )
    result.extend(_pick_consecutive_bias(data, min_streak=min_streak, top_n=top_n))
    if output_path:
        fieldnames = ["badcase_type", "key_columns"]
        for row in result:
            for key in row.keys():
                if key not in fieldnames:
                    fieldnames.append(key)
        with Path(output_path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(result)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--extreme-ape-threshold", type=float, default=0.2)
    parser.add_argument("--extreme-abs-error-threshold", type=float, default=10)
    parser.add_argument("--min-streak", type=int, default=3)
    args = parser.parse_args()
    merged = load_prediction_actual(args.prediction, args.actual)
    mine_badcases(
        merged,
        top_n=args.top_n,
        output_path=args.output,
        extreme_ape_threshold=args.extreme_ape_threshold,
        extreme_abs_error_threshold=args.extreme_abs_error_threshold,
        min_streak=args.min_streak,
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())