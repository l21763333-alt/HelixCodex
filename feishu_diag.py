#!/usr/bin/env python3
"""
飞书集成诊断脚本 — 检查所有必要配置和权限

用法:
  python feishu_diag.py
"""

from __future__ import annotations
import json
import os
import urllib.request
import urllib.error

FS_BASE = "https://open.feishu.cn/open-apis"

# ═══════════════════════════════════════════════════════════
# 1. 读取配置
# ═══════════════════════════════════════════════════════════

from config import get_config

cfg = get_config()
fcfg = cfg.feishu

print("=" * 60)
print("🔍 飞书集成诊断")
print("=" * 60)

# ── 配置检查 ──
checks = [
    ("FEISHU_APP_ID", fcfg.app_id, "飞书应用 ID (cli_xxx)"),
    ("FEISHU_APP_SECRET", fcfg.app_secret, "飞书应用密钥 (⚠️ 不能为空)"),
    ("FEISHU_CHAT_ID", fcfg.chat_id, "目标群聊 ID (oc_xxx)"),
    ("feishu.enabled", cfg.feishu.enabled, "飞书集成开关"),
    ("loop.human_review.enabled", cfg.loop.human_review.enabled, "人工审核开关"),
]

all_ok = True
for name, value, desc in checks:
    ok = bool(value)
    icon = "✅" if ok else "❌"
    display = value if not (ok and "SECRET" in name) else "***已设置***"
    print(f"  {icon} {name}: {display}  → {desc}")
    if not ok:
        all_ok = False

if not all_ok:
    print("\n⚠️ 缺少必要配置, 请检查 flow_config.yaml 或环境变量")
else:
    print("\n✅ 基础配置完整")

# ═══════════════════════════════════════════════════════════
# 2. 获取 Tenant Access Token
# ═══════════════════════════════════════════════════════════

print("\n" + "-" * 60)
print("🔑 Tenant Access Token")

token_ok = False
try:
    body = json.dumps({"app_id": fcfg.app_id, "app_secret": fcfg.app_secret}).encode()
    req = urllib.request.Request(
        f"{FS_BASE}/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") == 0:
        print(f"  ✅ Token 获取成功 (expires in {data.get('expire')}s)")
        token_ok = True
        TOKEN = data["tenant_access_token"]
    else:
        print(f"  ❌ Token 获取失败: {data.get('msg')}")
except Exception as e:
    print(f"  ❌ 网络错误: {e}")

# ═══════════════════════════════════════════════════════════
# 3. 检查应用权限
# ═══════════════════════════════════════════════════════════

