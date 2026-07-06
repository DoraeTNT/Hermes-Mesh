"""
Hermes Unified v3.0 — GUI Agent + Chat UI 二合一
  - 后台静默运行 GUI Agent（无弹窗）
  - 前台只显示一个 Chat 对话窗口
  - Agent 启动日志显示为聊天区系统消息
  - 日志面板默认展开
  - 热更新支持（外部 updater 替换，不替换运行中的 exe）
  - 设备选择使用真实 device_id
  - /devices 后台轮询，不阻塞 UI
"""

import os, sys, threading, time, queue

# ── GUI ──
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# ── 静默模式：防止 exe 弹出控制台 ──
if sys.platform == "win32" and getattr(sys, "frozen", False):
    _null = open(os.devnull, "w")
    sys.stdout = _null
    sys.stderr = _null

# ── 版本 ──
# Hardcoded fallback version. Hot update writes version.txt with the server-provided
# version during do_restart(). We read version.txt first so the UI displays the actual
# updated version, not the stale hardcoded value. This also prevents infinite update
# loops: without this, VERSION stays at the hardcoded value and check_update() always
# thinks an update is needed.
_HARDCODED_VERSION = "3.13"

def _get_version():
    try:
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        ver_file = os.path.join(base_dir, "version.txt")
        if os.path.exists(ver_file):
            with open(ver_file) as f:
                ver = f.read().strip()
            if ver:
                return ver
    except:
        pass
    return _HARDCODED_VERSION

VERSION = _get_version()

# ── 热更新（加密配置 / 环境变量）──
try:
    from crypto_loader import get_config, get_api_key
    SERVER_URL = get_config("SERVER_URL", "")
    API_KEY = get_api_key()
except ImportError:
    SERVER_URL = ""
    API_KEY = ""

VERSION_URL = f"{SERVER_URL}/unified_version.json"


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

# ── 设备列表缓存（后台线程更新，UI 读取）──
devices_cache = {"devices": {}, "device_ids": [], "updated": 0}
devices_lock = threading.Lock()

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# ── 隐藏/最小化控制台窗口 ──
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

# ── 热更新 v3（语义版本 + SHA256 + 外部 updater）──
update_queue = queue.Queue(maxsize=5)

# ── 模块级热更新（内联，不依赖 hermes.updater）──
MODULES_JSON_URL = f"{SERVER_URL}/modules/modules.json"

def _parse_ver(v):
    import re
    return tuple(int(p) for p in re.findall(r'\d+', str(v))) or (0,)

def _check_modules():
    """对比 modules.json，返回需要更新的模块列表"""
    import urllib.request, json
    try:
        req = urllib.request.Request(MODULES_JSON_URL, headers={"X-Api-Key": API_KEY})
        resp = urllib.request.urlopen(req, timeout=10)
        remote = json.loads(resp.read())
    except Exception:
        return []

    base_dir = get_base_dir()
    local_path = os.path.join(base_dir, "modules", "modules.json")
    local = {}
    if os.path.exists(local_path):
        try:
            with open(local_path) as f:
                local = json.load(f)
        except:
            pass

    updates = []
    for mod_name, info in remote.get("modules", {}).items():
        remote_ver = info.get("version", "0")
        local_ver = local.get(mod_name, {}).get("version", "0")
        if _parse_ver(remote_ver) > _parse_ver(local_ver):
            updates.append({
                "name": mod_name,
                "version": remote_ver,
                "path": info["path"],
                "url": info["url"],
                "sha256": info.get("sha256", ""),
            })
    return updates

