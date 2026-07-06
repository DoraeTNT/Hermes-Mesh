# -*- coding: utf-8 -*-
"""
hermes.updater — 热更新：启动检查 + 定期轮询 + 下载 + 替换
版本: 1.3
"""
__version__ = "1.3"

import os, sys, time, threading, re, hashlib, tempfile

from hermes.config import (
    VERSION, SERVER_URL, MODULES_JSON, API_KEY, get_base_dir,
    agent_log_queue, chat_system_queue, update_queue, agent_status,
    devices_cache, devices_lock,
)


def _parse_ver(v):
    return tuple(int(p) for p in re.findall(r'\d+', str(v))) or (0,)


def _file_sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except:
        return None


def _download_file(url, dest, retries=3):
    import urllib.request
    for attempt in range(retries):
        try:
            urllib.request.urlretrieve(url, dest)
            return True
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                raise e
    return False


def agent_log(tag, msg):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    try:
        agent_log_queue.put_nowait(line)
    except:
        pass
    if tag in ("INIT", "AGENT", "AUTH") or "ERR" in tag or "Connected" in msg:
        try:
            chat_system_queue.put_nowait(line)
        except:
            pass


def check_modules():
    """对比 modules.json，返回需要更新的模块列表"""
    import urllib.request, json
    try:
        url = f"{SERVER_URL}/{MODULES_JSON}"
        req = urllib.request.Request(url, headers={"X-Api-Key": API_KEY})
        resp = urllib.request.urlopen(req, timeout=10)
        remote = json.loads(resp.read())
    except Exception as e:
        agent_log("UPDATE", f"检查更新失败: {e}")
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


def download_modules(updates, silent=False):
    """下载更新的模块文件到本地"""
    import urllib.request
    base_dir = get_base_dir()
    latest_version = VERSION
    any_updated = False
    any_failed = False

    for mod in updates:
        dest = os.path.join(base_dir, mod["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if not silent:
            try:
                update_queue.put_nowait({"status": "downloading", "version": mod["version"]})
            except:
                pass

        try:
            _download_file(mod["url"], dest)
            # SHA256 校验
            if mod.get("sha256"):
                actual = _file_sha256(dest)
                if actual != mod["sha256"]:
                    os.remove(dest)
                    raise Exception(f"SHA256 mismatch: {actual[:16]}... != {mod['sha256'][:16]}...")
            agent_log("UPDATE", f"✓ {mod['name']} v{mod['version']}")
            any_updated = True
            latest_version = mod["version"]
        except Exception as e:
            agent_log("UPDATE", f"✗ {mod['name']}: {e}")
            any_failed = True

    # 统一发送一次信号（避免多模块更新时 UI 出现多个重启按钮）
    if not silent:
        if any_updated:
            try:
                update_queue.put_nowait({"status": "ready", "version": latest_version})
            except:
                pass
        elif any_failed:
            try:
                update_queue.put_nowait({"status": "error", "msg": "部分模块下载失败"})
            except:
                pass

    return not any_failed


def save_modules_versions(updates):
    """更新本地 modules.json"""
    import json
    base_dir = get_base_dir()
    local_path = os.path.join(base_dir, "modules", "modules.json")
    local = {}
    if os.path.exists(local_path):
        try:
            with open(local_path) as f:
                local = json.load(f)
        except:
            pass

    for mod in updates:
        local[mod["name"]] = {"version": mod["version"]}

    with open(local_path, "w") as f:
        json.dump(local, f, indent=2, ensure_ascii=False)


def startup_update():
    """启动时清理旧文件 + 检查更新"""
    base_dir = get_base_dir()
    import shutil

    # 清理旧备份
    for f in ("HermesUnified_old.exe", "hermes_unified_bak.py"):
        p = os.path.join(base_dir, f)
        if os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

    # 清理 PyInstaller 残留
    if getattr(sys, "frozen", False):
        tmp_dir = tempfile.gettempdir()
        now = time.time()
        for entry in os.listdir(tmp_dir):
            if entry.startswith("_MEI"):
                mei_path = os.path.join(tmp_dir, entry)
                try:
                    if os.path.isdir(mei_path) and (now - os.path.getmtime(mei_path)) > 3600:
                        shutil.rmtree(mei_path, ignore_errors=True)
                except:
                    pass

    def _bg():
        time.sleep(3)  # 等 ChatApp 初始化完成再检测
        updates = check_modules()
        if updates:
            download_modules(updates)
            save_modules_versions(updates)
        else:
            # 检查 launcher 是否刚刚下载过（modules.json 刚被更新）
            local_path = os.path.join(base_dir, "modules", "modules.json")
            if os.path.exists(local_path) and os.path.getmtime(local_path) > time.time() - 10:
                try:
                    with open(local_path) as f:
                        local_mods = json.loads(f.read())
                    new_ver = local_mods.get("app_version", VERSION)
                    update_queue.put_nowait({"status": "ready", "version": new_ver})
                except:
                    pass
    threading.Thread(target=_bg, daemon=True).start()


class UpdateChecker:
    """定期轮询更新（后台线程）"""
    def __init__(self):
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="updater")

    def start(self):
        self._t.start()

    def _run(self):
        self._stop.wait(300)  # 启动后等5分钟
        while not self._stop.is_set():
            try:
                updates = check_modules()
                if updates:
                    download_modules(updates)
                    save_modules_versions(updates)
            except:
                pass
            self._stop.wait(600)  # 每10分钟


class DevicePoller:
    """后台每30秒拉取设备列表"""
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
