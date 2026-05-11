"""调 md-generate-mcp-config 通过 password grant 刷新 token。"""
from __future__ import annotations

import json
import subprocess
import time
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
