from __future__ import annotations

import argparse
import sys
from pathlib import Path


DIAGNOSTIC_NOTICE = "由于缺少 prediction 或 actual，本次无法重新计算 WAPE/MAPE/Bias。"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--mode", choices=["full", "diagnostic"], required=True)
    args = parser.parse_args()
    text = Path(args.report).read_text(encoding="utf-8")
    errors = []
    forbidden_baseline_claims = ["优于 baseline", "低于 baseline", "baseline_wape"]
    for phrase in forbidden_baseline_claims:
        if phrase in text:
            errors.append(f"Report must not contain baseline comparison claim: {phrase}")
    if args.mode == "diagnostic":
        if DIAGNOSTIC_NOTICE not in text:
            errors.append("Diagnostic report missing limitation notice")
        for phrase in ["模型效果很好", "模型效果很差", "可以上线"]:
            if phrase in text:
                errors.append(f"Forbidden diagnostic phrase: {phrase}")
    if errors:
        print("\n".join(errors))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
