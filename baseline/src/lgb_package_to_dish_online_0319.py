import argparse
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
BASELINE_DIR = CURRENT_DIR.parent
PYTHON_PACKAGE_DIR = BASELINE_DIR / ".python_packages"
for path_item in [str(PYTHON_PACKAGE_DIR), str(BASELINE_DIR), str(CURRENT_DIR)]:
    if path_item not in sys.path:
        sys.path.insert(0, path_item)

import numpy as np
import pandas as pd

DEFAULT_DATA_PATH = BASELINE_DIR / "data" / "dish_package_feature_df.csv"
DEFAULT_HOLIDAY_PATH = BASELINE_DIR / "data" / "holiday_imformation.csv"
DEFAULT_FUTURE_WEATHER_PATH = BASELINE_DIR / "data" / "future_weather_0319.csv"
DEFAULT_PEAK_PATH = Path("/data/zhangxiaotian/data/gdf_xx_completed.csv")
DEFAULT_OUTPUT_DIR = BASELINE_DIR / "outputs"

try:
    from util import (
        PACKAGE_ID_COLS,
        ROW_ID_COLS,
        add_package_lifecycle_features,
        allocate_dish_prediction,
        apply_recent_trend_guardrail,
        build_features,
        build_package_frame,
        build_ratio_bundle,
        build_train_sample_weights,
        deduplicate_to_package_level,
        evaluate,
        fit_fixed_iter_model,
        load_data,
        prepare_raw_rows,
        setup_logger,
        split_data,
        time_series_continuization,
        logging,
        train_package_model,
    )
