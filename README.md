# Hermes GUI Agent

AI 驱动的 Windows 自动化运维平台。通过云端技能分发 + Windows Agent 后台执行，实现鼠标键盘操控、文件检索、病毒查杀、系统修复等自动化能力。AI 作为决策引擎，通过自然语言对话即可完成复杂运维任务。

## 核心能力

```
你说"帮我检查这台电脑有没有病毒" 
    → Chat Server (DeepSeek 理解意图)
    → Bridge 下发技能指令
    → Windows Agent 后台执行（进程扫描、文件检索、注册表检查）
    → 结果回传，AI 分析并给出建议
```

### Windows Agent 能做什么

| 能力 | 实现方式 | 用途 |
|------|----------|------|
| 🖱️ 鼠标键盘操控 | ctypes 直接发送输入事件 | 操作任意桌面应用 |
| 📸 截图分析 | mss 快速截图 + vision 模型识别 | 定位 UI 元素、验证操作结果 |
| 🔍 后台文件检索 | cmd 命令执行 | 全盘搜索、文件内容检查 |
| 🛡️ 病毒查杀 | 多引擎扫描（进程/签名/网络/持久化 7 层 41+ 检查项） | 安全运维 |
| 🔧 系统修复 | 自动诊断 + 修复 | Windows 常见问题一键修复 |
| 📋 剪贴板读写 | Win32 clipboard API | 数据传输 |

### 技能分发体系

Hermes 云端维护了一套技能库，通过 Bridge 下发给 Windows Agent：

- **windows-malware-scan** — 恶意软件全面扫描（7 层 41+ 检查项）
- **windows-repair** — 系统诊断与自动修复
- **windows-mouse-precision** — 视觉定位 + 反馈闭环的精准鼠标操作
- **windows-cmd-reference** — 120+ 常用 Windows 命令速查

Agent 收到技能指令后本地执行，结果回传 AI 分析，全程无需人工远程桌面。

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
