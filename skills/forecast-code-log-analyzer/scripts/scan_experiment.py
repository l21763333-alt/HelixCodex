from __future__ import annotations

from pathlib import Path
from typing import Any

from common import relpath


CODE_EXTS = {".py", ".ipynb", ".sql", ".md"}
CONFIG_NAMES = {"config.yaml", "config.yml", "params.yaml", "model_config.yaml", "feature_config.yaml"}
CONFIG_EXTS = {".yaml", ".yml", ".json"}
LOG_NAMES = {"train.log", "eval.log", "training.log", "pipeline.log"}
DATA_EXTS = {".csv", ".parquet", ".xlsx"}
ENTRYPOINT_NAMES = {"main.py", "train.py", "run.py", "evaluate.py", "eval.py", "run_train.py"}
IGNORED_DIR_PARTS = {
    ".git",
    ".comboscope_backups",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".python_packages",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "archive",
    "backup",
    "backups",
    "node_modules",
}
RELEVANT_DIR_KEYWORDS = (
    "src",
    "script",
    "notebook",
    "code",
    "train",
    "model",
    "pipeline",
    "config",
    "conf",
    "data",
    "input",
    "output",
    "result",
    "prediction",
    "forecast",
    "eval",
    "metric",
    "log",
    "audit",
)
FORECAST_HINTS = (
    "forecast",
    "predict",
    "prediction",
    "actual",
    "label",
    "truth",
    "sales",
    "demand",
    "train",
    "evaluate",
    "metric",
    "wape",
    "mape",
    "rmse",
    "bias",
    "预测",
    "销量",
    "需求",
    "真实",
    "标签",
    "回测",
    "训练",
    "评估",
    "指标",
    "误差",
    "特征",
    "模型",
)
PREDICTION_HINTS = ("prediction", "pred", "forecast", "yhat", "forecast_value", "predict_value", "预测")
ACTUAL_HINTS = ("actual", "label", "truth", "ground", "target", "real", "sales", "sale_qty", "真实", "标签", "销量")
METRIC_HINTS = ("metrics", "metric", "eval", "result", "wape", "mape", "rmse", "mae", "bias", "指标", "评估", "误差")
MAX_FILES_PER_DIR = 500
MAX_HEADER_FILES_PER_DIR = 8
MAX_SCAN_DEPTH = 3
MAX_PARENT_FALLBACKS = 2


def _contains(text: str, needles: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle in lower for needle in needles)


def _is_ignored(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(part in IGNORED_DIR_PARTS or part.startswith("_archive") for part in rel_parts)


def _read_head(path: Path, line_count: int = 8, byte_limit: int = 4096) -> str:
    try:
        raw = path.open("rb").read(byte_limit)
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="ignore")
    return "\n".join(text.splitlines()[:line_count]).lower()


def _relative_depth(path: Path, root: Path) -> int:
    return len(path.relative_to(root).parts) - 1


def _dir_name_is_relevant(path: Path) -> bool:
    return _contains(path.name, RELEVANT_DIR_KEYWORDS)


def _dir_header_is_relevant(path: Path) -> bool:
    checked = 0
    for child in sorted(path.iterdir()):
        if checked >= MAX_HEADER_FILES_PER_DIR:
            break
        if child.is_dir() or child.suffix.lower() not in CODE_EXTS | DATA_EXTS | CONFIG_EXTS | {".log"}:
            continue
        checked += 1
        if _contains(child.name, FORECAST_HINTS) or _contains(_read_head(child), FORECAST_HINTS):
            return True
    return False


def _select_scan_dirs(root: Path) -> list[Path]:
    dirs = [root]
    for child in sorted(root.iterdir()):
        if not child.is_dir() or _is_ignored(child, root):
            continue
        if _dir_name_is_relevant(child) or _dir_header_is_relevant(child):
            dirs.append(child)
    return dirs


def _iter_focused_files(root: Path) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for scan_dir in _select_scan_dirs(root):
        collected = 0
        for path in scan_dir.rglob("*"):
            if collected >= MAX_FILES_PER_DIR:
                break
            if not path.is_file() or _is_ignored(path, root):
                continue
            if scan_dir != root and _relative_depth(path, scan_dir) > MAX_SCAN_DEPTH:
                continue
            if scan_dir == root and path.parent != root:
                continue
            suffix = path.suffix.lower()
            if suffix not in CODE_EXTS | CONFIG_EXTS | DATA_EXTS | {".log"} and path.name.lower() not in LOG_NAMES:
                continue
            if path not in seen:
                seen.add(path)
                files.append(path)
                collected += 1
    return files


