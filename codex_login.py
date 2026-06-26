#!/usr/bin/env python3
"""
codex_login.py — 设备码登录, session 持久化到 CODEX_HOME

跑一次即可，后续 codex_flow.py 会复用这个 session。

验证方式: 实际模型调用 (不依赖 account() 的 requires_openai_auth 标志)
"""

from __future__ import annotations

import time
import sys
import signal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from openai_codex import Codex
from openai_codex.client import CodexConfig
from config import build_codex_config, get_config
from codex_gateway import ensure_codex_gateway



class AuthVerificationTimeout(RuntimeError):
    pass


def _timeout_handler(signum, frame):
    raise AuthVerificationTimeout("Codex session verification timed out")


def _with_timeout(seconds: int, fn):
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _verify_session_once(codex: Codex):
    t = codex.thread_start()
    return t.run("回复: OK")


def _build_codex_config() -> CodexConfig:
    return build_codex_config()


def _mask_proxy_url(value: str) -> str:
    if not value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.username and not parts.password:
        return value
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))

CODEX_CONFIG = _build_codex_config()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Codex 设备码登录")
    parser.add_argument("--logout", action="store_true", help="强制退出当前账号, 重新登录")
    args = parser.parse_args()

    print("Codex 设备码登录 (一次性)")
    print(f"CODEX_HOME: {CODEX_CONFIG.env.get('CODEX_HOME')}")
    if CODEX_CONFIG.env.get("HTTPS_PROXY"):
        print(f"Codex gateway: {_mask_proxy_url(CODEX_CONFIG.env.get('HTTPS_PROXY', ''))}")
    else:
        print("Codex gateway: not configured")
    print("=" * 50)

    ensure_codex_gateway()

    with Codex(config=CODEX_CONFIG) as codex:
        # ── 强制退出 ──
        if args.logout:
            print("[Auth] 强制退出当前账号...")
            try:
                codex.logout()
                print("[Auth] ✅ 已退出")
            except Exception as e:
                print(f"[Auth] ⚠️ 退出失败 (可能未登录): {e}")

        # ── 检查已有 session 是否实际可用 ──
        if not args.logout:
            try:
                r = _with_timeout(45, lambda: _verify_session_once(codex))
                if r.final_response and "OK" in (r.final_response or "").upper():
                    print("[Auth] ✅ 已有可用 session, 无需重新登录")
                    return 0
            except AuthVerificationTimeout:
                print("[Auth] ⚠️ 已有 session 验证超时, 将重新登录")
            except Exception:
                pass

        api_key = get_config().api_key
        if api_key:
            print("[Auth] 使用 API Key 认证并验证 session...")
            codex.login_api_key(api_key)
            try:
                r = _with_timeout(45, lambda: _verify_session_once(codex))
                if r.final_response:
                    print("[Auth] ✅ API Key 可用, Codex SDK 已接通")
                    return 0
            except AuthVerificationTimeout:
                print("[Auth] ❌ API Key 验证超时")
                return 1
            except Exception as e:
                print(f"[Auth] ❌ API Key 验证失败: {e}")
                return 1

        # 设备码登录
        print("[Auth] 启动设备码登录...")
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
                r = _with_timeout(45, lambda: _verify_session_once(codex))
                if r.final_response:
                    print(f"[Auth] ✅ 登录成功! session 可用 (attempt {retry + 1})")
                    break
            except AuthVerificationTimeout:
                print(f"  [Auth] session 验证调用超时... ({retry + 1}/15)")
                continue
            except Exception as e:
                print(f"  [Auth] session 暂不可用: {e} ({retry + 1}/15)")
                continue
            print(f"  [Auth] 等待 session 生效... ({retry + 1}/15)")
        else:
            print("[Auth] ❌ session 不可用, 请设置 OPENAI_API_KEY 或 flow_config.yaml 的 auth.openai_api_key")
            return 1

        # 等 app-server 把 session 写入磁盘
        print("[Auth] 等待 session 写入磁盘...")
        time.sleep(5)

    print("\n✅ 登录完成, 后续运行 codex_flow.py 无需再次认证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
