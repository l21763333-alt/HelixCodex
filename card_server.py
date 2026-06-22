#!/usr/bin/env python3
"""
card_server.py — 飞书卡片回调 HTTP 服务器

接收飞书卡片按钮点击事件，写入决策队列供 feishu_review() 消费。

用法:
  python card_server.py --port 8080
  python card_server.py --port 8080 --path /feishu/card

飞书后台配置:
  机器人 → 消息卡片请求网址 → http://<服务器IP>:8080/feishu/card

工作原理:
  用户点击卡片按钮 [KEEP]
    → 飞书 POST → http://server:8080/feishu/card
      → 服务器验证 token → 解析 action → 写入 runs/decisions.jsonl
        → feishu_review() 轮询此文件 → 返回 (decision, supplement)

零外部依赖 — 纯 Python 标准库。
"""

from __future__ import annotations

import json
import argparse
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DECISIONS_LOG = PROJECT_ROOT / "runs" / "feishu_card_decisions.jsonl"


class CardCallbackHandler(BaseHTTPRequestHandler):
    """处理飞书卡片回调 POST 请求"""

    verification_token: str = ""
    log_path: Path = DECISIONS_LOG

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, {"error": "invalid json"})
            return

        # ── URL 验证 (飞书首次配置时会发 challenge) ──
        if payload.get("type") == "url_verification":
            token = payload.get("token", "")
            if self.verification_token and token == self.verification_token:
                challenge = payload.get("challenge", "")
                self._respond(200, {"challenge": challenge})
                print(f"[Card] URL 验证成功")
            else:
                self._respond(403, {"error": "token mismatch"})
            return

        # ── 卡片按钮回调 ──
        action = payload.get("action", {})
        value = action.get("value", {}) if isinstance(action, dict) else {}
        form = action.get("form_value", {}) if isinstance(action, dict) else {}

        command = value.get("command", "") if isinstance(value, dict) else ""
        trial_id = value.get("trial_id", "") if isinstance(value, dict) else ""
        suggestion = str(form.get("suggestion", "") or "").strip() if isinstance(form, dict) else ""

        decision_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "command": command,
            "trial_id": trial_id,
            "suggestion": suggestion,
        }

        # 写入决策队列
        DecisionQueue.append(decision_entry)
        print(f"[Card] 收到: {command} trial={trial_id} suggestion={suggestion[:60] if suggestion else '-'}")

        self._respond(200, {"ok": True})

    def _respond(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 静默默认日志


# ═══════════════════════════════════════════════════════════
# 决策队列
# ═══════════════════════════════════════════════════════════

class _DecisionQueue:
    """线程安全的决策队列 (文件 + 内存双写)"""

    def __init__(self, log_path: Path):
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: list[dict] = []
        self._last_read: int = 0

    def append(self, entry: dict) -> None:
        self._items.append(entry)
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def poll(self) -> dict | None:
        """获取并移除队列头部 (非阻塞)"""
        if self._last_read < len(self._items):
            item = self._items[self._last_read]
            self._last_read += 1
            return item
        return None

    def wait(self, timeout: int = 600, poll_interval: int = 2) -> dict | None:
        """阻塞等待新决策 (类似 wait_for_new_message)"""
        deadline = float("inf") if timeout <= 0 else time.time() + timeout
        while time.time() < deadline:
            item = self.poll()
            if item:
                return item
            time.sleep(poll_interval)
        return None


DecisionQueue = _DecisionQueue(DECISIONS_LOG)


# ═══════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="飞书卡片回调服务器")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--path", default="/feishu/card", help="回调路径")
    parser.add_argument("--token", default="", help="Verification Token (默认从 flow_config.yaml 读取)")
    args = parser.parse_args()

    # 从配置读取 verification_token
    token = args.token
    if not token:
        try:
            from config import get_config
            token = get_config().feishu.verification_token
        except Exception:
            pass

    if not token:
        print("[Card] ⚠️ verification_token 未设置 — URL 验证将失败")
        print("[Card] 在 flow_config.yaml 的 feishu.verification_token 中设置")
    else:
        print(f"[Card] verification_token: {'***' if token else '(空)'}")

    CardCallbackHandler.verification_token = token
    DecisionQueue._log_path.parent.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(("0.0.0.0", args.port), CardCallbackHandler)
    url = f"http://<服务器IP>:{args.port}{args.path}"

    print(f"[Card] 回调服务器启动: 0.0.0.0:{args.port}{args.path}")
    print(f"[Card] 飞书后台配置 → 消息卡片请求网址 → {url}")
    print(f"[Card] 决策队列: {DecisionQueue._log_path}")
    print(f"[Card] Ctrl+C 停止")

    def shutdown(sig, frame):
        print("\n[Card] 正在停止...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
