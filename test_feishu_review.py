#!/usr/bin/env python3
"""
飞书人审交互端到端测试

测试项:
  1. 发送 Markdown 消息
  2. 发送交互卡片 (KEEP/ROLLBACK/REVISE 按钮)
  3. 等待文字回复或卡片按钮回调

用法:
  # 需要先启动 lark_card_bot.py (另一个终端)
  python lark_card_bot.py --host 0.0.0.0 --port 8787

  # 然后运行测试
  PYTHONIOENCODING=utf-8 python test_feishu_review.py
"""

from __future__ import annotations
import json
import os
import sys
import time

# 确保写中文不出错
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from config import get_config
from lark_notify import (
    send_text,
    send_markdown,
    send_review_card_text,
    build_review_card,
    build_trial_review_text,
    send_interactive_card,
    wait_for_review_event,
    parse_feishu_command,
    parse_feishu_card_action,
    poll_recent_messages,
    _get_tenant_token,
)

cfg = get_config()
FEISHU = cfg.feishu
CHAT_ID = FEISHU.chat_id

# ── 颜色 ──
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg):   print(f"  {GREEN}✅{RESET} {msg}")
def fail(msg): print(f"  {RED}❌{RESET} {msg}")
def info(msg): print(f"  {CYAN}ℹ️{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠️{RESET} {msg}")
def step(n, title):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f" {BOLD}测试 {n}: {title}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")


# ═══════════════════════════════════════════════════════════
# 测试 1: Token 获取
# ═══════════════════════════════════════════════════════════

step(1, "获取 Tenant Access Token")

try:
    token = _get_tenant_token()
    ok(f"Token 获取成功 ({len(token)} chars)")
