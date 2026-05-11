"""配置加载：TOML 格式（Python 3.11+ tomllib 零依赖）。

位置优先级：env TOKEN_BROKER_CONFIG > ~/.config/hap-token-broker/config.toml

Schema 示例见同级 `config.example.toml`。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("HAP Token Broker requires Python 3.11+ (tomllib).")

import tomllib  # noqa: E402

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "hap-token-broker" / "config.toml"
DEFAULT_MD_GEN_BIN = Path.home() / ".qoder" / "skills" / "hap-oauth-mcp" / ".venv" / "bin" / "md-generate-mcp-config"


class ConfigError(RuntimeError):
    """配置文件缺失/格式错/字段缺失。"""


@dataclass
class Profile:
    name: str
    account: str
    password: str
    oauth_app_id: str
    client_secret: str = ""


@dataclass
class SyncConfig:
    """远程同步配置：从 152 服务器拉取 token，本机不刷新。"""
    enabled: bool = False
    remote_host: str = ""
    remote_user: str = "ubuntu"
    remote_token_dir: str = "/root/.local/share/hap-token-broker/tokens"
    ssh_key: str = "~/.ssh/id_ed25519"

    def is_configured(self) -> bool:
        return self.enabled and bool(self.remote_host)


@dataclass
class Config:
    check_interval_minutes: int = 30
    refresh_before_expire_hours: float = 4.0
    max_consecutive_failures: int = 5
    md_generate_bin: Path = DEFAULT_MD_GEN_BIN
    profiles: dict[str, Profile] = field(default_factory=dict)
    mirror_to_legacy: dict[str, Path] = field(default_factory=dict)
    sync: SyncConfig = field(default_factory=SyncConfig)
    source_path: Path | None = None


def resolve_config_path() -> Path:
    override = os.environ.get("TOKEN_BROKER_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def _normalize_account(raw: str) -> str:
    """11 位以 1 开头的大陆号自动补 +86。其它原样返回。"""
    s = raw.strip()
    if s.startswith("+"):
        return s
    if len(s) == 11 and s.isdigit() and s.startswith("1"):
        return "+86" + s
    return s


def load_config(path: Path | None = None) -> Config:
    p = (path or resolve_config_path()).expanduser()
    if not p.exists():
        raise ConfigError(
            f"配置文件不存在: {p}。请参考仓库根 config.example.toml 创建，或运行 install.sh。"
        )

    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件 TOML 解析失败 ({p}): {e}") from e

    cfg = Config(source_path=p)

    refresh = raw.get("refresh", {}) or {}
    if "check_interval_minutes" in refresh:
        cfg.check_interval_minutes = int(refresh["check_interval_minutes"])
    if "refresh_before_expire_hours" in refresh:
        cfg.refresh_before_expire_hours = float(refresh["refresh_before_expire_hours"])
    if "max_consecutive_failures" in refresh:
        cfg.max_consecutive_failures = int(refresh["max_consecutive_failures"])

    if cfg.check_interval_minutes < 1:
        raise ConfigError("refresh.check_interval_minutes 必须 >= 1")
    if cfg.refresh_before_expire_hours <= 0:
        raise ConfigError("refresh.refresh_before_expire_hours 必须 > 0")

    if "md_generate_bin" in raw:
        cfg.md_generate_bin = Path(str(raw["md_generate_bin"])).expanduser()

    profiles_raw = raw.get("profiles", {}) or {}
    if not profiles_raw:
        raise ConfigError("profiles 为空：至少配置一个 [profiles.<name>] 段")

    for name, pdata in profiles_raw.items():
        if not isinstance(pdata, dict):
            raise ConfigError(f"[profiles.{name}] 必须是 table 段")
        account = str(pdata.get("account", "")).strip()
        password = str(pdata.get("password", "")).strip()
        oauth_app_id = str(pdata.get("oauth_app_id", "")).strip()
        client_secret = str(pdata.get("client_secret", "")).strip()
        missing = [k for k, v in [("account", account), ("password", password), ("oauth_app_id", oauth_app_id)] if not v]
        if missing:
            raise ConfigError(f"[profiles.{name}] 缺少必填字段: {', '.join(missing)}")
        cfg.profiles[name] = Profile(
            name=name,
            account=_normalize_account(account),
            password=password,
            oauth_app_id=oauth_app_id,
            client_secret=client_secret,
        )

    mirror_raw = raw.get("mirror_to_legacy", {}) or {}
    for k, v in mirror_raw.items():
        if k not in cfg.profiles:
            raise ConfigError(f"mirror_to_legacy.{k} 引用了不存在的 profile")
        cfg.mirror_to_legacy[k] = Path(str(v)).expanduser()

    # [sync] 远程同步配置（本机纯读模式）
    sync_raw = raw.get("sync", {}) or {}
    if sync_raw:
        cfg.sync = SyncConfig(
            enabled=bool(sync_raw.get("enabled", False)),
            remote_host=str(sync_raw.get("remote_host", "")).strip(),
            remote_user=str(sync_raw.get("remote_user", "ubuntu")).strip(),
            remote_token_dir=str(sync_raw.get("remote_token_dir", "/root/.local/share/hap-token-broker/tokens")).strip(),
            ssh_key=str(sync_raw.get("ssh_key", "~/.ssh/id_ed25519")).strip(),
        )
        if cfg.sync.enabled and not cfg.sync.remote_host:
            raise ConfigError("sync.enabled=true 但 remote_host 为空")

    return cfg


def check_config_permissions(path: Path) -> list[str]:
    """返回警告列表；空则权限正常。"""
    warnings: list[str] = []
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            warnings.append(
                f"{path} 权限 {oct(mode)} 过宽（应 chmod 600）"
            )
    except OSError:
        pass
    return warnings
