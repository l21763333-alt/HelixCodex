#!/usr/bin/env python3
"""
card_server.py - Feishu card callback HTTP server.

This is a compatibility entrypoint for the same callback contract used by
lark_card_bot.py:

  Feishu POST /feishu/card
    -> parse card action payload
    -> append runs/feishu_card_actions.jsonl
    -> lark_notify.wait_for_review_event() consumes the action

Run:
  python card_server.py --host 0.0.0.0 --port 8787 --path /feishu/card
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from lark_notify import parse_feishu_card_action


PROJECT_ROOT = Path(__file__).resolve().parent
ACTIONS_LOG = PROJECT_ROOT / "runs" / "feishu_card_actions.jsonl"
DECISIONS_LOG = ACTIONS_LOG  # Backward-compatible name for older imports.
CALLBACK_PATH = "/feishu/card"


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


def _payload_token(payload: dict[str, Any]) -> str | None:
    token = payload.get("token") or payload.get("verification_token")
    if not token and isinstance(payload.get("header"), dict):
        token = payload["header"].get("token")
    return token


def _verify_token(payload: dict[str, Any], expected: str = "") -> bool:
    if not expected:
        return True
    return _payload_token(payload) == expected


def _toast(parsed: dict[str, Any]) -> dict[str, Any]:
    action = parsed.get("action", "supplement")
    supplement = parsed.get("supplement")
    text = f"已收到: {str(action).upper()}"
    if supplement:
        text += f" | 建议: {str(supplement)[:80]}"
    return {"toast": {"type": "success", "content": text}}


class _DecisionQueue:
    """Tiny JSONL queue used by wait_for_review_event via the action log file."""

    def __init__(self, log_path: Path):
        self._log_path = Path(log_path)
        self._items: list[dict] = []
        self._last_read: int = 0

    def append(self, entry: dict) -> None:
        self._items.append(entry)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def poll(self) -> dict | None:
        if self._last_read < len(self._items):
            item = self._items[self._last_read]
            self._last_read += 1
            return item
        return None

    def wait(self, timeout: int = 600, poll_interval: int = 2) -> dict | None:
        deadline = float("inf") if timeout <= 0 else time.time() + timeout
        while time.time() < deadline:
            item = self.poll()
            if item:
                return item
            time.sleep(poll_interval)
        return None


DecisionQueue = _DecisionQueue(ACTIONS_LOG)


def handle_callback(payload: dict[str, Any], verification_token: str = "") -> tuple[int, dict]:
    if payload.get("type") == "url_verification" or "challenge" in payload:
        if not _verify_token(payload, verification_token):
            return 403, {"msg": "invalid verification token"}
        return 200, {"challenge": payload.get("challenge", "")}

    if not _verify_token(payload, verification_token):
        return 403, {"msg": "invalid verification token"}

    event = _extract_event(payload)
    parsed = parse_feishu_card_action(event)
    parsed["received_at"] = time.time()
    parsed["operator"] = event.get("operator") or event.get("user_id") or {}

    DecisionQueue.append(parsed)
    print(
        "[Card] received: "
        f"action={parsed.get('action')} trial={parsed.get('trial_id', '')} "
        f"supplement={str(parsed.get('supplement') or '-')[:60]}"
    )
    return 200, _toast(parsed)


class CardCallbackHandler(BaseHTTPRequestHandler):
    """Handle Feishu URL verification and card action callbacks."""

    verification_token: str = ""
    callback_path: str = CALLBACK_PATH

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
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            _json_response(self, 400, {"msg": f"invalid json: {exc}"})
            return

        status, data = handle_callback(payload, self.verification_token)
        _json_response(self, status, data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[Card] {self.address_string()} {fmt % args}")


def _load_verification_token() -> str:
    try:
        from config import get_config

        return get_config().feishu.verification_token
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu card callback server")
    parser.add_argument("--host", default="0.0.0.0", help="listen host")
    parser.add_argument("--port", type=int, default=8787, help="listen port")
    parser.add_argument("--path", default=CALLBACK_PATH, help="callback path")
    parser.add_argument("--token", default="", help="Verification Token; defaults to FEISHU_VERIFICATION_TOKEN/config")
    args = parser.parse_args()

    token = args.token or _load_verification_token()
    if not token:
        print("[Card] WARNING: verification_token is not set; callback token checks are disabled")
    else:
        print("[Card] verification_token: ***")

    CardCallbackHandler.verification_token = token
    CardCallbackHandler.callback_path = args.path

    server = HTTPServer((args.host, args.port), CardCallbackHandler)
    print(f"[Card] listening on http://{args.host}:{args.port}{args.path}")
    print(f"[Card] action log: {DecisionQueue._log_path.resolve()}")
    print("[Card] configure Feishu callback URL as: https://<public-host>" + args.path)
    print("[Card] Ctrl+C to stop")

    def shutdown(sig, frame):
        print("\n[Card] stopping...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
