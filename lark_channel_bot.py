#!/usr/bin/env python3
"""
Feishu/Lark long-connection receiver for Codex Flow review actions.

Run:
  python lark_channel_bot.py

The process opens the SDK WebSocket channel, receives message/cardAction
events, and appends parsed commands to runs/feishu_card_actions.jsonl. The
existing wait_for_review_event() loop consumes the same file, so loop.py does
not need an inbound HTTP callback URL when this process is running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

try:
    from lark_oapi.channel import Events, FeishuChannel, PolicyConfig
except ImportError:  # Keep imports/test helpers usable when SDK is absent.
    Events = None
    FeishuChannel = None
    PolicyConfig = None

from config import get_config, get_paths
from lark_notify import parse_feishu_card_action, parse_feishu_command, send_review_card_text


ACTION_LOG = get_paths().global_artifact("feishu_action_log")
TEST_CARD = """Review card long-connection smoke test

Suggestion: KEEP

Click a button or reply with /keep, /rollback, or /revise <suggestion>.
"""


def _dict_from_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {"value": value}


def _first_nested_dict(value: Any, keys: set[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, dict):
                return found
        for child in value.values():
            found = _first_nested_dict(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_nested_dict(child, keys)
            if found:
                return found
    return {}


def _operator_dict(operator: Any) -> dict[str, Any]:
    return {
        "open_id": getattr(operator, "open_id", None),
        "user_id": getattr(operator, "user_id", None),
        "name": getattr(operator, "name", None),
    }


def _append_action(action: dict[str, Any]) -> dict[str, Any]:
    entry = dict(action)
    entry.setdefault("received_at", time.time())
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def card_event_to_action(card_event: Any) -> dict[str, Any]:
    action = getattr(card_event, "action", None)
    raw = getattr(card_event, "raw", {}) or {}
    form_value = _first_nested_dict(
        raw,
        {"form_value", "formValue", "input_values", "inputValues"},
    )
    payload = {
        "action": {
            "value": _dict_from_value(getattr(action, "value", None)),
            "form_value": form_value,
        },
        "operator": _operator_dict(getattr(card_event, "operator", None)),
        "open_chat_id": getattr(card_event, "chat_id", ""),
        "open_message_id": getattr(card_event, "message_id", ""),
    }
    parsed = parse_feishu_card_action(payload)
    parsed.update({
        "source": "channel_card",
        "operator": payload["operator"],
        "chat_id": payload["open_chat_id"],
        "message_id": payload["open_message_id"],
    })
    return parsed


def message_event_to_action(message: Any) -> dict[str, Any] | None:
    text = str(getattr(message, "content_text", "") or "").strip()
    if not text:
        return None
    parsed = parse_feishu_command(text)
    parsed.update({
        "source": "channel_message",
        "operator": _operator_dict(getattr(message, "sender", None)),
        "chat_id": getattr(message, "chat_id", ""),
        "message_id": getattr(message, "message_id", ""),
    })
    return parsed


def handle_card_action(card_event: Any) -> dict[str, Any]:
    parsed = _append_action(card_event_to_action(card_event))
    print(
        "[LarkChannel] card action: "
        f"action={parsed.get('action')} trial={parsed.get('trial_id', '')} "
        f"supplement={str(parsed.get('supplement') or '-')[:80]}"
    )
    return parsed


def handle_message(message: Any) -> dict[str, Any] | None:
    parsed = message_event_to_action(message)
    if parsed is None:
        return None
    parsed = _append_action(parsed)
    print(
        "[LarkChannel] message command: "
        f"action={parsed.get('action')} chat={parsed.get('chat_id', '')} "
        f"text={str(parsed.get('raw') or '')[:80]}"
    )
    return parsed


def build_channel(require_mention: bool = False) -> Any:
    if FeishuChannel is None or Events is None or PolicyConfig is None:
        raise RuntimeError("lark-oapi with channel support is not installed")
    cfg = get_config()
    fcfg = cfg.feishu
    if not fcfg.app_id or not fcfg.app_secret:
        raise RuntimeError("please set FEISHU_APP_ID and FEISHU_APP_SECRET")

    policy_kwargs: dict[str, Any] = {"require_mention": require_mention}
    if fcfg.chat_id.startswith("oc_"):
        policy_kwargs.update({
            "group_policy": "allowlist",
            "group_allowlist": [fcfg.chat_id],
        })

    channel = FeishuChannel(
        app_id=fcfg.app_id,
        app_secret=fcfg.app_secret,
        encrypt_key=fcfg.encrypt_key or None,
        verification_token=fcfg.verification_token or None,
        transport="ws",
        policy=PolicyConfig(**policy_kwargs),
    )
    channel.on(Events.MESSAGE, handle_message)
    channel.on(Events.CARD_ACTION, handle_card_action)
    return channel


async def run(require_mention: bool = False) -> None:
    channel = build_channel(require_mention=require_mention)
    print("[LarkChannel] connecting via SDK WebSocket long connection")
    print(f"[LarkChannel] action log: {Path(ACTION_LOG).resolve()}")
    await channel.connect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu SDK long-connection receiver")
    parser.add_argument(
        "--require-mention",
        action="store_true",
        help="only accept group text messages that mention the bot",
    )
    parser.add_argument("--send-test-card", action="store_true")
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()

    if args.send_test_card:
        ok = send_review_card_text(TEST_CARD, "trial_ws_test", args.chat_id or None)
        print(f"send_test_card={ok}")
        return 0 if ok else 1

    asyncio.run(run(require_mention=args.require_mention))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