def _download_modules(updates):
    """下载更新的模块文件到本地，成功后保存版本并发送 ready 信号"""
    import urllib.request, json
    base_dir = get_base_dir()
    any_updated = False
    any_failed = False
    latest_version = VERSION

    for mod in updates:
        dest = os.path.join(base_dir, mod["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # 确保 hermes 包有 __init__.py
        pkg_dir = os.path.dirname(dest)
        if pkg_dir.endswith("hermes"):
            init_file = os.path.join(pkg_dir, "__init__.py")
            if not os.path.exists(init_file):
                try:
                    with open(init_file, "w") as f:
                        f.write("# hermes\n")
                except:
                    pass

        try:
            urllib.request.urlretrieve(mod["url"], dest)
            any_updated = True
            latest_version = mod["version"]
        except Exception as e:
            any_failed = True

    if not any_updated:
        return False

    # 保存本地 modules.json 防止重复下载
    local_path = os.path.join(base_dir, "modules", "modules.json")
    try:
        local = {}
        if os.path.exists(local_path):
            with open(local_path) as f:
                local = json.load(f)
        for mod in updates:
            local[mod["name"]] = {"version": mod["version"]}
        with open(local_path, "w") as f:
            json.dump(local, f)
    except:
        pass

    # 只发一次 ready 信号
    try:
        update_queue.put_nowait({"status": "ready", "version": latest_version})
    except:
        pass
    return not any_failed

def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except:
        return None

def _download_resumable(url, dest, expected_sha=None, min_size=1024, timeout=30):
    """支持断点续传的下载，向 UI 推送进度
    
    返回: (ok, msg)  msg 为 'ok' 或错误描述
    """
    import urllib.request, os
    tmp_path = dest + ".tmp"
    
    # 检查已有部分下载
    existing = 0
    headers = {"X-Api-Key": API_KEY}
    if os.path.exists(tmp_path):
        existing = os.path.getsize(tmp_path)
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
    
    # 估算总大小
    total_size = None
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"X-Api-Key": API_KEY})
        resp = urllib.request.urlopen(req, timeout=5)
        total_size = int(resp.headers.get("Content-Length", 0))
        if existing > 0:
            total_size += existing
    except:
        pass
    
    # 开始下载
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        downloaded = existing
        last_report = 0
        
        with open(tmp_path, "ab" if existing > 0 else "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                # 每 500KB 或 100% 报告一次
                if downloaded - last_report >= 524288 or (total_size and downloaded >= total_size):
                    last_report = downloaded
                    pct = round(downloaded / total_size * 100, 1) if total_size else 0
                    try:
                        update_queue.put_nowait({
                            "status": "progress",
                            "downloaded": downloaded,
                            "total": total_size,
                            "percent": pct,
                        })
                    except queue.Full:
                        pass
        
        # 下载完成，移除 .tmp 后缀
        if os.path.exists(dest):
            os.remove(dest)
        os.rename(tmp_path, dest)
        return True, "ok"
        
    except Exception as e:
        return False, str(e)

def _load_skipped_versions():
    """加载用户跳过的版本列表"""
    skip_file = os.path.join(get_base_dir(), ".skipped_versions")
    if os.path.exists(skip_file):
        try:
            with open(skip_file) as f:
                return set(line.strip() for line in f if line.strip())
        except:
            pass
    return set()

def _save_skipped_versions(skipped):
    """保存跳过的版本列表"""
    skip_file = os.path.join(get_base_dir(), ".skipped_versions")
    try:
        with open(skip_file, "w") as f:
            for v in skipped:
                f.write(v + "\n")
    except:
        pass

def _check_pending_update():
    """启动时检查是否有上次下载完成但未应用的更新"""
    tmp_dir = _get_tmp_dir()
    base_dir = get_base_dir()
    
    # exe 模式：检查是否有 HermesUnified_new.exe
    if getattr(sys, "frozen", False):
        new_exe = os.path.join(tmp_dir, "HermesUnified_new.exe")
        if os.path.exists(new_exe):
            # 检查是否有效（大小 > 1MB）
            if os.path.getsize(new_exe) > 1024 * 1024:
                # 读取版本号
                ver = "unknown"
                ver_file = os.path.join(tmp_dir, ".update_ver")
                if os.path.exists(ver_file):
                    ver = open(ver_file).read().strip()
                return {"status": "ready", "version": ver, "tmp": new_exe}
    else:
        new_py = os.path.join(tmp_dir, "hermes_unified_new.py")
        if os.path.exists(new_py):
            if os.path.getsize(new_py) > 500:
                ver = "unknown"
                ver_file = os.path.join(tmp_dir, ".update_ver")
                if os.path.exists(ver_file):
                    ver = open(ver_file).read().strip()
                return {"status": "ready", "version": ver, "tmp": new_py}
    return None

def _write_update_version(ver, tmp_dir):
    """写入待应用更新的版本号"""
    try:
        with open(os.path.join(tmp_dir, ".update_ver"), "w") as f:
            f.write(ver)
    except:
        pass

def _remove_old_backups():
    """清理旧备份文件"""
    base_dir = get_base_dir()
    for f in ("HermesUnified_old.exe", "hermes_unified_bak.py"):
        p = os.path.join(base_dir, f)
        if os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

def check_update():
    import urllib.request, json
    try:
        req = urllib.request.Request(VERSION_URL, headers={"X-Api-Key": API_KEY})
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read())
        remote_ver = info.get("version", "0.0")
        if _parse_ver(remote_ver) > _parse_ver(VERSION):
            return True, remote_ver, info
        return False, remote_ver, None
    except Exception as e:
        # #13: 错误不吞掉，写入日志
        agent_log("UPDATE", f"检查更新失败: {e}")
        return False, VERSION, None