def _looks_like_entrypoint(path: Path, head: str) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix != ".py":
        return False
    return name in ENTRYPOINT_NAMES or _contains(name, ("pipeline", "train", "model", "forecast", "predict")) or _contains(
        head, ("forecast", "predict", "training entrypoint", "run_pipeline", "fit(", "train", "argparse", "output_dir", "预测", "训练", "主入口")
    )


def _classify_file(path: Path, root: Path, result: dict[str, Any]) -> None:
    name = path.name.lower()
    suffix = path.suffix.lower()
    rel = relpath(path, root)
    head = _read_head(path) if suffix in CODE_EXTS | DATA_EXTS | CONFIG_EXTS | {".log"} else ""

    if suffix in CODE_EXTS:
        result["code_files"].append(rel)
    if name in CONFIG_NAMES or suffix in CONFIG_EXTS:
        result["config_files"].append(rel)
    if suffix == ".log" or name in LOG_NAMES:
        result["log_files"].append(rel)
    if suffix in DATA_EXTS:
        result["data_files"].append(rel)
    if _looks_like_entrypoint(path, head):
        result["possible_entrypoints"].append(rel)

    haystack = f"{name}\n{head}"
    if suffix in DATA_EXTS and _contains(haystack, PREDICTION_HINTS):
        result["candidate_prediction_files"].append(rel)
    if suffix in DATA_EXTS and _contains(haystack, ACTUAL_HINTS):
        result["actual_files"].append(rel)
    if suffix in DATA_EXTS and _contains(haystack, METRIC_HINTS):
        result["metrics_files"].append(rel)
    if suffix in DATA_EXTS and _contains(haystack, FORECAST_HINTS):
        result["domain_artifact_files"].append(rel)


def _empty_result(root: Path, requested: Path, fallback_used: bool) -> dict[str, Any]:
    return {
        "requested_dir": requested.as_posix(),
        "experiment_dir": root.as_posix(),
        "fallback_used": fallback_used,
        "scan_strategy": "focused_dir_and_header_scan",
        "code_files": [],
        "config_files": [],
        "log_files": [],
        "data_files": [],
        "candidate_prediction_files": [],
        "actual_files": [],
        "metrics_files": [],
        "domain_artifact_files": [],
        "possible_entrypoints": [],
        "ignored_dir_parts": sorted(IGNORED_DIR_PARTS),
        "scanned_dirs": [],
        "warnings": [],
    }


def _scan_root(root: Path, requested: Path, fallback_used: bool) -> dict[str, Any]:
    result = _empty_result(root, requested, fallback_used)
    result["scanned_dirs"] = [relpath(path, root) if path != root else "." for path in _select_scan_dirs(root)]
    for path in _iter_focused_files(root):
        _classify_file(path, root, result)
    for key in (
        "code_files",
        "config_files",
        "log_files",
        "data_files",
        "candidate_prediction_files",
        "actual_files",
        "metrics_files",
        "domain_artifact_files",
        "possible_entrypoints",
    ):
        result[key] = sorted(dict.fromkeys(result[key]))
    return result


def _has_minimum_artifacts(result: dict[str, Any]) -> bool:
    return bool(result["code_files"] and result["data_files"] and (result["candidate_prediction_files"] or result["metrics_files"]))


def _add_warnings(result: dict[str, Any]) -> dict[str, Any]:
    if not result["candidate_prediction_files"]:
        result["warnings"].append("prediction artifact not found")
    if not result["actual_files"]:
        result["warnings"].append("actual artifact not found")
    if not result["log_files"]:
        result["warnings"].append("log file not found")
    if not result["code_files"]:
        result["warnings"].append("code file not found")
    if result["fallback_used"]:
        result["warnings"].append("requested directory was too narrow; scanned parent experiment directory")
    return result


def scan_experiment(experiment_dir: str | Path) -> dict[str, Any]:
    requested = Path(experiment_dir).resolve()
    if not requested.exists() or not requested.is_dir():
        raise FileNotFoundError(f"experiment directory does not exist: {requested}")

    candidates = [requested]
    current = requested
    for _ in range(MAX_PARENT_FALLBACKS):
        parent = current.parent
        if parent == current:
            break
        candidates.append(parent)
        current = parent

    best = _scan_root(requested, requested, False)
    if _has_minimum_artifacts(best):
        return _add_warnings(best)
    for candidate in candidates[1:]:
        scanned = _scan_root(candidate, requested, True)
        if _has_minimum_artifacts(scanned):
            return _add_warnings(scanned)
        if len(scanned["code_files"]) + len(scanned["data_files"]) > len(best["code_files"]) + len(best["data_files"]):
            best = scanned
    return _add_warnings(best)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    print(json.dumps(scan_experiment(args.experiment), ensure_ascii=False, indent=2))
