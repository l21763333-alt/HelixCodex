from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import write_json


def _read_json(path: str | Path | None, default: Any) -> Any:
    if not path:
        return default
    target = Path(path)
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def _read_csv_records(path: str | Path | None, limit: int = 20) -> list[dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        import csv
        return list(csv.DictReader(handle))[:limit]


def build_report_context(
    *,
    ask: str,
    mode: str,
    scan_result: dict[str, Any],
    artifact_summary: dict[str, Any],
    code_analysis: dict[str, Any],
    log_summary: dict[str, Any],
    metrics_path: str | Path | None,
    scene_metrics_path: str | Path | None,
    badcases_path: str | Path | None,
    anomaly_summary: dict[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    context = {
        "task": {"ask": ask},
        "mode": mode,
        "scan_result": scan_result,
        "artifact_summary": artifact_summary,
        "code_analysis": code_analysis,
        "log_summary": log_summary,
        "metrics": _read_csv_records(metrics_path),
        "scene_metrics": _read_csv_records(scene_metrics_path),
        "badcases": _read_csv_records(badcases_path),
        "anomaly_summary": anomaly_summary,
    }
    write_json(output_path, context)
    return context


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build report_context.json from forecast skill outputs.")
    parser.add_argument("--ask", required=True)
    parser.add_argument("--mode", choices=["full", "diagnostic"], required=True)
    parser.add_argument("--scan-result")
    parser.add_argument("--artifact-summary")
    parser.add_argument("--code-analysis")
    parser.add_argument("--log-summary")
    parser.add_argument("--metrics")
    parser.add_argument("--scene-metrics")
    parser.add_argument("--badcases")
    parser.add_argument("--anomaly-summary")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    context = build_report_context(
        ask=args.ask,
        mode=args.mode,
        scan_result=_read_json(args.scan_result, {}),
        artifact_summary=_read_json(args.artifact_summary, {}),
        code_analysis=_read_json(args.code_analysis, {}),
        log_summary=_read_json(args.log_summary, {}),
        metrics_path=args.metrics,
        scene_metrics_path=args.scene_metrics,
        badcases_path=args.badcases,
        anomaly_summary=_read_json(args.anomaly_summary, {}),
        output_path=args.output,
    )
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
