#!/usr/bin/env python3
"""HAP Token Broker 守护进程。

由 systemd 托管，每 check_interval_minutes 巡检一次：
  - 缺 token 或距离 expires_at 小于 refresh_before_expire_hours 即刷新
  - 刷新使用 password grant (md-generate-mcp-config)
  - 刷新成功：写主仓库 + 可选 mirror 到 legacy 路径
  - 刷新失败：累计计数，连续超过阈值触告警

信号：
  SIGUSR1 → 立即对所有 profile 跑一轮刷新
  SIGTERM / SIGINT → 优雅退出

调试：
  python3 broker.py --oneshot       # 单次巡检后退出
  python3 broker.py --config PATH   # 覆盖 config 路径
"""
from __future__ import annotations

import argparse
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
        try:
            url, duration_ms = refresher.md_generate(
                self.cfg.md_generate_bin,
                profile.account,
                profile.password,
                profile.oauth_app_id,
            )
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

        record = storage.build_record(name, url, profile.oauth_app_id, profile.account, duration_ms)
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

    def run_once(self) -> None:
        """对所有 profile 跑一轮检查 + 必要时刷新。"""
        for name in self.cfg.profiles:
            need, reason = self.needs_refresh(name)
            log.info(f"[{name}] check: need_refresh={need} ({reason})")
            if need:
                self.refresh_one(name)

    def run_forever(self) -> int:
        interval_sec = self.cfg.check_interval_minutes * 60
        log.info(
            f"broker started, mode=password_grant, config={self.cfg.source_path}, "
            f"profiles={list(self.cfg.profiles.keys())}, "
            f"interval={self.cfg.check_interval_minutes}min, "
            f"refresh_before_expire={self.cfg.refresh_before_expire_hours}h"
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
