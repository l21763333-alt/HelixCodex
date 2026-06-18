#!/usr/bin/env python3
"""
codex_login.py — 设备码登录, session 持久化到 CODEX_HOME

跑一次即可，后续 codex_flow.py 会复用这个 session。

验证方式: 实际模型调用 (不依赖 account() 的 requires_openai_auth 标志)
"""

from __future__ import annotations

import time
import sys
from pathlib import Path
from openai_codex import Codex
from openai_codex.client import CodexConfig
from config import get_config


def _build_codex_config() -> CodexConfig:
    env: dict[str, str] = {"RUST_LOG": "info"}
    env["CODEX_HOME"] = get_config().resolved_codex_home
    return CodexConfig(env=env)

CODEX_CONFIG = _build_codex_config()


def main() -> int:
    print("Codex 设备码登录 (一次性)")
    print(f"CODEX_HOME: {CODEX_CONFIG.env.get('CODEX_HOME')}")
    print("=" * 50)

    with Codex(config=CODEX_CONFIG) as codex:
        # 先检查已有 session 是否实际可用 (直接跑模型调用)
        try:
            t = codex.thread_start()
            r = t.run("回复: OK")
            if r.final_response and "OK" in (r.final_response or "").upper():
                print("[Auth] ✅ 已有可用 session, 无需重新登录")
                return 0
        except Exception:
            pass

        # 设备码登录
        handle = codex.login_chatgpt_device_code()
        print(f"\n  请在浏览器中打开: {handle.verification_url}")
        print(f"  输入验证码: {handle.user_code}\n")

        result = handle.wait()
        if not result.success:
            print(f"[Auth] ❌ 登录失败: {result}")
            return 1

        # 用实际模型调用验证 session (不调 account())
        print("[Auth] 验证 session...")
        for retry in range(15):
            time.sleep(1)
            try:
                t = codex.thread_start()
                r = t.run("回复: OK")
                if r.final_response:
                    print(f"[Auth] ✅ 登录成功! session 可用 (attempt {retry + 1})")
                    break
            except Exception:
                pass
            print(f"  [Auth] 等待 session 生效... ({retry + 1}/15)")
        else:
            print("[Auth] ❌ session 不可用, 请在 codex_flow_config.json 中设置 openai_api_key")
            return 1

        # 等 app-server 把 session 写入磁盘
        print("[Auth] 等待 session 写入磁盘...")
        time.sleep(5)

    print("\n✅ 登录完成, 后续运行 codex_flow.py 无需再次认证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
