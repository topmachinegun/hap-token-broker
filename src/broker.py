#!/usr/bin/env python3
"""HAP Token Broker 守护进程。

由 launchd 托管，每 check_interval_minutes 巡检一次：
  - sync 模式：SSH 到远程服务器拉取 token JSON，本机纯读不刷新
  - 正常模式：缺 token 或 距离 expires_at 小于 refresh_before_expire_hours 即刷新
  - 刷新/同步成功：写主仓库 + 可选 mirror 到 legacy 路径
  - 刷新/同步失败：累计计数，连续超过阈值触发 macOS 通知

信号：
  SIGUSR1 → 立即对所有 profile 跑一轮刷新/同步
  SIGTERM / SIGINT → 优雅退出

调试：
  python3 broker.py --oneshot       # 单次巡检后退出（launchd 不调用此模式）
  python3 broker.py --config PATH   # 覆盖 config 路径
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 支持独立脚本执行
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config as cfg_mod  # noqa: E402
import refresher  # noqa: E402
import storage  # noqa: E402


log = logging.getLogger("hap-token-broker")


def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%Y-%m-%dT%H:%M:%S")


def notify_alert(title: str, message: str) -> None:
    """告警通知。macOS 用 osascript，Linux 走 journal。均可静默。"""
    try:
        if sys.platform == "darwin":
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        else:
            log.warning(f"ALERT [{title}] {message}")
    except Exception:
        log.warning(f"ALERT [{title}] {message}")


class Broker:
    def __init__(self, cfg: cfg_mod.Config):
        self.cfg = cfg
        self.failure_count: dict[str, int] = {name: 0 for name in cfg.profiles}
        self.alerted: dict[str, bool] = {name: False for name in cfg.profiles}
        self._stop = threading.Event()
        self._kick = threading.Event()

    # --- signal handlers ---

    def request_stop(self, *_):
        log.info("received stop signal, shutting down")
        self._stop.set()
        self._kick.set()

    def request_kick(self, *_):
        log.info("received SIGUSR1, kicking refresh cycle")
        self._kick.set()

    # --- refresh logic ---

    def needs_refresh(self, name: str) -> tuple[bool, str]:
        rec = storage.read(name)
        if rec is None:
            return True, "no token yet"
        threshold_sec = self.cfg.refresh_before_expire_hours * 3600
        remain = rec.seconds_until_expiry()
        if remain <= threshold_sec:
            return True, f"expires in {remain / 3600:.2f}h (threshold {self.cfg.refresh_before_expire_hours}h)"
        return False, f"fresh, expires in {remain / 3600:.2f}h"

    def refresh_one(self, name: str) -> bool:
        profile = self.cfg.profiles[name]

        # sync 模式：从远程服务器拉取 token，本机不刷新
        if self.cfg.sync.is_configured():
            return self._sync_from_remote(name)

        try:
            url, duration_ms, new_refresh_token = self._try_refresh(name, profile)
        except refresher.RefreshError as e:
            self.failure_count[name] += 1
            log.error(f"[{name}] refresh failed ({self.failure_count[name]}x): {e}")
            if (
                self.failure_count[name] >= self.cfg.max_consecutive_failures
                and not self.alerted[name]
            ):
                notify_alert(
                    "HAP Token Broker",
                    f"{name} 连续 {self.failure_count[name]} 次刷新失败，请检查配置",
                )
                self.alerted[name] = True
            return False

        record = storage.build_record(
            name, url, profile.oauth_app_id, profile.account,
            duration_ms, refresh_token=new_refresh_token,
        )
        storage.write_atomic(record)

        legacy_path = self.cfg.mirror_to_legacy.get(name)
        if legacy_path is not None:
            try:
                storage.mirror_to_legacy(record, legacy_path)
            except Exception as e:
                log.warning(f"[{name}] legacy mirror failed ({legacy_path}): {e}")

        self.failure_count[name] = 0
        self.alerted[name] = False
        log.info(
            f"[{name}] refreshed ok in {duration_ms}ms, expires_at={record.expires_at.isoformat()}, "
            f"url={storage.redact_url(url)}"
            + (f", mirror={legacy_path}" if legacy_path else "")
        )
        return True

    def _sync_from_remote(self, name: str) -> bool:
        """SSH 到远程服务器拉取 token JSON，写入本地存储。"""
        sync = self.cfg.sync
        remote_path = f"{sync.remote_token_dir}/{name}.json"
        ssh_key = os.path.expanduser(sync.ssh_key)

        cmd = [
            "ssh",
            "-i", ssh_key,
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{sync.remote_user}@{sync.remote_host}",
            "sudo", "cat", remote_path,
        ]

        t0 = time.monotonic()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self._record_failure(name, f"SSH 到 {sync.remote_host} 超时")
            return False
        except OSError as e:
            self._record_failure(name, f"SSH 命令启动失败: {e}")
            return False
        duration_ms = int((time.monotonic() - t0) * 1000)

        if result.returncode != 0:
            err = (result.stderr or "").strip()[-200:]
            self._record_failure(name, f"远程读取失败 (exit={result.returncode}): {err}")
            return False

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self._record_failure(name, f"远程 token JSON 解析失败: {e}")
            return False

        try:
            record = storage.TokenRecord.from_json(data)
        except (KeyError, ValueError) as e:
            self._record_failure(name, f"远程 token 数据格式异常: {e}")
            return False

        storage.write_atomic(record)

        legacy_path = self.cfg.mirror_to_legacy.get(name)
        if legacy_path is not None:
            try:
                storage.mirror_to_legacy(record, legacy_path)
            except Exception as e:
                log.warning(f"[{name}] legacy mirror failed ({legacy_path}): {e}")

        self.failure_count[name] = 0
        self.alerted[name] = False
        log.info(
            f"[{name}] synced from {sync.remote_host} in {duration_ms}ms, "
            f"expires_at={record.expires_at.isoformat()}, "
            f"url={storage.redact_url(record.url)}"
            + (f", mirror={legacy_path}" if legacy_path else "")
        )
        return True

    def _record_failure(self, name: str, msg: str) -> None:
        self.failure_count[name] += 1
        log.error(f"[{name}] sync failed ({self.failure_count[name]}x): {msg}")
        if (
            self.failure_count[name] >= self.cfg.max_consecutive_failures
            and not self.alerted[name]
        ):
            notify_alert(
                "HAP Token Broker",
                f"{name} 连续 {self.failure_count[name]} 次同步失败，请检查 152 连接",
            )
            self.alerted[name] = True

    def _try_refresh(self, name: str, profile) -> tuple[str, int, str | None]:
        """尝试刷新 token：优先 refresh_token grant，失败回退 password grant。

        Returns:
            (url, duration_ms, new_refresh_token_or_None)
        """
        rec = storage.read(name)
        host = self._extract_host_from_url(rec.url) if rec else None

        # 方案A：优先尝试 refresh_token grant
        if (
            rec is not None
            and getattr(rec, 'refresh_token', None)
            and profile.client_secret
            and host
        ):
            log.info(f"[{name}] trying refresh_token grant (host={host})")
            try:
                url, new_rt, duration_ms = refresher.refresh_token_grant(
                    host=host,
                    refresh_token=getattr(rec, 'refresh_token', None),
                    client_id=profile.oauth_app_id,
                    client_secret=profile.client_secret,
                )
                log.info(f"[{name}] refresh_token grant ok in {duration_ms}ms")
                return url, duration_ms, new_rt
            except refresher.RefreshError as e:
                log.warning(
                    f"[{name}] refresh_token grant failed, falling back to password grant: {e}"
                )

        # 回退：password grant (md-generate-mcp-config)
        url, duration_ms = refresher.md_generate(
            self.cfg.md_generate_bin,
            profile.account,
            profile.password,
            profile.oauth_app_id,
        )
        return url, duration_ms, None

    @staticmethod
    def _extract_host_from_url(url: str) -> str | None:
        """从 MCP URL 中提取 host，如 api2.mingdao.com。"""
        import re
        m = re.search(r'https?://([^/]+)', url)
        return m.group(1) if m else None

    def run_once(self) -> None:
        """对所有 profile 跑一轮检查 + 必要时刷新。"""
        for name in self.cfg.profiles:
            need, reason = self.needs_refresh(name)
            log.info(f"[{name}] check: need_refresh={need} ({reason})")
            if need:
                self.refresh_one(name)

    def run_forever(self) -> int:
        interval_sec = self.cfg.check_interval_minutes * 60
        mode = "sync" if self.cfg.sync.is_configured() else "refresh"
        extra = f", sync_from={self.cfg.sync.remote_host}" if self.cfg.sync.is_configured() else ""
        log.info(
            f"broker started, mode={mode}, config={self.cfg.source_path}, "
            f"profiles={list(self.cfg.profiles.keys())}, "
            f"interval={self.cfg.check_interval_minutes}min, "
            f"refresh_before_expire={self.cfg.refresh_before_expire_hours}h"
            + extra
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                log.exception(f"unhandled error in run_once: {e}")
            # wait interval_sec, early wake on SIGUSR1 / stop
            self._kick.clear()
            self._kick.wait(timeout=interval_sec)
        log.info("broker stopped")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HAP Token Broker daemon")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径（覆盖默认）")
    parser.add_argument("--oneshot", action="store_true", help="跑一轮即退出（调试）")
    args = parser.parse_args()

    _setup_logging()

    try:
        cfg = cfg_mod.load_config(Path(args.config)) if args.config else cfg_mod.load_config()
    except cfg_mod.ConfigError as e:
        log.error(f"config error: {e}")
        return 2

    for w in cfg_mod.check_config_permissions(cfg.source_path):
        log.warning(w)

    broker = Broker(cfg)

    if args.oneshot:
        log.info("--oneshot mode: running single cycle")
        broker.run_once()
        return 0

    signal.signal(signal.SIGTERM, broker.request_stop)
    signal.signal(signal.SIGINT, broker.request_stop)
    try:
        signal.signal(signal.SIGUSR1, broker.request_kick)
    except (AttributeError, ValueError):
        pass  # Windows 无 SIGUSR1

    # 为信号写 pid 文件，支持 `hap-token refresh` 用 SIGUSR1 kick
    pid_file = Path.home() / ".local" / "share" / "hap-token-broker" / "broker.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return broker.run_forever()
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
