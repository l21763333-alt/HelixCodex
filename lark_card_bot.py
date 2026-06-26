#!/usr/bin/env python3
"""
Minimal Feishu card callback server for Codex Flow review cards.

Run:
  python lark_card_bot.py --host 0.0.0.0 --port 8787

Set the public callback URL in Feishu Developer Console to:
  https://<public-host>/feishu/card
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest, RawResponse
except ImportError:  # SDK callback handling is optional; manual fallback remains available.
    lark = None
    RawRequest = None
    RawResponse = None

from config import get_config
from lark_notify import parse_feishu_card_action, send_review_card_text


PROJECT_ROOT = Path(__file__).resolve().parent
ACTION_LOG = PROJECT_ROOT / "runs" / "feishu_card_actions.jsonl"
CALLBACK_PATH = "/feishu/card"
_sdk_card_handler_cache: dict[str, Any] = {"key": None, "handler": None}
TEST_CARD = """🔬 **实验完成: trial_034**

**决策建议: KEEP** | 来源: auto

**核心指标 (package_detail)**
| 指标 | Baseline | New | Delta |
|------|----------|-----|-------|
| WAPE | 0.7009 | 0.6429 | +0.0580 |
| Bias | +0.1695 | +0.0837 | -0.0858 |

**主要改动**
1. 启用 T+2 可售性窗口特征
2. 启用日历峰值节假日特征
3. 目标函数切换为 Tweedie count objective
4. 启用验证集分组校准
5. 菜品分摊策略改为 weighted

**⚠ 风险**
- 校准可能掩盖上游特征质量问题
- 可售性字段缺失可能造成错误判断
"""


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _raw_response(handler: BaseHTTPRequestHandler, response: Any) -> None:
    content = response.content or b""
    handler.send_response(response.status_code or 200)
    for key, value in (response.headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def _extract_event(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("event"), dict):
        return payload["event"]
    if isinstance(payload.get("action"), dict):
        return payload
    return payload


def _payload_token(payload: dict[str, Any]) -> str | None:
    token = payload.get("token") or payload.get("verification_token")
    if not token and isinstance(payload.get("header"), dict):
        token = payload["header"].get("token")
    return token


def _verify_token(payload: dict[str, Any], expected: str | None = None) -> bool:
    if expected is None:
        expected = get_config().feishu.verification_token
    if not expected:
        return True
    return _payload_token(payload) == expected


def _append_action(event: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_feishu_card_action(event)
    parsed["received_at"] = time.time()
    parsed["operator"] = event.get("operator") or event.get("user_id") or {}

    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(parsed, ensure_ascii=False) + "\n")
    return parsed


def _toast(parsed: dict[str, Any]) -> dict[str, Any]:
    action = str(parsed.get("action", "supplement")).upper()
    supplement = parsed.get("supplement")
    text = f"已收到: {action}"
    if supplement:
        text += f" | 建议: {str(supplement)[:80]}"
    return {"toast": {"type": "success", "content": text}}


def handle_callback(payload: dict[str, Any]) -> tuple[int, dict]:
    if payload.get("type") == "url_verification" or "challenge" in payload:
        if not _verify_token(payload):
            return 403, {"msg": "invalid verification token"}
        return 200, {"challenge": payload.get("challenge", "")}

    if not _verify_token(payload):
        return 403, {"msg": "invalid verification token"}

    parsed = _append_action(_extract_event(payload))
    return 200, _toast(parsed)


def _event_from_sdk_card(card: Any) -> dict[str, Any]:
    action = getattr(card, "action", None)
    value = getattr(action, "value", {}) or {}
    form_value = getattr(action, "form_value", {}) or {}
    event = {
        "action": {
            "value": value,
            "form_value": form_value,
        },
        "operator": {
            "open_id": getattr(card, "open_id", None),
            "user_id": getattr(card, "user_id", None),
        },
    }
    if getattr(card, "open_chat_id", None):
        event["open_chat_id"] = card.open_chat_id
    if getattr(card, "open_message_id", None):
        event["open_message_id"] = card.open_message_id
    return event


def _process_sdk_card(card: Any) -> dict[str, Any]:
    parsed = _append_action(_event_from_sdk_card(card))
    return _toast(parsed)


def _sdk_card_handler() -> Any | None:
    if lark is None or RawRequest is None:
        return None
    cfg = get_config().feishu
    token = cfg.verification_token
    if not token:
        return None
    encrypt_key = getattr(cfg, "encrypt_key", "") or ""
    key = (token, encrypt_key)
    if _sdk_card_handler_cache.get("key") == key and _sdk_card_handler_cache.get("handler") is not None:
        return _sdk_card_handler_cache["handler"]
    handler = lark.CardActionHandler.builder(encrypt_key, token).register(_process_sdk_card).build()
    _sdk_card_handler_cache["key"] = key
    _sdk_card_handler_cache["handler"] = handler
    return handler


def _raw_request(uri: str, headers: dict[str, str], body: bytes) -> Any:
    request = RawRequest()
    request.uri = uri
    request.headers = headers
    request.body = body
    return request


def _has_lark_signature(headers: dict[str, str]) -> bool:
    return any(key.lower() == "x-lark-signature" for key in headers)


def _should_use_sdk(payload: dict[str, Any], headers: dict[str, str]) -> bool:
    if _sdk_card_handler() is None:
        return False
    return (
        payload.get("type") == "url_verification"
        or "encrypt" in payload
        or _has_lark_signature(headers)
    )


class CardBotHandler(BaseHTTPRequestHandler):
    callback_path = CALLBACK_PATH

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            _json_response(self, 200, {"ok": True})
            return
        _json_response(self, 404, {"ok": False, "msg": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != self.callback_path:
            _json_response(self, 404, {"ok": False, "msg": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            _json_response(self, 400, {"msg": f"invalid json: {exc}"})
            return

        headers = {key: value for key, value in self.headers.items()}
        sdk_handler = _sdk_card_handler()
        if sdk_handler is not None and _should_use_sdk(payload, headers):
            response = sdk_handler.do(_raw_request(self.path, headers, body))
            _raw_response(self, response)
            return

        status, data = handle_callback(payload)
        _json_response(self, status, data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[CardBot] {self.address_string()} {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--path", default=CALLBACK_PATH)
    parser.add_argument("--send-test-card", action="store_true")
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()

    if args.send_test_card:
        ok = send_review_card_text(TEST_CARD, "trial_034", args.chat_id or None)
        print(f"send_test_card={ok}")
        return 0 if ok else 1

    CardBotHandler.callback_path = args.path
    server = ThreadingHTTPServer((args.host, args.port), CardBotHandler)
    print(f"[CardBot] listening on http://{args.host}:{args.port}{args.path}")
    print(f"[CardBot] action log: {ACTION_LOG.resolve()}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
