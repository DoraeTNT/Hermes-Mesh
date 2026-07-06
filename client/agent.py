"""
Hermes GUI Agent v4.1 — Windows TCP + ACK Two-Phase + Capability
  HMAC | Device ID | Screenshot(quality/region) | cmd | download | clipboard | drag

v4.1 改进 (from v4.0):
  1. ACK 两阶段确认：收到命令立即回 ACK，执行完回 Done
  2. Capability 握手：连接时上报能力列表
  3. Python logging 统一日志
  4. Packet ID 追踪
  5. 向后兼容旧版 Bridge
"""

import hmac, hashlib, base64, time, uuid, threading, json, subprocess, os, sys, platform, re, struct, socket, ssl, io, traceback, math, ctypes, logging, tempfile, shutil, mimetypes, urllib.request, urllib.parse, itertools, binascii
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ── 统一日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes.agent")

# ── Config（通过加密加载器，环境变量优先）──
try:
    from crypto_loader import get_config
    HOST = get_config("SERVER_HOST", "")
    UPDATE_SERVER = get_config("UPDATE_URL", "")
    SECRET = get_config("SHARED_SECRET", "")
except ImportError:
    # 回退：未加密模式（开发/调试）
    HOST = os.environ.get("BRIDGE_HOST", "")
    UPDATE_SERVER = os.environ.get("UPDATE_SERVER", "")
    SECRET = os.environ.get("SHARED_SECRET", "")

PORT = int(os.environ.get("BRIDGE_TCP_PORT", "25917"))

RECONNECT_BASE = 3
RECONNECT_MAX  = 60
MAX_RECONNECT_ATTEMPTS = int(os.environ.get("MAX_RECONNECT_ATTEMPTS", "10"))
RECONNECT_COOLDOWN = int(os.environ.get("RECONNECT_COOLDOWN", "3600"))
SCREENSHOT_RETRIES = 3
SCREENSHOT_RETRY_DELAY = 1
CMD_WORKERS = 4

# ── Protocol ──
PROTOCOL_VERSION = 1

# ── Blacklist flag ──
BLACKLIST_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".blacklisted")

VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".version")
# ── 版本号：与 config.AGENT_VERSION 保持一致（canonical source: config.py）──
# Agent 独立运行在 Windows，无法导入 config.py，此处为同步副本
CURRENT_VERSION = "4.1"

def check_self_update():
    try:
        import urllib.request
        url = f"{UPDATE_SERVER}/agent_version.json"
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read())
        remote_ver = info.get("version", "0")
    except Exception:
        return False

    local_ver = CURRENT_VERSION
    if os.path.exists(VERSION_FILE):
        try:
            local_ver = open(VERSION_FILE).read().strip()
        except:
            pass

    if remote_ver <= local_ver:
        return False

    logger.info("UPDATE 发现新版本: %s → %s，正在下载...", local_ver, remote_ver)
    try:
        py_url = info.get("python_url")
        if not py_url:
            return False
        this_file = os.path.abspath(__file__)
        new_file = this_file + ".new"
        urllib.request.urlretrieve(py_url, new_file)
        bak_file = this_file + ".bak"
        if os.path.exists(bak_file):
            os.remove(bak_file)
        if os.path.exists(this_file):
            os.rename(this_file, bak_file)
        os.rename(new_file, this_file)
        with open(VERSION_FILE, "w") as f:
            f.write(remote_ver)
        logger.info("UPDATE 更新完成 %s，重启中...", remote_ver)
        return True
    except Exception as e:
        logger.error("UPDATE 下载失败: %s", e)
        return False

DID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".device_id")
if os.path.exists(DID_FILE):
    DEVICE_ID = open(DID_FILE).read().strip()
else:
    DEVICE_ID = str(uuid.uuid4())[:8]
    open(DID_FILE, "w").write(DEVICE_ID)
DEVICE_NAME = os.environ.get("DEVICE_NAME", platform.node())

# ── Agent 能力列表 ──
CAPABILITIES = [
    "screenshot", "click", "move", "drag", "keyboard",
    "type_text", "key", "press", "scroll", "cmd",
    "clipboard", "download", "open_url", "platform:windows",
    "stream",
]

