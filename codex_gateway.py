#!/usr/bin/env python3
"""Manage the local Cloudflare Access gateway used by Codex."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import time
from pathlib import Path

from config import PROJECT_ROOT, get_config


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _split_host_port(listener: str) -> tuple[str, int]:
    host, sep, port = listener.rpartition(":")
    if not sep or not host or not port:
        raise ValueError(f"invalid gateway listener {listener!r}; expected host:port")
    return host, int(port)


def _can_connect(listener: str, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(_split_host_port(listener), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_running(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_codex_gateway(wait_seconds: float = 15.0) -> None:
    cfg = get_config().codex_gateway
    if not cfg.enabled:
        return
    if cfg.mode == "proxy_env_only":
        return
    if cfg.mode != "cloudflared_access_tcp":
        raise ValueError(f"unsupported codex gateway mode: {cfg.mode}")
    if not cfg.hostname:
        raise ValueError(
            "codex_gateway.hostname is required when codex_gateway.enabled=true"
        )

    cloudflared = _resolve_path(cfg.cloudflared_path)
    if not cloudflared.exists():
        raise FileNotFoundError(f"cloudflared not found: {cloudflared}")

    pid_file = _resolve_path(cfg.pid_file)
    log_file = _resolve_path(cfg.log_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if _pid_running(pid_file) and _can_connect(cfg.listener):
        print(f"[Gateway] cloudflared already listening on {cfg.listener}")
        return

    cmd = [
        str(cloudflared),
        "access",
        "tcp",
        "--hostname",
        cfg.hostname,
        "--url",
        cfg.listener,
        "--logfile",
        str(log_file),
        "--log-level",
        cfg.log_level,
    ]
    if cfg.service_token_id:
        cmd.extend(["--service-token-id", cfg.service_token_id])
    if cfg.service_token_secret:
        cmd.extend(["--service-token-secret", cfg.service_token_secret])

    print(f"[Gateway] starting cloudflared access tcp: {cfg.listener} -> {cfg.hostname}")
    with log_file.open("ab") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"cloudflared exited with code {proc.returncode}; see {log_file}"
            )
        if _can_connect(cfg.listener):
            print(f"[Gateway] cloudflared ready on {cfg.listener}")
            return
        time.sleep(0.5)
    raise TimeoutError(f"cloudflared did not become ready on {cfg.listener}; see {log_file}")


def stop_codex_gateway() -> None:
    cfg = get_config().codex_gateway
    pid_file = _resolve_path(cfg.pid_file)
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    pid_file.unlink(missing_ok=True)


def login_codex_gateway() -> None:
    cfg = get_config().codex_gateway
    if not cfg.hostname:
        raise ValueError("codex_gateway.hostname is required for gateway login")
    cloudflared = _resolve_path(cfg.cloudflared_path)
    if not cloudflared.exists():
        raise FileNotFoundError(f"cloudflared not found: {cloudflared}")
    if cfg.hostname.startswith(("http://", "https://")):
        login_url = cfg.hostname
    else:
        login_url = f"https://{cfg.hostname}"
    subprocess.run(
        [str(cloudflared), "access", "login", login_url],
        cwd=str(PROJECT_ROOT),
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Codex Cloudflare gateway")
    parser.add_argument("action", choices=["start", "stop", "status", "login"])
    args = parser.parse_args()

    cfg = get_config().codex_gateway
    if args.action == "start":
        ensure_codex_gateway()
        return 0
    if args.action == "stop":
        stop_codex_gateway()
        return 0
    if args.action == "login":
        login_codex_gateway()
        return 0

    pid_file = _resolve_path(cfg.pid_file)
    ready = _pid_running(pid_file) and _can_connect(cfg.listener)
    print(f"enabled={cfg.enabled} listener={cfg.listener} ready={ready}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
