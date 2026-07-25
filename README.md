# Hermes GUI Agent

远程 Windows 桌面管理与 Agent 协作平台。通过 TCP Bridge + HTTP API 实现对多台 Windows 主机的远程控制、实时视频流、文件传输、LLM 对话等功能。

## 架构

```
┌──────────────────────────────────────────────────┐
│  Linux 服务端                                       │
│                                                    │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐        │
│  │ Bridge  │  │ Chat     │  │ Dashboard │        │
│  │ :25917  │  │ :8891    │  │ :8892     │        │
│  │ :9123   │  └──────────┘  └───────────┘        │
│  └────┬────┘  ┌──────────┐  ┌───────────┐        │
│       │       │ Update   │  │ Threat    │        │
│       │       │ :8890    │  │ :8893     │        │
│       │       └──────────┘  └───────────┘        │
│       │                                            │
│  ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─     │
│  Nginx│(:80/:443)                                  │
└───────┼────────────────────────────────────────────┘
        │  TCP (v5 protocol, HMAC auth)
        │
┌───────┴────────────────────────────────────────────┐
│  Windows Agent (PyInstaller .exe)                   │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐   │
│  │ Launcher │  │ Unified  │  │ Streamer       │   │
│  │ (bootstrap)│ │ (GUI+tray)│ │ (FFmpeg H.264) │   │
│  └──────────┘  └──────────┘  └────────────────┘   │
│                                                     │
│  - 远程命令执行 + 截图                                │
│  - 实时视频流 (H.264 fMP4 / MJPEG)                   │
│  - LLM 对话集成                                      │
│  - 热更新 (Swap modules/*.py)                        │
└─────────────────────────────────────────────────────┘
```

## 项目结构

```
hermes_gui_agent/
├── server/               # 服务端（Linux）
│   ├── bridge.py         # TCP Bridge v5.0 + HTTP API (:25917/:9123)
│   ├── chat_server.py    # LLM 对话 API (:8891)
│   ├── dashboard.py      # Web 监控看板 (:8892)
│   ├── update_server.py  # 客户端 OTA 更新 (:8890)
│   ├── threat_engine.py  # 威胁情报引擎 (:8893)
│   └── video.html        # 实时视频查看页面
├── client/               # Windows 客户端源码
│   ├── launcher.py       # 薄启动器 (→ exe)
│   ├── unified.py        # GUI 界面 + 托盘
│   ├── agent.py          # TCP 连接 + 协议处理
│   ├── streamer.py       # 屏幕采集 + FFmpeg 编码
│   ├── runtime.py        # 运行时环境
│   ├── crypto_loader.py  # 加密配置解密
│   └── build.bat         # PyInstaller 构建脚本
├── modules/              # 客户端热更新模块副本
│   ├── unified.py
│   ├── agent.py
│   ├── streamer.py
│   ├── runtime.py
│   ├── launcher.py
│   └── crypto_loader.py
├── hermes/               # 共享库
│   ├── config.py         # 统一日志配置
│   ├── server_config.py  # 密钥/端口/路径配置（唯一真源）
│   ├── packet.py         # v5 协议定义
│   ├── stream_manager.py # 视频流管理（FFmpeg→fMP4→WebSocket）
│   ├── updater.py        # OTA 更新逻辑
│   ├── agent.py          # Agent 连接模型
│   ├── chat.py           # LLM 对话处理
│   ├── core/             # 核心模块
│   │   ├── bridge.py     # Bridge 内部逻辑
│   │   ├── security.py   # 安全/认证
│   │   └── packet.py     # 协议数据包
│   ├── api/              # API 路由
│   └── services/         # 后台服务
├── scripts/              # systemd 服务定义
├── tests/                # 测试
├── docs/                 # 文档
├── requirements.txt      # Python 依赖
└── versions.json         # 客户端版本元数据
```

## 服务端口

| 服务 | 端口 | 协议 | 用途 |
|------|------|------|------|
| Bridge TCP | 25917 | TCP (v5) | Agent 长连接 |
| Bridge HTTP | 9123 | HTTP | 内部 API |
| Update Server | 8890 | HTTP | 客户端下载更新 |
| Chat Server | 8891 | HTTP | LLM 对话 API |
| Dashboard | 8892 | HTTP | 监控看板 |
| Threat Engine | 8893 | HTTP | 威胁情报 |

## 核心功能

### 远程控制
- Windows Agent 通过 TCP 长连接注册到 Bridge
- 支持命令执行、截图、文件传输、屏幕采集
- v5 协议：HMAC 认证、ACK 两阶段确认

### 实时视频流
- FFmpeg 采集 Windows 桌面 (gdigrab)
- H.264 编码 → fMP4 分段 → MediaSource API 浏览器播放
- MJPEG 备用方案（`<img>` 标签原生支持）
- 自动清理断开设备的 FFmpeg 进程（防内存泄漏）

### 热更新
- 客户端启动时检查 `versions.json`
- 自动下载新版本模块文件 (`modules/*.py`)
- 无需重新打包 exe，替换 `.py` 即可生效

### 安全
- 加密配置：`_enc_config.py`（XOR + base64），客户端零明文 IP
- 所有密钥通过环境变量注入，无硬编码
- 速率限制（60 req / 60s 窗口，本地 IP 白名单绕过）

## 部署

### 服务端
```bash
# 安装依赖
pip install -r requirements.txt

# 配置密钥（环境变量或 ~/.hermes/.env）
export SHARED_SECRET=your_shared_secret
export CHAT_API_KEY=your_chat_key
export DASHBOARD_API_KEY=your_dashboard_key
export THREAT_API_KEY=your_threat_key
export UPDATE_API_KEY=your_update_key
export REQUIRE_CUSTOM_KEYS=0   # 开发环境跳过强制密钥检查

# 安装 systemd 服务
sudo cp scripts/hermes-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-bridge hermes-chat hermes-dashboard hermes-update-server hermes-threat
```

### Windows 客户端
```batch
cd client
build.bat
:: 产物在 dist/HermesLauncher.exe + dist/modules/
```

## 版本

**Agent v4.1** — 当前版本，定义于 `hermes/server_config.py`。

## 依赖

- Python 3.11+
- FFmpeg（服务端视频流转码、客户端屏幕采集）
- DeepSeek API（聊天功能）
- 阿里云 ECS（当前生产环境）
