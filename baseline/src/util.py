import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Tuple

BASELINE_DIR = Path(__file__).resolve().parent.parent
PYTHON_PACKAGE_DIR = BASELINE_DIR / ".python_packages"
if str(PYTHON_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_PACKAGE_DIR))

import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.model_selection import train_test_split


PACKAGE_ID_COLS = ["store_code", "package_dish_code"]
ROW_ID_COLS = ["store_code", "package_dish_code", "dish_unicode"]


def setup_logger() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_data(data_path: str) -> pd.DataFrame:
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {data_path}")
    try:
        df = pd.read_csv(path, encoding="gbk")
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="utf-8")

    logging.info(f"Loaded data: {path}, shape={df.shape}")
    logging.info(f"Columns: {list(df.columns)}")

    if "use_start_date" not in df.columns and "sold_start_time" in df.columns:
        df["use_start_date"] = df["sold_start_time"]
    if "use_end_date" not in df.columns and "sold_ent_time" in df.columns:
        df["use_end_date"] = df["sold_ent_time"]
    return df.copy()


def normalize_columns(df: pd.DataFrame, label_col: str, date_col: str, id_cols: List[str]) -> pd.DataFrame:
    if label_col not in df.columns:
        raise ValueError(f"Missing label column: {label_col}")
    if date_col not in df.columns:
        raise ValueError(f"Missing date column: {date_col}")

    missing_ids = [c for c in id_cols if c not in df.columns]
    if missing_ids:
        logging.warning(f"Missing id columns: {missing_ids}")

    df = df[~df["store_code"].isna()].copy()
    date_num = pd.to_numeric(df[date_col], errors="coerce").round().astype("Int64")
    df[date_col] = pd.to_datetime(date_num.astype(str), errors="coerce", format="%Y%m%d")

    before = len(df)
    df[label_col] = pd.to_numeric(df[label_col], errors="coerce")
    df = df.dropna(subset=[date_col, label_col]).reset_index(drop=True)
    df[label_col] = df[label_col].clip(lower=0.0)
    logging.info(f"Cleaned rows: {before} -> {len(df)}")

    print("no empty num is ", len(df[~df["store_code"].isna()]))
    print("empty num is ", len(df[df["store_code"].isna()]))
    df = df[~df["store_code"].isna()]
    if df[label_col].nunique() <= 1:
        raise ValueError(f"Label column {label_col} has <=1 unique values.")
    return df