def _verify_download(path, expected_sha256=None, min_size=1024):
    if not os.path.exists(path):
        return False, "文件不存在"
    size = os.path.getsize(path)
    if size < min_size:
        os.remove(path)
        return False, f"文件太小 ({size} < {min_size})"
    if expected_sha256:
        actual = _file_sha256(path)
        if actual != expected_sha256:
            os.remove(path)
            return False, f"SHA256 校验失败"
    return True, "ok"

def _get_tmp_dir():
    import tempfile
    base_dir = get_base_dir()
    tmp_dir = os.path.join(base_dir, "_update_tmp")
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        test_file = os.path.join(tmp_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return tmp_dir
    except (OSError, PermissionError):
        pass
    sys_tmp = os.path.join(tempfile.gettempdir(), "hermes_update")
    os.makedirs(sys_tmp, exist_ok=True)
    return sys_tmp

def download_update(info):
    import urllib.request
    remote_ver = info.get("version", "unknown")
    base_dir = get_base_dir()
    tmp_dir = _get_tmp_dir()
    modules_dir = os.path.join(base_dir, "modules")
    
    # 记录版本号到 .update_ver
    _write_update_version(remote_ver, tmp_dir)

    # 下载模块 .py 文件到 modules/ 目录（frozen 和非 frozen 都走这条路）
    py_url = info.get("py_url", "")
    if not py_url:
        return
    tmp = os.path.join(tmp_dir, "hermes_unified_new.py")
    expected_sha = info.get("py_sha256")

    try:
        update_queue.put_nowait({"status": "downloading", "version": remote_ver})
        ok, msg = _download_resumable(py_url, tmp, expected_sha, 500)
        if not ok:
            update_queue.put_nowait({"status": "error", "msg": f"下载失败: {msg}"})
            return
        update_queue.put_nowait({"status": "ready", "version": remote_ver, "tmp": tmp,
                                 "modules_dir": modules_dir})
    except Exception as e:
        update_queue.put_nowait({"status": "error", "msg": str(e)[:80]})

def do_restart(info):
    """用户点击重启 → 启动外部 updater，然后退出当前进程"""
    import subprocess
    remote_ver = info.get("version", "unknown")
    tmp = info.get("tmp", "")
    base_dir = get_base_dir()

    # 模块热更新：文件已在 modules/ 下，直接重启即可
    if not tmp:
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            os.startfile(sys.executable)
        else:
            os.execv(sys.executable, [sys.executable])
        os._exit(0)
        return

    if not os.path.exists(tmp):
        update_queue.put_nowait({"status": "error", "msg": "更新文件不存在"})
        return

    if getattr(sys, "frozen", False):
        # 模块热更新：下载的 .py 替换到 modules/ 目录，重启 exe 加载新模块
        modules_dir = info.get("modules_dir", os.path.join(base_dir, "modules"))
        dest = os.path.join(modules_dir, "unified.py")
        backup = os.path.join(modules_dir, "unified_bak.py")
        try:
            if os.path.exists(backup):
                os.remove(backup)
            if os.path.exists(dest):
                os.replace(dest, backup)
            os.replace(tmp, dest)
            with open(os.path.join(base_dir, "version.txt"), "w") as f:
                f.write(remote_ver)
            # 重启 launcher
            if sys.platform == "win32":
                os.startfile(sys.executable)
            else:
                os.execv(sys.executable, [sys.executable])
            os._exit(0)
        except Exception as e:
            update_queue.put_nowait({"status": "error", "msg": f"替换失败: {e}"})
    else:
        # .py 模式：直接替换
        final = os.path.join(base_dir, "hermes_unified.py")
        backup = os.path.join(base_dir, "hermes_unified_bak.py")
        try:
            if os.path.exists(backup):
                os.remove(backup)
            if os.path.exists(final):
                os.replace(final, backup)
            os.replace(tmp, final)
            with open(os.path.join(base_dir, "version.txt"), "w") as f:
                f.write(remote_ver)
            os.execv(sys.executable, [sys.executable, final])
        except Exception as e:
            if os.path.exists(backup) and not os.path.exists(final):
                try:
                    os.replace(backup, final)
                except:
                    pass
            update_queue.put_nowait({"status": "error", "msg": f"替换失败: {e}"})

def startup_update():
    """启动时清理旧文件，检查是否有待应用更新（断点续传）"""
    _remove_old_backups()
    
    # 清理 PyInstaller 残留临时目录（_MEI*）
    if getattr(sys, "frozen", False):
        import tempfile, shutil, time as _t
        tmp_dir = tempfile.gettempdir()
        now = _t.time()
        for entry in os.listdir(tmp_dir):
            if entry.startswith("_MEI"):
                mei_path = os.path.join(tmp_dir, entry)
                try:
                    if os.path.isdir(mei_path) and (now - os.path.getmtime(mei_path)) > 3600:
                        shutil.rmtree(mei_path, ignore_errors=True)
                except:
                    pass
    
    # 检查是否有上次下载但未应用的更新
    pending = _check_pending_update()
    if pending:
        update_queue.put_nowait(pending)
        return
    
    # 否则执行在线检查
    def _bg():
        # 旧的 unified 自更新
        need, ver, info = check_update()
        if need and info:
            skipped = _load_skipped_versions()
            if ver not in skipped:
                download_update(info)
        
        # 新的模块级热更新（modules.json）
        updates = _check_modules()
        if updates:
            _download_modules(updates)
    threading.Thread(target=_bg, daemon=True).start()

class UpdateChecker:
    def __init__(self):
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="updater")

    def start(self):
        self._t.start()

    def _run(self):
        self._stop.wait(300)
        while not self._stop.is_set():
            try:
                need, ver, info = check_update()
                if need and info:
                    download_update(info)
                # 模块级热更新
                updates = _check_modules()
                if updates:
                    _download_modules(updates)
            except:
                pass
            self._stop.wait(600)