# ── Thread Pool ──
executor = ThreadPoolExecutor(max_workers=CMD_WORKERS, thread_name_prefix="cmd")

# ── Socket send lock (线程安全：streamer 线程和主线程共享 socket) ──
_send_lock = threading.Lock()

# ── Streamer (lazy import) ──
_streamer = None

def _get_streamer():
    global _streamer
    if _streamer is None:
        try:
            import streamer as _mod
            _streamer = _mod
        except ImportError:
            logger.warning("[STREAM] streamer module not found")
    return _streamer

# ── Download Tracking ──
_downloads = {}
_dl_lock = threading.Lock()

# ── Network ──
def send_msg(sock, data):
    if isinstance(data, str): data = data.encode()
    with _send_lock:
        sock.sendall(struct.pack(">I", len(data)) + data)

def recv_msg(sock):
    hdr = sock.recv(4)
    if len(hdr) < 4: raise ConnectionError("short header")
    length = struct.unpack(">I", hdr)[0]
    if length > 5 * 1024 * 1024: raise ConnectionError(f"payload too large: {length}")
    data = b""
    reads = 0
    while len(data) < length:
        chunk = sock.recv(min(length - len(data), 65536))
        if not chunk: raise ConnectionError("connection closed")
        data += chunk
        reads += 1
        if reads > 1000:  # 防止恶意小数据包导致无限循环
            raise ConnectionError("too many recv calls")
    return data

def authenticate(sock):
    """认证握手，上报 capability"""
    msg = json.loads(recv_msg(sock))
    if msg.get("type") != "challenge":
        raise Exception("No challenge received")
    resp = hmac.new(SECRET.encode(), msg["challenge"].encode(), hashlib.sha256).hexdigest()
    auth_pkt = {
        "v": PROTOCOL_VERSION,
        "type": "auth_response",
        "response": resp,
        "hostname": platform.node(),
        "device_id": DEVICE_ID,
        "device_name": DEVICE_NAME,
        "capabilities": CAPABILITIES,
        "agent_version": CURRENT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
    }
    send_msg(sock, json.dumps(auth_pkt))
    logger.info("AUTH Authenticated as %s (%s) | caps=%d", DEVICE_NAME, DEVICE_ID, len(CAPABILITIES))


# ── Handlers ──
def do_screenshot(region=None, quality=None, fmt="png"):
    for attempt in range(SCREENSHOT_RETRIES):
        try:
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab(all_screens=True)
                if region and len(region) == 4:
                    img = img.crop(tuple(region))
                buf = io.BytesIO()
                if quality and 1 <= quality <= 100:
                    img = img.convert("RGB")
                    img.save(buf, "JPEG", quality=quality, optimize=True)
                    fmt = "jpeg"
                else:
                    img.save(buf, "PNG", optimize=True)
                    fmt = "png"
                return {"type": "screenshot", "data": base64.b64encode(buf.getvalue()).decode(),
                        "width": img.width, "height": img.height, "format": fmt, "size": buf.tell()}
            except ImportError:
                import subprocess
                ps_cmd = (
                    'Add-Type -AssemblyName System.Drawing,System.Windows.Forms;'
                    '$s=[System.Windows.Forms.Screen]::PrimaryScreen;'
                    '$b=New-Object System.Drawing.Bitmap($s.Bounds.Width,$s.Bounds.Height);'
                    '$g=[System.Drawing.Graphics]::FromImage($b);'
                    '$g.CopyFromScreen(0,0,0,0,$b.Size);'
                    '$ms=New-Object System.IO.MemoryStream;'
                    f'$b.Save($ms,[System.Drawing.Imaging.ImageFormat]::Jpeg);'
                    '$bytes=$ms.ToArray();'
                    '[Convert]::ToBase64String($bytes);'
                    '$g.Dispose();$b.Dispose();$ms.Dispose()'
                )
                q = quality or 50
                proc = subprocess.run(
                    ['powershell', '-NoProfile', '-Command', ps_cmd],
                    capture_output=True, text=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                b64 = proc.stdout.strip()
                if not b64:
                    raise RuntimeError(f"PS screenshot failed: {proc.stderr[:200]}")
                img_bytes = base64.b64decode(b64)
                return {"type": "screenshot", "data": b64,
                        "width": 0, "height": 0, "format": "jpeg",
                        "size": len(img_bytes)}
        except Exception as e:
            if attempt < SCREENSHOT_RETRIES - 1:
                logger.warning("SCREENSHOT Attempt %d failed: %s, retrying...", attempt+1, e)
                time.sleep(SCREENSHOT_RETRY_DELAY)
            else:
                logger.error("SCREENSHOT All %d attempts failed: %s", SCREENSHOT_RETRIES, e)
                return {"type": "screenshot", "error": str(e)}

# ── 命令注入防护 ──
# 黑名单字符：任何包含这些字符的命令都会被拒绝
_CMD_BLACKLIST = {';', '&', '|', '&&', '||', '$(', '`', '\n', '\r'}

def _is_safe_cmd(cmd: str) -> bool:
    """检查命令字符串是否包含危险字符，防止命令注入"""
    if not isinstance(cmd, str):
        return False
    # 拒绝包含任何黑名单字符的命令
    for bad in _CMD_BLACKLIST:
        if bad in cmd:
            return False
    return True

def do_cmd(cmd, timeout=60, session=False):
    """执行命令。session=True 使用持久 cmd 会话（保持环境变量/cwd）"""
    if not _is_safe_cmd(cmd):
        logger.warning("CMD blocked: contains dangerous characters: %s", cmd[:100])
        return {"type": "cmd", "error": "command contains dangerous characters (blocked for security)"}
    try:
        if session:
            from runtime import get_cmd_session
            s = get_cmd_session()
            result = s.run(cmd, timeout=timeout)
            return {"type": "cmd", **result}
        else:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout, errors='replace')
            return {"type": "cmd", "stdout": p.stdout, "stderr": p.stderr, "exit_code": p.returncode}
    except subprocess.TimeoutExpired:
        return {"type": "cmd", "error": "timeout", "cmd": cmd[:100]}
    except Exception as e:
        return {"type": "cmd", "error": str(e)}

