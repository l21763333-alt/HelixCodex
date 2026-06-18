import argparse
from pathlib import Path
import sys

BASE_DIR = Path("/data/zhangxiaotian/baseline")
LOCAL_PACKAGE_DIR = BASE_DIR / ".python_packages"
if str(LOCAL_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_PACKAGE_DIR))

import pandas as pd


KEY_FIELDS = [
    "store_code",
    "package_dish_code",
    "dish_unicode",
    "ds",
    "pos_cnt",
    "real_qty",
    "use_start_date",
    "use_end_date",
    "sold_start_time",
    "sold_ent_time",
    "day_weather_code",
    "day_air_temperature",
    "night_weather_code",
    "night_air_temperature",
]


def read_csv_auto(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input data file not found: {path}")
    try:
        return pd.read_csv(path, encoding="gbk")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="utf-8")


def parse_date_series(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    dt_yyyymmdd = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    dt_general = pd.to_datetime(text, errors="coerce")
    return dt_yyyymmdd.fillna(dt_general)


def print_numeric_check(df: pd.DataFrame, col: str) -> None:
    if col not in df.columns:
        print(f"{col}: MISSING")
        return
    converted = pd.to_numeric(df[col], errors="coerce")
    non_null = df[col].notna()
    failed = non_null & converted.isna()
    print(
        f"{col}: numeric_convert_failed={int(failed.sum())}, "
        f"non_null={int(non_null.sum())}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check baseline input CSV.")
    parser.add_argument(
        "--data_path",
        type=str,
        default=str(BASE_DIR / "data" / "dish_package_feature_df.csv"),
    )
    args = parser.parse_args()

    data_path = Path(args.data_path)
    df = read_csv_auto(data_path)

    print("1. 数据 shape")
    print(df.shape)

    print("\n2. 所有字段名")
    print(list(df.columns))

    print("\n3. 前 5 行")
    with pd.option_context("display.max_columns", None, "display.width", 240):
        print(df.head(5))

    print("\n4. 关键字段是否存在")
    missing = [c for c in KEY_FIELDS if c not in df.columns]
    existing = [c for c in KEY_FIELDS if c in df.columns]
    print(f"existing_key_fields={existing}")
    print(f"missing_key_fields={missing}")

    print("\n5. 关键字段空值数量")
    if existing:
        print(df[existing].isna().sum().sort_index())
    else:
        print("No key fields found.")

    print("\n6. ds 是否能转成日期")
    if "ds" in df.columns:
        ds_dt = parse_date_series(df["ds"])
        print(f"ds_parse_success={int(ds_dt.notna().sum())}")
        print(f"ds_parse_failed={int(ds_dt.isna().sum())}")
        if ds_dt.notna().any():
            print(f"ds_min={ds_dt.min()}")
            print(f"ds_max={ds_dt.max()}")
    else:
        print("ds: MISSING")

    print("\n7. pos_cnt 是否存在、是否全空、唯一值数量")
    if "pos_cnt" in df.columns:
        print("pos_cnt_exists=True")
        print(f"pos_cnt_all_null={bool(df['pos_cnt'].isna().all())}")
        print(f"pos_cnt_unique_count={int(df['pos_cnt'].nunique(dropna=True))}")
    else:
        print("pos_cnt_exists=False")

    print("\n8. real_qty 是否存在、是否全空")
    if "real_qty" in df.columns:
        print("real_qty_exists=True")
        print(f"real_qty_all_null={bool(df['real_qty'].isna().all())}")
    else:
        print("real_qty_exists=False")

    print("\n9. package_dish_code、dish_unicode 是否能转成数值")
    print_numeric_check(df, "package_dish_code")
    print_numeric_check(df, "dish_unicode")

    if missing:
        print(f"\nERROR: 缺少关键字段: {missing}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
