from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_client import LLMClient, extract_json_object, load_llm_config


DEFAULT_TOOL_SEQUENCE = [
    "scan_experiment",
    "discover_artifacts",
    "parse_log",
    "analyze_code",
    "select_mode",
    "calculate_metrics_if_full",
    "tag_scenes_if_full",
    "mine_badcases_if_full",
    "detect_anomalies",
    "build_report_context",
    "write_report",
]


def build_llm_tool_plan(
    *,
    ask: str,
    experiment_dir: str,
    skills_prompt: str,
    llm_client: LLMClient,
) -> dict[str, Any]:
    instructions = (
        "你是 Skill-guided forecast evaluation planner。请读取提供的 Skills，并根据用户 ask 选择需要调用的确定性工具。"
        "只返回 JSON。不要虚构文件路径。不要自己计算指标。"
    )
    input_text = f"""
用户 ask：
{ask}

实验目录：
{experiment_dir}

可用确定性工具：
{json.dumps(DEFAULT_TOOL_SEQUENCE, ensure_ascii=False)}

Skills：
{skills_prompt}

返回 JSON，字段包括：
selected_skills: string[]
tool_plan: string[]
notes: string[]
"""
    text = llm_client.complete_text(instructions=instructions, input_text=input_text)
    parsed = extract_json_object(text)
    if not parsed or "tool_plan" not in parsed:
        return {
            "planner": "fallback",
            "selected_skills": [
                "forecast-task-planner",
                "forecast-experiment-scanner",
                "forecast-code-log-analyzer",
                "forecast-evaluation-analyzer",
                "forecast-badcase-locator",
                "forecast-optimization-advisor",
                "forecast-report-writer",
            ],
            "tool_plan": DEFAULT_TOOL_SEQUENCE,
            "notes": ["LLM planner 不可用，已使用确定性 fallback 工具计划。"],
        }
    parsed["planner"] = "llm"
    return parsed



def main() -> int:
    import argparse
    import json

    from skill_loader import load_skills, render_skills_for_prompt

    parser = argparse.ArgumentParser(description="Build a forecast tool plan with optional LLM fallback.")
    parser.add_argument("--ask", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--skills-dir", default="skills")
    parser.add_argument("--config")
    parser.add_argument("--output")
    args = parser.parse_args()

    skills_prompt = render_skills_for_prompt(load_skills(args.skills_dir))
    client = LLMClient(load_llm_config(args.config))
    plan = build_llm_tool_plan(
        ask=args.ask,
        experiment_dir=args.experiment,
        skills_prompt=skills_prompt,
        llm_client=client,
    )
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