except Exception as e:
    fail(f"Token 获取失败: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════
# 测试 2: 发送纯文本消息
# ═══════════════════════════════════════════════════════════

step(2, "发送纯文本消息")

ts = time.strftime("%H:%M:%S")
ok1 = send_text(f"🧪 [测试 {ts}] 这是一条纯文本消息，请回复 /keep 或 /rollback", CHAT_ID)
if ok1:
    ok("文本消息已发送 → 请在飞书群确认")
else:
    fail("文本消息发送失败")

# ═══════════════════════════════════════════════════════════
# 测试 3: 发送 Markdown 消息
# ═══════════════════════════════════════════════════════════

step(3, "发送 Markdown 消息")

ok2 = send_markdown(
    f"📊 **Codex Flow 实验模拟: trial_test**\n\n"
    f"**核心指标 (package_detail)**\n"
    f"| 指标 | Baseline | New | Delta |\n"
    f"|------|----------|-----|-------|\n"
    f"| WAPE | 0.7009 | 0.6829 | **+0.0180** |\n"
    f"| Bias | +0.1695 | +0.1420 | -0.0275 |\n\n"
    f"**自动建议: KEEP** ✅\n\n"
    f"测试: 请回复 `/keep` 或 `/rollback` 或点击下方卡片按钮",
    CHAT_ID,
)
if ok2:
    ok("Markdown 消息已发送 → 请在飞书群确认")
else:
    fail("Markdown 消息发送失败")

# ═══════════════════════════════════════════════════════════
# 测试 4: 发送交互卡片
# ═══════════════════════════════════════════════════════════

step(4, "发送交互卡片 (含 KEEP/ROLLBACK/REVISE 按钮)")

TEST_TRIAL_ID = "trial_test"
CARD_TEXT = build_trial_review_text(
    TEST_TRIAL_ID,
    comparison={
        "primary": {"old_wape": 0.7009, "new_wape": 0.6829, "old_bias": 0.1695, "new_bias": 0.1420},
        "secondary": {"old_wape": 0.6500},
        "wape_delta": +0.0180,
        "bias_delta": -0.0275,
        "decision": "keep",
    },
    auto_suggestion="keep",
)

ok3 = send_review_card_text(CARD_TEXT, TEST_TRIAL_ID, CHAT_ID)
if ok3:
    ok("交互卡片已发送 → 请在飞书群点击按钮测试")
    warn("如果按钮显示「已失效」→ 说明消息卡片请求网址未配置")
else:
    fail("卡片发送失败")

# ═══════════════════════════════════════════════════════════
# 测试 5: 读取最近群消息
# ═══════════════════════════════════════════════════════════

step(5, "读取群消息 (测试 im:message:readonly 权限)")

time.sleep(2)  # 给飞书一点时间
recent = poll_recent_messages(CHAT_ID, page_size=5)
if recent:
    ok(f"成功读取 {len(recent)} 条消息:")
    for m in recent:
        text = m["text"][:80].replace("\n", " ")
        print(f"    [{m['sender_id'][:12]}...] {text}...")
else:
    fail("无法读取群消息!")
    warn("可能原因:")
    warn("  1. 缺少 im:message:readonly 权限 → 飞书开放平台开启")
    warn("  2. 机器人不在群聊中 → 群设置添加机器人")
    warn("  3. 群聊近期无消息")

# ═══════════════════════════════════════════════════════════
# 测试 6: 等待人工回复 (30 秒超时)
# ═══════════════════════════════════════════════════════════

step(6, f"等待人工回复 (文字 /keep 或卡片按钮) — 30s 超时")

print(f"\n  {BOLD}⏳ 请在飞书群中:{RESET}")
print(f"     • 回复文字: /keep 或 /rollback 或 /revise 测试通过")
print(f"     • 或点击卡片上的 [KEEP] / [ROLLBACK] / [REVISE] 按钮")
print(f"     • 或输入任意补充文本")
print()

start = time.time()
event = wait_for_review_event(CHAT_ID, TEST_TRIAL_ID, timeout=30, poll_interval=3)
elapsed = time.time() - start

if event is None:
    warn(f"超时 ({elapsed:.0f}s) — 未收到回复")
    warn("可能原因:")
    warn("  • im:message:readonly 权限未开启 → 文字回复收不到")
    warn("  • 消息卡片请求网址未配置 → 卡片按钮收不到")
    warn("  • 两个通道都不通 → 请先确认权限配置")
else:
    ok(f"收到回复! (耗时 {elapsed:.0f}s)")

    if event.get("source") == "card":
        cmd = event["command"]
        info(f"来源: 卡片按钮")
        info(f"解析结果: action={cmd.get('action')}, trial_id={cmd.get('trial_id', '?')}")
        info(f"补充: {cmd.get('supplement', '(无)')}")
    elif event.get("source") == "message":
        msg = event["message"]
        info(f"来源: 文字消息")
        info(f"发送者: {msg['sender_id']}")
        info(f"内容: {msg['text'][:200]}")
        parsed = parse_feishu_command(msg["text"])
        info(f"解析结果: action={parsed['action']}, supplement={parsed.get('supplement', '(无)')}")
    else:
        info(f"来源: 未知")
        info(f"原始: {json.dumps(event, ensure_ascii=False, indent=2)}")

# ═══════════════════════════════════════════════════════════
# 测试 7: 命令解析单元测试
# ═══════════════════════════════════════════════════════════

step(7, "命令解析器单元测试")

COMMAND_TESTS = [
    ("/keep",           "keep", None),
    ("/KEEP",           "keep", None),
    ("keep",            "keep", None),
    ("保留",            "keep", None),
    ("/rollback",       "rollback", None),
    ("重跑",            "rollback", None),
    ("/reverse",        "reverse", None),
    ("撤销",            "reverse", None),
    ("/stop",           "stop", None),
    ("停止",            "stop", None),
    ("/status",         "status", None),
    ("/revise 调小校准强度并重跑", "rollback", "调小校准强度并重跑"),
    ("/branch A;B",     "rollback", "分支探索: A;B"),
    ("这是一条普通建议", "supplement", "这是一条普通建议"),
]

all_passed = True
for text, exp_action, exp_supp in COMMAND_TESTS:
    result = parse_feishu_command(text)
    a_ok = result["action"] == exp_action
    s_ok = result.get("supplement") == exp_supp
    if a_ok and s_ok:
        print(f"  {GREEN}✅{RESET} \"{text}\" → action={result['action']}, supplement={result.get('supplement')}")
    else:
        all_passed = False
        print(f"  {RED}❌{RESET} \"{text}\"")
        print(f"     期望: action={exp_action}, supplement={exp_supp}")
        print(f"     实际: action={result['action']}, supplement={result.get('supplement')}")

if all_passed:
    ok("所有命令解析测试通过")
else:
    fail("部分命令解析测试失败")

# ═══════════════════════════════════════════════════════════
# 测试 8: 卡片回调 JSONL 检测
# ═══════════════════════════════════════════════════════════

step(8, "卡片回调 JSONL 文件状态")

from pathlib import Path
log_path = Path("runs/feishu_card_actions.jsonl")

if log_path.exists():
    lines = log_path.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if l.strip()]
    ok(f"JSONL 文件存在, {len(lines)} 条记录")
    for l in lines[-3:]:
        try:
            item = json.loads(l)
            print(f"    action={item.get('action')}, trial={item.get('trial_id')}, "
                  f"time={item.get('received_at', 0)}")
        except Exception:
            print(f"    (解析失败) {l[:80]}...")
else:
    warn("JSONL 文件不存在 → lark_card_bot.py 未收到过卡片回调")
    warn("  确认: 1) lark_card_bot.py 正在运行  2) 飞书开放平台已配置消息卡片请求网址")

# ═══════════════════════════════════════════════════════════
# 总结
# ═══════════════════════════════════════════════════════════

print(f"\n{BOLD}{'='*60}{RESET}")
print(f" {BOLD}📋 测试总结{RESET}")
print(f"{BOLD}{'='*60}{RESET}")
print()
print("  ✅ = 正常   ❌ = 需要配置   ⚠️ = 需要确认")
print()
print("  必要条件检查:")
print(f"  1. 飞书 App Secret 已配置  → {'✅' if FEISHU.app_secret else '❌'}")
print(f"  2. Tenant Token 可获取     → {'✅' if token else '❌'}")
print(f"  3. 可发送消息              → {'✅' if ok1 else '❌'}")
print(f"  4. 可读取群消息             → {'✅' if recent else '❌ 检查 im:message:readonly 权限'}")
print(f"  5. 文字回复可收到           → {'✅' if event and event.get('source') == 'message' else '⚠️'}")
print(f"  6. 卡片按钮可收到           → {'✅' if event and event.get('source') == 'card' else '⚠️ 检查消息卡片请求网址'}")
print()
print("  关键配置入口:")
print("  - 飞书开放平台: https://open.feishu.cn/app → 你的应用")
print("  - 权限管理 → 搜索 im:message:readonly → 开启 → 重新发布")
print("  - 机器人 → 消息卡片 → 设置消息卡片请求网址")
