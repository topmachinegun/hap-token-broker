"""调 md-generate-mcp-config 刷新 token，或通过 OAuth2 refresh_token 自刷。"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# md-generate-mcp-config 输出 JSON 的顶层 key（hap-oauth-mcp 默认）
MCP_KEY = "HAP Personal MCP"


class RefreshError(RuntimeError):
    """刷新失败（凭证错 / 服务异常 / 二进制缺失）。错误消息已脱敏。"""


def md_generate(md_bin: Path, account: str, password: str, oauth_app_id: str, timeout: int = 60) -> tuple[str, int]:
    """调 md-generate-mcp-config。

    Returns:
        (url, duration_ms)
    Raises:
        RefreshError 带脱敏的失败信息与诊断提示。
    """
    md_bin = md_bin.expanduser()
    if not md_bin.exists():
        raise RefreshError(
            f"md-generate-mcp-config 不存在: {md_bin}。"
            f"请先安装 hap-oauth-mcp skill。"
        )

    cmd = [
        str(md_bin),
        "--account", account,
        "--password", password,
        "--oauth-app-id", oauth_app_id,
        "--no-open-browser",
        "--skip-wait",
        "--log-page-size", "1",
    ]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RefreshError(f"md-generate-mcp-config 超时 ({timeout}s)") from e
    except OSError as e:
        raise RefreshError(f"md-generate-mcp-config 启动失败: {e}") from e
    duration_ms = int((time.monotonic() - t0) * 1000)

    if proc.returncode != 0:
        err_tail = (proc.stderr or "")[-400:].strip()
        hint = ""
        if "服务异常" in err_tail or '"state":0' in err_tail or '"state": 0' in err_tail:
            hint = (
                " | 诊断提示：明道云 MDAccountLogin「服务异常」是反刷库脱敏响应，概率降序："
                "①账号不存在/拼错 ②密码错 ③账号风控 ④OAuth App 被撤销 ⑤服务端真挂。"
                "先去 https://www.mingdao.com 手工登录验证账号密码。"
            )
        raise RefreshError(
            f"md-generate-mcp-config exit={proc.returncode}; stderr_tail: {err_tail}{hint}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RefreshError(f"md-generate-mcp-config 输出非 JSON: {e}") from e

    if not isinstance(data, dict):
        raise RefreshError("md-generate-mcp-config 输出不是 JSON object")

    entry = data.get(MCP_KEY)
    if not entry:
        entry = next(iter(data.values()), None)
    url = (entry or {}).get("url") if isinstance(entry, dict) else None
    if not url or "Authorization=Bearer" not in url:
        raise RefreshError("md-generate-mcp-config 输出里没找到合法 MCP URL")

    return _encode_bearer_space(url), duration_ms


def _encode_bearer_space(url: str) -> str:
    """Bearer 后面的空格 URL-encode 成 %20（urllib InvalidURL 避坑）。"""
    return url.replace("Bearer ", "Bearer%20")


def refresh_token_grant(
    host: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    timeout: int = 30,
) -> tuple[str, str, int]:
    """直接调 /oauth2/token 用 refresh_token 换新 access_token。

    Args:
        host: API 域名，如 api2.mingdao.com
        refresh_token: 当前的 refresh_token
        client_id: OAuth App 的 client_id（即 oauth_app_id）
        client_secret: OAuth App 的 client_secret
        timeout: HTTP 超时秒数

    Returns:
        (mcp_url, new_refresh_token, duration_ms)

    Raises:
        RefreshError 带脱敏的失败信息。
    """
    url = f"https://{host}/oauth2/token"
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        raise RefreshError(f"OAuth2 token endpoint 不可达 ({host}): {e.reason}") from e
    except OSError as e:
        raise RefreshError(f"OAuth2 token 请求失败: {e}") from e
    duration_ms = int((time.monotonic() - t0) * 1000)

    if status != 200:
        # 尝试解析错误信息
        err_msg = body[:400]
        try:
            err_data = json.loads(body)
            err_msg = err_data.get("error_description", err_data.get("error", err_msg))
        except (json.JSONDecodeError, TypeError):
            pass
        hint = ""
        if "invalid_grant" in err_msg or "invalid" in str(err_msg).lower():
            hint = (
                " | 诊断提示：refresh_token 已失效（过期/被撤销/已在服务端使用过）。"
                "需重新获取 refresh_token：从明道云后台「已授权账户」点「重新授权」，"
                "或从 152 服务器获取有效的 refresh_token。"
            )
        raise RefreshError(
            f"OAuth2 refresh_token grant 失败 (HTTP {status}): {err_msg}{hint}"
        )

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RefreshError(f"OAuth2 token 响应非 JSON: {e}") from e

    access_token = data.get("access_token")
    if not access_token:
        raise RefreshError(f"OAuth2 token 响应缺少 access_token: {body[:200]}")

    new_refresh_token = data.get("refresh_token", refresh_token)
    mcp_url = f"https://{host}/mcp?Authorization=Bearer%20{access_token}"

    return mcp_url, new_refresh_token, duration_ms