if token_ok:
    print("\n" + "-" * 60)
    print("📋 应用权限范围")

    try:
        req = urllib.request.Request(
            f"{FS_BASE}/auth/v3/app_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            app_data = json.loads(resp.read().decode())
        APP_TOKEN = app_data.get("app_access_token", "")

        # 获取应用信息
        req2 = urllib.request.Request(
            f"{FS_BASE}/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=10) as resp:
            pass

        print(f"  ℹ️ 请到飞书开放平台确认以下权限已开启:")
        print(f"    - im:message              (发送消息)")
        print(f"    - im:message:readonly     (读取群消息 — 必须)")
        print(f"    - im:resource             (上传/下载文件)")
        print(f"  路径: 开放平台 → 你的应用 → 权限管理 → 搜索权限 → 开启 → 重新发布")
    except Exception as e:
        print(f"  ⚠️ 无法检查权限: {e}")

# ═══════════════════════════════════════════════════════════
# 4. 测试发送消息
# ═══════════════════════════════════════════════════════════

if token_ok and fcfg.chat_id:
    print("\n" + "-" * 60)
    print("📤 消息发送测试")

    msg_body = {
        "receive_id": fcfg.chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": "🔍 [飞书诊断] 发送测试 OK"}, ensure_ascii=False),
    }
    rid_type = "chat_id" if fcfg.chat_id.startswith("oc_") else "user_id"

    try:
        req = urllib.request.Request(
            f"{FS_BASE}/im/v1/messages?receive_id_type={rid_type}",
            data=json.dumps(msg_body).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            send_result = json.loads(resp.read().decode())
        if send_result.get("code") == 0:
            msg_id = send_result.get("data", {}).get("message_id", "?")
            print(f"  ✅ 消息发送成功! message_id={msg_id}")
            print(f"  → 请在飞书群确认收到「发送测试 OK」消息")
        else:
            print(f"  ❌ 消息发送失败: code={send_result.get('code')} msg={send_result.get('msg')}")
    except Exception as e:
        print(f"  ❌ 发送异常: {e}")

# ═══════════════════════════════════════════════════════════
# 5. 测试读取消息 (验证 im:message:readonly 权限)
# ═══════════════════════════════════════════════════════════

if token_ok and fcfg.chat_id:
    print("\n" + "-" * 60)
    print("📥 消息读取测试 (im:message:readonly 权限)")

    try:
        rid_type = "chat_id" if fcfg.chat_id.startswith("oc_") else "user_id"
        url = (f"{FS_BASE}/im/v1/messages?receive_id_type={rid_type}"
               f"&receive_id={fcfg.chat_id}&page_size=3&sort_type=ByCreateTimeDesc")
        req = urllib.request.Request(
            url,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {TOKEN}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            msg_result = json.loads(resp.read().decode())

        if msg_result.get("code") == 0:
            items = msg_result.get("data", {}).get("items", [])
            print(f"  ✅ 读取成功! 最近 {len(items)} 条消息:")
            for item in items[:3]:
                sender = item.get("sender", {}).get("id", "?")
                msg_type = item.get("msg_type", "?")
                text = item.get("body", {}).get("content", {}).get("text", f"[{msg_type}]")
                if len(text) > 60:
                    text = text[:60] + "..."
                print(f"    - [{sender}] {text}")
        elif msg_result.get("code") == 99991663:
            print(f"  ❌ 权限不足! 错误码 99991663 = 缺少 im:message:readonly 权限")
            print(f"  → 请在飞书开放平台开启此权限并重新发布应用")
        elif msg_result.get("code") == 230001:
            print(f"  ❌ 机器人不在群聊中! 错误码 230001")
            print(f"  → 请将机器人添加到群聊: 群设置 → 群机器人 → 添加机器人")
        else:
            print(f"  ❌ 读取失败: code={msg_result.get('code')} msg={msg_result.get('msg')}")
    except Exception as e:
        print(f"  ❌ 读取异常: {e}")

# ═══════════════════════════════════════════════════════════
# 6. 卡片回调配置提醒
# ═══════════════════════════════════════════════════════════

print("\n" + "-" * 60)
print("🔘 卡片按钮回调检查")

print(f"  verification_token: {'✅ 已设置' if fcfg.verification_token else '❌ 未设置 (将跳过验证)'}")
print()
print("  卡片按钮需要以下配置才能正常工作:")
print()
print("  1️⃣ 飞书开放平台 → 你的应用 → 机器人 → 消息卡片 →")
print("     消息卡片请求网址: http://<你的公网地址>:8787/feishu/card")
print(f"     验证令牌: {fcfg.verification_token or '(建议设置)'}")
print()
print("  2️⃣ 本地/服务器上启动卡片回调服务:")
print("     python lark_card_bot.py --host 0.0.0.0 --port 8787")
print()
print("  3️⃣ 如果是本地开发, 需要内网穿透:")
print("     ngrok http 8787")
print("     → 复制 ngrok 提供的 https URL 填入飞书开放平台")
print()
print("  ⚠️ 没有配置消息卡片请求网址 → 按钮点击必然失效!")

# ═══════════════════════════════════════════════════════════
# 7. 总结
# ═══════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("📋 诊断总结")
print("=" * 60)

issues = []

if not fcfg.app_id or not fcfg.app_secret:
    issues.append("缺少飞书应用凭据 (app_id/app_secret)")

if not fcfg.chat_id:
    issues.append("缺少目标群聊 ID (chat_id)")

if not token_ok:
    issues.append("无法获取 Tenant Access Token — 检查 app_id/app_secret")

if not cfg.loop.human_review.enabled:
    issues.append("loop.human_review.enabled 未开启 — 审核功能被禁用")

if not fcfg.verification_token:
    issues.append("verification_token 未设置 — 卡片回调将跳过验证 (建议补齐)")

if issues:
    print()
    for i, issue in enumerate(issues, 1):
        print(f"  ❌ {i}. {issue}")
else:
    print()
    print("  ✅ 配置层面无明显问题")
    print()
    print("  ⚠️ 仍需人工确认:")
    print("  - 飞书开放平台是否已开启 im:message:readonly 权限")
    print("  - 机器人是否已加入目标群聊")
    print("  - 消息卡片请求网址是否已配置")
    print("  - lark_card_bot.py 是否正在运行且可被公网访问")

print()
print("参考: https://open.feishu.cn/document/server-docs/im-v1/message/list")