except ImportError:
    from util import (  # type: ignore
        PACKAGE_ID_COLS,
        ROW_ID_COLS,
        add_package_lifecycle_features,
        allocate_dish_prediction,
        apply_recent_trend_guardrail,
        build_features,
        build_package_frame,
        build_ratio_bundle,
        build_train_sample_weights,
        deduplicate_to_package_level,
        evaluate,
        fit_fixed_iter_model,
        load_data,
        prepare_raw_rows,
        setup_logger,
        split_data,
        time_series_continuization,
        logging,
        train_package_model,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T+2 package forecast baseline experiments")
    parser.add_argument("--experiment", type=str, default="baseline", choices=["baseline", "exp_02_tweedie"])
    # parser.add_argument("--data_path", type=str, default=r"D:\hdl_data\ai_order_tc_forecast_0319_a.csv")
    # parser.add_argument("--holiday_path", type=str, default=r"D:\hdl_data\holiday_imformation.csv")
    # parser.add_argument("--future_weather_path", type=str, default=r"D:\hdl_data\future_weather_0319.csv")
    # parser.add_argument("--output_dir", type=str, default=r"d:\vs_code\outputs_package_to_dish_online_0319")
    # parser.add_argument("--data_path", type=str, default=r"./data/ai_order_tc_forecast_0319_a.csv")
    parser.add_argument("--data_path", type=str, default=str(DEFAULT_DATA_PATH))
    parser.add_argument("--holiday_path", type=str, default=str(DEFAULT_HOLIDAY_PATH))
    parser.add_argument("--future_weather_path", type=str, default=str(DEFAULT_FUTURE_WEATHER_PATH))
    parser.add_argument("--peak_path", type=str, default=str(DEFAULT_PEAK_PATH), help="High/low peak calendar CSV.")
    parser.add_argument("--output_dir", type=str, default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--bizdate", type=str, default=None)
    parser.add_argument("--forecast_days", type=int, default=8)
    parser.add_argument("--package_label_col", type=str, default="pos_cnt")
    parser.add_argument("--dish_label_col", type=str, default="real_qty")
    parser.add_argument("--date_col", type=str, default="ds")
    parser.add_argument("--package_id_cols", type=str, nargs="*", default=PACKAGE_ID_COLS)
    parser.add_argument("--valid_days", type=int, default=7)
    parser.add_argument("--test_days", type=int, default=7)
    parser.add_argument("--lag_days", type=int, nargs="*", default=[1, 2, 3, 7])
    parser.add_argument("--rolling_windows", type=int, nargs="*", default=[3, 7])
    parser.add_argument("--n_estimators", type=int, default=1500)
    parser.add_argument("--learning_rate", type=float, default=0.01)
    parser.add_argument("--num_leaves", type=int, default=63)
    parser.add_argument("--random_state", type=int, default=42)
    parser.add_argument("--objective", type=str, default="regression", choices=["regression", "regression_l1", "huber", "fair", "poisson", "tweedie"])
    parser.add_argument("--tweedie_variance_power", type=float, default=1.2)
    parser.add_argument("--ratio_strategy", type=str, default="full", choices=["full", "recent", "weighted"])
    parser.add_argument("--ratio_windows", type=int, nargs="*", default=[7, 14, 30])
    parser.add_argument("--ratio_half_life_days", type=float, default=7.0)
    parser.add_argument("--train_weight_strategy", type=str, default="none", choices=["none", "recent"])
    parser.add_argument("--train_weight_half_life_days", type=float, default=7.0)
    parser.add_argument("--trend_guardrail", type=str, default="none", choices=["none", "downtrend_clip"])
    parser.add_argument("--guardrail_recent_threshold", type=float, default=0.80)
    parser.add_argument("--guardrail_pred_threshold", type=float, default=1.15)
    parser.add_argument("--guardrail_cap_multiplier", type=float, default=1.30)
    parser.add_argument("--output1", type=str, default=None, help="Output OSS port 1.")
    parser.add_argument("--output2", type=str, default=None, help="Output OSS port 2.")
    parser.add_argument("--output3", type=str, default=None, help="Output MaxComputeTable 1.")
    parser.add_argument("--output4", type=str, default=None, help="Output MaxComputeTable 2.")
    parser.add_argument("--history_eval_only", action="store_true", help="Only train and evaluate on historical holdout data.")
    parser.add_argument("--t2_backtest", action="store_true", default=True, help="Run direct T+2 package-count backtest only.")
    parser.add_argument("--train_eval_end", type=str, default="20260430", help="Last label date allowed in T+2 train/valid/test.")
    parser.add_argument("--backtest_output_prefix", type=str, default="exp_00_baseline", help="Prefix for T+2 backtest output files.")
    parser.add_argument("--enable_peak_history_mean", action="store_true", default=True, help="Add historical high/low peak package mean features.")
    parser.add_argument("--peak_mean_windows", type=int, nargs="*", default=[1, 2, 3, 7])
    parser.add_argument("--enable_holiday_position", action="store_true", default=True, help="Add holiday first/last/middle day features.")
    parser.add_argument("--enable_t2_availability_features", action="store_true", default=True, help="Add T+2 known availability/window features.")
    parser.add_argument("--enable_manual_public_holiday_features", action="store_true", default=True, help="Add manual public holiday and holiday-eve features.")
    parser.add_argument("--enable_package_activity_features", action="store_true", default=True, help="Add package heat/lifecycle features using T+2-safe history.")
    parser.add_argument("--enable_group_calibration", action="store_true", default=True, help="Calibrate T+2 test predictions by validation residual groups.")
    parser.add_argument("--calibration_min_rows", type=int, default=30)
    parser.add_argument("--calibration_min_true_sum", type=float, default=20.0)
    parser.add_argument("--calibration_min_pred_sum", type=float, default=20.0)
    parser.add_argument("--calibration_clip_lower", type=float, default=0.70)
    parser.add_argument("--calibration_clip_upper", type=float, default=1.50)
    args = parser.parse_args()
    return apply_experiment_preset(args)


def apply_experiment_preset(args: argparse.Namespace) -> argparse.Namespace:
    args.data_path = str(DEFAULT_DATA_PATH)
    args.output_dir = str(DEFAULT_OUTPUT_DIR)
    args.train_eval_end = "20260430"
    args.t2_backtest = True

    if args.experiment == "baseline":
        args.peak_path = None
        args.enable_peak_history_mean = False
        args.enable_holiday_position = False
        args.enable_t2_availability_features = False
        args.enable_manual_public_holiday_features = False
        args.enable_package_activity_features = False
        args.enable_group_calibration = False
        args.objective = "regression"
        args.backtest_output_prefix = "exp_00_baseline"
        return args

    args.peak_path = str(DEFAULT_PEAK_PATH)
    args.enable_peak_history_mean = True
    args.enable_holiday_position = True
    args.objective = "tweedie"
    args.tweedie_variance_power = 1.2
    args.enable_t2_availability_features = True
    args.enable_manual_public_holiday_features = True
    args.enable_package_activity_features = True
    args.enable_group_calibration = True
    args.backtest_output_prefix = "exp_02_tweedie_t2_availability_holiday_calibrated"
    return args

def load_holiday_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ds"] = pd.to_datetime(pd.to_numeric(df["ds"], errors="coerce").astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    if df["ds"].isna().any():
        raise ValueError(f"Invalid ds found in holiday file: {path}")
    return df

def load_peak_calendar(path: str) -> pd.DataFrame:
    peak_df = pd.read_csv(path)
    required_cols = {"use_date", "date_attribute"}
    missing_cols = required_cols - set(peak_df.columns)
    if missing_cols:
        raise ValueError(f"Peak calendar missing required columns: {sorted(missing_cols)}")

    peak_df = peak_df[["use_date", "date_attribute"]].copy()
    peak_df["ds"] = pd.to_datetime(
        pd.to_numeric(peak_df["use_date"], errors="coerce").astype("Int64").astype(str),
        format="%Y%m%d",
        errors="coerce",
    )
    peak_df["date_attribute"] = pd.to_numeric(peak_df["date_attribute"], errors="coerce")
    peak_df = peak_df.dropna(subset=["ds", "date_attribute"]).drop_duplicates("ds", keep="last")
    if peak_df.empty:
        raise ValueError(f"Peak calendar has no valid rows: {path}")
    return peak_df[["ds", "date_attribute"]].copy()


def parse_arg_date(date_text: str, arg_name: str) -> pd.Timestamp:
    text = str(date_text).strip().replace("-", "")
    dt = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(dt):
        raise ValueError(f"{arg_name} must be YYYYMMDD, got: {date_text}")
    return pd.Timestamp(dt).normalize()


def parse_mixed_date_series(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    numeric = pd.to_numeric(text.str.replace(r"\.0$", "", regex=True), errors="coerce")
    numeric_dt = pd.to_datetime(numeric.astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    generic_dt = pd.to_datetime(text, errors="coerce")
    return numeric_dt.fillna(generic_dt).dt.normalize()


def add_t2_availability_features(
    df: pd.DataFrame,
    args: argparse.Namespace,
    horizon_days: int = 2,
) -> pd.DataFrame:
    out_df = df.copy()
    target_date = pd.to_datetime(out_df[args.date_col], errors="coerce").dt.normalize()
    known_date = target_date - pd.Timedelta(days=max(int(horizon_days), 1))

    weekday_cols = ["avail_mon", "avail_tue", "avail_wed", "avail_thu", "avail_fri", "avail_sat", "avail_sun"]
    if all(c in out_df.columns for c in weekday_cols):
        weekday_flags = out_df[weekday_cols].apply(pd.to_numeric, errors="coerce").fillna(1).astype(int)
        weekday_idx = target_date.dt.weekday.fillna(0).astype(int).to_numpy()
        out_df["available_by_weekday"] = weekday_flags.to_numpy()[np.arange(len(out_df)), weekday_idx].astype(int)
        out_df["weekday_available_cnt"] = weekday_flags.sum(axis=1).astype(int)
        pattern = np.zeros(len(out_df), dtype=int)
        for idx, col in enumerate(weekday_cols):
            pattern += (weekday_flags[col].to_numpy() > 0).astype(int) * (2 ** idx)
        out_df["availability_pattern_code"] = pattern
    else:
        out_df["available_by_weekday"] = 1
        out_df["weekday_available_cnt"] = 7
        out_df["availability_pattern_code"] = 127

    sold_start = parse_mixed_date_series(out_df["sold_start_time"]) if "sold_start_time" in out_df.columns else pd.Series(pd.NaT, index=out_df.index)
    sold_end_col = "sold_ent_time" if "sold_ent_time" in out_df.columns else "sold_end_time"
    sold_end = parse_mixed_date_series(out_df[sold_end_col]) if sold_end_col in out_df.columns else pd.Series(pd.NaT, index=out_df.index)
    use_start = parse_mixed_date_series(out_df["use_start_date"]) if "use_start_date" in out_df.columns else pd.Series(pd.NaT, index=out_df.index)
    use_end = parse_mixed_date_series(out_df["use_end_date"]) if "use_end_date" in out_df.columns else pd.Series(pd.NaT, index=out_df.index)

    out_df["sale_known_started"] = (sold_start.isna() | known_date.ge(sold_start)).astype(int)
    out_df["sale_known_not_ended"] = (sold_end.isna() | known_date.le(sold_end)).astype(int)
    out_df["sale_known_active"] = (out_df["sale_known_started"].eq(1) & out_df["sale_known_not_ended"].eq(1)).astype(int)
    out_df["sale_active_on_target"] = ((sold_start.isna() | target_date.ge(sold_start)) & (sold_end.isna() | target_date.le(sold_end))).astype(int)
    out_df["available_by_sold_date"] = out_df["sale_known_active"]
    out_df["available_by_use_date"] = ((use_start.isna() | target_date.ge(use_start)) & (use_end.isna() | target_date.le(use_end))).astype(int)
    if "is_sale" in out_df.columns:
        out_df["available_by_is_sale"] = pd.to_numeric(out_df["is_sale"], errors="coerce").fillna(out_df["sale_known_active"]).clip(0, 1).astype(int)
    else:
        out_df["available_by_is_sale"] = out_df["sale_known_active"]

    out_df["is_package_available_on_target"] = (
        out_df["available_by_weekday"].eq(1)
        & out_df["available_by_use_date"].eq(1)
        & out_df["available_by_sold_date"].eq(1)
        & out_df["available_by_is_sale"].eq(1)
    ).astype(int)
    reason_cols = ["available_by_weekday", "available_by_use_date", "available_by_sold_date", "available_by_is_sale"]
    out_df["not_available_reason_cnt"] = sum((1 - out_df[col].astype(int)) for col in reason_cols).astype(int)
    out_df["is_weekday_unavailable"] = (1 - out_df["available_by_weekday"].astype(int)).astype(int)
    out_df["is_before_use_window"] = (use_start.notna() & target_date.lt(use_start)).astype(int)
    out_df["is_after_use_window"] = (use_end.notna() & target_date.gt(use_end)).astype(int)
    out_df["is_sale_not_started_by_known_date"] = (sold_start.notna() & known_date.lt(sold_start)).astype(int)
    out_df["is_sale_ended_by_known_date"] = (sold_end.notna() & known_date.gt(sold_end)).astype(int)

    for col_name, end_date in [("sale_remaining_days_known", sold_end), ("use_remaining_days_target", use_end)]:
        out_df[col_name] = (end_date - known_date).dt.days.fillna(999).clip(lower=-999, upper=999).astype(int)
    out_df["days_to_use_start_target"] = (use_start - target_date).dt.days.fillna(999).clip(lower=-999, upper=999).astype(int)
    out_df["days_since_sold_start_known"] = (known_date - sold_start).dt.days.fillna(999).clip(lower=-999, upper=999).astype(int)
    logging.info("T+2 availability features added.")
    return out_df


def add_peak_features(df: pd.DataFrame, peak_path: str | None, date_col: str) -> pd.DataFrame:
    if not peak_path:
        return df

    peak_df = load_peak_calendar(peak_path)
    peak_feature_df = peak_df.copy().sort_values("ds").reset_index(drop=True)
    peak_feature_df["peak_date_attribute"] = peak_feature_df["date_attribute"].astype(int)
    peak_feature_df["is_high_peak_day"] = (peak_feature_df["peak_date_attribute"] == 1).astype(int)
    peak_feature_df["is_low_peak_day"] = (peak_feature_df["peak_date_attribute"] == 2).astype(int)

    for peak_name, flag_col in [("high", "is_high_peak_day"), ("low", "is_low_peak_day")]:
        peak_dates = peak_feature_df.loc[peak_feature_df[flag_col].eq(1), "ds"].sort_values().to_numpy()
        current_dates = peak_feature_df["ds"].to_numpy()
        next_idx = np.searchsorted(peak_dates, current_dates, side="left")
        prev_idx = np.searchsorted(peak_dates, current_dates, side="right") - 1

        next_days = np.full(len(peak_feature_df), 999, dtype=int)
        valid_next = next_idx < len(peak_dates)
        next_days[valid_next] = (
            peak_dates[next_idx[valid_next]] - current_dates[valid_next]
        ).astype("timedelta64[D]").astype(int)

        prev_days = np.full(len(peak_feature_df), 999, dtype=int)
        valid_prev = prev_idx >= 0
        prev_days[valid_prev] = (
            current_dates[valid_prev] - peak_dates[prev_idx[valid_prev]]
        ).astype("timedelta64[D]").astype(int)

        peak_feature_df[f"days_to_{peak_name}_peak"] = next_days
        peak_feature_df[f"days_after_{peak_name}_peak"] = prev_days
        for window in [1, 3, 7]:
            peak_feature_df[f"is_{peak_name}_peak_lead_{window}"] = (
                (next_days > 0) & (next_days <= window)
            ).astype(int)
            peak_feature_df[f"is_{peak_name}_peak_lag_{window}"] = (
                (prev_days > 0) & (prev_days <= window)
            ).astype(int)

    peak_feature_df = peak_feature_df.drop(columns=["date_attribute"])
    peak_feature_cols = [c for c in peak_feature_df.columns if c != "ds"]
    df = df.drop(columns=[c for c in peak_feature_cols if c in df.columns], errors="ignore")
    out_df = df.merge(peak_feature_df, left_on=date_col, right_on="ds", how="left", suffixes=("", "_peak_calendar"))
    if "ds_peak_calendar" in out_df.columns:
        out_df = out_df.drop(columns=["ds_peak_calendar"])

    for col in peak_feature_cols:
        out_df[col] = pd.to_numeric(out_df[col], errors="coerce").fillna(0 if col.startswith("is_") else 999)

    logging.info("Peak features added from %s: %s", peak_path, peak_feature_cols)
    return out_df


def add_holiday_position_features(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = ["is_holiday", "holiday_span_day_idx", "holiday_span_days"]
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Holiday position features require columns: {missing_cols}")

    out_df = df.copy()
    is_holiday = pd.to_numeric(out_df["is_holiday"], errors="coerce").fillna(0).eq(1)
    span_idx = pd.to_numeric(out_df["holiday_span_day_idx"], errors="coerce")
    span_days = pd.to_numeric(out_df["holiday_span_days"], errors="coerce")

    out_df["is_holiday_first_day"] = (is_holiday & span_idx.eq(1)).astype(int)
    out_df["is_holiday_last_day"] = (is_holiday & span_idx.eq(span_days)).astype(int)
    out_df["is_holiday_middle_day"] = (
        is_holiday & span_idx.gt(1) & span_days.gt(1) & span_idx.lt(span_days)
    ).astype(int)
    logging.info(
        "Holiday position features added: %s",
        ["is_holiday_first_day", "is_holiday_last_day", "is_holiday_middle_day"],
    )
    return out_df


def add_manual_public_holiday_features(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out_df = df.copy()
    target_date = pd.to_datetime(out_df[args.date_col], errors="coerce").dt.normalize()

    is_holiday_source = pd.to_numeric(out_df.get("is_holiday", 0), errors="coerce").fillna(0).astype(int)
    out_df["is_public_holiday_manual"] = is_holiday_source.eq(1).astype(int)
    out_df["holiday_name_code"] = pd.to_numeric(out_df.get("holiday_type", 0), errors="coerce").fillna(0).astype(int)
    out_df["holiday_day_idx"] = pd.to_numeric(out_df.get("holiday_span_day_idx", 0), errors="coerce").fillna(0).astype(int)
    out_df["manual_holiday_span_days"] = pd.to_numeric(out_df.get("holiday_span_days", 0), errors="coerce").fillna(0).astype(int)
    out_df["days_to_public_holiday"] = pd.to_numeric(out_df.get("days_to_holiday", 999), errors="coerce").fillna(999).astype(int)
    out_df["days_after_public_holiday"] = pd.to_numeric(out_df.get("days_after_holiday", 999), errors="coerce").fillna(999).astype(int)
    out_df["is_holiday_eve_1"] = out_df["days_to_public_holiday"].eq(1).astype(int)
    out_df["is_holiday_eve_2"] = out_df["days_to_public_holiday"].eq(2).astype(int)
    out_df["is_pre_public_holiday_3d"] = out_df["days_to_public_holiday"].between(1, 3).astype(int)
    out_df["is_pre_public_holiday_7d"] = out_df["days_to_public_holiday"].between(1, 7).astype(int)

    default_weekend = target_date.dt.weekday.ge(5).astype(int)
    is_weekend_source = out_df["is_weekend"] if "is_weekend" in out_df.columns else default_weekend
    is_makeup_source = out_df["is_makeup_workday"] if "is_makeup_workday" in out_df.columns else pd.Series(0, index=out_df.index)
    is_working_source = out_df["is_working_day"] if "is_working_day" in out_df.columns else 1 - pd.to_numeric(is_weekend_source, errors="coerce").fillna(0).astype(int)
    is_weekend = pd.to_numeric(is_weekend_source, errors="coerce").fillna(0).astype(int)
    is_makeup = pd.to_numeric(is_makeup_source, errors="coerce").fillna(0).astype(int)
    is_working = pd.to_numeric(is_working_source, errors="coerce").fillna(0).astype(int)
    out_df["is_makeup_workday_weekend"] = (is_weekend.eq(1) & is_makeup.eq(1)).astype(int)
    out_df["is_weekend_before_public_holiday_7d"] = (is_weekend.eq(1) & out_df["is_pre_public_holiday_7d"].eq(1)).astype(int)
    out_df["is_workday_before_public_holiday_3d"] = (is_working.eq(1) & out_df["is_pre_public_holiday_3d"].eq(1)).astype(int)
    logging.info("Source-derived public holiday features added.")
    return out_df


def add_peak_history_mean_features(
    df: pd.DataFrame,
    args: argparse.Namespace,
    horizon_days: int = 2,
) -> pd.DataFrame:
    required_cols = ["is_high_peak_day", "is_low_peak_day", args.package_label_col, args.date_col]
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Peak history mean features require columns: {missing_cols}. Provide --peak_path.")

    group_cols = [c for c in args.package_id_cols if c in df.columns]
    if not group_cols:
        raise ValueError("Peak history mean features require package id columns.")

    out_df = df.copy().sort_values(group_cols + [args.date_col]).reset_index(drop=True)
    windows = sorted({int(w) for w in args.peak_mean_windows if int(w) > 0})
    safe_horizon = max(int(horizon_days), 1)

    for peak_name, flag_col in [("high", "is_high_peak_day"), ("low", "is_low_peak_day")]:
        source_col = f"__{peak_name}_peak_label_for_mean"
        shifted_col = f"__{peak_name}_peak_shifted_label"
        out_df[source_col] = np.where(
            pd.to_numeric(out_df[flag_col], errors="coerce").fillna(0).eq(1),
            pd.to_numeric(out_df[args.package_label_col], errors="coerce").fillna(0.0),
            np.nan,
        )
        out_df[shifted_col] = out_df.groupby(group_cols, sort=False)[source_col].shift(safe_horizon)

        for window in windows:
            feature_col = f"{args.package_label_col}_{peak_name}_peak_mean_{window}"

            def _last_peak_mean(series: pd.Series, w: int = window) -> pd.Series:
                values: list[float] = []
                result: list[float] = []
                for value in series:
                    if pd.notna(value):
                        values.append(float(value))
                    if values:
                        result.append(float(np.mean(values[-w:])))
                    else:
                        result.append(0.0)
                return pd.Series(result, index=series.index)

            out_df[feature_col] = out_df.groupby(group_cols, sort=False)[shifted_col].transform(_last_peak_mean)

        out_df = out_df.drop(columns=[source_col, shifted_col])

    added_cols = [
        f"{args.package_label_col}_{peak_name}_peak_mean_{window}"
        for peak_name in ["high", "low"]
        for window in windows
    ]
    logging.info("Peak history mean features added: %s", added_cols)
    return out_df


def add_package_activity_features(
    df: pd.DataFrame,
    args: argparse.Namespace,
    horizon_days: int = 2,
) -> pd.DataFrame:
    group_cols = [c for c in args.package_id_cols if c in df.columns]
    if not group_cols or "package_dish_code" not in df.columns:
        return df

    out_df = df.copy()
    out_df[args.date_col] = pd.to_datetime(out_df[args.date_col], errors="coerce").dt.normalize()
    out_df[args.package_label_col] = pd.to_numeric(out_df[args.package_label_col], errors="coerce").fillna(0.0)
    safe_gap = max(int(horizon_days), 1)
    package_col = "package_dish_code"

    package_daily_df = out_df.groupby([package_col, args.date_col], as_index=False).agg(
        package_global_pos=(args.package_label_col, "sum"),
        package_global_store_cnt=("store_code", "nunique") if "store_code" in out_df.columns else (args.package_label_col, "size"),
    ).sort_values([package_col, args.date_col])
    grouped_package = package_daily_df.groupby(package_col, sort=False)
    package_daily_df["package_global_pos_lag2_sum_7d"] = grouped_package["package_global_pos"].transform(
        lambda s: s.shift(safe_gap).rolling(7, min_periods=1).sum()
    )
    package_daily_df["package_global_pos_lag2_sum_14d"] = grouped_package["package_global_pos"].transform(
        lambda s: s.shift(safe_gap).rolling(14, min_periods=1).sum()
    )
    package_daily_df["package_global_store_cnt_lag2_7d"] = grouped_package["package_global_store_cnt"].transform(
        lambda s: s.shift(safe_gap).rolling(7, min_periods=1).mean()
    )
    heat_cols = ["package_global_pos_lag2_sum_7d", "package_global_pos_lag2_sum_14d", "package_global_store_cnt_lag2_7d"]
    out_df = out_df.merge(package_daily_df[[package_col, args.date_col] + heat_cols], on=[package_col, args.date_col], how="left")

    store_group_cols = [c for c in ["store_code", package_col] if c in out_df.columns]
    if store_group_cols:
        out_df = out_df.sort_values(store_group_cols + [args.date_col]).reset_index(drop=True)
        store_grouped = out_df.groupby(store_group_cols, sort=False)[args.package_label_col]
        out_df["store_package_pos_lag2_sum_7d"] = store_grouped.transform(
            lambda s: s.shift(safe_gap).rolling(7, min_periods=1).sum()
        )
        out_df["store_package_active_days_lag2_7d"] = store_grouped.transform(
            lambda s: s.shift(safe_gap).gt(0).rolling(7, min_periods=1).sum()
        )

    if "area_city_id" in out_df.columns:
        city_daily_df = out_df.groupby(["area_city_id", package_col, args.date_col], as_index=False)[args.package_label_col].sum()
        city_daily_df = city_daily_df.sort_values(["area_city_id", package_col, args.date_col])
        city_daily_df["city_package_pos_lag2_sum_7d"] = city_daily_df.groupby(["area_city_id", package_col], sort=False)[args.package_label_col].transform(
            lambda s: s.shift(safe_gap).rolling(7, min_periods=1).sum()
        )
        out_df = out_df.merge(
            city_daily_df[["area_city_id", package_col, args.date_col, "city_package_pos_lag2_sum_7d"]],
            on=["area_city_id", package_col, args.date_col],
            how="left",
        )

    if "package_dish_name" in out_df.columns:
        package_name = out_df["package_dish_name"].astype(str)
        out_df["is_ip_collab_package"] = package_name.str.contains("联名|×|x|X|第五人格|小马宝莉|三角洲", regex=True, na=False).astype(int)
    else:
        out_df["is_ip_collab_package"] = 0

    if "package_global_pos_lag2_sum_14d" in out_df.columns:
        out_df["package_global_heat_rank_pct_14d"] = out_df.groupby(args.date_col)["package_global_pos_lag2_sum_14d"].rank(pct=True).fillna(0.0)
        out_df["is_top_package_heat_14d"] = out_df["package_global_heat_rank_pct_14d"].ge(0.80).astype(int)
    else:
        out_df["package_global_heat_rank_pct_14d"] = 0.0
        out_df["is_top_package_heat_14d"] = 0

    fill_cols = [
        "package_global_pos_lag2_sum_7d",
        "package_global_pos_lag2_sum_14d",
        "package_global_store_cnt_lag2_7d",
        "store_package_pos_lag2_sum_7d",
        "store_package_active_days_lag2_7d",
        "city_package_pos_lag2_sum_7d",
    ]
    for col in fill_cols:
        if col in out_df.columns:
            out_df[col] = pd.to_numeric(out_df[col], errors="coerce").fillna(0.0)
    logging.info("Package activity features added.")
    return out_df


def add_enhanced_lifecycle_features(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out_df = df.copy()
    if "package_age_days" in out_df.columns:
        age = pd.to_numeric(out_df["package_age_days"], errors="coerce").fillna(0)
        out_df["is_new_package_14d"] = age.le(14).astype(int)
        out_df["is_new_package_30d"] = age.le(30).astype(int)
        out_df["package_age_bucket"] = pd.cut(
            age,
            bins=[-1, 7, 14, 30, 999999],
            labels=[0, 1, 2, 3],
        ).astype(int)
        if "is_ip_collab_package" in out_df.columns:
            out_df["is_new_ip_collab_package_30d"] = (out_df["is_ip_collab_package"].eq(1) & age.le(30)).astype(int)
    if "use_end_date" in out_df.columns:
        use_end = parse_mixed_date_series(out_df["use_end_date"])
        target_date = pd.to_datetime(out_df[args.date_col], errors="coerce").dt.normalize()
        out_df["days_to_package_use_end"] = (use_end - target_date).dt.days.fillna(999).clip(lower=-999, upper=999).astype(int)
    return out_df

def get_odps_client():
    from odps import ODPS

    return ODPS()


def fetch(sql):
    o = get_odps_client()
    data = []
    with o.execute_sql(sql).open_reader(tunnel=True, limit=False) as reader:
        data = reader.to_pandas()
    get_df = pd.DataFrame(data)
    return get_df

def load_future_weather_data(path,bizdate) -> pd.DataFrame:
    # df = pd.read_csv(path)
    if bizdate is  None:
        bizdate = '20260301'
    sql1 = f"""select
        store_code,
        ds,
        case
            when day_weather = '雾' then '18'
            when day_weather = '大雨' then '09'
            when day_weather = '中雨' then '08'
            when day_weather = '大暴雨' then '11'
            when day_weather = '阵雨' then '03'
            when day_weather = '多云' then '01'
            when day_weather = '晴' then '00'
            when day_weather = '小雨' then '07'
            when day_weather = '阴' then '02'
            when day_weather = '暴雨' then '10'
            when day_weather = '雷阵雨' then '04'
            else day_weather_code
        end as day_weather_code,
        day_air_temperature,
        case
            when night_weather = '雾' then '18'
            when night_weather = '大雨' then '09'
            when night_weather = '中雨' then '08'
            when night_weather = '大暴雨' then '11'
            when night_weather = '阵雨' then '03'
            when night_weather = '多云' then '01'
            when night_weather = '晴' then '00'
            when night_weather = '小雨' then '07'
            when night_weather = '阴' then '02'
            when night_weather = '暴雨' then '10'
            when night_weather = '雷阵雨' then '04'
            else night_weather_code
        end as night_weather_code,
        night_air_temperature
    from alg_prd.ods_future_weather_week_di
    where ds >= {bizdate}"""
    df = fetch(sql1)
    print(sql1)
    df["store_code"] = pd.to_numeric(df["store_code"], errors="coerce")
    df["ds"] = pd.to_datetime(pd.to_numeric(df["ds"], errors="coerce").astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    for col in ["day_air_temperature", "night_air_temperature"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_weather_fallbacks(raw_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    weather_cols = [
        "day_weather_code",
        "day_air_temperature",
        "night_weather_code",
        "night_air_temperature",
    ]
    hist_weather = raw_df[["store_code"] + weather_cols].copy()
    hist_weather["day_weather_code"] = hist_weather["day_weather_code"].astype(str)
    hist_weather["night_weather_code"] = hist_weather["night_weather_code"].astype(str)

    def _mode(s: pd.Series) -> str:
        mode = s.mode(dropna=True)
        return str(mode.iloc[0]) if not mode.empty else "NA"

    store_weather = hist_weather.groupby("store_code", as_index=False).agg(
        store_day_weather_code=("day_weather_code", _mode),
        store_day_air_temperature=("day_air_temperature", "median"),
        store_night_weather_code=("night_weather_code", _mode),
        store_night_air_temperature=("night_air_temperature", "median"),
    )
    global_weather = {
        "day_weather_code": _mode(hist_weather["day_weather_code"]),
        "day_air_temperature": float(hist_weather["day_air_temperature"].median()),
        "night_weather_code": _mode(hist_weather["night_weather_code"]),
        "night_air_temperature": float(hist_weather["night_air_temperature"].median()),
    }
    return store_weather, global_weather


def build_future_dates(max_hist_date: pd.Timestamp, forecast_days: int) -> list[pd.Timestamp]:
    return [max_hist_date + pd.Timedelta(days=i) for i in range(1, forecast_days + 1)]


def build_online_anchor_frames(
    raw_df: pd.DataFrame,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    max_hist_date = raw_df[args.date_col].max()
    last_day_rows = raw_df[raw_df[args.date_col] == max_hist_date].copy()
    if last_day_rows.empty:
        raise ValueError("No rows found on max historical ds for future anchor generation.")

    package_input_df = add_package_lifecycle_features(
        build_package_frame(raw_df, args),
        args.package_id_cols,
        args.date_col,
    )
    package_anchor_df = package_input_df[package_input_df[args.date_col] == max_hist_date].copy()
    package_anchor_df = deduplicate_to_package_level(
        package_anchor_df,
        args.package_id_cols,
        args.date_col,
        args.package_label_col,
    )
    dish_anchor_df = last_day_rows.sort_values(ROW_ID_COLS).drop_duplicates(ROW_ID_COLS, keep="first").reset_index(drop=True)
    return package_anchor_df, dish_anchor_df, max_hist_date


def apply_future_exogenous(
    package_anchor_df: pd.DataFrame,
    future_date: pd.Timestamp,
    holiday_df: pd.DataFrame,
    future_weather_df: pd.DataFrame,
    store_weather_fallback_df: pd.DataFrame,
    global_weather_fallback: dict[str, object],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, dict[str, object]]:
    future_df = package_anchor_df.copy()
    anchor_date = pd.to_datetime(future_df[args.date_col], errors="coerce")
    future_df[args.date_col] = future_date
    if "package_age_days" in future_df.columns:
        delta_days = (future_date - anchor_date).dt.days.fillna(0).astype(int)
        future_df["package_age_days"] = (pd.to_numeric(future_df["package_age_days"], errors="coerce").fillna(0) + delta_days).clip(lower=0)
        future_df["package_seen_days"] = future_df["package_age_days"] + 1
        future_df["is_new_package_1d"] = (future_df["package_age_days"] <= 1).astype(int)
        future_df["is_new_package_3d"] = (future_df["package_age_days"] <= 3).astype(int)
        future_df["is_new_package_7d"] = (future_df["package_age_days"] <= 7).astype(int)

    holiday_row = holiday_df[holiday_df["ds"] == future_date].copy()
    if holiday_row.empty:
        raise ValueError(f"Holiday info missing for future date {future_date.date()}")
    holiday_row = holiday_row.iloc[0].to_dict()
    holiday_cols = [c for c in holiday_df.columns if c != "ds"]
    for col in holiday_cols:
        future_df[col] = holiday_row[col]

    future_df["day_of_week"] = future_date.weekday() + 1
    future_df["is_weekend"] = int(future_date.weekday() >= 5)

    weather_slice = future_weather_df[future_weather_df["ds"] == future_date].copy()
    before_missing_weather = len(future_df)
    future_df = future_df.merge(
        weather_slice[
            ["store_code", "day_weather_code", "day_air_temperature", "night_weather_code", "night_air_temperature"]
        ],
        on="store_code",
        how="left",
        suffixes=("", "_future"),
    )

    if "day_weather_code_future" in future_df.columns:
        future_df["day_weather_code"] = future_df["day_weather_code_future"]
        future_df["day_air_temperature"] = future_df["day_air_temperature_future"]
        future_df["night_weather_code"] = future_df["night_weather_code_future"]
        future_df["night_air_temperature"] = future_df["night_air_temperature_future"]
        future_df = future_df.drop(
            columns=[
                "day_weather_code_future",
                "day_air_temperature_future",
                "night_weather_code_future",
                "night_air_temperature_future",
            ]
        )

    future_df = future_df.merge(store_weather_fallback_df, on="store_code", how="left")
    future_df["day_weather_code"] = future_df["day_weather_code"].fillna(future_df["store_day_weather_code"])
    future_df["day_air_temperature"] = future_df["day_air_temperature"].fillna(future_df["store_day_air_temperature"])
    future_df["night_weather_code"] = future_df["night_weather_code"].fillna(future_df["store_night_weather_code"])
    future_df["night_air_temperature"] = future_df["night_air_temperature"].fillna(future_df["store_night_air_temperature"])
    future_df["day_weather_code"] = future_df["day_weather_code"].fillna(global_weather_fallback["day_weather_code"])
    future_df["day_air_temperature"] = future_df["day_air_temperature"].fillna(global_weather_fallback["day_air_temperature"])
    future_df["night_weather_code"] = future_df["night_weather_code"].fillna(global_weather_fallback["night_weather_code"])
    future_df["night_air_temperature"] = future_df["night_air_temperature"].fillna(global_weather_fallback["night_air_temperature"])
    future_df = future_df.drop(
        columns=[
            "store_day_weather_code",
            "store_day_air_temperature",
            "store_night_weather_code",
            "store_night_air_temperature",
        ]
    )

    sold_end_dt = pd.to_datetime(future_df.get("sold_ent_time"), errors="coerce")
    if "is_sale" in future_df.columns:
        future_df["is_sale"] = np.where(sold_end_dt.notna() & (future_date <= sold_end_dt.dt.normalize()), 1, 0)

    missing_weather_after_fill = int(
        future_df[["day_weather_code", "day_air_temperature", "night_weather_code", "night_air_temperature"]].isna().any(axis=1).sum()
    )
    diagnostics = {
        "ds": future_date.strftime("%Y-%m-%d"),
        "future_weather_rows": int(len(weather_slice)),
        "anchor_rows": int(before_missing_weather),
        "missing_weather_after_fill": missing_weather_after_fill,
        "is_sale_zero_rows": int((future_df.get("is_sale", 1) == 0).sum()) if "is_sale" in future_df.columns else 0,
    }
    return future_df, diagnostics


def validate_future_features(future_pkg_df: pd.DataFrame, args: argparse.Namespace) -> None:
    required_cols = [
        "store_code",
        "package_dish_code",
        args.date_col,
        "day_weather_code",
        "day_air_temperature",
        "night_weather_code",
        "night_air_temperature",
        "use_start_date",
        "use_end_date",
    ]
    missing_cols = [c for c in required_cols if c not in future_pkg_df.columns]
    if missing_cols:
        raise ValueError(f"Missing required future feature columns: {missing_cols}")

    critical_na = future_pkg_df[required_cols].isna().sum()
    bad_cols = critical_na[critical_na > 0]
    if not bad_cols.empty:
        raise ValueError(f"Future feature frame has NA in critical columns: {bad_cols.to_dict()}")


def train_backtest_and_refit_model(
    package_history_raw_df: pd.DataFrame,
    raw_df: pd.DataFrame,
    args: argparse.Namespace,
    output_dir: Path,
) -> tuple[object, pd.DataFrame, list[str], list[str], dict[str, dict[str, int]]]:
    package_history_feat_df = add_package_lifecycle_features(
        package_history_raw_df.copy(),
        args.package_id_cols,
        args.date_col,
    )
    df_feat, feature_cols, cat_cols, category_maps = build_features(
        df=package_history_feat_df,
        label_col=args.package_label_col,
        date_col=args.date_col,
        id_cols=args.package_id_cols,
        lag_days=args.lag_days,
        rolling_windows=args.rolling_windows,
        return_category_maps=True,
    )

    train_df, valid_df, test_df = split_data(df_feat, args.date_col, args.valid_days, args.test_days)
    raw_train_df, raw_valid_df, raw_test_df = split_data(package_history_raw_df, args.date_col, args.valid_days, args.test_days)
    valid_start = valid_df[args.date_col].min()
    test_start = test_df[args.date_col].min()

    X_train, y_train = train_df[feature_cols], train_df[args.package_label_col]
    X_valid, y_valid = valid_df[feature_cols], valid_df[args.package_label_col]
    X_all, y_all = df_feat[feature_cols], df_feat[args.package_label_col]

    train_sample_weight = build_train_sample_weights(
        train_df,
        args.date_col,
        valid_start,
        args.train_weight_strategy,
        args.train_weight_half_life_days,
    )
    model = train_package_model(X_train, y_train, X_valid, y_valid, cat_cols, args, sample_weight=train_sample_weight)
    best_iter = model.best_iteration_ if model.best_iteration_ else args.n_estimators

    valid_pred = np.clip(model.predict(X_valid, num_iteration=best_iter), a_min=0, a_max=None)
    valid_pred, valid_guardrail_stats = apply_recent_trend_guardrail(valid_pred, valid_df, args.package_label_col, args)
    package_valid_metrics = evaluate(y_valid.values, valid_pred, "package_valid")

    valid_rows = raw_df[(raw_df[args.date_col] >= valid_start) & (raw_df[args.date_col] < test_start)].copy()
    train_rows = raw_df[raw_df[args.date_col] < valid_start].copy()
    valid_ratio_bundle = build_ratio_bundle(train_rows, valid_start, args)
    valid_package_pred_df = raw_valid_df[[c for c in args.package_id_cols if c in raw_valid_df.columns] + [args.date_col]].copy()
    valid_package_pred_df[f"pred_{args.package_label_col}"] = valid_pred
    valid_alloc_df = allocate_dish_prediction(valid_rows, valid_package_pred_df, valid_ratio_bundle, args)
    dish_valid_metrics = evaluate(
        valid_alloc_df[args.dish_label_col].values,
        valid_alloc_df[f"pred_{args.dish_label_col}"].values,
        "dish_valid",
    )

    all_sample_weight = build_train_sample_weights(
        df_feat,
        args.date_col,
        df_feat[args.date_col].max() + pd.Timedelta(days=1),
        args.train_weight_strategy,
        args.train_weight_half_life_days,
    )
    final_model = fit_fixed_iter_model(
        X_all,
        y_all,
        cat_cols,
        model,
        best_iter,
        sample_weight=all_sample_weight,
    )

    backtest_pred = np.clip(model.predict(test_df[feature_cols], num_iteration=best_iter), a_min=0, a_max=None)
    backtest_pred, _ = apply_recent_trend_guardrail(backtest_pred, test_df, args.package_label_col, args)

    test_leak_pred = np.clip(final_model.predict(test_df[feature_cols]), a_min=0, a_max=None)
    test_leak_pred, _ = apply_recent_trend_guardrail(
        test_leak_pred,
        test_df,
        args.package_label_col,
        args,
    )
    package_test_refit_all_metrics = evaluate(
        test_df[args.package_label_col].values,
        test_leak_pred,
        "package_test_refit_all",
    )
    test_rows = raw_df[raw_df[args.date_col] >= test_start].copy()
    full_ratio_bundle = build_ratio_bundle(raw_df, raw_df[args.date_col].max() + pd.Timedelta(days=1), args)
    test_package_pred_df = raw_test_df[[c for c in args.package_id_cols if c in raw_test_df.columns] + [args.date_col]].copy()
    test_package_pred_df[f"pred_{args.package_label_col}"] = test_leak_pred
    test_alloc_refit_all_df = allocate_dish_prediction(test_rows, test_package_pred_df, full_ratio_bundle, args)
    dish_test_refit_all_metrics = evaluate(
        test_alloc_refit_all_df[args.dish_label_col].values,
        test_alloc_refit_all_df[f"pred_{args.dish_label_col}"].values,
        "dish_test_refit_all",
    )
    logging.info(
        "Backtest summary | best_iter=%s | valid_window=%s~%s | test_window=%s~%s",
        int(best_iter),
        valid_df[args.date_col].min().strftime("%Y-%m-%d"),
        valid_df[args.date_col].max().strftime("%Y-%m-%d"),
        test_df[args.date_col].min().strftime("%Y-%m-%d"),
        test_df[args.date_col].max().strftime("%Y-%m-%d"),
    )
    logging.info(
        "Valid integrated bias | package=%.4f | dish=%.4f",
        float(package_valid_metrics["package_valid_bias_rate"]),
        float(dish_valid_metrics["dish_valid_bias_rate"]),
    )
    logging.info(
        "Refit-all test integrated bias | package=%.4f | dish=%.4f",
        float(package_test_refit_all_metrics["package_test_refit_all_bias_rate"]),
        float(dish_test_refit_all_metrics["dish_test_refit_all_bias_rate"]),
    )
    return final_model, package_history_feat_df, feature_cols, cat_cols, category_maps


def package_bias_rate(series_df: pd.DataFrame, true_col: str, pred_col: str) -> float:
    true_sum = float(series_df[true_col].abs().sum())
    if true_sum <= 0:
        return 0.0
    return float((series_df[pred_col] - series_df[true_col]).abs().sum() / true_sum)


def build_calibration_context(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    context_df = pd.DataFrame(index=df.index)
    if "day_of_week" in df.columns:
        context_df["day_of_week"] = pd.to_numeric(df["day_of_week"], errors="coerce").fillna(0).astype(int)
    else:
        context_df["day_of_week"] = pd.to_datetime(df[args.date_col], errors="coerce").dt.weekday.fillna(-1).astype(int) + 1
    context_df["peak_date_attribute"] = pd.to_numeric(df.get("peak_date_attribute", 0), errors="coerce").fillna(0).astype(int)
    age = pd.to_numeric(df.get("package_age_days", 0), errors="coerce").fillna(0)
    context_df["package_age_bucket"] = pd.cut(age, bins=[-1, 7, 14, 30, 999999], labels=[0, 1, 2, 3]).astype(int)
    context_df["is_top_package_heat_14d"] = pd.to_numeric(df.get("is_top_package_heat_14d", 0), errors="coerce").fillna(0).astype(int)
    return context_df


def fit_group_calibrator(
    valid_context_df: pd.DataFrame,
    y_true: pd.Series,
    y_pred: np.ndarray,
    args: argparse.Namespace,
) -> dict[str, object]:
    context_df = build_calibration_context(valid_context_df, args)
    fit_df = context_df.copy()
    fit_df["true_value"] = pd.to_numeric(pd.Series(y_true).reset_index(drop=True), errors="coerce").fillna(0.0).to_numpy()
    fit_df["pred_value"] = np.clip(np.asarray(y_pred, dtype=float), a_min=0, a_max=None)
    group_specs = [
        ["day_of_week", "peak_date_attribute", "package_age_bucket", "is_top_package_heat_14d"],
        ["day_of_week", "peak_date_attribute", "is_top_package_heat_14d"],
        ["day_of_week", "peak_date_attribute"],
        ["day_of_week"],
        [],
    ]

    tables = []
    for cols in group_specs:
        if cols:
            agg_df = fit_df.groupby(cols, as_index=False).agg(
                true_sum=("true_value", "sum"),
                pred_sum=("pred_value", "sum"),
                row_cnt=("true_value", "size"),
            )
        else:
            agg_df = pd.DataFrame([{
                "true_sum": float(fit_df["true_value"].sum()),
                "pred_sum": float(fit_df["pred_value"].sum()),
                "row_cnt": int(len(fit_df)),
            }])
        usable = (
            agg_df["row_cnt"].ge(int(args.calibration_min_rows))
            & agg_df["true_sum"].ge(float(args.calibration_min_true_sum))
            & agg_df["pred_sum"].ge(float(args.calibration_min_pred_sum))
        )
        agg_df = agg_df[usable].copy()
        if agg_df.empty:
            continue
        agg_df["factor"] = (agg_df["true_sum"] / agg_df["pred_sum"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        agg_df["factor"] = agg_df["factor"].fillna(1.0).clip(float(args.calibration_clip_lower), float(args.calibration_clip_upper))
        tables.append({"cols": cols, "table": agg_df[cols + ["factor"]] if cols else agg_df[["factor"]]})

    if not tables:
        tables.append({"cols": [], "table": pd.DataFrame([{"factor": 1.0}])})
    return {"tables": tables}


def apply_group_calibration(
    pred: np.ndarray,
    context_df: pd.DataFrame,
    calibrator: dict[str, object],
    args: argparse.Namespace,
) -> tuple[np.ndarray, pd.DataFrame]:
    pred_arr = np.clip(np.asarray(pred, dtype=float), a_min=0, a_max=None)
    apply_context_df = build_calibration_context(context_df, args).reset_index(drop=True).copy()
    apply_context_df["_row_id"] = np.arange(len(apply_context_df))
    factors = pd.Series(np.nan, index=apply_context_df.index, dtype=float)

    for item in calibrator.get("tables", []):
        cols = item["cols"]
        table = item["table"]
        if cols:
            merged = apply_context_df[["_row_id"] + cols].merge(table, on=cols, how="left", sort=False)
            factor_values = merged.sort_values("_row_id")["factor"].reset_index(drop=True)
        else:
            factor_values = pd.Series(float(table["factor"].iloc[0]), index=apply_context_df.index)
        mask = factors.isna() & factor_values.notna()
        factors.loc[mask] = factor_values.loc[mask].astype(float)

    factors = factors.fillna(1.0).clip(float(args.calibration_clip_lower), float(args.calibration_clip_upper))
    adjusted_pred = np.clip(pred_arr * factors.to_numpy(), a_min=0, a_max=None)
    stats_df = pd.DataFrame([{
        "calibrated_rows": int(len(pred_arr)),
        "mean_factor": float(factors.mean()) if len(factors) else 1.0,
        "min_factor": float(factors.min()) if len(factors) else 1.0,
        "max_factor": float(factors.max()) if len(factors) else 1.0,
        "pred_sum_before": float(pred_arr.sum()),
        "pred_sum_after": float(adjusted_pred.sum()),
    }])
    return adjusted_pred, stats_df


def build_package_prediction_output(
    split_name: str,
    raw_split_df: pd.DataFrame,
    feat_split_df: pd.DataFrame,
    pred: np.ndarray,
    args: argparse.Namespace,
) -> pd.DataFrame:
    if len(raw_split_df) != len(pred):
        raise ValueError(
            f"Prediction output length mismatch for {split_name}: raw={len(raw_split_df)}, pred={len(pred)}"
        )
    out_df = raw_split_df.copy().reset_index(drop=True)
    out_df[f"pred_{args.package_label_col}"] = np.asarray(pred, dtype=float)
    out_df["split"] = split_name
    out_df = out_df.rename(columns={args.package_label_col: f"true_{args.package_label_col}"})
    out_df[f"error_{args.package_label_col}"] = out_df[f"pred_{args.package_label_col}"] - out_df[f"true_{args.package_label_col}"]
    out_df[f"abs_error_{args.package_label_col}"] = out_df[f"error_{args.package_label_col}"].abs()

    keep_cols = [
        "split",
        args.date_col,
        "store_code",
        "store_name",
        "package_dish_code",
        "package_dish_name",
        "combo_for_psnnum",
        f"true_{args.package_label_col}",
        f"pred_{args.package_label_col}",
        f"error_{args.package_label_col}",
        f"abs_error_{args.package_label_col}",
        "peak_date_attribute",
        "is_high_peak_day",
        "is_low_peak_day",
        "is_holiday",
        "is_holiday_first_day",
        "is_holiday_last_day",
        "is_holiday_middle_day",
    ]
    keep_cols = [c for c in keep_cols if c in out_df.columns]
    return out_df[keep_cols].copy()


def build_daily_package_output(detail_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    true_col = f"true_{args.package_label_col}"
    pred_col = f"pred_{args.package_label_col}"
    daily_df = detail_df.groupby(["split", args.date_col], as_index=False).agg(
        true_pos_cnt_sum=(true_col, "sum"),
        pred_pos_cnt_sum=(pred_col, "sum"),
        package_cnt=("package_dish_code", "nunique"),
        row_cnt=("package_dish_code", "size"),
    )
    daily_df["abs_error"] = (daily_df["pred_pos_cnt_sum"] - daily_df["true_pos_cnt_sum"]).abs()
    daily_df["bias_rate"] = np.where(
        daily_df["true_pos_cnt_sum"].abs() > 0,
        daily_df["abs_error"] / daily_df["true_pos_cnt_sum"].abs(),
        0.0,
    )
    return daily_df


def build_package_day_output(detail_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    true_col = f"true_{args.package_label_col}"
    pred_col = f"pred_{args.package_label_col}"
    group_cols = [
        "split",
        args.date_col,
        "package_dish_code",
        "package_dish_name",
        "combo_for_psnnum",
    ]
    group_cols = [c for c in group_cols if c in detail_df.columns]
    package_day_df = detail_df.groupby(group_cols, as_index=False).agg(
        true_pos_cnt_sum=(true_col, "sum"),
        pred_pos_cnt_sum=(pred_col, "sum"),
        store_cnt=("store_code", "nunique"),
        row_cnt=("package_dish_code", "size"),
    )
    package_day_df["error"] = package_day_df["pred_pos_cnt_sum"] - package_day_df["true_pos_cnt_sum"]
    package_day_df["abs_error"] = package_day_df["error"].abs()
    package_day_df["bias_rate"] = np.where(
        package_day_df["true_pos_cnt_sum"].abs() > 0,
        package_day_df["abs_error"] / package_day_df["true_pos_cnt_sum"].abs(),
        np.nan,
    )
    split_order = {"train": 0, "valid": 1, "test": 2}
    package_day_df["_split_order"] = package_day_df["split"].map(split_order).fillna(99)
    package_day_df = package_day_df.sort_values(
        ["_split_order", args.date_col, "abs_error"],
        ascending=[True, True, False],
    )
    return package_day_df.drop(columns=["_split_order"])


def build_package_aggregate_outputs(detail_df: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    daily_df = build_daily_package_output(detail_df, args)
    package_day_df = build_package_day_output(detail_df, args)

    metrics = {}
    for split_name in ["valid", "test"]:
        daily_split_df = daily_df[daily_df["split"].eq(split_name)]
        package_day_split_df = package_day_df[package_day_df["split"].eq(split_name)]
        metrics[f"package_daily_{split_name}_bias"] = package_bias_rate(
            daily_split_df,
            "true_pos_cnt_sum",
            "pred_pos_cnt_sum",
        )
        metrics[f"package_day_{split_name}_bias"] = package_bias_rate(
            package_day_split_df,
            "true_pos_cnt_sum",
            "pred_pos_cnt_sum",
        )
    return daily_df, package_day_df, metrics


def build_dish_prediction_output(
    split_name: str,
    alloc_df: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    out_df = alloc_df.copy().reset_index(drop=True)
    out_df["split"] = split_name

    true_package_col = f"true_{args.package_label_col}"
    true_dish_col = f"true_{args.dish_label_col}"
    pred_dish_col = f"pred_{args.dish_label_col}"
    error_dish_col = f"error_{args.dish_label_col}"
    abs_error_dish_col = f"abs_error_{args.dish_label_col}"

    rename_cols = {}
    if args.package_label_col in out_df.columns:
        rename_cols[args.package_label_col] = true_package_col
    if args.dish_label_col in out_df.columns:
        rename_cols[args.dish_label_col] = true_dish_col
    out_df = out_df.rename(columns=rename_cols)

    out_df[error_dish_col] = out_df[pred_dish_col] - out_df[true_dish_col]
    out_df[abs_error_dish_col] = out_df[error_dish_col].abs()

    keep_cols = [
        "split",
        args.date_col,
        "store_code",
        "store_name",
        "package_dish_code",
        "package_dish_name",
        "combo_for_psnnum",
        "dish_unicode",
        "dish_code",
        "dish_name",
        "dish_dish_name",
        "dish_sku_name",
        true_package_col,
        f"pred_{args.package_label_col}",
        true_dish_col,
        pred_dish_col,
        error_dish_col,
        abs_error_dish_col,
        "dish_ratio",
        "ratio_source",
        "peak_date_attribute",
        "is_high_peak_day",
        "is_low_peak_day",
        "is_holiday",
        "is_holiday_first_day",
        "is_holiday_last_day",
        "is_holiday_middle_day",
    ]
    keep_cols = [c for c in keep_cols if c in out_df.columns]
    return out_df[keep_cols].copy()


def build_dish_day_output(detail_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    true_col = f"true_{args.dish_label_col}"
    pred_col = f"pred_{args.dish_label_col}"
    group_cols = [
        "split",
        args.date_col,
        "dish_unicode",
        "dish_code",
        "dish_name",
        "dish_dish_name",
        "dish_sku_name",
    ]
    group_cols = [c for c in group_cols if c in detail_df.columns]
    dish_day_df = detail_df.groupby(group_cols, as_index=False, dropna=False).agg(
        true_real_qty_sum=(true_col, "sum"),
        pred_real_qty_sum=(pred_col, "sum"),
        store_cnt=("store_code", "nunique"),
        package_cnt=("package_dish_code", "nunique"),
        row_cnt=("dish_unicode", "size"),
    )
    dish_day_df["error"] = dish_day_df["pred_real_qty_sum"] - dish_day_df["true_real_qty_sum"]
    dish_day_df["abs_error"] = dish_day_df["error"].abs()
    dish_day_df["bias_rate"] = np.where(
        dish_day_df["true_real_qty_sum"].abs() > 0,
        dish_day_df["abs_error"] / dish_day_df["true_real_qty_sum"].abs(),
        np.nan,
    )
    split_order = {"train": 0, "valid": 1, "test": 2}
    dish_day_df["_split_order"] = dish_day_df["split"].map(split_order).fillna(99)
    dish_day_df = dish_day_df.sort_values(
        ["_split_order", args.date_col, "abs_error"],
        ascending=[True, True, False],
    )
    return dish_day_df.drop(columns=["_split_order"])


def build_store_dish_day_output(detail_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    true_col = f"true_{args.dish_label_col}"
    pred_col = f"pred_{args.dish_label_col}"
    group_cols = [
        "split",
        args.date_col,
        "store_code",
        "store_name",
        "dish_unicode",
        "dish_code",
        "dish_name",
        "dish_dish_name",
        "dish_sku_name",
    ]
    group_cols = [c for c in group_cols if c in detail_df.columns]
    store_dish_day_df = detail_df.groupby(group_cols, as_index=False, dropna=False).agg(
        true_real_qty_sum=(true_col, "sum"),
        pred_real_qty_sum=(pred_col, "sum"),
        package_cnt=("package_dish_code", "nunique"),
        row_cnt=("dish_unicode", "size"),
    )
    store_dish_day_df["error"] = store_dish_day_df["pred_real_qty_sum"] - store_dish_day_df["true_real_qty_sum"]
    store_dish_day_df["abs_error"] = store_dish_day_df["error"].abs()
    store_dish_day_df["bias_rate"] = np.where(
        store_dish_day_df["true_real_qty_sum"].abs() > 0,
        store_dish_day_df["abs_error"] / store_dish_day_df["true_real_qty_sum"].abs(),
        np.nan,
    )
    split_order = {"train": 0, "valid": 1, "test": 2}
    store_dish_day_df["_split_order"] = store_dish_day_df["split"].map(split_order).fillna(99)
    store_dish_day_df = store_dish_day_df.sort_values(
        ["_split_order", args.date_col, "abs_error"],
        ascending=[True, True, False],
    )
    return store_dish_day_df.drop(columns=["_split_order"])


def allocate_dish_prediction_by_target_date(
    rows_df: pd.DataFrame,
    package_pred_df: pd.DataFrame,
    history_rows_df: pd.DataFrame,
    args: argparse.Namespace,
    horizon_days: int = 2,
) -> pd.DataFrame:
    if rows_df.empty:
        return rows_df.copy()

    safe_horizon_days = max(int(horizon_days), 1)
    rows_df = rows_df.copy()
    package_pred_df = package_pred_df.copy()
    history_rows_df = history_rows_df.copy()
    rows_df[args.date_col] = pd.to_datetime(rows_df[args.date_col], errors="coerce").dt.normalize()
    package_pred_df[args.date_col] = pd.to_datetime(package_pred_df[args.date_col], errors="coerce").dt.normalize()
    history_rows_df[args.date_col] = pd.to_datetime(history_rows_df[args.date_col], errors="coerce").dt.normalize()

    all_alloc_df = []
    target_dates = rows_df[args.date_col].dropna().drop_duplicates().sort_values()
    for target_date in target_dates:
        ratio_cutoff_date = target_date - pd.Timedelta(days=safe_horizon_days - 1)
        target_rows_df = rows_df[rows_df[args.date_col].eq(target_date)].copy()
        target_package_pred_df = package_pred_df[package_pred_df[args.date_col].eq(target_date)].copy()
        ratio_history_rows_df = history_rows_df[history_rows_df[args.date_col] < ratio_cutoff_date].copy()
        ratio_bundle = build_ratio_bundle(ratio_history_rows_df, ratio_cutoff_date, args)
        all_alloc_df.append(allocate_dish_prediction(target_rows_df, target_package_pred_df, ratio_bundle, args))

    if not all_alloc_df:
        return rows_df.iloc[0:0].copy()
    return pd.concat(all_alloc_df, axis=0, ignore_index=True)


def run_t2_package_backtest(raw_df: pd.DataFrame, args: argparse.Namespace, output_dir: Path) -> None:
    end_date = parse_arg_date(args.train_eval_end, "--train_eval_end")
    raw_df = raw_df[raw_df[args.date_col] <= end_date].copy()
    if raw_df.empty:
        raise ValueError(f"No rows left after train_eval_end filter: {args.train_eval_end}")

    if args.peak_path:
        raw_df = add_peak_features(raw_df, args.peak_path, args.date_col)
    if args.enable_holiday_position:
        raw_df = add_holiday_position_features(raw_df)
    if args.enable_manual_public_holiday_features:
        raw_df = add_manual_public_holiday_features(raw_df, args)
    if args.enable_t2_availability_features:
        raw_df = add_t2_availability_features(raw_df, args, horizon_days=2)

    package_input_df = build_package_frame(raw_df, args)
    package_history_raw_df = deduplicate_to_package_level(
        package_input_df,
        args.package_id_cols,
        args.date_col,
        args.package_label_col,
    )
    package_history_raw_df = time_series_continuization(package_history_raw_df, args.date_col, args.package_id_cols)

    if args.peak_path:
        package_history_raw_df = add_peak_features(package_history_raw_df, args.peak_path, args.date_col)
    if args.enable_holiday_position:
        package_history_raw_df = add_holiday_position_features(package_history_raw_df)
    if args.enable_manual_public_holiday_features:
        package_history_raw_df = add_manual_public_holiday_features(package_history_raw_df, args)
    if args.enable_t2_availability_features:
        package_history_raw_df = add_t2_availability_features(package_history_raw_df, args, horizon_days=2)
    if args.enable_peak_history_mean:
        package_history_raw_df = add_peak_history_mean_features(package_history_raw_df, args, horizon_days=2)
    if args.enable_package_activity_features:
        package_history_raw_df = add_package_activity_features(package_history_raw_df, args, horizon_days=2)

    package_history_feat_df = add_package_lifecycle_features(
        package_history_raw_df.copy(),
        args.package_id_cols,
        args.date_col,
    )
    if args.enable_package_activity_features:
        package_history_feat_df = add_enhanced_lifecycle_features(package_history_feat_df, args)
    df_feat, feature_cols, cat_cols, category_maps = build_features(
        df=package_history_feat_df,
        label_col=args.package_label_col,
        date_col=args.date_col,
        id_cols=args.package_id_cols,
        lag_days=args.lag_days,
        rolling_windows=args.rolling_windows,
        return_category_maps=True,
        history_gap_days=2,
    )

    train_df, valid_df, test_df = split_data(df_feat, args.date_col, args.valid_days, args.test_days)
    raw_train_df, raw_valid_df, raw_test_df = split_data(package_history_raw_df, args.date_col, args.valid_days, args.test_days)
    _, calib_valid_context_df, calib_test_context_df = split_data(package_history_feat_df, args.date_col, args.valid_days, args.test_days)
    valid_start = valid_df[args.date_col].min()
    test_start = test_df[args.date_col].min()

    X_train, y_train = train_df[feature_cols], train_df[args.package_label_col]
    X_valid, y_valid = valid_df[feature_cols], valid_df[args.package_label_col]
    train_sample_weight = build_train_sample_weights(
        train_df,
        args.date_col,
        valid_start,
        args.train_weight_strategy,
        args.train_weight_half_life_days,
    )
    model = train_package_model(X_train, y_train, X_valid, y_valid, cat_cols, args, sample_weight=train_sample_weight)
    best_iter = model.best_iteration_ if model.best_iteration_ else args.n_estimators

    train_pred = np.clip(model.predict(train_df[feature_cols], num_iteration=best_iter), a_min=0, a_max=None)
    train_pred, _ = apply_recent_trend_guardrail(train_pred, train_df, args.package_label_col, args)
    valid_pred = np.clip(model.predict(valid_df[feature_cols], num_iteration=best_iter), a_min=0, a_max=None)
    valid_pred, _ = apply_recent_trend_guardrail(valid_pred, valid_df, args.package_label_col, args)
    test_pred = np.clip(model.predict(test_df[feature_cols], num_iteration=best_iter), a_min=0, a_max=None)
    test_pred, _ = apply_recent_trend_guardrail(test_pred, test_df, args.package_label_col, args)
    raw_test_pred = test_pred.copy()
    calibration_stats = pd.DataFrame([{"calibrated_rows": 0, "mean_factor": 1.0, "min_factor": 1.0, "max_factor": 1.0, "pred_sum_before": float(test_pred.sum()), "pred_sum_after": float(test_pred.sum())}])
    if args.enable_group_calibration:
        calibrator = fit_group_calibrator(calib_valid_context_df, y_valid, valid_pred, args)
        test_pred, calibration_stats = apply_group_calibration(raw_test_pred, calib_test_context_df, calibrator, args)

    package_valid_metrics = evaluate(valid_df[args.package_label_col].values, valid_pred, "package_valid_t2")
    package_test_metrics = evaluate(test_df[args.package_label_col].values, test_pred, "package_test_t2")
    if args.enable_group_calibration:
        raw_package_test_metrics = evaluate(test_df[args.package_label_col].values, raw_test_pred, "package_test_t2_raw_before_calibration")
    else:
        raw_package_test_metrics = package_test_metrics

    valid_rows = raw_df[(raw_df[args.date_col] >= valid_start) & (raw_df[args.date_col] < test_start)].copy()
    test_rows = raw_df[raw_df[args.date_col] >= test_start].copy()

    valid_package_pred_df = raw_valid_df[[c for c in args.package_id_cols if c in raw_valid_df.columns] + [args.date_col]].copy()
    valid_package_pred_df[f"pred_{args.package_label_col}"] = valid_pred
    test_package_pred_df = raw_test_df[[c for c in args.package_id_cols if c in raw_test_df.columns] + [args.date_col]].copy()
    test_package_pred_df[f"pred_{args.package_label_col}"] = test_pred

    valid_alloc_df = allocate_dish_prediction_by_target_date(valid_rows, valid_package_pred_df, raw_df, args, horizon_days=2)
    test_alloc_df = allocate_dish_prediction_by_target_date(test_rows, test_package_pred_df, raw_df, args, horizon_days=2)
    dish_valid_metrics = evaluate(
        valid_alloc_df[args.dish_label_col].values,
        valid_alloc_df[f"pred_{args.dish_label_col}"].values,
        "dish_valid_t2",
    )
    dish_test_metrics = evaluate(
        test_alloc_df[args.dish_label_col].values,
        test_alloc_df[f"pred_{args.dish_label_col}"].values,
        "dish_test_t2",
    )

    train_detail_df = build_package_prediction_output("train", raw_train_df, train_df, train_pred, args)
    valid_detail_df = build_package_prediction_output("valid", raw_valid_df, valid_df, valid_pred, args)
    test_detail_df = build_package_prediction_output("test", raw_test_df, test_df, test_pred, args)
    detail_df = pd.concat([train_detail_df, valid_detail_df, test_detail_df], axis=0, ignore_index=True)

    detail_path = output_dir / f"{args.backtest_output_prefix}_package_detail.csv"
    detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")

    valid_dish_detail_df = build_dish_prediction_output("valid", valid_alloc_df, args)
    test_dish_detail_df = build_dish_prediction_output("test", test_alloc_df, args)
    dish_detail_df = pd.concat([valid_dish_detail_df, test_dish_detail_df], axis=0, ignore_index=True)
    store_dish_day_df = build_store_dish_day_output(dish_detail_df, args)
    store_dish_metrics = {}
    for split_name in ["valid", "test"]:
        store_dish_split_df = store_dish_day_df[store_dish_day_df["split"].eq(split_name)]
        store_dish_metrics[f"store_dish_day_{split_name}_bias"] = package_bias_rate(
            store_dish_split_df,
            "true_real_qty_sum",
            "pred_real_qty_sum",
        )

    store_dish_day_path = output_dir / f"{args.backtest_output_prefix}_store_dish_day.csv"
    store_dish_day_df.to_csv(store_dish_day_path, index=False, encoding="utf-8-sig")

    logging.info(
        "T2 backtest summary | prefix=%s | train_eval_end=%s | best_iter=%s | valid_days=%s | test_days=%s | valid_window=%s~%s | test_window=%s~%s",
        args.backtest_output_prefix,
        end_date.strftime("%Y-%m-%d"),
        int(best_iter),
        int(args.valid_days),
        int(args.test_days),
        valid_df[args.date_col].min().strftime("%Y-%m-%d"),
        valid_df[args.date_col].max().strftime("%Y-%m-%d"),
        test_df[args.date_col].min().strftime("%Y-%m-%d"),
        test_df[args.date_col].max().strftime("%Y-%m-%d"),
    )
    logging.info(
        "T2 package detail bias | valid=%.4f | test=%.4f | valid_badcase=%.4f | test_badcase=%.4f",
        float(package_valid_metrics["package_valid_t2_bias_rate"]),
        float(package_test_metrics["package_test_t2_bias_rate"]),
        float(package_valid_metrics["package_valid_t2_badcase"]),
        float(package_test_metrics["package_test_t2_badcase"]),
    )
    if args.enable_group_calibration:
        logging.info(
            "T2 package calibration | raw_test_bias=%.4f | calibrated_test_bias=%.4f | rows=%s | mean_factor=%.4f | pred_sum_before=%.1f | pred_sum_after=%.1f",
            float(raw_package_test_metrics["package_test_t2_raw_before_calibration_bias_rate"]),
            float(package_test_metrics["package_test_t2_bias_rate"]),
            int(calibration_stats["calibrated_rows"].iloc[0]),
            float(calibration_stats["mean_factor"].iloc[0]),
            float(calibration_stats["pred_sum_before"].iloc[0]),
            float(calibration_stats["pred_sum_after"].iloc[0]),
        )
    logging.info(
        "T2 dish detail bias | valid=%.4f | test=%.4f | valid_badcase=%.4f | test_badcase=%.4f",
        float(dish_valid_metrics["dish_valid_t2_bias_rate"]),
        float(dish_test_metrics["dish_test_t2_bias_rate"]),
        float(dish_valid_metrics["dish_valid_t2_badcase"]),
        float(dish_test_metrics["dish_test_t2_badcase"]),
    )
    logging.info(
        "T2 store dish day bias | valid=%.4f | test=%.4f",
        float(store_dish_metrics["store_dish_day_valid_bias"]),
        float(store_dish_metrics["store_dish_day_test_bias"]),
    )
    logging.info("T2 package detail saved: %s", detail_path)
    logging.info("T2 store dish day saved: %s", store_dish_day_path)


def build_recent_feature_context(
    running_feat_df: pd.DataFrame,
    future_pkg_raw_df: pd.DataFrame,
    feature_cols: list[str],
    category_maps: dict[str, dict[str, int]],
    args: argparse.Namespace,
) -> pd.DataFrame:
    max_history_span = max(args.lag_days + args.rolling_windows)
    future_date = pd.to_datetime(future_pkg_raw_df[args.date_col].iloc[0])
    min_keep_date = future_date - pd.Timedelta(days=max_history_span)
    recent_running_df = running_feat_df[running_feat_df[args.date_col] >= min_keep_date].copy()
    recent_running_df[args.package_label_col] = pd.to_numeric(recent_running_df[args.package_label_col], errors="coerce").fillna(0.0)
    recent_running_df = recent_running_df.drop_duplicates(args.package_id_cols + [args.date_col], keep="last")

    combined_feat_src_df = pd.concat([recent_running_df, future_pkg_raw_df], axis=0, ignore_index=True)
    combined_feat_df, _, _ = build_features(
        df=combined_feat_src_df,
        label_col=args.package_label_col,
        date_col=args.date_col,
        id_cols=args.package_id_cols,
        lag_days=args.lag_days,
        rolling_windows=args.rolling_windows,
        category_maps=category_maps,
    )
    return combined_feat_df[combined_feat_df[args.date_col] == future_date].copy()


def iterative_predict_future_packages(
    package_history_feat_df: pd.DataFrame,
    package_anchor_df: pd.DataFrame,
    future_dates: list[pd.Timestamp],
    holiday_df: pd.DataFrame,
    future_weather_df: pd.DataFrame,
    store_weather_fallback_df: pd.DataFrame,
    global_weather_fallback: dict[str, object],
    final_model,
    feature_cols: list[str],
    category_maps: dict[str, dict[str, int]],
    args: argparse.Namespace,
) -> pd.DataFrame:
    running_feat_df = package_history_feat_df.copy()
    future_package_preds: list[pd.DataFrame] = []

    for future_date in future_dates:
        future_pkg_raw_df, diag = apply_future_exogenous(
            package_anchor_df,
            future_date,
            holiday_df,
            future_weather_df,
            store_weather_fallback_df,
            global_weather_fallback,
            args,
        )
        future_pkg_raw_df[args.package_label_col] = np.nan
        validate_future_features(future_pkg_raw_df, args)

        pred_rows = build_recent_feature_context(
            running_feat_df,
            future_pkg_raw_df,
            feature_cols,
            category_maps,
            args,
        )
        pred = np.clip(final_model.predict(pred_rows[feature_cols]), a_min=0, a_max=None)
        pred, guardrail_stats = apply_recent_trend_guardrail(pred, pred_rows, args.package_label_col, args)
        guardrail_row = guardrail_stats.iloc[0].to_dict()
        guardrail_row["ds"] = future_date.strftime("%Y-%m-%d")

        sold_end_dt = pd.to_datetime(future_pkg_raw_df.get("sold_ent_time"), errors="coerce")
        expired_mask = sold_end_dt.notna() & (future_date > sold_end_dt.dt.normalize())
        pred = np.where(expired_mask, 0.0, pred)

        future_pred_df = future_pkg_raw_df[[c for c in args.package_id_cols if c in future_pkg_raw_df.columns] + [args.date_col]].copy()
        future_pred_df[f"pred_{args.package_label_col}"] = pred
        future_pred_df["is_future_expired"] = expired_mask.astype(int)
        future_package_preds.append(future_pred_df)

        append_rows = future_pkg_raw_df.copy()
        append_rows[args.package_label_col] = pred
        running_feat_df = pd.concat([running_feat_df, append_rows], axis=0, ignore_index=True)
        max_history_span = max(args.lag_days + args.rolling_windows)
        min_keep_date = future_date - pd.Timedelta(days=max_history_span)
        running_feat_df = running_feat_df[running_feat_df[args.date_col] >= min_keep_date].copy()

        logging.info(
            "Future day done | ds=%s | rows=%s | pred_sum=%.2f | expired_zero_rows=%s | guardrail_adjusted_rows=%s | missing_weather_after_fill=%s",
            future_date.strftime("%Y-%m-%d"),
            len(future_pred_df),
            float(np.sum(pred)),
            int(expired_mask.sum()),
            int(guardrail_row.get("adjusted_rows", 0)),
            int(diag.get("missing_weather_after_fill", 0)),
        )

    return pd.concat(future_package_preds, axis=0, ignore_index=True)


def build_future_dish_rows(
    dish_anchor_df: pd.DataFrame,
    future_dates: list[pd.Timestamp],
    holiday_df: pd.DataFrame,
    future_weather_df: pd.DataFrame,
    store_weather_fallback_df: pd.DataFrame,
    global_weather_fallback: dict[str, object],
    args: argparse.Namespace,
) -> pd.DataFrame:
    all_future_rows: list[pd.DataFrame] = []
    for future_date in future_dates:
        future_rows = dish_anchor_df.copy()
        future_rows[args.date_col] = future_date

        holiday_row = holiday_df[holiday_df["ds"] == future_date].iloc[0].to_dict()
        for col in [c for c in holiday_df.columns if c != "ds"]:
            future_rows[col] = holiday_row[col]
        future_rows["day_of_week"] = future_date.weekday() + 1
        future_rows["is_weekend"] = int(future_date.weekday() >= 5)

        weather_slice = future_weather_df[future_weather_df["ds"] == future_date].copy()
        future_rows = future_rows.merge(
            weather_slice[
                ["store_code", "day_weather_code", "day_air_temperature", "night_weather_code", "night_air_temperature"]
            ],
            on="store_code",
            how="left",
            suffixes=("", "_future"),
        )
        if "day_weather_code_future" in future_rows.columns:
            future_rows["day_weather_code"] = future_rows["day_weather_code_future"]
            future_rows["day_air_temperature"] = future_rows["day_air_temperature_future"]
            future_rows["night_weather_code"] = future_rows["night_weather_code_future"]
            future_rows["night_air_temperature"] = future_rows["night_air_temperature_future"]
            future_rows = future_rows.drop(
                columns=[
                    "day_weather_code_future",
                    "day_air_temperature_future",
                    "night_weather_code_future",
                    "night_air_temperature_future",
                ]
            )

        future_rows = future_rows.merge(store_weather_fallback_df, on="store_code", how="left")
        future_rows["day_weather_code"] = future_rows["day_weather_code"].fillna(future_rows["store_day_weather_code"])
        future_rows["day_air_temperature"] = future_rows["day_air_temperature"].fillna(future_rows["store_day_air_temperature"])
        future_rows["night_weather_code"] = future_rows["night_weather_code"].fillna(future_rows["store_night_weather_code"])
        future_rows["night_air_temperature"] = future_rows["night_air_temperature"].fillna(future_rows["store_night_air_temperature"])
        future_rows["day_weather_code"] = future_rows["day_weather_code"].fillna(global_weather_fallback["day_weather_code"])
        future_rows["day_air_temperature"] = future_rows["day_air_temperature"].fillna(global_weather_fallback["day_air_temperature"])
        future_rows["night_weather_code"] = future_rows["night_weather_code"].fillna(global_weather_fallback["night_weather_code"])
        future_rows["night_air_temperature"] = future_rows["night_air_temperature"].fillna(global_weather_fallback["night_air_temperature"])
        future_rows = future_rows.drop(
            columns=[
                "store_day_weather_code",
                "store_day_air_temperature",
                "store_night_weather_code",
                "store_night_air_temperature",
            ]
        )

        sold_end_dt = pd.to_datetime(future_rows.get("sold_ent_time"), errors="coerce")
        if "is_sale" in future_rows.columns:
            future_rows["is_sale"] = np.where(sold_end_dt.notna() & (future_date <= sold_end_dt.dt.normalize()), 1, 0)
        future_rows[args.dish_label_col] = 0.0
        all_future_rows.append(future_rows)

    future_dish_df = pd.concat(all_future_rows, axis=0, ignore_index=True)
    future_dish_df["future_missing_weather"] = future_dish_df[
        ["day_weather_code", "day_air_temperature", "night_weather_code", "night_air_temperature"]
    ].isna().any(axis=1).astype(int)
    return future_dish_df


def select_package_output_columns(future_package_pred_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    package_output_df = future_package_pred_df.copy()
    keep_cols = [
        "store_code",
        "package_dish_code",
        args.date_col,
        f"pred_{args.package_label_col}",
    ]
    keep_cols = [c for c in keep_cols if c in package_output_df.columns]
    return package_output_df[keep_cols].copy()


def select_dish_output_columns(future_dish_pred_df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    dish_output_df = future_dish_pred_df.copy()
    keep_cols = [
        "store_code",
        "package_dish_code",
        "dish_unicode",
        args.date_col,
        f"pred_{args.dish_label_col}",
    ]
    keep_cols = [c for c in keep_cols if c in dish_output_df.columns]
    return dish_output_df[keep_cols].copy()


def run_online_pipeline(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_df = load_data(args.data_path)
    raw_df = prepare_raw_rows(raw_df, args)
    if args.t2_backtest:
        run_t2_package_backtest(raw_df, args, output_dir)
        return

    raw_df = add_peak_features(raw_df, args.peak_path, args.date_col)

    package_input_df = build_package_frame(raw_df, args)
    package_history_raw_df = deduplicate_to_package_level(
        package_input_df,
        args.package_id_cols,
        args.date_col,
        args.package_label_col,
    )
    package_history_raw_df = time_series_continuization(package_history_raw_df, args.date_col, args.package_id_cols)
    package_history_feat_df = add_package_lifecycle_features(
        package_history_raw_df.copy(),
        args.package_id_cols,
        args.date_col,
    )

    final_model, package_history_feat_df, feature_cols, cat_cols, category_maps = train_backtest_and_refit_model(
        package_history_raw_df,
        raw_df,
        args,
        output_dir,
    )

    if args.history_eval_only:
        logging.info("history_eval_only=True; historical train/validation/test evaluation complete.")
        return

    holiday_df = load_holiday_data(args.holiday_path)
    future_weather_df = load_future_weather_data(args.future_weather_path,args.bizdate)
    store_weather_fallback_df, global_weather_fallback = build_weather_fallbacks(raw_df)

    package_anchor_df, dish_anchor_df, max_hist_date = build_online_anchor_frames(raw_df, args)
    future_dates = build_future_dates(max_hist_date, args.forecast_days)

    future_package_pred_df = iterative_predict_future_packages(
        package_history_feat_df,
        package_anchor_df,
        future_dates,
        holiday_df,
        future_weather_df,
        store_weather_fallback_df,
        global_weather_fallback,
        final_model,
        feature_cols,
        category_maps,
        args,
    )
    package_output_df = select_package_output_columns(future_package_pred_df, args)
    package_output_df.to_csv(output_dir / "future_8d_package_prediction.csv", index=False, encoding="utf-8-sig")

    future_dish_rows = build_future_dish_rows(
        dish_anchor_df,
        future_dates,
        holiday_df,
        future_weather_df,
        store_weather_fallback_df,
        global_weather_fallback,
        args,
    )
    ratio_bundle = build_ratio_bundle(raw_df, max_hist_date + pd.Timedelta(days=1), args)
    future_dish_pred_df = allocate_dish_prediction(
        future_dish_rows,
        future_package_pred_df,
        ratio_bundle,
        args,
    )
    dish_output_df = select_dish_output_columns(future_dish_pred_df, args)
    dish_output_df.to_csv(output_dir / "future_8d_dish_prediction.csv", index=False, encoding="utf-8-sig")

    logging.info("Online future forecast complete: %s", output_dir)
    logging.info("Future package prediction saved: %s", output_dir / "future_8d_package_prediction.csv")
    logging.info("Future dish prediction saved: %s", output_dir / "future_8d_dish_prediction.csv")

    df_new = dish_output_df
    bizdate = args.bizdate if args.bizdate else '20260301'
    df_new['calc_log'] = ''
    df_new['version'] = 'type_model_'+ str(bizdate) +'.pkl'
    print(df_new.shape[0])
    data_list = [list(i) for i in df_new.values]
    #print(df_new)

    o = get_odps_client()
    tobj = o.get_table('dwd_forecast_dish_package_model_new_hzy_di', project='alg_prd')
    part = "ds='"+ bizdate +"',source='lgbm2'"
    print(part)
    if tobj.exist_partition(part):
        tobj.delete_partition(part)
    with tobj.open_writer(partition=part, create_partition=True) as writer:
        writer.write(data_list)
    print("-----------odps insert success------------")


if __name__ == "__main__":
    setup_logger()
    run_online_pipeline(parse_args())
