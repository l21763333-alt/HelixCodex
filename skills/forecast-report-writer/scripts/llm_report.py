from __future__ import annotations

import json
from typing import Any

from llm_client import LLMClient


def generate_llm_report(
    *,
    context: dict[str, Any],
    fallback_report: str,
    skills_prompt: str,
    llm_client: LLMClient,
) -> tuple[str, str]:
    if not llm_client.available:
        return fallback_report, "fallback"

    instructions = """
你正在编写预测实验评测报告，必须严格遵守 Skills 和 guardrails。
只能使用提供的 report_context 证据。
不要虚构文件路径。
不要修改指标数值。
不要提到 baseline 或对照模型比较。
如果 mode 是 diagnostic，必须包含“由于缺少 prediction 或 actual，本次无法重新计算 WAPE/MAPE/Bias。”这类限制说明，且不得输出确定性模型好坏判断。
只返回 Markdown。
"""
    input_text = f"""
Skills：
{skills_prompt}

报告上下文 JSON：
{json.dumps(context, ensure_ascii=False, indent=2)}
"""
    report = llm_client.complete_text(instructions=instructions, input_text=input_text)
    if not report or "llm_error" in report:
        return fallback_report, "fallback"
    return report, "llm"
