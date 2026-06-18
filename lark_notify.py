#!/usr/bin/env python3
"""
lark_notify.py — 飞书通知模块 (直接 HTTP API, 无需 lark-cli)

通过飞书开放平台 API 发送消息, 零外部依赖。
凭据从 codex_flow_config.json 读取。

API 文档: https://open.feishu.cn/document/server-docs/im-v1/message/create
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from config import get_config

# ── 飞书 API 端点 ──
FS_BASE = "https://open.feishu.cn/open-apis"
FS_TOKEN_URL = f"{FS_BASE}/auth/v3/tenant_access_token/internal"
FS_SEND_URL = f"{FS_BASE}/im/v1/messages"
FS_REPLY_URL = f"{FS_BASE}/im/v1/messages/{{message_id}}/reply"

# ── Token 缓存 ──
_token_cache: dict = {"token": "", "expires_at": 0.0}


def _get_tenant_token() -> str:
    """获取/刷新 tenant_access_token (自动缓存, 2h 有效期)"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    cfg = get_config().feishu
    if not cfg.app_id or not cfg.app_secret:
        raise RuntimeError("feishu.app_id 或 app_secret 未在配置文件中设置")

    body = json.dumps({
        "app_id": cfg.app_id,
        "app_secret": cfg.app_secret,
    }).encode("utf-8")

    req = urllib.request.Request(
        FS_TOKEN_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"飞书 token 请求失败: {e.code} {e.reason}") from e

    if data.get("code") != 0:
        raise RuntimeError(f"飞书 token 获取失败: {data.get('msg', data)}")

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200)
    return _token_cache["token"]


def _get_chat_id() -> str:
    """获取目标群聊 ID (优先 feishu.chat_id, 回退 lark.chat_id)"""
    cfg = get_config()
    return cfg.feishu.chat_id or cfg.lark.chat_id


def _build_post_content(md: str) -> str:
    """将 Markdown 文本包装为飞书 post 格式"""
    return json.dumps({
        "zh_cn": {
            "title": "",
            "content": [[{"tag": "md", "text": md}]],
        }
    }, ensure_ascii=False)


def _build_text_content(text: str) -> str:
    """将纯文本包装为飞书 text 格式"""
    return json.dumps({"text": text}, ensure_ascii=False)