# ══════════════════════════════════════
# 设备列表后台轮询（#8）
# ══════════════════════════════════════
class DevicePoller:
    """后台每 30 秒拉取设备列表，缓存供 UI 使用"""
    def __init__(self):
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="device-poller")

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import urllib.request, json
        while not self._stop.is_set():
            try:
                resp = urllib.request.urlopen(f"{SERVER_URL}/devices", timeout=5)
                data = json.loads(resp.read())
                with devices_lock:
                    devices_cache["devices"] = data.get("devices", {})
                    devices_cache["device_ids"] = data.get("device_ids", [])
                    devices_cache["updated"] = time.time()
            except:
                pass
            self._stop.wait(30)


# ══════════════════════════════════════
# GUI Agent（后台静默线程）
# ══════════════════════════════════════
def agent_log(tag, msg):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    try:
        agent_log_queue.put_nowait(line)
    except queue.Full:
        try:
            agent_log_queue.get_nowait()
            agent_log_queue.put_nowait(line)
        except:
            pass
    if tag in ("INIT", "AGENT", "AUTH") or "ERR" in tag or "Connected" in msg:
        try:
            chat_system_queue.put_nowait(line)
        except queue.Full:
            pass

def run_agent():
    base = get_base_dir()
    sys.path.insert(0, os.path.join(base, "modules"))
    sys.path.insert(0, base)

    import agent as agent_mod
    agent_mod.log = agent_log
    agent_mod.log_err = lambda tag, msg: agent_log(f"{tag}:ERR", msg)
    agent_mod._on_status = lambda v: agent_status.__setitem__("connected", v)
    agent_mod._in_launcher = True  # 由 unified 热更新接管，跳过 agent 自更新

    agent_status["device_name"] = agent_mod.DEVICE_NAME
    agent_status["device_id"] = agent_mod.DEVICE_ID

    agent_log("INIT", f"Agent v{agent_mod.CURRENT_VERSION} | Bridge: [SERVER]:{agent_mod.PORT}")

    # v1.1: 废弃 wrapped_main() 旧协议重复实现，直接调用 agent_main()
    # agent_main() 内置：ACK 两阶段确认 + 新旧协议兼容 + 黑名单处理 + 重连冷却
    try:
        agent_mod.agent_main()
    except KeyboardInterrupt:
        agent_log("AGENT", "KeyboardInterrupt, shutting down")
    except Exception as e:
        agent_log("AGENT:ERR", f"agent_main crashed: {e}")
    finally:
        agent_status["connected"] = False
        agent_log("AGENT", "Agent thread exiting")


