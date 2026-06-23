from __future__ import annotations

import time
from typing import Any

from config import get_config
from lark_notify import (
    build_trial_review_text,
    notify_error,
    parse_feishu_card_action,
    parse_feishu_command,
    send_markdown,
    send_review_card_text,
    wait_for_review_event,
)

from .schemas import HumanFeedback


def _normalize_decision(action: str) -> str:
    if action == "supplement":
        return "supplement"
    return action or "supplement"


def parse_feedback(raw_text_or_card_payload: Any, trial_id: str = "") -> dict[str, Any]:
    """Return one standard feedback shape for text commands and card callbacks."""
    if isinstance(raw_text_or_card_payload, dict):
        parsed = parse_feishu_card_action(raw_text_or_card_payload)
        return HumanFeedback(
            trial_id=parsed.get("trial_id") or trial_id,
            decision=_normalize_decision(parsed.get("action", "supplement")),
            supplement=parsed.get("supplement"),
            reviewer=str(parsed.get("operator") or parsed.get("user_id") or ""),
            source="card",
            received_at=parsed.get("received_at") or time.time(),
            raw=raw_text_or_card_payload,
        ).to_dict()

    text = str(raw_text_or_card_payload or "")
    parsed = parse_feishu_command(text)
    decision = _normalize_decision(parsed.get("action", "supplement"))
    if decision == "rollback" and parsed.get("raw", "").lstrip().lower().startswith(("/branch", "branch")):
        decision = "branch"
    return HumanFeedback(
        trial_id=trial_id,
        decision=decision,
        supplement=parsed.get("supplement"),
        source="message",
        received_at=time.time(),
        raw=text,
    ).to_dict()


def send_experiment_review(
    trial_id: str,
    comparison: dict[str, Any],
    ask: str,
    model_diff_summary: str = "",
    auto_suggestion: str = "keep",
) -> dict[str, Any]:
    text = build_trial_review_text(trial_id, comparison, auto_suggestion)
    if model_diff_summary:
        text += "\n\n**Model code diff**\n" + model_diff_summary[:3000]
    ok = send_review_card_text(text, trial_id)
    return {"ok": ok, "trial_id": trial_id}


def wait_human_feedback(
    trial_id: str,
    timeout: int | None = None,
    auto_suggestion: str = "keep",
) -> dict[str, Any] | None:
    cfg = get_config()
    chat_id = cfg.feishu.chat_id
    review_cfg = cfg.loop.human_review
    wait_timeout = review_cfg.timeout if timeout is None else timeout
    authorized = review_cfg.authorized_senders or None
    event = wait_for_review_event(
        chat_id,
        trial_id,
        wait_timeout,
        cfg.feishu.poll_interval,
        authorized,
    )
    if event is None:
        if review_cfg.auto_fallback:
            return HumanFeedback(
                trial_id=trial_id,
                decision=auto_suggestion,  # type: ignore[arg-type]
                source="timeout",
                received_at=time.time(),
                raw={"timeout": wait_timeout},
            ).to_dict()
        return None

    if event.get("source") == "card":
        command = event.get("command", {})
        return HumanFeedback(
            trial_id=trial_id,
            decision=_normalize_decision(command.get("action", "supplement")),
            supplement=command.get("supplement"),
            source="card",
            received_at=event.get("received_at") or time.time(),
            raw=event,
        ).to_dict()

    message = event.get("message", {})
    feedback = parse_feedback(message.get("text", ""), trial_id)
    feedback["reviewer"] = message.get("sender_id")
    feedback["raw"] = event
    return feedback


def send_status_update(stage: str, message: str) -> dict[str, Any]:
    ok = send_markdown(f"**{stage}**\n\n{message}")
    return {"ok": ok, "stage": stage}


def send_error_alert(context: str, error: str) -> dict[str, Any]:
    ok = notify_error(error, context)
    return {"ok": ok, "context": context}


def send_final_summary(summary: str) -> dict[str, Any]:
    ok = send_markdown(summary)
    return {"ok": ok}


def feishu_review_via_mcp(
    trial_id: str,
    ask: str,
    comparison: dict[str, Any],
    round_num: int = 0,
    auto_suggestion: str = "keep",
    model_diff_summary: str = "",
) -> tuple[str, str | None]:
    send_experiment_review(trial_id, comparison, ask, model_diff_summary, auto_suggestion)
    send_status_update(
        f"Round {round_num} review",
        f"Reply /keep, /rollback, /reverse, /revise <text>, /branch A;B, or /stop for {trial_id}.",
    )
    feedback = wait_human_feedback(trial_id, None, auto_suggestion)
    if feedback is None:
        return ("stop", None)
    decision = feedback.get("decision") or auto_suggestion
    if decision in ("supplement", "status"):
        decision = auto_suggestion
    if decision == "revise":
        decision = "rollback"
    if decision == "branch":
        decision = "rollback"
    return (str(decision), feedback.get("supplement") or feedback.get("raw"))
