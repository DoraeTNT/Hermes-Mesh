# Hermes GUI Agent

让 Windows 电脑接入 Hermes 平台的 Agent 客户端。安装后，Windows 主机可通过 Bridge 长连接使用 Hermes 的技能（Skills）、AI 对话、截图分析等能力，实现鼠标键盘操控、文件检索、病毒查杀、系统修复等自动化运维。

## 工作原理

```
Windows 装 Agent → 连上 Bridge → 这台电脑就能用 Hermes 的全部能力了
                                    ↑
                            你说"查一下有没有病毒"
                            Chat Server 理解意图 → 调用对应 Skill
                            → Agent 在 Windows 上后台执行
                            → 结果回传 AI 分析
```

## Windows 装上 Agent 后能用什么

| 能力 | 说明 |
|------|------|
| 🖱️ 鼠标键盘操控 | 模拟真人操作桌面应用 |
| 📸 截图 + AI 分析 | 视觉模型识别界面元素、验证结果 |
| 🔍 文件检索 | 全盘搜索、内容扫描 |
| 🛡️ 病毒查杀 | 进程/签名/网络/持久化 7 层检查 |
| 🔧 系统修复 | 自动诊断 Windows 常见问题 |
| 💬 AI 对话 | 接入 Hermes Skill 体系，LLM 驱动的交互 |

## 架构

```
┌─────────────────────────────────────────────────┐
│  Linux 服务端 (阿里云 ECS)                         │
│                                                   │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐       │
│  │ Bridge  │  │ Chat     │  │ Dashboard │       │
│  │ v5.0    │  │ DeepSeek │  │ 监控看板   │       │
│  └────┬────┘  └──────────┘  └───────────┘       │
│       │       ┌──────────┐  ┌───────────┐       │
│       │       │ Update   │  │ Threat    │       │
│       │       │ OTA 更新  │  │ 情报引擎   │       │
│       │       └──────────┘  └───────────┘       │
│       │                                           │
│  ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─    │
│  Nginx (反代对外暴露)                               │
└───────┼───────────────────────────────────────────┘
        │  TCP 长连接 (v5 协议, HMAC 认证)
        │
┌───────┴───────────────────────────────────────────┐
│  Windows Agent (后台运行, PyInstaller 打包 .exe)     │
│                                                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │ Launcher │ │ Agent    │ │ Skill Executor   │  │
│  │ 启 动器  │ │ 协议通信  │ │ 技能执行引擎      │  │
│  └──────────┘ └──────────┘ └──────────────────┘  │
│                                                    │
│  后台执行: 鼠标键盘操控 / 截图 / 命令 / 文件检索      │
└────────────────────────────────────────────────────┘
```

## 项目结构

```
hermes_gui_agent/
├── server/               # 服务端（Linux）
│   ├── bridge.py         # TCP Bridge v5.0 + HTTP API
│   ├── chat_server.py    # LLM 对话 + Bridge 调用
│   ├── dashboard.py      # Agent 状态监控
│   ├── update_server.py  # 客户端 OTA 更新
│   └── threat_engine.py  # 威胁情报引擎
├── client/               # Windows Agent 源码 (→ PyInstaller .exe)
│   ├── agent.py          # TCP 连接 + 技能执行
│   ├── unified.py        # GUI 托盘界面
│   ├── crypto_loader.py  # 加密配置解密
│   └── build.bat         # 构建脚本
├── hermes/               # 共享库
│   ├── server_config.py  # 统一配置（密钥/端口/路径）
│   ├── packet.py         # v5 协议定义
│   ├── stream_manager.py # 视频流管理
│   └── services/         # 后台服务
├── scripts/              # systemd 服务定义
└── requirements.txt
```

## 服务端口

| 服务 | 端口 | 用途 |
|------|------|------|
| Bridge TCP | 25917 | Agent 长连接 |
| Bridge HTTP | 9123 | 内部 API |
| Chat Server | 8891 | LLM 对话 |
| Dashboard | 8892 | 监控看板 |
| Update Server | 8890 | OTA 更新 |
| Threat Engine | 8893 | 威胁情报 |

## 部署

```bash
# 服务端
pip install -r requirements.txt
sudo cp scripts/hermes-*.service /etc/systemd/system/
sudo systemctl enable --now hermes-bridge hermes-chat hermes-dashboard

# Windows 客户端
cd client && build.bat
# → dist/HermesLauncher.exe
```

## 关于本项目

> 本项目由 **AI 自主构建**，经测试可正常使用。如有问题或更好的想法，欢迎通过 [GitHub Issues](../../issues) 留言反馈。

## 依赖

- Python 3.11+ | FFmpeg | DeepSeek API
- 阿里云 ECS（服务端）| Windows 10/11（Agent 端）