def do_download(url, filename=None):
    import urllib.request, urllib.parse
    tid = str(int(time.time() * 1000))
    if not filename:
        filename = url.split("/")[-1].split("?")[0]
    filename = os.path.basename(filename)
    if not filename or filename in (".", ".."):
        filename = "download.bin"

    # URL 域名白名单（防 SSRF）
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        allowed = {"127.0.0.1", "localhost"}
        if UPDATE_SERVER:
            allowed.add(urllib.parse.urlparse(UPDATE_SERVER).hostname or "")
        if HOST:
            allowed.add(HOST)
        if host not in allowed:
            return {"type": "download", "error": f"domain not allowed: {host}"}
    except Exception:
        return {"type": "download", "error": "invalid URL"}

    path = os.path.join(os.environ.get("USERPROFILE", "C:\\"), "Downloads", filename)
    with _dl_lock:
        _downloads[tid] = {"url": url, "path": path, "status": "downloading", "started": time.time()}
    def _bg():
        try:
            urllib.request.urlretrieve(url, path)
            with _dl_lock:
                _downloads[tid]["status"] = "done"
            logger.info("DOWNLOAD Done: %s", filename)
        except Exception as e:
            with _dl_lock:
                _downloads[tid]["status"] = "error"
                _downloads[tid]["error"] = str(e)
            logger.error("DOWNLOAD Failed: %s", e)
    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return {"type": "download", "task_id": tid, "path": path, "status": "started"}

def do_download_status(tid=None):
    with _dl_lock:
        if tid:
            info = _downloads.get(tid)
            if not info:
                return {"type": "download_status", "error": "task not found"}
            elapsed = time.time() - info["started"]
            return {"type": "download_status", "task_id": tid, "status": info["status"],
                    "path": info.get("path"), "elapsed": round(elapsed, 1)}
        return {"type": "download_status", "tasks": len(_downloads)}

# ── Win32 Input ──
import ctypes as _ct
_udll = _ct.windll.user32

class _MOUSEINPUT(_ct.Structure):
    _fields_ = [("dx",_ct.c_long),("dy",_ct.c_long),("mouseData",_ct.c_ulong),
                ("dwFlags",_ct.c_ulong),("time",_ct.c_ulong),("dwExtraInfo",_ct.POINTER(_ct.c_ulong))]