def _send_api(msg_type: str, content: str, chat_id: str | None = None) -> bool:
    """底层: 调用飞书发送消息 API"""
    cid = chat_id or _get_chat_id()
    if not cid:
        print("[Lark] chat_id 未设置, 跳过通知")
        return False

    try:
        token = _get_tenant_token()
    except RuntimeError as e:
        print(f"[Lark] token 获取失败: {e}")
        return False

    body = json.dumps({
        "receive_id": cid,
        "msg_type": msg_type,
        "content": content,
    }, ensure_ascii=False).encode("utf-8")

    # receive_id_type 推断: oc_ 开头是 chat_id, ou_ 开头是 user_id
    receive_id_type = "chat_id" if cid.startswith("oc_") else "user_id"

    req = urllib.request.Request(
        f"{FS_SEND_URL}?receive_id_type={receive_id_type}",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[Lark] API 错误 {e.code}: {err_body[:300]}")
        return False

    if data.get("code") != 0:
        print(f"[Lark] 发送失败: {data.get('msg', data)}")
        return False
    return True


# ── 公开 API ──

def send_text(text: str, chat_id: str | None = None) -> bool:
    """发送纯文本消息"""
    return _send_api("text", _build_text_content(text), chat_id)


def send_markdown(md: str, chat_id: str | None = None) -> bool:
    """发送 Markdown 消息 (自动转为飞书 post+md 格式)"""
    return _send_api("post", _build_post_content(md), chat_id)


# ── 结构化通知 ──

def notify_loop_start(
    experiment: str,
    ask: str,
    max_iter: int,
    target_wape: float | None,
    model: str = "gpt-5.5",
    credits: dict | None = None,
) -> bool:
    """通知循环启动"""
    lines = [
        "🚀 **Codex Flow 循环启动**",
        "",
        f"| 参数 | 值 |",
        f"|------|-----|",
        f"| 实验 | {experiment} |",
        f"| 目标 | {ask} |",
        f"| 最大迭代 | {max_iter} |",
        f"| 目标 WAPE | {target_wape or '无'} |",
        f"| 模型 | {model} |",
    ]
    if credits:
        lines.extend([
            "",
            f"💰 余额: {credits.get('balance', 'N/A')} | "
            f"用量: {credits.get('used_percent', '?')}%",
        ])
    return send_markdown("\n".join(lines))


def notify_trial_done(
    trial_id: str,
    comparison: dict,
    credits: dict | None = None,
) -> bool:
    """通知单轮实验完成"""
    primary = comparison.get("primary", {})
    old_wape = primary.get("old_wape", 999)
    new_wape = primary.get("new_wape", 999)
    old_bias = primary.get("old_bias", 0)
    new_bias = primary.get("new_bias", 0)
    wape_delta = comparison.get("wape_delta", 0)
    bias_delta = comparison.get("bias_delta", 0)
    decision = comparison.get("decision", "unknown").upper()

    secondary = comparison.get("secondary", {})
    old_dish = secondary.get("old_wape", 999)

    icon = "✅" if decision == "KEEP" else "❌"

    lines = [
        f"🔬 **实验完成: {trial_id}**",
        "",
        f"| 指标 | Baseline | New | Delta |",
        f"|------|----------|-----|-------|",
        f"| WAPE (package) | {old_wape:.4f} | {new_wape:.4f} | {wape_delta:+.4f} |",
        f"| Bias (package) | {old_bias:+.4f} | {new_bias:+.4f} | {bias_delta:+.4f} |",
        f"| WAPE (dish) | {old_dish:.4f} | — | — |",
        "",
        f"**{icon} 决策: {decision}**",
    ]
    if credits:
        lines.extend([
            "",
            f"💰 余额: {credits.get('balance', '?')} | "
            f"用量: {credits.get('used_percent', '?')}%",
        ])
    return send_markdown("\n".join(lines))


def notify_loop_stop(reason: str, manifests: list) -> bool:
    """通知循环停止 + 汇总表"""
    lines = [
        f"🛑 **循环停止: {reason}**",
        "",
        f"| Trial | WAPE | Decision |",
        f"|-------|------|----------|",
    ]
    for m in manifests:
        from pathlib import Path as _Path
        metric_path = _Path(m.output_dir) / "evaluation" / "metric_comparison.json"
        wape_str = "?"
        dec_str = "?"
        if metric_path.exists():
            try:
                data = json.loads(metric_path.read_text(encoding="utf-8"))
                wape_str = f"{data.get('primary', {}).get('new_wape', '?'):.4f}"
                dec_str = data.get("decision", "?").upper()
            except Exception:
                pass
        lines.append(f"| {m.trial_id} | {wape_str} | {dec_str} |")
    return send_markdown("\n".join(lines))


def notify_error(error_msg: str, context: str = "") -> bool:
    """通知异常"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    md = f"❌ **异常 [{ts}]**\n\n"
    if context:
        md += f"**上下文**: {context}\n\n"
    md += f"```\n{str(error_msg)[:1500]}\n```"
    return send_markdown(md)


def notify_credits_low(credits: dict, wait_hours: float) -> bool:
    """通知额度不足"""
    reset_str = ""
    if credits.get("resets_at"):
        reset_str = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(float(credits["resets_at"]))
        )
    return send_markdown(
        f"💰 **额度不足**\n\n"
        f"预计 {wait_hours:.1f}h 后恢复\n"
        f"恢复时间: {reset_str}\n"
        f"余额: {credits.get('balance', 'N/A')}\n"
        f"用量: {credits.get('used_percent', '?')}%\n"
        f"原因: {credits.get('rate_limit_type', 'unknown')}"
    )
