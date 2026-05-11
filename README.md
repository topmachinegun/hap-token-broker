# hap-token-broker

HAP Token 中控服务。独立部署的服务器级 Token 刷新守护进程，支持多 profile 管理、过期巡检、refresh_token 直刷（方案A）和远程同步模式。

## 架构位置

```
L1: hap-token-broker     本仓库 — Token 中控服务（独立部署）
         │
         │ 提供 token URL（文件接口）
         ▼
L2: hap_app_access       基础技能 — HAP 通用访问方法论 + 共享代码
         │
         ▼
L3: 业务技能             各自独立 repo 分发
```

源码在 [hap-skill-claw-lite](https://github.com/topmachinegun/hap-skill-claw-lite) monorepo 的 `token-broker/` 下维护，本仓库为分发版（手动同步）。

## 快速开始

```bash
# 部署
sudo bash install.sh
sudo -e /root/.config/hap-token-broker/config.toml   # 填入真实凭据
sudo bash install.sh --restart

# 查看状态
hap-token status

# 列出 token
hap-token list
```

## 配置

复制 `config.example.toml` 为 `config.toml` 并填入凭据。支持两种刷新模式：

- **正常模式**：本机 password grant 登录 + refresh_token grant 自刷新（方案A）
- **sync 模式**：SSH 到远程服务器拉取已刷好的 token，本机不刷新（适用于多机器共享 OAuth）

详见 `config.example.toml` 注释。

## License

MIT