def prepare_raw_rows(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    df = normalize_columns(df, args.package_label_col, args.date_col, args.package_id_cols)
    df[args.dish_label_col] = pd.to_numeric(df[args.dish_label_col], errors="coerce").fillna(0.0).clip(lower=0.0)

    if "package_dish_code" in df.columns:
        df = df[~df["package_dish_code"].isna()].copy()
        df["package_dish_code"] = pd.to_numeric(df["package_dish_code"], errors="coerce")
        df = df.dropna(subset=["package_dish_code"]).copy()
        df["package_dish_code"] = df["package_dish_code"].astype(int)

    if "dish_unicode" in df.columns:
        df = df[~df["dish_unicode"].isna()].copy()
        df["dish_unicode"] = pd.to_numeric(df["dish_unicode"], errors="coerce")
        df = df.dropna(subset=["dish_unicode"]).copy()
        df["dish_unicode"] = df["dish_unicode"].astype(int)

    return df


def build_package_frame(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    reserved_drop_cols = {"dish_unicode", "dish_code", args.dish_label_col}
    keep_cols = [c for c in df.columns if c not in reserved_drop_cols]
    return df[keep_cols].copy()


def deduplicate_to_package_level(df: pd.DataFrame, id_cols: list[str], date_col: str, label_col: str) -> pd.DataFrame:
    group_cols = [c for c in id_cols if c in df.columns] + [date_col]
    if not group_cols:
        raise ValueError("No package-level grouping columns available for deduplication.")

    before = len(df)
    dup_factor = before / max(df[group_cols].drop_duplicates().shape[0], 1)
    logging.info(f"Package-level dedup start: rows={before}, duplicate_factor={dup_factor:.2f}")

    agg_map: dict[str, str] = {}
    for col in df.columns:
        if col in group_cols:
            continue
        agg_map[col] = "max" if col == label_col else "first"

    dedup_df = df.sort_values(group_cols).groupby(group_cols, as_index=False).agg(agg_map)
    logging.info(f"Package-level dedup done: {before} -> {len(dedup_df)}")
    return dedup_df


def add_package_lifecycle_features(df: pd.DataFrame, id_cols: list[str], date_col: str) -> pd.DataFrame:
    group_cols = [c for c in id_cols if c in df.columns]
    if not group_cols:
        return df

    df = df.copy().sort_values(group_cols + [date_col]).reset_index(drop=True)
    first_seen = df.groupby(group_cols)[date_col].transform("min")
    df["package_age_days"] = (df[date_col] - first_seen).dt.days.clip(lower=0)
    df["package_seen_days"] = df["package_age_days"] + 1
    df["is_new_package_1d"] = (df["package_age_days"] <= 1).astype(int)
    df["is_new_package_3d"] = (df["package_age_days"] <= 3).astype(int)
    df["is_new_package_7d"] = (df["package_age_days"] <= 7).astype(int)
    return df


def time_series_continuization(df: pd.DataFrame, date_col: str, id_cols: List[str]) -> pd.DataFrame:
    usable_id_cols = [c for c in id_cols if c in df.columns]
    if not usable_id_cols:
        return df.sort_values(date_col).reset_index(drop=True)

    df = df.copy().sort_values(usable_id_cols + [date_col]).reset_index(drop=True)
    span_df = df.groupby(usable_id_cols, as_index=False)[date_col].agg(min_ds="min", max_ds="max")
    span_df[date_col] = span_df.apply(lambda row: pd.date_range(row["min_ds"], row["max_ds"], freq="D"), axis=1)
    full_index = span_df[usable_id_cols + [date_col]].explode(date_col, ignore_index=True)
    merged = full_index.merge(df, on=usable_id_cols + [date_col], how="left", sort=False)

    target_candidates = [c for c in ["real_qty", "qty", "label", "pos_cnt"] if c in merged.columns]
    fill_cols = [c for c in merged.columns if c not in usable_id_cols + [date_col]]
    static_cols = [c for c in fill_cols if c not in target_candidates]

    if static_cols:
        merged[static_cols] = merged.groupby(usable_id_cols, sort=False)[static_cols].transform(lambda s: s.ffill().bfill())
    for col in target_candidates:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    residual_num_cols = [c for c in fill_cols if c not in target_candidates and pd.api.types.is_numeric_dtype(merged[c])]
    residual_obj_cols = [c for c in fill_cols if c not in target_candidates and not pd.api.types.is_numeric_dtype(merged[c])]
    if residual_num_cols:
        merged[residual_num_cols] = merged[residual_num_cols].fillna(0)
    if residual_obj_cols:
        merged[residual_obj_cols] = merged[residual_obj_cols].fillna("NA")
    return merged.sort_values(usable_id_cols + [date_col]).reset_index(drop=True)


def add_time_features(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    df = df.copy()
    df["year"] = df[date_col].dt.year
    df["month"] = df[date_col].dt.month
    df["day"] = df[date_col].dt.day
    df["day_of_week"] = df[date_col].dt.weekday + 1
    df["is_weekend"] = (df["day_of_week"] >= 6).astype(int)
    return df


def generate_lag_features(
    df: pd.DataFrame,
    group_cols: List[str],
    target_col: str,
    lag_periods: List[int],
    date_col: str,
    history_gap_days: int = 1,
) -> pd.DataFrame:
    df = df.copy().sort_values(group_cols + [date_col]).reset_index(drop=True)
    safe_gap = max(int(history_gap_days), 1)
    for lag in lag_periods:
        df[f"{target_col}_lag_{lag}"] = df.groupby(group_cols)[target_col].shift(lag + safe_gap - 1)
    return df


def generate_rolling_features(
    df: pd.DataFrame,
    group_cols: List[str],
    target_col: str,
    window_sizes: List[int],
    date_col: str,
    functions: Tuple[str, ...] = ("mean", "std", "min", "max", "median"),
    history_gap_days: int = 1,
) -> pd.DataFrame:
    df = df.copy().sort_values(group_cols + [date_col]).reset_index(drop=True)
    grouped = df.groupby(group_cols)[target_col]
    safe_gap = max(int(history_gap_days), 1)
    for window in window_sizes:
        for func in functions:
            col = f"{target_col}_rolling_{window}_{func}"
            df[col] = grouped.transform(lambda s, w=window, f=func, g=safe_gap: s.shift(g).rolling(window=w, min_periods=1).agg(f))
    return df


def infer_categorical_features(df: pd.DataFrame, id_cols: List[str], exclude_cols: List[str]) -> List[str]:
    cat_cols = set()
    safe_known_cats = {
        "day_of_week",
        "is_weekend",
        "is_bad_weather",
        "is_sale",
        "show_channel_num",
        "use_date_type_num",
        "use_time_type_num",
    }
    for c in df.columns:
        if c in exclude_cols:
            continue
        s = df[c]
        if (
            pd.api.types.is_object_dtype(s)
            or pd.api.types.is_categorical_dtype(s)
            or pd.api.types.is_string_dtype(s)
        ):
            cat_cols.add(c)
            continue
        if c in id_cols:
            cat_cols.add(c)
            continue
        if c in safe_known_cats or c.endswith("_code"):
            cat_cols.add(c)
    return sorted(cat_cols)


def build_category_maps(df: pd.DataFrame, cat_cols: List[str]) -> dict[str, dict[str, int]]:
    category_maps: dict[str, dict[str, int]] = {}
    for c in cat_cols:
        if c not in df.columns:
            continue
        values = df[c].astype(str).fillna("NA")
        categories = pd.Index(pd.Categorical(values).categories)
        category_maps[c] = {str(v): int(i) for i, v in enumerate(categories)}
    return category_maps


def encode_categorical(df: pd.DataFrame, cat_cols: List[str], category_maps: dict[str, dict[str, int]] | None = None) -> pd.DataFrame:
    df = df.copy()
    for c in cat_cols:
        if c not in df.columns:
            continue
        values = df[c].astype(str).fillna("NA")
        if category_maps is None or c not in category_maps:
            df[c] = pd.Categorical(values).codes
        else:
            mapping = category_maps[c]
            unknown_code = len(mapping)
            df[c] = values.map(mapping).fillna(unknown_code).astype(int)
    return df


def build_features(
    df: pd.DataFrame,
    label_col: str,
    date_col: str,
    id_cols: List[str],
    lag_days: List[int],
    rolling_windows: List[int],
    category_maps: dict[str, dict[str, int]] | None = None,
    return_category_maps: bool = False,
    history_gap_days: int = 1,
) -> Tuple[pd.DataFrame, List[str], List[str]] | Tuple[pd.DataFrame, List[str], List[str], dict[str, dict[str, int]]]:
    df = add_time_features(df, date_col)
    group_cols = [c for c in id_cols if c in df.columns]

    df["use_start_date"] = pd.to_datetime(df["use_start_date"], errors="coerce")
    df["use_end_date"] = pd.to_datetime(df["use_end_date"], errors="coerce")
    df["sale_day_num"] = (df[date_col] - df["use_start_date"]).dt.days.clip(lower=0)
    df["activity_duration"] = (df["use_end_date"] - df["use_start_date"]).dt.days.clip(lower=0)
    df["days_to_end"] = (df["use_end_date"] - df[date_col]).dt.days.clip(lower=0)
    df.pop("use_start_date")
    df.pop("use_end_date")

    if not group_cols:
        logging.warning("No valid id columns found, lag/rolling will be global by date.")
        group_cols = [date_col]

    if group_cols == [date_col]:
        df = df.sort_values(date_col).reset_index(drop=True)
        safe_gap = max(int(history_gap_days), 1)
        for lag in lag_days:
            df[f"{label_col}_lag_{lag}"] = df[label_col].shift(lag + safe_gap - 1)
        for w in rolling_windows:
            s = df[label_col].shift(safe_gap)
            df[f"{label_col}_rolling_{w}_mean"] = s.rolling(w, min_periods=1).mean()
            df[f"{label_col}_rolling_{w}_std"] = s.rolling(w, min_periods=1).std()
            df[f"{label_col}_rolling_{w}_min"] = s.rolling(w, min_periods=1).min()
            df[f"{label_col}_rolling_{w}_max"] = s.rolling(w, min_periods=1).max()
            df[f"{label_col}_rolling_{w}_median"] = s.rolling(w, min_periods=1).median()
    else:
        df = generate_lag_features(df, group_cols, label_col, lag_days, date_col, history_gap_days=history_gap_days)
        df = generate_rolling_features(df, group_cols, label_col, rolling_windows, date_col, history_gap_days=history_gap_days)

    lag_roll_cols = [c for c in df.columns if c.startswith(f"{label_col}_lag_") or c.startswith(f"{label_col}_rolling_")]
    for c in lag_roll_cols:
        df[c] = df[c].fillna(0.0)

    cat_cols = infer_categorical_features(df, id_cols=id_cols, exclude_cols=[label_col, date_col])
    fitted_category_maps = category_maps if category_maps is not None else build_category_maps(df, cat_cols)
    df = encode_categorical(df, cat_cols, fitted_category_maps)
    feature_cols = [c for c in df.columns if c not in {label_col, date_col}]
    logging.info(f"Feature engineering done: total {len(feature_cols)} features, categorical {len(cat_cols)}")
    if return_category_maps:
        return df, feature_cols, cat_cols, fitted_category_maps
    return df, feature_cols, cat_cols


def split_data(df: pd.DataFrame, date_col: str, valid_days: int, test_days: int):
    df = df.sort_values(date_col).reset_index(drop=True)
    max_date = df[date_col].max()
    test_start = max_date - pd.Timedelta(days=test_days - 1)
    valid_start = test_start - pd.Timedelta(days=valid_days)

    train_df = df[df[date_col] < valid_start].copy()
    valid_df = df[(df[date_col] >= valid_start) & (df[date_col] < test_start)].copy()
    test_df = df[df[date_col] >= test_start].copy()
    print(f"Split date: train < {valid_start.date()}, valid [{valid_start.date()}, {test_start.date()}), test >= {test_start.date()}")
    if len(train_df) < 50 or len(valid_df) < 20 or len(test_df) < 20:
        logging.warning("Time split samples too small; fallback to random split.")
        temp_df, test_df = train_test_split(df, test_size=0.2, random_state=42)
        train_df, valid_df = train_test_split(temp_df, test_size=0.25, random_state=42)
    logging.info(f"Data split done: train={len(train_df)}, valid={len(valid_df)}, test={len(test_df)}")
    return train_df, valid_df, test_df


def train_package_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_valid: pd.DataFrame,
    y_valid: pd.Series,
    cat_cols: list[str],
    args: argparse.Namespace,
    sample_weight: np.ndarray | None = None,
) -> LGBMRegressor:
    model_params = dict(
        objective=args.objective,
        boosting_type="gbdt",
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        min_child_samples=40,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.05,
        reg_lambda=0.2,
        random_state=args.random_state,
        n_jobs=8,
    )
    if args.objective == "tweedie":
        model_params["tweedie_variance_power"] = args.tweedie_variance_power

    logging.info(
        "Train package model: objective=%s, tweedie_variance_power=%s",
        args.objective,
        model_params.get("tweedie_variance_power"),
    )
    model = LGBMRegressor(**model_params)
    model.fit(
        X_train,
        y_train,
        sample_weight=sample_weight,
        eval_set=[(X_valid, y_valid)],
        eval_metric="l1",
        categorical_feature=[c for c in cat_cols if c in X_train.columns],
        callbacks=[lgb.early_stopping(100), lgb.log_evaluation(100)],
    )
    return model


def fit_fixed_iter_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    cat_cols: list[str],
    base_model: LGBMRegressor,
    n_estimators: int,
    sample_weight: np.ndarray | None = None,
) -> LGBMRegressor:
    params = base_model.get_params()
    params["n_estimators"] = max(int(n_estimators), 1)
    refit_model = base_model.__class__(**params)
    refit_model.fit(
        X_train,
        y_train,
        sample_weight=sample_weight,
        categorical_feature=[c for c in cat_cols if c in X_train.columns],
    )
    return refit_model


def build_train_sample_weights(df: pd.DataFrame, date_col: str, cutoff_date: pd.Timestamp, strategy: str, half_life_days: float) -> np.ndarray | None:
    if strategy == "none":
        return None
    safe_half_life = max(float(half_life_days), 1e-6)
    days_diff = (cutoff_date - df[date_col]).dt.days.clip(lower=0)
    return np.power(0.5, days_diff / safe_half_life).astype(float).to_numpy()


def apply_recent_trend_guardrail(pred: np.ndarray, feature_df: pd.DataFrame, label_col: str, args: argparse.Namespace) -> tuple[np.ndarray, pd.DataFrame]:
    if args.trend_guardrail == "none":
        return pred, pd.DataFrame([{
            "trend_guardrail": "none",
            "adjusted_rows": 0,
            "adjusted_ratio": 0.0,
            "pred_sum_before": float(np.sum(pred)),
            "pred_sum_after": float(np.sum(pred)),
        }])

    pred_arr = np.asarray(pred, dtype=float).copy()
    lag_1 = pd.to_numeric(feature_df.get(f"{label_col}_lag_1", 0.0), errors="coerce").fillna(0.0).to_numpy()
    lag_2 = pd.to_numeric(feature_df.get(f"{label_col}_lag_2", 0.0), errors="coerce").fillna(0.0).to_numpy()
    lag_3 = pd.to_numeric(feature_df.get(f"{label_col}_lag_3", 0.0), errors="coerce").fillna(0.0).to_numpy()
    rolling_3 = pd.to_numeric(feature_df.get(f"{label_col}_rolling_3_mean", 0.0), errors="coerce").fillna(0.0).to_numpy()
    rolling_7 = pd.to_numeric(feature_df.get(f"{label_col}_rolling_7_mean", 0.0), errors="coerce").fillna(0.0).to_numpy()

    recent_anchor = np.maximum(0.55 * lag_1 + 0.30 * lag_2 + 0.15 * lag_3, 0.70 * rolling_3)
    trend_ratio = recent_anchor / np.maximum(rolling_7, 1.0)
    pred_ratio = pred_arr / np.maximum(recent_anchor, 1.0)
    adjust_mask = (rolling_7 > 0) & (trend_ratio < args.guardrail_recent_threshold) & (pred_ratio > args.guardrail_pred_threshold)
    capped_pred = np.maximum(recent_anchor * args.guardrail_cap_multiplier, lag_1)
    pred_arr[adjust_mask] = np.minimum(pred_arr[adjust_mask], capped_pred[adjust_mask])
    pred_arr = np.clip(pred_arr, a_min=0, a_max=None)
    stats_df = pd.DataFrame([{
        "trend_guardrail": args.trend_guardrail,
        "adjusted_rows": int(adjust_mask.sum()),
        "adjusted_ratio": float(adjust_mask.mean()) if len(adjust_mask) else 0.0,
        "pred_sum_before": float(np.sum(pred)),
        "pred_sum_after": float(np.sum(pred_arr)),
        "recent_threshold": float(args.guardrail_recent_threshold),
        "pred_threshold": float(args.guardrail_pred_threshold),
        "cap_multiplier": float(args.guardrail_cap_multiplier),
    }])
    return pred_arr, stats_df


def build_ratio_table(train_rows: pd.DataFrame, package_label_col: str, dish_label_col: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, float]:
    ratio_df = train_rows[ROW_ID_COLS + [dish_label_col, package_label_col]].copy()
    ratio_df = ratio_df[ratio_df[package_label_col] > 0].copy()
    ratio_df["dish_ratio"] = ratio_df[dish_label_col] / ratio_df[package_label_col]

    ratio_store_pkg_dish = ratio_df.groupby(ROW_ID_COLS, as_index=False)["dish_ratio"].median().rename(columns={"dish_ratio": "ratio_store_pkg_dish"})
    ratio_pkg_dish = ratio_df.groupby(["package_dish_code", "dish_unicode"], as_index=False)["dish_ratio"].median().rename(columns={"dish_ratio": "ratio_pkg_dish"})
    ratio_dish = ratio_df.groupby(["dish_unicode"], as_index=False)["dish_ratio"].median().rename(columns={"dish_ratio": "ratio_dish"})
    global_ratio = float(ratio_df["dish_ratio"].median()) if not ratio_df.empty else 1.0
    logging.info(
        "Ratio table sizes: store+package+dish=%s, package+dish=%s, dish=%s, global_ratio=%.4f",
        len(ratio_store_pkg_dish), len(ratio_pkg_dish), len(ratio_dish), global_ratio
    )
    return ratio_store_pkg_dish, ratio_pkg_dish, ratio_dish, global_ratio


def build_weighted_ratio_table(train_rows: pd.DataFrame, cutoff_date: pd.Timestamp, package_label_col: str, dish_label_col: str, half_life_days: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, float]:
    ratio_df = train_rows[ROW_ID_COLS + ["ds", dish_label_col, package_label_col]].copy()
    ratio_df = ratio_df[(ratio_df["ds"] < cutoff_date) & (ratio_df[package_label_col] > 0)].copy()
    if ratio_df.empty:
        return build_ratio_table(train_rows.iloc[0:0].copy(), package_label_col, dish_label_col)

    days_diff = (cutoff_date - ratio_df["ds"]).dt.days.clip(lower=0)
    safe_half_life = max(half_life_days, 1e-6)
    ratio_df["weight"] = np.power(0.5, days_diff / safe_half_life)
    ratio_df["weighted_dish"] = ratio_df[dish_label_col] * ratio_df["weight"]
    ratio_df["weighted_pkg"] = ratio_df[package_label_col] * ratio_df["weight"]

    def _agg_ratio(group_cols: list[str], out_col: str) -> pd.DataFrame:
        agg_df = ratio_df.groupby(group_cols, as_index=False)[["weighted_dish", "weighted_pkg"]].sum()
        agg_df[out_col] = agg_df["weighted_dish"] / agg_df["weighted_pkg"].replace(0, np.nan)
        agg_df[out_col] = agg_df[out_col].fillna(0.0)
        return agg_df[group_cols + [out_col]]

    ratio_store_pkg_dish = _agg_ratio(ROW_ID_COLS, "ratio_store_pkg_dish")
    ratio_pkg_dish = _agg_ratio(["package_dish_code", "dish_unicode"], "ratio_pkg_dish")
    ratio_dish = _agg_ratio(["dish_unicode"], "ratio_dish")
    weighted_dish_sum = float(ratio_df["weighted_dish"].sum())
    weighted_pkg_sum = float(ratio_df["weighted_pkg"].sum())
    global_ratio = weighted_dish_sum / weighted_pkg_sum if weighted_pkg_sum > 0 else 1.0
    return ratio_store_pkg_dish, ratio_pkg_dish, ratio_dish, global_ratio


def build_recent_ratio_bundle(train_rows: pd.DataFrame, cutoff_date: pd.Timestamp, ratio_windows: list[int], package_label_col: str, dish_label_col: str):
    bundle: dict[str, object] = {"windows": []}
    eligible_rows = train_rows[train_rows["ds"] < cutoff_date].copy()
    bundle["full"] = build_ratio_table(eligible_rows, package_label_col, dish_label_col)
    for window in ratio_windows:
        start_date = cutoff_date - pd.Timedelta(days=window)
        window_rows = eligible_rows[eligible_rows["ds"] >= start_date].copy()
        if window_rows.empty:
            continue
        bundle["windows"].append((window, build_ratio_table(window_rows, package_label_col, dish_label_col)))
    return bundle


def build_ratio_bundle(train_rows: pd.DataFrame, cutoff_date: pd.Timestamp, args: argparse.Namespace):
    if args.ratio_strategy == "full":
        full_ratio = build_ratio_table(train_rows[train_rows["ds"] < cutoff_date].copy(), args.package_label_col, args.dish_label_col)
        return {"windows": [], "full": full_ratio}
    if args.ratio_strategy == "weighted":
        weighted_ratio = build_weighted_ratio_table(train_rows, cutoff_date, args.package_label_col, args.dish_label_col, args.ratio_half_life_days)
        return {"windows": [], "full": weighted_ratio}
    return build_recent_ratio_bundle(train_rows, cutoff_date, args.ratio_windows, args.package_label_col, args.dish_label_col)


def allocate_dish_prediction(rows_df: pd.DataFrame, package_pred_df: pd.DataFrame, ratio_bundle, args: argparse.Namespace) -> pd.DataFrame:
    merge_cols = [c for c in args.package_id_cols if c in rows_df.columns] + [args.date_col]
    rows_df = rows_df.copy()
    package_pred_df = package_pred_df.copy()
    for col in merge_cols:
        if col not in rows_df.columns or col not in package_pred_df.columns:
            continue
        if col == args.date_col:
            rows_df[col] = pd.to_datetime(rows_df[col], errors="coerce")
            package_pred_df[col] = pd.to_datetime(package_pred_df[col], errors="coerce")
        else:
            rows_df[col] = pd.to_numeric(rows_df[col], errors="coerce").astype("Int64")
            package_pred_df[col] = pd.to_numeric(package_pred_df[col], errors="coerce").astype("Int64")
    pred_df = rows_df.merge(package_pred_df, on=merge_cols, how="left")
    pred_df["dish_ratio"] = np.nan
    pred_df["ratio_source"] = ""

    for window, ratio_tables in ratio_bundle["windows"]:
        ratio_store_pkg_dish, ratio_pkg_dish, ratio_dish, _ = ratio_tables
        temp_df = pred_df[ROW_ID_COLS].copy()
        temp_df = temp_df.merge(ratio_store_pkg_dish, on=ROW_ID_COLS, how="left")
        temp_df = temp_df.merge(ratio_pkg_dish, on=["package_dish_code", "dish_unicode"], how="left")
        temp_df = temp_df.merge(ratio_dish, on=["dish_unicode"], how="left")
        candidate = temp_df["ratio_store_pkg_dish"].fillna(temp_df["ratio_pkg_dish"]).fillna(temp_df["ratio_dish"])
        mask = pred_df["dish_ratio"].isna() & candidate.notna()
        pred_df.loc[mask, "dish_ratio"] = candidate[mask]
        pred_df.loc[mask, "ratio_source"] = f"recent_{window}d"

    full_ratio_store_pkg_dish, full_ratio_pkg_dish, full_ratio_dish, global_ratio = ratio_bundle["full"]
    temp_df = pred_df[ROW_ID_COLS].copy()
    temp_df = temp_df.merge(full_ratio_store_pkg_dish, on=ROW_ID_COLS, how="left")
    temp_df = temp_df.merge(full_ratio_pkg_dish, on=["package_dish_code", "dish_unicode"], how="left")
    temp_df = temp_df.merge(full_ratio_dish, on=["dish_unicode"], how="left")
    candidate = temp_df["ratio_store_pkg_dish"].fillna(temp_df["ratio_pkg_dish"]).fillna(temp_df["ratio_dish"])
    mask = pred_df["dish_ratio"].isna() & candidate.notna()
    pred_df.loc[mask, "dish_ratio"] = candidate[mask]
    pred_df.loc[mask, "ratio_source"] = "full_history"

    mask = pred_df["dish_ratio"].isna()
    pred_df.loc[mask, "dish_ratio"] = global_ratio
    pred_df.loc[mask, "ratio_source"] = "global"
    pred_df[f"pred_{args.package_label_col}"] = pred_df[f"pred_{args.package_label_col}"].fillna(0.0)
    pred_df[f"pred_{args.dish_label_col}"] = np.clip(pred_df[f"pred_{args.package_label_col}"] * pred_df["dish_ratio"], a_min=0, a_max=None)
    return pred_df


def save_feature_importance(model: LGBMRegressor, feature_cols: List[str], output_dir: Path) -> Path:
    fi = pd.DataFrame({
        "feature": feature_cols,
        "importance_gain": model.booster_.feature_importance(importance_type="gain"),
        "importance_split": model.booster_.feature_importance(importance_type="split"),
    }).sort_values("importance_gain", ascending=False)
    fi_path = output_dir / "feature_importance.csv"
    fi.to_csv(fi_path, index=False, encoding="utf-8-sig")
    return fi_path


def save_config_snapshot(args: argparse.Namespace, feature_cols: List[str], cat_cols: List[str], output_dir: Path) -> Path:
    cfg = {
        "args": vars(args),
        "feature_count": len(feature_cols),
        "categorical_feature_count": len(cat_cols),
        "categorical_features": cat_cols,
    }
    cfg_path = output_dir / "train_config_snapshot.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg_path


def badcase(y_true, y_pred):
    y_true_arr = np.asarray(y_true, dtype=float)
    y_pred_arr = np.asarray(y_pred, dtype=float)
    denom = np.maximum(np.abs(y_true_arr), 1.0)
    ape = np.abs(y_pred_arr - y_true_arr) / denom
    return float(np.mean(ape > 0.8))


def bias_rate(y_true, y_pred):
    y_true_arr = np.asarray(y_true, dtype=float)
    y_pred_arr = np.asarray(y_pred, dtype=float)
    denom = float(np.sum(np.abs(y_true_arr)))
    if denom <= 0:
        return 0.0
    return float(np.sum(np.abs(y_pred_arr - y_true_arr)) / denom)


def evaluate(y_true, y_pred, prefix: str) -> dict[str, float]:
    metrics = {
        f"{prefix}_bias_rate": bias_rate(y_true, y_pred),
        f"{prefix}_badcase": badcase(y_true, y_pred),
    }
    logging.info("%s metrics: %s", prefix, metrics)
    return metrics
