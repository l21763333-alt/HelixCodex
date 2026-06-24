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

from config import get_config
from lark_notify import parse_feishu_card_action, send_review_card_text


PROJECT_ROOT = Path(__file__).resolve().parent
ACTION_LOG = PROJECT_ROOT / "runs" / "feishu_card_actions.jsonl"
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


def _extract_event(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("event"), dict):
        return payload["event"]
    if isinstance(payload.get("action"), dict):
        return payload
    return payload


def _verify_token(payload: dict[str, Any]) -> bool:
    expected = get_config().feishu.verification_token
    if not expected:
        return True
    token = payload.get("token") or payload.get("verification_token")
    if not token and isinstance(payload.get("header"), dict):
        token = payload["header"].get("token")
    return token == expected


def handle_callback(payload: dict[str, Any]) -> tuple[int, dict]:
    if "challenge" in payload:
        return 200, {"challenge": payload["challenge"]}

    if not _verify_token(payload):
        return 403, {"msg": "invalid verification token"}

    event = _extract_event(payload)
    parsed = parse_feishu_card_action(event)
    parsed["received_at"] = time.time()
    parsed["operator"] = event.get("operator") or event.get("user_id") or {}

    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(parsed, ensure_ascii=False) + "\n")

    action = parsed.get("action", "supplement")
    supplement = parsed.get("supplement")
    text = f"已收到: {action.upper()}"
    if supplement:
        text += f" | 建议: {supplement[:80]}"

    return 200, {
        "toast": {
            "type": "success",
            "content": text,
        }
    }


class CardBotHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            _json_response(self, 200, {"ok": True})
            return
        _json_response(self, 404, {"ok": False, "msg": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            _json_response(self, 400, {"msg": f"invalid json: {exc}"})
            return

        status, data = handle_callback(payload)
        _json_response(self, status, data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[CardBot] {self.address_string()} {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--send-test-card", action="store_true")
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()

    if args.send_test_card:
        ok = send_review_card_text(TEST_CARD, "trial_034", args.chat_id or None)
        print(f"send_test_card={ok}")
        return 0 if ok else 1

    server = ThreadingHTTPServer((args.host, args.port), CardBotHandler)
    print(f"[CardBot] listening on http://{args.host}:{args.port}/feishu/card")
    print(f"[CardBot] action log: {ACTION_LOG.resolve()}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
