"""HAP Token Broker: 服务器级 Personal MCP Token 中控。

组件：
  - config.py     ：TOML 配置加载 + schema 校验
  - storage.py    ：token 原子读写 + legacy mirror
  - refresher.py  ：调 md-generate-mcp-config
  - broker.py     ：daemon 主循环（launchd 托管）
  - cli.py        ：hap-token 子命令
"""

__version__ = "1.0.0"
