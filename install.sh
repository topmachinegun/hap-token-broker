#!/usr/bin/env bash
# HAP Token Broker - Linux systemd 一键安装 / 升级脚本
#
# 用法:
#   bash install.sh                    # 安装或升级（需要 root）
#   bash install.sh --uninstall        # 停止并卸载
#   bash install.sh --restart          # 仅重启 daemon
#
# 做什么:
#   1. 检测 Python 3.11+（tomllib 必需）
#   2. 生成 /etc/systemd/system/hap-token-broker.service（占位符替换）
#   3. 若不存在则拷贝 config.example.toml → $HOME/.config/hap-token-broker/config.toml
#   4. 建立 /usr/local/bin/hap-token symlink（指向 scripts/broker/cli.py）
#   5. systemctl daemon-reload + enable + start
#
# 目标环境: Ubuntu 22.04+ / Debian 12+ / 其他 systemd 253+ 发行版

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="hap-token-broker"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_SRC="$REPO_ROOT/systemd/${SERVICE_NAME}.service.template"

# 允许通过环境变量指定运行身份；默认用 root（生产 152 场景）
RUN_USER="${BROKER_USER:-root}"
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
[[ -z "$RUN_HOME" ]] && { echo "用户 $RUN_USER 不存在"; exit 1; }

CONFIG_DIR="$RUN_HOME/.config/hap-token-broker"
CONFIG_FILE="$CONFIG_DIR/config.toml"
DATA_DIR="$RUN_HOME/.local/share/hap-token-broker"
SYMLINK="/usr/local/bin/hap-token"

log() { printf '\033[36m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "请用 root 运行: sudo bash $0"
    exit 1
  fi
}

uninstall() {
  require_root
  log "uninstalling ${SERVICE_NAME}..."
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$SERVICE_DST"
  systemctl daemon-reload
  rm -f "$SYMLINK"
  log "done. 配置 $CONFIG_FILE 与 token 数据保留在 $DATA_DIR，如需清理请手动 rm。"
}

restart_only() {
  require_root
  systemctl restart "${SERVICE_NAME}"
  systemctl status "${SERVICE_NAME}" --no-pager | head -15
}

case "${1:-}" in
  --uninstall) uninstall; exit 0 ;;
  --restart)   restart_only; exit 0 ;;
esac

require_root

# ---- 1. 找 Python 3.11+ ----
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for cand in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys,tomllib; assert sys.version_info>=(3,11)' 2>/dev/null; then
        PYTHON_BIN=$(command -v "$cand")
        break
      fi
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  err "未找到 Python 3.11+（需要内置 tomllib）。Ubuntu: apt install python3.12"
  exit 1
fi
log "python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"
log "run user: $RUN_USER (HOME=$RUN_HOME)"

# ---- 2. 建目录 ----
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$CONFIG_DIR"
install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$DATA_DIR" "$DATA_DIR/tokens"

# ---- 3. 配置文件 ----
if [[ ! -f "$CONFIG_FILE" ]]; then
  install -m 600 -o "$RUN_USER" -g "$RUN_USER" "$REPO_ROOT/config.example.toml" "$CONFIG_FILE"
  log "已生成 $CONFIG_FILE （权限 600）"
  log "⚠️  请编辑并填入真实 account/password/oauth_app_id，然后执行："
  log "    sudo bash $0 --restart"
else
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
  chown "$RUN_USER:$RUN_USER" "$CONFIG_FILE" 2>/dev/null || true
  log "已存在 $CONFIG_FILE，保留不覆盖。"
fi

# ---- 4. service 占位符替换 ----
[[ -f "$SERVICE_SRC" ]] || { err "模板缺失: $SERVICE_SRC"; exit 1; }
sed \
  -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
  -e "s|__INSTALL_DIR__|${REPO_ROOT}|g" \
  -e "s|__USER__|${RUN_USER}|g" \
  "$SERVICE_SRC" > "$SERVICE_DST"
chmod 644 "$SERVICE_DST"
log "已写 $SERVICE_DST"

# ---- 5. CLI symlink ----
CLI_TARGET="$REPO_ROOT/bin/hap-token"
chmod +x "$CLI_TARGET" 2>/dev/null || true
ln -sfn "$CLI_TARGET" "$SYMLINK"
log "CLI 入口: $SYMLINK → $CLI_TARGET"

# ---- 6. systemctl ----
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl restart "${SERVICE_NAME}"
sleep 1
systemctl --no-pager status "${SERVICE_NAME}" | head -12 || true
log ""
log "✅ 安装完成。"
log "日志:  journalctl -u ${SERVICE_NAME} -f"
log "状态:  systemctl status ${SERVICE_NAME}"
log "CLI:   hap-token status"
log "触发刷新: hap-token refresh claw-crm  (或 systemctl kill -s SIGUSR1 ${SERVICE_NAME})"