class _KEYBDINPUT(_ct.Structure):
    _fields_ = [("wVk",_ct.c_ushort),("wScan",_ct.c_ushort),("dwFlags",_ct.c_ulong),
                ("time",_ct.c_ulong),("dwExtraInfo",_ct.POINTER(_ct.c_ulong))]
class _INPUTUNION(_ct.Union):
    _fields_ = [("mi",_MOUSEINPUT),("ki",_KEYBDINPUT)]
class _INPUT(_ct.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type",_ct.c_ulong),("_u",_INPUTUNION)]

MOUSEEVENTF_MOVE=0x0001; MOUSEEVENTF_LEFTDOWN=0x0002; MOUSEEVENTF_LEFTUP=0x0004; MOUSEEVENTF_ABSOLUTE=0x8000

def do_click(x, y, button="left"):
    sw, sh = _udll.GetSystemMetrics(0), _udll.GetSystemMetrics(1)
    ax, ay = int(x*65535/sw), int(y*65535/sh)
    events = (_INPUT * 3)()
    events[0].type = 0; events[0].mi.dx = ax; events[0].mi.dy = ay
    events[0].mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    events[1].type = 0; events[1].mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    events[2].type = 0; events[2].mi.dwFlags = MOUSEEVENTF_LEFTUP
    _udll.SendInput(3, events, _ct.sizeof(_INPUT))
    return {"type": "click", "x": x, "y": y, "status": "ok"}

def do_move(x, y):
    sw, sh = _udll.GetSystemMetrics(0), _udll.GetSystemMetrics(1)
    ax, ay = int(x*65535/sw), int(y*65535/sh)
    inp = _INPUT(); inp.type = 0
    inp.mi.dx = ax; inp.mi.dy = ay; inp.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    _udll.SendInput(1, _ct.byref(inp), _ct.sizeof(_INPUT))
    return {"type": "move", "x": x, "y": y, "status": "ok"}

def do_drag(x1, y1, x2, y2, button="left", steps=10, delay=0.01):
    import math
    sw, sh = _udll.GetSystemMetrics(0), _udll.GetSystemMetrics(1)
    ax1, ay1 = int(x1*65535/sw), int(y1*65535/sh)
    ev = _INPUT(); ev.type = 0; ev.mi.dx = ax1; ev.mi.dy = ay1
    ev.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    _udll.SendInput(1, _ct.byref(ev), _ct.sizeof(_INPUT))
    time.sleep(0.02)
    ev2 = _INPUT(); ev2.type = 0; ev2.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    _udll.SendInput(1, _ct.byref(ev2), _ct.sizeof(_INPUT))
    time.sleep(0.02)
    for i in range(1, steps+1):
        t = i / steps; et = 1 - (1-t)**3
        cx = int((x1 + (x2-x1)*et) * 65535 / sw)
        cy = int((y1 + (y2-y1)*et) * 65535 / sh)
        mev = _INPUT(); mev.type = 0; mev.mi.dx = cx; mev.mi.dy = cy
        mev.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        _udll.SendInput(1, _ct.byref(mev), _ct.sizeof(_INPUT))
        time.sleep(delay)
    rev = _INPUT(); rev.type = 0; rev.mi.dwFlags = MOUSEEVENTF_LEFTUP
    _udll.SendInput(1, _ct.byref(rev), _ct.sizeof(_INPUT))
    return {"type": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "status": "ok"}

def do_press(keys):
    import pyautogui
    if isinstance(keys, list): pyautogui.hotkey(*keys)
    else: pyautogui.press(keys)
    return {"type": "press", "status": "ok"}

def do_type_text(text):
    count = 0
    for ch in text:
        inp = _INPUT(); inp.type = 1
        inp.ki.wScan = ord(ch); inp.ki.dwFlags = 0x0004
        _udll.SendInput(1, _ct.byref(inp), _ct.sizeof(_INPUT))
        count += 1
    return {"type": "type_text", "count": count, "status": "ok"}

def do_key(key_code):
    inp = _INPUT(); inp.type = 1
    inp.ki.wVk = key_code
    _udll.SendInput(1, _ct.byref(inp), _ct.sizeof(_INPUT))
    return {"type": "key", "code": key_code, "status": "ok"}

def do_scroll(amount):
    import pyautogui; pyautogui.scroll(amount)
    return {"type": "scroll", "status": "ok"}

def do_clipboard(action="read", text=None):
    try:
        if action == "write" and text:
            safe = text.replace('"', '`"')
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f'Set-Clipboard -Value "{safe}"'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            if r.returncode != 0:
                return {"type": "clipboard", "error": f"write failed: {r.stderr.strip()[:100]}"}
            return {"type": "clipboard", "action": "write", "status": "ok", "length": len(text)}
        elif action == "read":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            result = r.stdout.strip()
            return {"type": "clipboard", "action": "read", "text": result, "length": len(result)}
        return {"type": "clipboard", "error": f"unknown action: {action}"}
    except Exception as e:
        return {"type": "clipboard", "error": str(e)}

def do_open_url(url):
    import webbrowser
    try:
        if not url or not url.startswith(("http://", "https://")):
            return {"error": "invalid URL"}
        webbrowser.open(url)
        return {"status": "ok", "url": url}
    except Exception as e:
        return {"error": str(e)}

# ── Stream handlers ──
def do_stream_start(params):
    m = _get_streamer()
    if not m:
        return {"error": "streamer module not available (FFmpeg required)"}
    # 从当前 socket 获取引用（agent_main 里面连接的 sock）
    sock = getattr(do_stream_start, '_sock', None)
    if not sock:
        return {"error": "socket not ready (agent not connected)"}
    return m.start_stream(
        sock=sock,
        sock_lock=_send_lock,
        fps=params.get("fps", 20),
        bitrate=params.get("bitrate", "2M"),
        width=params.get("width", 0),
        height=params.get("height", 0),
    )

def do_stream_stop():
    m = _get_streamer()
    if not m:
        return {"error": "streamer module not available"}
    return m.stop_stream()

def do_stream_status():
    m = _get_streamer()
    if not m:
        return {"error": "streamer module not available"}
    return m.stream_status()

# ── Handler Registry ──
HANDLERS = {
    "screenshot": lambda p: do_screenshot(p.get("region"), p.get("quality")),
    "cmd": lambda p: do_cmd(p.get("cmd",""), p.get("timeout",60), p.get("session", False)),
    "download": lambda p: do_download(p.get("url",""), p.get("filename")),
    "download_status": lambda p: do_download_status(p.get("task_id")),
    "click": lambda p: do_click(p.get("x",0), p.get("y",0)),
    "drag": lambda p: do_drag(p.get("x1",0), p.get("y1",0), p.get("x2",0), p.get("y2",0),
                              button=p.get("button","left"), steps=p.get("steps",10), delay=p.get("delay",0.01)),
    "press": lambda p: do_press(p.get("keys",[])),
    "scroll": lambda p: do_scroll(p.get("amount",100)),
    "move": lambda p: do_move(p.get("x",0), p.get("y",0)),
    "clipboard": lambda p: do_clipboard(p.get("action","read"), p.get("text")),
    "type_text": lambda p: do_type_text(p.get("text","")),
    "key": lambda p: do_key(p.get("code",0)),
    "open_url": lambda p: do_open_url(p.get("url","")),
    "stream_start": lambda p: do_stream_start(p),
    "stream_stop": lambda p: do_stream_stop(),
    "stream_status": lambda p: do_stream_status(),
}


def execute_action(action, params):
    handler = HANDLERS.get(action)
    if not handler:
        return {"error": f"unknown action: {action}"}
    try:
        future = executor.submit(handler, params)
        return future.result(timeout=180)
    except Exception as e:
        logger.error("EXEC Action %s failed: %s", action, e)
        return {"error": str(e)}


def agent_main():
    if not SECRET:
        logger.error("SECRET 未设置。请设置 SHARED_SECRET 环境变量后重试。")
        return

    if os.path.exists(BLACKLIST_FLAG):
        logger.error("[BLACKLIST] Local blacklist found (%s), remove this file to retry.", BLACKLIST_FLAG)
        return

    if not hasattr(sys.modules[__name__], '_in_launcher'):
        if check_self_update():
            import subprocess
            this_file = os.path.abspath(__file__)
            logger.info("UPDATE 重启 Agent: %s", this_file)
            time.sleep(1)
            subprocess.Popen([sys.executable, this_file],
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            return

    delay = RECONNECT_BASE
    stats = {"connects": 0, "commands": 0, "errors": 0}
    consecutive_failures = 0

    while True:
        try:
            sock = socket.socket()
            sock.settimeout(15)
            sock.connect((HOST, PORT))
            authenticate(sock)

            # 检查黑名单响应
            sock.settimeout(3)
            try:
                first_msg = json.loads(recv_msg(sock))
            except Exception:
                first_msg = None

            if first_msg and first_msg.get("type") == "blacklisted":
                logger.error("[BLACKLIST] Server blacklisted this agent: %s", first_msg.get('reason', 'unknown'))
                try:
                    open(BLACKLIST_FLAG, "w").write(first_msg.get("reason", "blacklisted"))
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                return

            stats["connects"] += 1
            delay = RECONNECT_BASE
            consecutive_failures = 0
            sock.settimeout(65)
            # 注入 socket 引用给 streamer
            do_stream_start._sock = sock
            if hasattr(sys.modules[__name__], '_on_status'):
                _on_status(True)
            logger.info("AGENT Connected #%d | Commands: %d", stats['connects'], stats['commands'])

            while True:
                msg = json.loads(recv_msg(sock))
                msg_type = msg.get("type", "")

                # 心跳
                if msg_type == "ping":
                    pong = {"v": PROTOCOL_VERSION, "type": "pong", "timestamp": time.time()}
                    send_msg(sock, json.dumps(pong))
                    continue

                # 新协议命令
                if msg_type == "command":
                    packet_id = msg.get("packet_id", "")
                    action = msg.get("action", "")
                    params = msg.get("params", {})

                    # 立即回 ACK
                    ack = {"v": PROTOCOL_VERSION, "type": "ack",
                           "packet_id": packet_id, "timestamp": time.time()}
                    send_msg(sock, json.dumps(ack))

                    # 执行命令
                    result = execute_action(action, params)
                    stats["commands"] += 1

                    # 回 Done
                    done = {"v": PROTOCOL_VERSION, "type": "done",
                            "packet_id": packet_id, "result": result,
                            "timestamp": time.time()}
                    send_msg(sock, json.dumps(done))
                    continue

                # 旧协议命令（兼容：无 type 字段，有 action 字段）
                if "action" in msg:
                    action = msg.get("action", "")
                    params = msg.get("params", {})
                    rid = msg.get("id", "")
                    result = execute_action(action, params)
                    stats["commands"] += 1
                    send_msg(sock, json.dumps({"id": rid, "result": result}))
                    continue

                logger.warning("AGENT Unknown message: %s", str(msg)[:200])

        except KeyboardInterrupt:
            logger.info("AGENT Shutting down...")
            executor.shutdown(wait=False)
            break
        except Exception as e:
            stats["errors"] += 1
            consecutive_failures += 1
            if hasattr(sys.modules[__name__], '_on_status'):
                _on_status(False)
            logger.error("AGENT Disconnected: %s | Retry %ds | Failures: %d/%d",
                        e, delay, consecutive_failures, MAX_RECONNECT_ATTEMPTS)

            if consecutive_failures >= MAX_RECONNECT_ATTEMPTS:
                logger.info("AGENT 连续重连 %d 次失败，进入冷却 %ds ...",
                           MAX_RECONNECT_ATTEMPTS, RECONNECT_COOLDOWN)
                time.sleep(RECONNECT_COOLDOWN)
                consecutive_failures = 0
                delay = RECONNECT_BASE
                logger.info("AGENT 冷却结束，重新尝试连接...")
            else:
                time.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)

if __name__ == "__main__":
    logger.info("INIT Agent v%s | [SERVER]:%d | %s (%s) | caps=%d",
                CURRENT_VERSION, HOST, PORT, DEVICE_NAME, DEVICE_ID, len(CAPABILITIES))
    logger.info("INIT ThreadPool: %d workers | Reconnect: %d-%ds | MaxAttempts: %d | Cooldown: %ds",
                CMD_WORKERS, RECONNECT_BASE, RECONNECT_MAX, MAX_RECONNECT_ATTEMPTS, RECONNECT_COOLDOWN)
    agent_main()