# ══════════════════════════════════════
# Chat UI（主线程）— 唯一窗口
# ══════════════════════════════════════
def run_chat():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, font as tkfont
    import json, urllib.request, uuid

    # ── 启动时检查模块热更新（launcher 模式下 if __name__ == \"__main__\" 不执行）──
    startup_update()
    UpdateChecker().start()

    try:
        from PIL import Image, ImageTk
        HAS_PIL = True
    except ImportError:
        HAS_PIL = False

    CHAT_URL = f"{SERVER_URL}/chat"

    def get_device_id():
        id_file = os.path.join(get_base_dir(), ".chat_device_id")
        if os.path.exists(id_file):
            return open(id_file).read().strip()
        did = uuid.uuid4().hex[:8]
        with open(id_file, "w") as f:
            f.write(did)
        return did

    DEVICE_ID = get_device_id()

    def send_message(msg, target_device_id=None):
        try:
            payload_dict = {"message": msg, "device_id": DEVICE_ID}
            if target_device_id:
                payload_dict["target_device_id"] = target_device_id
            payload = json.dumps(payload_dict).encode()
            req = urllib.request.Request(CHAT_URL, data=payload, headers={
                "Content-Type": "application/json", "X-Api-Key": API_KEY, "X-Device-Id": DEVICE_ID,
            })
            resp = urllib.request.urlopen(req, timeout=120)
            r = json.loads(resp.read())
            return r.get("reply", ""), r.get("images", []), r.get("history_len", 0)
        except Exception as e:
            return f"[错误] {e}", [], 0

    def clear_history():
        try:
            url = SERVER_URL.rstrip("/") + "/clear"
            req = urllib.request.Request(url, data=b"{}",
                headers={"Content-Type": "application/json", "X-Api-Key": API_KEY, "X-Device-Id": DEVICE_ID})
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            agent_log("CLEAR", f"清除失败: {e}")
            return False

    class ChatApp:
        def __init__(self, root):
            self.root = root
            self.root.title(f"Hermes Unified v{VERSION}")
            self.root.geometry("600x780")
            self.root.configure(bg="#0d1117")
            self.root.resizable(True, True)
            self.is_sending = False
            self.photo_refs = []
            self.show_logs = True
            self._pending_update = None
            # #4: label → device_id 映射
            self._device_map = {}  # display_label -> device_id

            self.msg_font = tkfont.Font(family="Microsoft YaHei", size=11)
            self.input_font = tkfont.Font(family="Microsoft YaHei", size=12)
            self.title_font = tkfont.Font(family="Microsoft YaHei", size=13, weight="bold")
            self.mono_font = tkfont.Font(family="Consolas", size=9)

            self._build_ui()
            self._check_health()
            self._poll_agent_status()

            self._add_system(f"v{VERSION} 启动完成 — Agent + Chat UI 已就绪")

        def _build_ui(self):
            header = tk.Frame(self.root, bg="#161b22", height=52)
            header.pack(fill=tk.X)
            header.pack_propagate(False)
            tk.Label(header, text=f"🤖 Hermes v{VERSION}", font=self.title_font,
                     bg="#161b22", fg="#58a6ff", padx=15).pack(side=tk.LEFT, pady=10)

            self.agent_dot = tk.Label(header, text="●", font=("Arial", 14), bg="#161b22", fg="#f0883e")
            self.agent_dot.pack(side=tk.RIGHT, padx=(0, 2), pady=10)
            self.agent_label = tk.Label(header, text="Agent: 连接中...", font=("Microsoft YaHei", 9),
                                         bg="#161b22", fg="#8b949e", padx=5)
            self.agent_label.pack(side=tk.RIGHT, pady=10)

            # #4: 设备选择下拉框，存储 label，发送时查映射
            self.target_device_var = tk.StringVar(value="自动")
            self.target_device_combo = ttk.Combobox(header, textvariable=self.target_device_var,
                values=["自动"], width=22, state="readonly", font=("Microsoft YaHei", 9))
            self.target_device_combo.pack(side=tk.RIGHT, padx=(10, 5), pady=10)
            tk.Label(header, text="目标:", font=("Microsoft YaHei", 9),
                     bg="#161b22", fg="#8b949e").pack(side=tk.RIGHT, pady=10)

            self.log_btn = tk.Button(header, text="日志", font=("Microsoft YaHei", 8),
                                      bg="#30363d", fg="#e6edf3", relief=tk.FLAT, padx=6,
                                      activebackground="#21262d", cursor="hand2",
                                      command=self._open_log_window)
            self.log_btn.pack(side=tk.RIGHT, padx=2, pady=10)

            tk.Button(header, text="清除", font=("Microsoft YaHei", 8), bg="#21262d", fg="#8b949e",
                      relief=tk.FLAT, padx=6, activebackground="#30363d", cursor="hand2",
                      command=self._clear_chat).pack(side=tk.RIGHT, padx=2, pady=10)

            # 日志弹窗（按需创建）+ 环形缓冲区
            self.log_window = None
            self.log_text = None
            self.log_buffer = []  # 保存最近 200 条日志

            chat_frame = tk.Frame(self.root, bg="#0d1117")
            chat_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 0))
            self.chat_area = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, font=self.msg_font,
                bg="#161b22", fg="#c9d1d9", insertbackground="#c9d1d9",
                relief=tk.FLAT, padx=12, pady=8, spacing1=2, spacing3=2, state=tk.DISABLED,
                highlightbackground="#30363d", highlightthickness=1)
            self.chat_area.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            self.chat_area.tag_config("user_name", foreground="#58a6ff", font=self.title_font)
            self.chat_area.tag_config("ai_name", foreground="#3fb950", font=self.title_font)
            self.chat_area.tag_config("user_msg", foreground="#e6edf3")
            self.chat_area.tag_config("ai_msg", foreground="#c9d1d9")
            self.chat_area.tag_config("system", foreground="#484f58", justify="center", font=self.mono_font)
            self.chat_area.tag_config("image_label", foreground="#8b949e", font=self.mono_font)

            input_frame = tk.Frame(self.root, bg="#161b22", height=70)
            input_frame.pack(fill=tk.X, padx=10, pady=10)
            input_frame.pack_propagate(False)
            self.input_box = tk.Text(input_frame, font=self.input_font, height=2, bg="#0d1117",
                fg="#c9d1d9", insertbackground="#c9d1d9", relief=tk.FLAT, padx=10, pady=8,
                wrap=tk.WORD, selectbackground="#264f78",
                highlightbackground="#30363d", highlightthickness=1)
            self.input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
            self.input_box.bind("<Return>", self._on_enter)
            self.send_btn = tk.Button(input_frame, text="发送", font=("Microsoft YaHei", 11, "bold"),
                bg="#238636", fg="white", relief=tk.FLAT, width=6, activebackground="#2ea043",
                cursor="hand2", command=self._send)
            self.send_btn.pack(side=tk.RIGHT, fill=tk.Y)

            self.status_var = tk.StringVar(value="就绪")
            tk.Label(self.root, textvariable=self.status_var, bg="#161b22", fg="#484f58",
                     font=("Consolas", 9), anchor=tk.W, padx=10).pack(fill=tk.X, side=tk.BOTTOM)

        def _open_log_window(self):
            if self.log_window is not None and tk.Toplevel.winfo_exists(self.log_window):
                self.log_window.lift()
                return
            self.log_window = tk.Toplevel(self.root)
            self.log_window.title("Hermes Agent Log")
            self.log_window.geometry("600x400")
            self.log_window.configure(bg="#0d1117")
            self.log_window.protocol("WM_DELETE_WINDOW", self._close_log_window)

            self.log_text = tk.Text(self.log_window, font=self.mono_font,
                                     bg="#161b22", fg="#8b949e",
                                     relief=tk.FLAT, padx=8, pady=4,
                                     state=tk.DISABLED,
                                     highlightbackground="#30363d", highlightthickness=1)
            self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            self.log_text.tag_config("log_err", foreground="#f85149")
            self.log_text.tag_config("log_ok", foreground="#3fb950")
            self.log_text.tag_config("log_info", foreground="#58a6ff")

            # 回放缓冲区已有日志
            for line, tag in self.log_buffer:
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, line + "\n", tag)
                self.log_text.configure(state=tk.DISABLED)
            self.log_text.see(tk.END)

            self.log_btn.configure(bg="#30363d", fg="#e6edf3")

        def _close_log_window(self):
            if self.log_window is not None:
                self.log_window.destroy()
                self.log_window = None
                self.log_text = None
                self.log_btn.configure(bg="#21262d", fg="#8b949e")

        def _on_enter(self, event):
            if not event.state & 0x1:
                self._send(); return "break"

        def _send(self):
            if self.is_sending: return
            msg = self.input_box.get("1.0", tk.END).strip()
            if not msg: return
            self.input_box.delete("1.0", tk.END)
            self.is_sending = True
            self.send_btn.configure(state=tk.DISABLED, bg="#21262d")
            self._add_message("你", msg, "user")
            self.status_var.set("⏳ 思考中...")
            # #4: 从下拉框 label 查找真实 device_id
            label = self.target_device_var.get()
            target_device_id = self._device_map.get(label) if label != "自动" else None
            threading.Thread(target=self._get_reply, args=(msg, target_device_id), daemon=True).start()

        def _get_reply(self, msg, target=None):
            reply, images, hist_len = send_message(msg, target_device_id=target)
            self.root.after(0, lambda: self._on_reply(reply, images, hist_len))

        def _on_reply(self, reply, images, hist_len):
            self._add_message("Hermes", reply, "ai", images=images)
            self.status_var.set(f"就绪 | 历史: {hist_len}")
            self.is_sending = False
            self.send_btn.configure(state=tk.NORMAL, bg="#238636")

        def _clear_chat(self):
            if clear_history():
                self.chat_area.configure(state=tk.NORMAL)
                self.chat_area.delete("1.0", tk.END)
                self.chat_area.configure(state=tk.DISABLED)
                self.photo_refs.clear()
                self._add_system("对话已清除")

        def _add_message(self, sender, text, role, images=None):
            self.chat_area.configure(state=tk.NORMAL)
            if self.chat_area.get("1.0", tk.END).strip():
                self.chat_area.insert(tk.END, "\n")
            prefix = "🧑 " if role == "user" else "🤖 "
            self.chat_area.insert(tk.END, f"{prefix}{sender}\n", f"{role}_name")
            if text: self.chat_area.insert(tk.END, f"{text}\n", f"{role}_msg")
            if images and HAS_PIL:
                for url in images:
                    self.chat_area.insert(tk.END, "📷 加载中...\n", "image_label")
                    threading.Thread(target=self._load_img, args=(url,), daemon=True).start()
            elif images:
                for url in images:
                    self.chat_area.insert(tk.END, f"📷 {url}\n", "image_label")
            self.chat_area.insert(tk.END, "\n")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _load_img(self, url):
            try:
                resp = urllib.request.urlopen(urllib.request.Request(url), timeout=30)
                import tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(resp.read()); tmp.close()
                self.root.after(0, lambda: self._show_img(tmp.name))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda err=err: self._replace_loading(f"📷 [{err}]"))

        def _show_img(self, path):
            try:
                img = Image.open(path)
                ratio = min(480/img.width, 360/img.height, 1)
                img = img.resize((int(img.width*ratio), int(img.height*ratio)), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.photo_refs.append(photo)
                self.chat_area.configure(state=tk.NORMAL)
                pos = self.chat_area.search("📷 加载中...", "1.0", tk.END)
                if pos:
                    self.chat_area.delete(pos, f"{pos}+{len('📷 加载中...')}c+1l")
                    self.chat_area.image_create(pos, image=photo)
                    self.chat_area.insert(pos, "\n")
                self.chat_area.configure(state=tk.DISABLED)
                self.chat_area.see(tk.END)
                try: os.unlink(path)
                except: pass
            except Exception as e:
                self._replace_loading(f"📷 [{e}]")

        def _replace_loading(self, text):
            self.chat_area.configure(state=tk.NORMAL)
            pos = self.chat_area.search("📷 加载中...", "1.0", tk.END)
            if pos:
                self.chat_area.delete(pos, f"{pos}+{len('📷 加载中...')}c+1l")
                self.chat_area.insert(pos, text + "\n", "image_label")
            self.chat_area.configure(state=tk.DISABLED)

        def _add_system(self, text):
            self.chat_area.configure(state=tk.NORMAL)
            self.chat_area.insert(tk.END, f"── {text} ──\n\n", "system")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _show_update_prompt(self, version, download_info=None):
            """显示更新提示，提供 立即更新/稍后/跳过 三种选择"""
            self._pending_update = download_info
            self.chat_area.configure(state=tk.NORMAL)
            self.chat_area.insert(tk.END, "\n")
            self.chat_area.insert(tk.END, f"🔔 新版本 v{version} 已下载完成\n", "system")
            self.chat_area.insert(tk.END, "   建议立即重启以获取最新功能和安全补丁\n", "system")
            
            btn_frame = tk.Frame(self.chat_area, bg="#161b22")
            btn_restart = tk.Button(btn_frame, text="🔄 立即重启", font=("Microsoft YaHei", 10, "bold"),
                bg="#238636", fg="white", relief=tk.FLAT, padx=12, pady=4,
                activebackground="#2ea043", cursor="hand2",
                command=self._do_restart)
            btn_restart.pack(side=tk.LEFT, padx=(0, 6))
            
            btn_later = tk.Button(btn_frame, text="⏰ 稍后", font=("Microsoft YaHei", 10),
                bg="#21262d", fg="#8b949e", relief=tk.FLAT, padx=10, pady=4,
                activebackground="#30363d", cursor="hand2",
                command=lambda: self._skip_update(version, temporary=True))
            btn_later.pack(side=tk.LEFT, padx=(0, 6))
            
            btn_skip = tk.Button(btn_frame, text="⏭️ 跳过此版本", font=("Microsoft YaHei", 9),
                bg="#161b22", fg="#484f58", relief=tk.FLAT, padx=8, pady=4,
                activebackground="#21262d", cursor="hand2",
                command=lambda: self._skip_update(version, temporary=False))
            btn_skip.pack(side=tk.LEFT)
            
            self.chat_area.window_create(tk.END, window=btn_frame)
            self.chat_area.insert(tk.END, "\n\n")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _skip_update(self, version, temporary=True):
            """处理用户跳过/稍后更新"""
            self._pending_update = None
            self.chat_area.configure(state=tk.NORMAL)
            if temporary:
                self.chat_area.insert(tk.END, f"⏰ 已推迟 v{version} 更新，下次启动将再次提示\n\n", "system")
            else:
                skipped = _load_skipped_versions()
                skipped.add(version)
                _save_skipped_versions(skipped)
                self.chat_area.insert(tk.END, f"⏭️ 已跳过 v{version}，不再提示此版本\n\n", "system")
            self.chat_area.configure(state=tk.DISABLED)
            self.chat_area.see(tk.END)

        def _do_restart(self):
            """用户点击「立即重启」→ 替换文件并重启"""
            if not self._pending_update:
                return
            do_restart(self._pending_update)

        def _poll_agent_status(self):
            if agent_status["connected"]:
                self.agent_dot.configure(fg="#3fb950")
                name = agent_status.get("device_name", "") or agent_status.get("device_id", "")
                self.agent_label.configure(text=f"Agent: ✅ {name}", fg="#3fb950")
            else:
                self.agent_dot.configure(fg="#f0883e")
                err = agent_status.get("last_error", "")
                if err:
                    self.agent_label.configure(text=f"Agent: ❌ {err}", fg="#f85149")
                else:
                    self.agent_label.configure(text="Agent: 连接中...", fg="#f0883e")

            # #4: 从缓存读取设备列表，维护 label→device_id 映射
            with devices_lock:
                devices = devices_cache.get("devices", {})
            if devices:
                new_map = {"自动": None}
                device_options = ["自动"]
                for did, info in devices.items():
                    label = f"{info.get('name', did)} ({did[:8]})"
                    new_map[label] = did
                    device_options.append(label)
                # 只在列表变化时更新
                if set(new_map.keys()) != set(self._device_map.keys()):
                    self._device_map = new_map
                    self.target_device_combo["values"] = device_options

            # 刷新日志（始终消费队列到缓冲区）
            while not agent_log_queue.empty():
                try:
                    line = agent_log_queue.get_nowait()
                    self.log_buffer.append((line, "log_err" if "ERR" in line or "Error" in line else "log_ok" if "Connected" in line else "log_info"))
                    if len(self.log_buffer) > 200:
                        self.log_buffer.pop(0)
                    if self.log_text is not None:
                        self.log_text.configure(state=tk.NORMAL)
                        tag = self.log_buffer[-1][1]
                        self.log_text.insert(tk.END, self.log_buffer[-1][0] + "\n", tag)
                        self.log_text.see(tk.END)
                        self.log_text.configure(state=tk.DISABLED)
                except:
                    break

            while not chat_system_queue.empty():
                try:
                    line = chat_system_queue.get_nowait()
                    self._add_system(line)
                except:
                    break

            # #13: 更新状态也显示在聊天区
            while not update_queue.empty():
                try:
                    msg = update_queue.get_nowait()
                    status = msg.get("status")
                    if status == "downloading":
                        self._add_system(f"[UPDATE] Downloading v{msg['version']} ...")
                    elif status == "progress":
                        pct = msg.get("percent", 0)
                        total = msg.get("total", 0)
                        downloaded = msg.get("downloaded", 0)
                        if total:
                            self._add_system(f"[UPDATE] Progress: {pct}% ({downloaded//1024//1024}MB / {total//1024//1024}MB)")
                    elif status == "ready":
                        self._pending_update = msg
                        self._show_update_prompt(msg["version"], msg)
                    elif status == "error":
                        self._add_system(f"[WARN] Update failed: {msg.get('msg','')}")
                except:
                    break

            self.root.after(1000, self._poll_agent_status)

        def _check_health(self):
            def _check():
                try:
                    req = urllib.request.Request(CHAT_URL.replace("/chat", "/health"),
                        headers={"X-Api-Key": API_KEY})
                    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
                    self.root.after(0, lambda: self._on_health(
                        data.get("bridge_connected", False), data.get("model", "?")))
                except Exception as e:
                    err = str(e)
                    self.root.after(0, lambda err=err: self._on_health(False, err))
            threading.Thread(target=_check, daemon=True).start()

        def _on_health(self, ok, model):
            pil = "✅" if HAS_PIL else "❌"
            bridge = "✅" if ok else "❌"
            self._add_system(f"v{VERSION} | 模型: {model} | Bridge: {bridge} | 图片: {pil}")

    root = tk.Tk()
    app = ChatApp(root)
    root.mainloop()


# ══════════════════════════════════════
# 主入口
# ══════════════════════════════════════
if __name__ == "__main__":
    hide_console()
    startup_update()
    UpdateChecker().start()
    DevicePoller().start()  # #8: 后台轮询设备列表

    agent_thread = threading.Thread(target=run_agent, daemon=True, name="gui-agent")
    agent_thread.start()

    run_chat()
