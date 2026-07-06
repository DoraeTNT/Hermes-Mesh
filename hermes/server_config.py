"""
Hermes Agent 共享配置
所有服务从这里读取密钥、端口、URL，不再硬编码。
优先级：环境变量 > .env 文件 > 默认值
"""

import os, sys, logging

# ── 统一日志配置（所有模块共享）──
# 必须最先初始化，否则后续模块的 basicConfig 调用会被跳过
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ── .env 文件加载 ──
def _load_env():
    env_path = os.path.expanduser("~/.hermes/.env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

_env = _load_env()

def get(key, default=""):
    """优先级：环境变量 > .env > 默认值"""
    return os.environ.get(key, _env.get(key, default))

# ── 密钥 ──
# 所有密钥必须通过环境变量或 ~/.hermes/.env 设置，无硬编码默认值
SHARED_SECRET     = get("SHARED_SECRET", "")
CHAT_API_KEY      = get("CHAT_API_KEY", "")
UPDATE_API_KEY    = get("UPDATE_API_KEY", "")
DASHBOARD_API_KEY = get("DASHBOARD_API_KEY", "")
THREAT_API_KEY    = get("THREAT_API_KEY", "")

# ── 安全启动检查 ──
# 默认强制要求自定义密钥（生产安全）。如需开发测试，设置 REQUIRE_CUSTOM_KEYS=0
_REQUIRE_CUSTOM = os.environ.get("REQUIRE_CUSTOM_KEYS", "1").lower() in ("1", "true", "yes")

_missing_keys = []
for _kname, _kval in [
    ("SHARED_SECRET",     SHARED_SECRET),
    ("CHAT_API_KEY",      CHAT_API_KEY),
    ("UPDATE_API_KEY",    UPDATE_API_KEY),
    ("DASHBOARD_API_KEY", DASHBOARD_API_KEY),
    ("THREAT_API_KEY",    THREAT_API_KEY),
]:
    if not _kval:
        _missing_keys.append(_kname)

if _missing_keys:
    import logging as _logging
    _logging.basicConfig(level=_logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _cfg_logger = _logging.getLogger("hermes.config")
    _cfg_logger.warning("⚠️  以下密钥未设置: %s", ', '.join(_missing_keys))
    _cfg_logger.warning("  请通过环境变量或 ~/.hermes/.env 设置:")
    _cfg_logger.warning("    echo 'SHARED_SECRET=your_key'     >> ~/.hermes/.env")
    _cfg_logger.warning("    echo 'CHAT_API_KEY=your_key'      >> ~/.hermes/.env")
    _cfg_logger.warning("    echo 'UPDATE_API_KEY=your_key'    >> ~/.hermes/.env")
    _cfg_logger.warning("    echo 'DASHBOARD_API_KEY=your_key' >> ~/.hermes/.env")
    _cfg_logger.warning("    echo 'THREAT_API_KEY=your_key'    >> ~/.hermes/.env")
    if _REQUIRE_CUSTOM:
        import sys as _sys
        _sys.exit("[SECURITY] 生产模式要求设置自定义密钥。如需跳过，设置 REQUIRE_CUSTOM_KEYS=0")

# ── 版本号（唯一真源，所有模块从这里引用）──
AGENT_VERSION = "4.1"

# ── LLM ──
LLM_API_KEY      = get("LLM_API_KEY", "") or get("DEEPSEEK_API_KEY", "") or CHAT_API_KEY  # 优先专用密钥，回退到通用密钥
LLM_BASE_URL     = get("CHAT_BASE_URL", "https://api.deepseek.com")
LLM_MODEL        = get("CHAT_MODEL", "deepseek-v4-flash")

# ── 端口 ──
BRIDGE_TCP_PORT  = int(get("BRIDGE_TCP_PORT", "25917"))
BRIDGE_HTTP_PORT = int(get("BRIDGE_HTTP_PORT", "9123"))
CHAT_PORT        = int(get("CHAT_PORT", "8891"))
UPDATE_PORT      = int(get("UPDATE_PORT", "8890"))

# ── 网络 ──
PUBLIC_IP        = get("PUBLIC_IP", "127.0.0.1")
PUBLIC_PORT      = int(get("PUBLIC_PORT", "80"))  # nginx 对外端口
PUBLIC_SCHEME    = get("PUBLIC_SCHEME", "http")

# ── TLS ──
# Bridge TCP TLS 配置。生产环境建议使用 Let’s Encrypt 真实证书。
# 若未提供证书，则回退到明文 TCP（开发/测试环境）。
TLS_ENABLED      = get("TLS_ENABLED", "0").lower() in ("1", "true", "yes")
TLS_CERT_FILE    = get("TLS_CERT_FILE", os.path.expanduser("~/.hermes/certs/server.crt"))
TLS_KEY_FILE     = get("TLS_KEY_FILE",  os.path.expanduser("~/.hermes/certs/server.key"))
TLS_CA_FILE      = get("TLS_CA_FILE",   os.path.expanduser("~/.hermes/certs/ca.crt"))  # Agent 端校验用

# ── 文件路径 ──
UPDATE_DIR       = get("UPDATE_DIR", "/home/admin/hermes_gui_agent")
SCREENSHOTS_DIR  = os.path.join(UPDATE_DIR, "screenshots")
VERSIONS_FILE    = os.path.join(UPDATE_DIR, "versions.json")

# ── 安全 ──
UPLOAD_MAX_SIZE  = int(get("UPLOAD_MAX_SIZE", str(100 * 1024 * 1024)))  # 100MB
UPLOAD_WHITELIST = set(get("UPLOAD_WHITELIST",
    "HermesUnified.exe,HermesAgent.exe,HermesChat.exe,WarehouseMS.exe,"
    "client/agent.py,client/unified.py,client/updater.py,client/gen_tls_certs.py,"
    "hermes/config.py,hermes/updater.py,hermes/agent.py,hermes/chat.py,hermes/main.py,hermes/__init__.py,"
    "hermes/server_config.py,hermes/packet.py,"
    "modules.json").split(","))

# ── 速率限制 ──
# 每个 IP 每窗口最大请求数，窗口大小（秒）
RATE_LIMIT_MAX    = int(get("RATE_LIMIT_MAX", "60"))      # 默认 60 请求/窗口
RATE_LIMIT_WINDOW = int(get("RATE_LIMIT_WINDOW", "60"))   # 默认 60 秒窗口
RATE_LIMIT_AUTH_BYPASS = get("RATE_LIMIT_AUTH_BYPASS", "1").lower() in ("1", "true", "yes")

# ── URL 生成 ──
def public_url(path, port=None):
    p = port or PUBLIC_PORT
    return f"{PUBLIC_SCHEME}://{PUBLIC_IP}:{p}{path}"
