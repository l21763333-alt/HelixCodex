#!/usr/bin/env python3
"""
lark_notify.py — 飞书通知 + 人审交互 (直接 HTTP API, 零外部依赖)

  发送:  send_text / send_markdown / notify_*
  接收:  wait_for_new_message / feishu_review
  解析:  parse_feishu_command → keep | reverse | rollback | stop | status | supplement

凭据从 flow_config.yaml 读取。支持环境变量 FEISHU_APP_SECRET。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path

from config import get_config

# ── API 端点 ────────────────────────────────────────────
FS_BASE        = "https://open.feishu.cn/open-apis"
FS_TOKEN_URL   = f"{FS_BASE}/auth/v3/tenant_access_token/internal"
FS_SEND_URL    = f"{FS_BASE}/im/v1/messages"
FS_MSG_CONTENT = f"{FS_BASE}/im/v1/messages/{{message_id}}"
FS_MSG_LIST    = f"{FS_BASE}/im/v1/messages"
CARD_ACTION_LOG = Path("runs") / "feishu_card_actions.jsonl"

# ── Token 缓存 ──────────────────────────────────────────
_token_cache: dict = {"token": "", "expires_at": 0.0}


def _get_tenant_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    cfg = get_config().feishu
    if not cfg.app_id or not cfg.app_secret:
        raise RuntimeError("feishu.app_id 或 app_secret 未配置")
    body = json.dumps({"app_id": cfg.app_id, "app_secret": cfg.app_secret}).encode()
    req = urllib.request.Request(FS_TOKEN_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 token 获取失败: {data.get('msg', data)}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200)
    return _token_cache["token"]


def _feishu_request(method: str, url: str, body: dict | None = None,
                    timeout: int = 15) -> dict:
    try:
        token = _get_tenant_token()
    except RuntimeError as e:
        return {"code": -1, "msg": str(e)}
    headers = {"Content-Type": "application/json; charset=utf-8",
               "Authorization": f"Bearer {token}"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": e.read().decode(errors="replace")[:300]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ═══════════════════════════════════════════════════════════
# 消息发送
# ═══════════════════════════════════════════════════════════

def _chat_id() -> str:
    return get_config().feishu.chat_id


def _send(msg_type: str, content: str, cid: str | None = None) -> bool:
    cid = cid or _chat_id()
    if not cid:
        return False
    body = {"receive_id": cid, "msg_type": msg_type, "content": content}
    rid_type = "chat_id" if cid.startswith("oc_") else "user_id"
    r = _feishu_request("POST", f"{FS_SEND_URL}?receive_id_type={rid_type}", body)
    if r.get("code") != 0:
        print(f"[Lark] 发送失败: {r.get('msg', '')}")
        return False
    return True


def send_text(text: str, chat_id: str | None = None) -> bool:
    return _send("text", json.dumps({"text": text}, ensure_ascii=False), chat_id)


def send_markdown(md: str, chat_id: str | None = None) -> bool:
    content = json.dumps({"zh_cn": {"title": "",
        "content": [[{"tag": "md", "text": md}]]}}, ensure_ascii=False)
    return _send("post", content, chat_id)


def send_interactive_card(card: dict, chat_id: str | None = None) -> bool:
    """Send a Feishu interactive card."""
    return _send("interactive", json.dumps(card, ensure_ascii=False), chat_id)


def build_review_card(card_text: str, trial_id: str = "") -> dict:
    """Build the human-review card used by the card bot."""
    title = f"实验完成: {trial_id}" if trial_id else "实验完成"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "markdown", "content": card_text},
            {"tag": "hr"},
            {
                "tag": "input",
                "name": "suggestion",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "<建议>: 例如 调小校准强度并重跑",
                },
                "default_value": "",
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "KEEP"},
                        "type": "primary",
                        "value": {"command": "/keep", "trial_id": trial_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "ROLLBACK"},
                        "type": "danger",
                        "value": {"command": "/rollback", "trial_id": trial_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "REVISE"},
                        "type": "default",
                        "value": {"command": "/revise", "trial_id": trial_id},
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": "也可回复: /keep /rollback /revise <建议> /branch A;B /stop",
                }],
            },
        ],
    }


def send_review_card_text(card_text: str, trial_id: str = "",
                          chat_id: str | None = None) -> bool:
    """Send a T5 review summary as an interactive card."""
    ok = send_interactive_card(build_review_card(card_text, trial_id), chat_id)
    if ok:
        return True
    print("[Lark] interactive card failed, fallback to markdown")
    return send_markdown(card_text, chat_id)


def send_review_card(trial_output: str, chat_id: str | None = None) -> bool:
    """Read feishu_review_card.md from a trial directory and send it."""
    path = Path(trial_output) / "feishu_review_card.md"
    if not path.exists():
        print(f"[Lark] card file not found: {path}")
        return False
    return send_review_card_text(path.read_text(encoding="utf-8"),
                                 Path(trial_output).name, chat_id)


def build_trial_review_text(trial_id: str, comparison: dict,
                            auto_suggestion: str = "keep") -> str:
    """Build a compact review-card markdown from metric comparison."""
    p = comparison.get("primary", {})
    s = comparison.get("secondary", {})
    return "\n".join([
        f"🔬 **实验完成: {trial_id}**",
        "",
        f"**决策建议: {auto_suggestion.upper()}** | 来源: auto",
        "",
        "**核心指标 (package_detail)**",
        "| 指标 | Baseline | New | Delta |",
        "|------|----------|-----|-------|",
        f"| WAPE | {p.get('old_wape', 999):.4f} | {p.get('new_wape', 999):.4f} | {comparison.get('wape_delta', 0):+.4f} |",
        f"| Bias | {p.get('old_bias', 0):+.4f} | {p.get('new_bias', 0):+.4f} | {comparison.get('bias_delta', 0):+.4f} |",
        "",
        f"辅助: store_dish_day WAPE {s.get('old_wape', 999):.4f}",
        "",
        "**请点击按钮或回复指令:**",
        "`/keep` `/rollback` `/revise <建议>` `/branch A;B` `/stop`",
    ])


# ═══════════════════════════════════════════════════════════
# 消息接收
# ═══════════════════════════════════════════════════════════

def _get_msg_text(msg_id: str) -> str:
    r = _feishu_request("GET", FS_MSG_CONTENT.format(message_id=msg_id))
    if r.get("code") != 0:
        return ""
    items = r.get("data", {}).get("items", [])
    if not items:
        return ""
    msg = items[0]
    msg_type = msg.get("msg_type", "")
    body = msg.get("body", {})

    # body.content 是 JSON 字符串，需要解析
    raw = body.get("content", "{}")
    try:
        content = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return str(raw)[:200] if raw else ""

    if msg_type == "text":
        return content.get("text", "")
    if msg_type == "post":
        parts = []
        for loc in content.values():
            if isinstance(loc, dict):
                for row in loc.get("content", []):
                    for e in row:
                        if e.get("tag") in ("text", "md"):
                            parts.append(e.get("text", ""))
        return "\n".join(parts) or str(raw)[:200]
    if msg_type in ("image", "file", "audio", "media", "sticker"):
        return f"[{msg_type}]"
    return str(raw)[:200]


def poll_recent_messages(chat_id: str, page_size: int = 10) -> list[dict]:
    url = (f"{FS_MSG_LIST}?container_id_type=chat"
           f"&container_id={chat_id}&page_size={page_size}&sort_type=ByCreateTimeDesc")
    r = _feishu_request("GET", url)
    if r.get("code") != 0:
        return []
    msgs = []
    for item in r.get("data", {}).get("items", []):
        sid = item.get("sender", {}).get("id", "") if isinstance(item.get("sender"), dict) else ""
        text = _get_msg_text(item.get("message_id", ""))
        if text:
            msgs.append({"message_id": item["message_id"], "sender_id": sid,
                         "text": text, "create_time": item.get("create_time", "0")})
    return msgs


def wait_for_new_message(chat_id: str, timeout: int = 600,
                         poll_interval: int = 5,
                         sender_filter: list[str] | None = None) -> dict | None:
    """阻塞等待飞书群新消息。只处理本函数调用之后到达的消息，忽略历史。"""
    if not chat_id:
        return None
    # 时间戳基线: 只接受此时间之后的消息 (毫秒)
    marker_ms = int(time.time() * 1000)
    deadline = float("inf") if timeout <= 0 else time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        for msg in poll_recent_messages(chat_id, 5):
            if int(msg.get("create_time", "0")) <= marker_ms:
                continue  # 历史消息, 跳过
            if sender_filter and msg["sender_id"] not in sender_filter:
                continue
            return msg
    return None


def _read_card_actions(after_ts: float, trial_id: str = "") -> list[dict]:
    if not CARD_ACTION_LOG.exists():
        return []
    actions = []
    try:
        lines = CARD_ACTION_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(item.get("received_at", 0)) + 1 < after_ts:
            continue
        if trial_id and item.get("trial_id") and item.get("trial_id") != trial_id:
            continue
        actions.append(item)
    return actions


def wait_for_review_event(chat_id: str, trial_id: str, timeout: int = 600,
                          poll_interval: int = 5,
                          sender_filter: list[str] | None = None) -> dict | None:
    """等待文本回复或卡片回调。只处理本函数调用之后到达的消息。"""
    if not chat_id:
        return None
    marker_ms = int(time.time() * 1000)  # 时间戳基线
    deadline = float("inf") if timeout <= 0 else time.time() + timeout
    seen_card_keys: set[tuple] = set()

    while time.time() < deadline:
        # 检查卡片回调
        for action in _read_card_actions(time.time(), trial_id):
            key = (action.get("received_at"), action.get("trial_id"),
                   action.get("action"), action.get("supplement"))
            if key in seen_card_keys:
                continue
            seen_card_keys.add(key)
            return {"source": "card", "command": action}

        # 检查文本消息 (只接受 marker 之后的新消息)
        for msg in poll_recent_messages(chat_id, 5):
            if int(msg.get("create_time", "0")) <= marker_ms:
                continue
            if sender_filter and msg["sender_id"] not in sender_filter:
                continue
            return {"source": "message", "message": msg}

        time.sleep(poll_interval)
    return None


def human_review_enabled() -> bool:
    cfg = get_config()
    return cfg.feishu.enabled and cfg.loop.human_review.enabled


# ═══════════════════════════════════════════════════════════
# 命令解析  keep | reverse | rollback | stop | status | supplement
# ═══════════════════════════════════════════════════════════

_RE_KEEP     = re.compile(r'^\s*/?(keep|保留|继续|接受|ok|yes)\s*$', re.I)
_RE_REVERSE  = re.compile(r'^\s*/?(reverse|反向|回溯|回退|放弃|撤销|undo)\s*$', re.I)
_RE_ROLLBACK = re.compile(r'^\s*/?(rollback|重跑|重试|重来|retry|redo)\s*$', re.I)
_RE_REVISE   = re.compile(r'^\s*/?(revise|修改|调整)\s+(.+)$', re.I)
_RE_BRANCH   = re.compile(r'^\s*/?(branch|分支)\s+(.+)$', re.I)
_RE_STOP     = re.compile(r'^\s*/?(stop|停止|结束|终止|退出|exit|quit)\s*$', re.I)
_RE_STATUS   = re.compile(r'^\s*/?(status|状态|进度|汇总)\s*$', re.I)
# "命令 + 补充文本": keep, 试试Tweedie / rollback 换seed / reverse 方向不对 / revise 关注异常
_RE_CMD_SUPP = re.compile(
    r'^\s*/?(keep|保留|继续|reverse|反向|回溯|rollback|重跑|重试|revise|修改|调整|branch|分支|stop|停止)'
    r'[\s,，。.]+(.+)$', re.I)


def parse_feishu_command(text: str) -> dict:
    """解析飞书消息 → {action, supplement}  action ∈ {keep, reverse, rollback, stop, status, supplement}"""
    text = text.strip()
    if not text:
        return {"raw": text, "action": "supplement", "supplement": None}

    # 1. 精确命令
    for regex, action in [(_RE_KEEP, "keep"), (_RE_REVERSE, "reverse"),
                          (_RE_ROLLBACK, "rollback"), (_RE_STOP, "stop"),
                          (_RE_STATUS, "status")]:
        if regex.match(text):
            return {"raw": text, "action": action, "supplement": None}

    # 2. "/revise <建议>" → rollback + 建议注入 Ask
    m = _RE_REVISE.match(text)
    if m:
        return {"raw": text, "action": "rollback", "supplement": m.group(2).strip()}

    # 3. "/branch A;B" → rollback + 分支探索
    m = _RE_BRANCH.match(text)
    if m:
        return {"raw": text, "action": "rollback",
                "supplement": f"分支探索: {m.group(2).strip()}"}

    # 4. "命令 补充文本" (如 "keep 但试试Tweedie loss")
    m = _RE_CMD_SUPP.match(text)
    if m:
        cmd = m.group(1).lower()
        supp = m.group(2).strip()
        if cmd in ("revise", "修改", "调整"):
            return {"raw": text, "action": "rollback", "supplement": supp}
        if cmd in ("branch", "分支"):
            return {"raw": text, "action": "rollback",
                    "supplement": f"分支探索: {supp}"}
        for regex, action in [(_RE_KEEP, "keep"), (_RE_REVERSE, "reverse"),
                              (_RE_ROLLBACK, "rollback"), (_RE_STOP, "stop")]:
            if regex.match(cmd):
                return {"raw": text, "action": action, "supplement": supp}

    # 4. 无法识别 → 人工补充文本, 剥离 /指令 前缀
    text = re.sub(r'^/\w+\s+', '', text).strip()
    return {"raw": text, "action": "supplement", "supplement": text or None}


def parse_feishu_card_action(payload: dict) -> dict:
    """Parse a Feishu card action callback into the command shape."""
    action = payload.get("action", {}) if isinstance(payload, dict) else {}
    value = action.get("value", {}) if isinstance(action, dict) else {}
    form_value = action.get("form_value", {}) if isinstance(action, dict) else {}
    command = value.get("command", "") if isinstance(value, dict) else ""
    suggestion = ""
    if isinstance(form_value, dict):
        suggestion = str(form_value.get("suggestion", "") or "").strip()
    text = f"{command} {suggestion}".strip() if suggestion else command
    parsed = parse_feishu_command(text)
    if isinstance(value, dict):
        parsed["trial_id"] = value.get("trial_id", "")
    parsed["card_action"] = True
    return parsed


# ═══════════════════════════════════════════════════════════
# 结构化通知
# ═══════════════════════════════════════════════════════════

def notify_loop_start(experiment: str, ask: str, max_iter: int,
                      target_wape: float | None, model: str = "gpt-5.5",
                      human_review: bool = False) -> bool:
    return send_markdown(
        f"🚀 **Codex Flow 循环启动**\n\n"
        f"| 参数 | 值 |\n|------|-----|\n"
        f"| 实验 | {experiment} |\n"
        f"| 目标 | {ask} |\n"
        f"| 最大轮次 | {max_iter} |\n"
        f"| 目标 WAPE | {target_wape or '无'} |\n"
        f"| 模型 | {model} |\n"
        f"| 人工审核 | {'✅' if human_review else '❌'} |")


def notify_trial_done(trial_id: str, comparison: dict, ask: str = "",
                      round_num: int = 0, auto_suggestion: str = "keep") -> bool:
    p = comparison.get("primary", {})
    s = comparison.get("secondary", {})
    icon = {"KEEP": "✅", "ROLLBACK": "❌", "REVERSE": "⏪"}.get(
        comparison.get("decision", "?").upper(), "❓")
    lines = [
        f"🔬 **实验完成: {trial_id}**",
        f"**📋 轮次:** Round {round_num}",
    ]
    if ask:
        lines.append(f"**🎯 Ask:** {ask[:300]}{'...' if len(ask) > 300 else ''}")
    lines.extend([
        "",
        f"**📊 指标对比 (package_detail, test 集)**",
        f"| 指标 | Baseline | {trial_id} | Delta |",
        f"|------|----------|------------|-------|",
        f"| WAPE | {p.get('old_wape', 999):.4f} | {p.get('new_wape', 999):.4f} "
        f"| **{comparison.get('wape_delta', 0):+.4f}** |",
        f"| Bias | {p.get('old_bias', 0):+.4f} | {p.get('new_bias', 0):+.4f} "
        f"| {comparison.get('bias_delta', 0):+.4f} |",
        f"| WAPE (dish) | {s.get('old_wape', 999):.4f} | — | — |",
        "",
        f"**{icon} 自动建议: {auto_suggestion.upper()}**",
    ])
    return send_markdown("\n".join(lines))


def notify_trial_prompt(round_num: int, auto_suggestion: str) -> bool:
    timeout = get_config().loop.human_review.timeout
    return send_markdown(
        "---\n\n⌨️ **请回复指令:**\n\n"
        "| 指令 | 说明 |\n|------|------|\n"
        "| **keep** | ✅ 保留此版本，继续下一轮 |\n"
        "| **reverse** | ⏪ 方向不对，回溯到上一轮 |\n"
        "| **rollback** | 🔄 重跑本轮（固定参数） |\n"
        "| **stop** | 🛑 停止实验 |\n"
        "| **status** | 📊 查看进度 |\n\n"
        "💡 也可直接输入补充文本（将注入下一轮 Ask）\n"
        f"⏰ 超时 {timeout}s 后使用自动建议: **{auto_suggestion.upper()}**")


def notify_loop_stop(reason: str, manifests: list) -> bool:
    lines = [f"🛑 **循环停止: {reason}**", "",
             "| Trial | WAPE | Decision |", "|-------|------|----------|"]
    for m in manifests:
        mp = Path(str(m.output_dir)) / "evaluation" / "metric_comparison.json"
        w, d = "?", "?"
        if mp.exists():
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
                w = f"{data.get('primary', {}).get('new_wape', '?'):.4f}"
                d = data.get("decision", "?").upper()
            except Exception:
                pass
        lines.append(f"| {m.trial_id} | {w} | {d} |")
    return send_markdown("\n".join(lines))


def notify_error(error_msg: str, context: str = "") -> bool:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    md = f"❌ **异常 [{ts}]**\n\n"
    if context:
        md += f"**上下文**: {context}\n\n"
    md += f"```\n{str(error_msg)[:1500]}\n```"
    return send_markdown(md)


def notify_command_result(decision: str, supplement: str | None,
                          next_round: int) -> bool:
    name = {"keep": "✅ 保留版本，继续前进",
            "reverse": "⏪ 回溯到上一轮",
            "rollback": "🔄 重跑本轮",
            "stop": "🛑 停止实验"}.get(decision, f"❓ {decision}")
    lines = [f"**收到指令: {name}**", f"▶ 下一轮: Round {next_round}"]
    if supplement:
        lines.append(f"📝 补充已注入: \"{supplement[:200]}\"")
    return send_markdown("\n".join(lines))


from pathlib import Path  # noqa: E402 (used in notify_loop_stop)

# ═══════════════════════════════════════════════════════════
# 人审交互主入口
# ═══════════════════════════════════════════════════════════

def feishu_review(trial_id: str, ask: str, comparison: dict,
                  round_num: int = 0, auto_suggestion: str = "keep") \
        -> tuple[str, str | None]:
    """
    发送实验结果到飞书 → 阻塞等人工指令 → 返回 (decision, supplement)。

    decision ∈ {keep, reverse, rollback, stop}
    飞书不可用/超时 → 返回 (auto_suggestion, None)
    """
    cfg = get_config()
    if not cfg.feishu.enabled or not cfg.loop.human_review.enabled:
        return (auto_suggestion, None)
    chat_id = cfg.feishu.chat_id
    if not chat_id:
        return (auto_suggestion, None)

    # 1. 发送结果
    if not notify_trial_done(trial_id, comparison, ask, round_num, auto_suggestion):
        return (auto_suggestion, None)
    send_review_card_text(
        build_trial_review_text(trial_id, comparison, auto_suggestion),
        trial_id,
        chat_id,
    )

    # 2. 发送提示 + 等待回复
    notify_trial_prompt(round_num, auto_suggestion)
    hr = cfg.loop.human_review
    auth = hr.authorized_senders or None
    event = wait_for_review_event(chat_id, trial_id, hr.timeout,
                                  cfg.feishu.poll_interval, auth)

    # 3. 超时处理
    if event is None:
        if hr.auto_fallback:
            send_markdown(f"⏰ 超时 ({hr.timeout}s), 使用自动建议: **{auto_suggestion.upper()}**")
            return (auto_suggestion, None)
        send_markdown("⏰ 超时, 实验暂停。回复指令继续。")
        event = wait_for_review_event(chat_id, trial_id, 0,
                                      cfg.feishu.poll_interval, auth)
        if event is None:
            return ("stop", None)

    # 4. 解析
    if event.get("source") == "card":
        cmd = event["command"]
    else:
        cmd = parse_feishu_command(event["message"]["text"])

    # status → 继续等
    if cmd["action"] == "status":
        send_markdown(f"📊 Round {round_num} | Trial {trial_id} | "
                      f"建议: {auto_suggestion.upper()}\n请回复指令。")
        event = wait_for_review_event(chat_id, trial_id, 300 if hr.timeout else 0,
                                      cfg.feishu.poll_interval, auth)
        if event is None:
            return (auto_suggestion, None)
        if event.get("source") == "card":
            cmd = event["command"]
        else:
            cmd = parse_feishu_command(event["message"]["text"])

    # supplement 纯文本 → 追问确认
    if cmd["action"] == "supplement" and cmd["supplement"]:
        supp = cmd["supplement"]
        send_markdown(f"📝 收到补充: \"{supp[:300]}\"\n\n请确认: **keep** | **reverse** | **rollback** | **stop**")
        event = wait_for_review_event(chat_id, trial_id, 300 if hr.timeout else 0,
                                      cfg.feishu.poll_interval, auth)
        if event is None:
            return (auto_suggestion, supp)
        if event.get("source") == "card":
            cmd2 = event["command"]
        else:
            cmd2 = parse_feishu_command(event["message"]["text"])
        if cmd2["supplement"]:
            supp = f"{supp}\n{cmd2['supplement']}"
        decision = cmd2["action"] if cmd2["action"] != "supplement" else auto_suggestion
        return (decision, supp)

    decision = cmd["action"] if cmd["action"] != "supplement" else auto_suggestion
    return (decision, cmd["supplement"] or cmd["raw"])
