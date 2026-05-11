#!/usr/bin/env python3
"""hap-token CLI：消费方接入 broker 的统一入口。

子命令：
  get <profile>            打印 URL（默认纯 stdout，方便 $(hap-token get xxx)）
  get <profile> --check    先校验 expires_at；已过期 → exit 1
  list                     列所有已缓存的 profile（名、过期时间、剩余小时）
  status                   打印 daemon 存活 + 各 profile 状态
  refresh <profile>        向 daemon 发 SIGUSR1 触发刷新；daemon 不在则前台跑一次
  path <profile>           打印 token JSON 文件绝对路径（供纯文件读消费方）

用法示例：
  URL=$(hap-token get claw-crm)
  URL=$(hap-token get claw-crm --check) || echo "token 已过期，请先 hap-token refresh"
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config as cfg_mod  # noqa: E402
import refresher  # noqa: E402
import storage  # noqa: E402


PID_FILE = Path.home() / ".local" / "share" / "hap-token-broker" / "broker.pid"


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # 探活
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def _fmt_remaining(rec: storage.TokenRecord) -> str:
    remain_sec = rec.seconds_until_expiry()
    if remain_sec <= 0:
        return "EXPIRED"
    hours = remain_sec / 3600
    if hours >= 1:
        return f"{hours:.1f}h"
    return f"{remain_sec / 60:.0f}min"


# ---- sub-commands ---------------------------------------------------------

def cmd_get(args: argparse.Namespace) -> int:
    rec = storage.read(args.profile)
    if rec is None:
        print(f"ERROR: profile '{args.profile}' 无缓存。先启动 broker 或跑 `hap-token refresh {args.profile}`", file=sys.stderr)
        return 1
    if args.check and rec.is_expired():
        print(f"ERROR: profile '{args.profile}' 已过期 (expires_at={rec.expires_at.isoformat()})", file=sys.stderr)
        return 1
    print(rec.url)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    profiles = storage.list_profiles()
    if not profiles:
        print("(no profiles cached yet)")
        return 0
    print(f"{'PROFILE':<20} {'EXPIRES_AT':<22} {'REMAIN':<10} {'ACCOUNT':<20}")
    print("-" * 78)
    for name in profiles:
        rec = storage.read(name)
        if rec is None:
            print(f"{name:<20} <corrupted>")
            continue
        print(
            f"{name:<20} "
            f"{rec.expires_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'):<22} "
            f"{_fmt_remaining(rec):<10} "
            f"{rec.account_redacted:<20}"
        )
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    cfg_path = cfg_mod.resolve_config_path()
    print(f"config:       {cfg_path} ({'exists' if cfg_path.exists() else 'MISSING'})")
    print(f"daemon_pid:   {pid if pid else '<not running>'}")
    print(f"token_dir:    {storage.TOKEN_DIR}")
    print()
    return cmd_list(argparse.Namespace())


def cmd_refresh(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
            print(f"signaled broker (pid={pid}) SIGUSR1; watch `~/Library/Logs/hap-token-broker.log`")
            return 0
        except OSError as e:
            print(f"WARN: failed to signal broker: {e}; falling back to foreground", file=sys.stderr)

    # fallback: 无 daemon 时前台跑一次
    try:
        cfg = cfg_mod.load_config()
    except cfg_mod.ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.profile not in cfg.profiles:
        print(f"ERROR: profile '{args.profile}' 未在配置中定义", file=sys.stderr)
        return 2

    profile = cfg.profiles[args.profile]
    try:
        url, duration_ms = refresher.md_generate(
            cfg.md_generate_bin,
            profile.account,
            profile.password,
            profile.oauth_app_id,
        )
    except refresher.RefreshError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    record = storage.build_record(
        args.profile, url, profile.oauth_app_id, profile.account, duration_ms,
    )
    storage.write_atomic(record)
    legacy = cfg.mirror_to_legacy.get(args.profile)
    if legacy:
        storage.mirror_to_legacy(record, legacy)
    print(f"refreshed {args.profile} in {duration_ms}ms, expires_at={record.expires_at.isoformat()}")
    print(f"url: {storage.redact_url(url)}" + (f"; mirror={legacy}" if legacy else ""))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    print(storage.token_path(args.profile))
    return 0


# ---- entry ----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="hap-token", description="HAP Personal MCP token broker CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="打印指定 profile 的 URL")
    p_get.add_argument("profile")
    p_get.add_argument("--check", action="store_true", help="先校验 expires_at；过期 exit 1")
    p_get.set_defaults(func=cmd_get)

    p_list = sub.add_parser("list", help="列所有缓存的 profile")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="打印 daemon 状态 + profile 状态")
    p_status.set_defaults(func=cmd_status)

    p_refresh = sub.add_parser("refresh", help="向 daemon 发 SIGUSR1 或前台跑一次")
    p_refresh.add_argument("profile")
    p_refresh.set_defaults(func=cmd_refresh)

    p_path = sub.add_parser("path", help="打印指定 profile 的 token 文件路径")
    p_path.add_argument("profile")
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
