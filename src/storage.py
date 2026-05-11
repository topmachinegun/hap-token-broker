"""Token 存储：每 profile 一份 JSON，原子写入；可选 legacy mirror。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_DIR = Path.home() / ".local" / "share" / "hap-token-broker" / "tokens"
TOKEN_TTL_HOURS = 23  # token 实际 ~24h，留 1h buffer


@dataclass
class TokenRecord:
    profile: str
    url: str
    fetched_at: datetime
    expires_at: datetime
    account_redacted: str
    oauth_app_id: str
    last_refresh_duration_ms: int | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def seconds_until_expiry(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds()

    def to_json(self) -> dict:
        d = {
            "profile": self.profile,
            "url": self.url,
            "fetched_at": _iso(self.fetched_at),
            "expires_at": _iso(self.expires_at),
            "account_redacted": self.account_redacted,
            "oauth_app_id": self.oauth_app_id,
            "last_refresh_duration_ms": self.last_refresh_duration_ms,
        }
        return d

    @classmethod
    def from_json(cls, d: dict) -> "TokenRecord":
        return cls(
            profile=d["profile"],
            url=d["url"],
            fetched_at=_parse_iso(d["fetched_at"]),
            expires_at=_parse_iso(d["expires_at"]),
            account_redacted=d.get("account_redacted", ""),
            oauth_app_id=d.get("oauth_app_id", ""),
            last_refresh_duration_ms=d.get("last_refresh_duration_ms"),
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def token_path(profile: str) -> Path:
    return TOKEN_DIR / f"{profile}.json"


def read(profile: str) -> TokenRecord | None:
    p = token_path(profile)
    if not p.exists():
        return None
    try:
        return TokenRecord.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def list_profiles() -> list[str]:
    if not TOKEN_DIR.exists():
        return []
    return sorted(p.stem for p in TOKEN_DIR.glob("*.json") if not p.stem.startswith("."))


def _atomic_write(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.rename(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_atomic(record: TokenRecord) -> None:
    _atomic_write(token_path(record.profile), record.to_json())


def mirror_to_legacy(record: TokenRecord, legacy_path: Path) -> None:
    """写老格式（mcp_token.py 兼容）：{url, fetched_at, expires_at}"""
    payload = {
        "url": record.url,
        "fetched_at": _iso(record.fetched_at),
        "expires_at": _iso(record.expires_at),
    }
    _atomic_write(legacy_path.expanduser(), payload)


def build_record(profile_name: str, url: str, oauth_app_id: str, account: str, duration_ms: int) -> TokenRecord:
    now = datetime.now(timezone.utc)
    return TokenRecord(
        profile=profile_name,
        url=url,
        fetched_at=now,
        expires_at=now + timedelta(hours=TOKEN_TTL_HOURS),
        account_redacted=redact_account(account),
        oauth_app_id=oauth_app_id,
        last_refresh_duration_ms=duration_ms,
    )


def redact_account(account: str) -> str:
    if len(account) < 8:
        return account[:2] + "..."
    return account[:5] + "..." + account[-4:]


def redact_url(url: str) -> str:
    if "Bearer" not in url:
        return url[:40] + "..."
    head, _, tail = url.partition("Bearer")
    tail = tail.lstrip("%20").lstrip(" ")
    return f"{head}Bearer%20...{tail[-6:]}"
