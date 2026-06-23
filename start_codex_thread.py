#!/usr/bin/env python3
import sys
from openai_codex import Codex
from config import build_codex_config, get_config

cfg = get_config()
prompt = " ".join(sys.argv[1:]) or "Say OK"

with Codex(config=build_codex_config()) as codex:
    if cfg.api_key:
        codex.login_api_key(cfg.api_key)
    thread = codex.thread_start()
    result = thread.run(prompt)
    if result.status.value == "failed":
        raise RuntimeError(result.error.message if result.error else "Codex failed")
    print(f"thread_id={thread.id}")
    print(result.final_response or "")
