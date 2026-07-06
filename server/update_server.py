"""
Hermes Agent Update Server v3
  - HMAC-SHA256 认证（/upload、/version 需要 X-Api-Key）
  - 文件名白名单 + 大小限制
  - 语义版本号 + SHA256 校验
  - 版本号持久化到 versions.json
  - 多产品版本管理

GET  /agent_version.json   → GUI Agent 版本信息
GET  /unified_version.json → Unified 客户端版本信息
GET  /chat_version.json    → Chat 客户端版本信息
GET  /<filename>           → 下载文件（白名单内）
POST /upload               → 上传文件（需认证）
POST /version              → 更新版本号（需认证）
GET  /health               → 健康检查
"""

import os, json, hashlib, hmac, re, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime

from collections import defaultdict, deque

from hermes import server_config as config
# ── 速率限制 ──
class RateLimiter:
    """简单滑动窗口速率限制器，每个 IP 独立计数"""
    def __init__(self, max_req=60, window=60):
        self.max_req = max_req
        self.window = window
        self._clients = defaultdict(lambda: deque())
    
    def is_allowed(self, client_ip):
        now = time.time()
        queue = self._clients[client_ip]
        while queue and queue[0] < now - self.window:
            queue.popleft()
        if len(queue) >= self.max_req:
            return False
        queue.append(now)
        return True

rate_limiter = RateLimiter(config.RATE_LIMIT_MAX, config.RATE_LIMIT_WINDOW)

# ── 版本配置（从 versions.json 加载，内存缓存）──
def _load_versions():
    """从 versions.json 加载，不存在则用默认值初始化"""
    defaults = {
        "agent": {
            "version": config.AGENT_VERSION,
            "files": {
                "exe": {"name": "HermesLauncher.exe", "key": "download_url"},
                "agent":  {"name": "modules/agent.py", "key": "python_url"},
                "unified": {"name": "modules/unified.py", "key": "unified_url"},
                "crypto": {"name": "modules/crypto_loader.py", "key": "crypto_url"},
                "config": {"name": "modules/_enc_config.py", "key": "config_url"},
                "streamer": {"name": "modules/streamer.py", "key": "streamer_url"},
                "h_config": {"name": "modules/hermes/config.py", "key": "h_config_url"},
                "h_updater": {"name": "modules/hermes/updater.py", "key": "h_updater_url"},
                "h_agent": {"name": "modules/hermes/agent.py", "key": "h_agent_url"},
                "h_chat": {"name": "modules/hermes/chat.py", "key": "h_chat_url"},
                "h_main": {"name": "modules/hermes/main.py", "key": "h_main_url"},
                "h_packet": {"name": "modules/hermes/packet.py", "key": "h_packet_url"},
            },
            "changelog": "v4.2: stream module",
        },
        "chat": {
            "version": "2.2",
            "files": {
                "exe": {"name": "HermesChat.exe", "key": "exe_url"},
                "py":  {"name": "chat_client.py", "key": "py_url"},
            },
            "changelog": "v2.2: 初始版本",
        },
        "unified": {
            "version": "3.2",
            "files": {
                "exe": {"name": "HermesLauncher.exe", "key": "exe_url"},
                "py":  {"name": "modules/unified.py", "key": "py_url"},
                "agent": {"name": "modules/agent.py", "key": "agent_url"},
                "crypto": {"name": "modules/crypto_loader.py", "key": "crypto_url"},
            },
            "changelog": "v3.2: status callback + emoji fix",
        },
    }
    if os.path.exists(config.VERSIONS_FILE):
        try:
            with open(config.VERSIONS_FILE) as f:
                saved = json.load(f)
            # 合并：saved 覆盖 defaults 的 version/changelog，保留 defaults 的 files 结构
            for product, cfg in defaults.items():
                if product in saved:
                    cfg["version"] = saved[product].get("version", cfg["version"])
                    cfg["changelog"] = saved[product].get("changelog", cfg["changelog"])
            return defaults
        except Exception:
            pass
    return defaults

def _save_versions():
    """持久化版本信息到 versions.json"""
    save_data = {}
    for product, cfg in VERSIONS.items():
        save_data[product] = {
            "version": cfg["version"],
            "changelog": cfg.get("changelog", ""),
        }
    try:
        with open(config.VERSIONS_FILE, "w") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to save versions.json: {e}")

VERSIONS = _load_versions()

def file_sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def parse_version(ver_str):
    parts = re.findall(r'\d+', ver_str)
    return tuple(int(p) for p in parts) if parts else (0,)

def build_version_info(product, port=None):
    cfg = VERSIONS.get(product)
    if not cfg:
        return None

    ver = cfg["version"]
    info = {
        "version": ver,
        "changelog": cfg.get("changelog", ""),
        "timestamp": int(time.time()),
    }

    for file_key, file_cfg in cfg["files"].items():
        fname = file_cfg["name"]
        url_key = file_cfg["key"]
        fpath = os.path.join(config.UPDATE_DIR, fname)

        info[url_key] = config.public_url(f"/{fname}", port)
        info[f"{file_key}_size"] = os.path.getsize(fpath) if os.path.exists(fpath) else 0

        sha = file_sha256(fpath)
        if sha:
            info[f"{file_key}_sha256"] = sha

    return info

class UpdateHandler(BaseHTTPRequestHandler):
    def _check_rate_limit(self):
        """检查请求频率，超过限制返回 429 Too Many Requests"""
        client_ip = self.client_address[0]
        # 本地回环和私有地址绕过速率限制
        if client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("192.168.") or client_ip.startswith("10."):
            return True
        if not rate_limiter.is_allowed(client_ip):
            self.send_error(429, "Too Many Requests")
            return False
        return True


    def log_message(self, format, *args):
        pass

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS: 限制来源，防止任意网站跨域访问
        origin = self.headers.get("Origin", "")
        if origin in ("https://localhost", "https://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        # 否则不发送 CORS 头，浏览器会阻止跨域
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _check_api_key(self):
        """验证 X-Api-Key（/upload 和 /version 需要），使用 hmac.compare_digest 防时序攻击"""
        api_key = self.headers.get("X-Api-Key", "")
        if not hmac.compare_digest(api_key, config.UPDATE_API_KEY):
            self._send_json({"error": "unauthorized"}, 401)
            return False
        return True

    def _send_file(self, path, filename):
        if not os.path.exists(path):
            self.send_error(404)
            return
        sha = file_sha256(path)
        fsize = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(fsize))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if sha:
            self.send_header("X-SHA256", sha)
        self.end_headers()
        # 流式发送，避免大文件撑爆内存
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)

    def do_GET(self):
        if not self._check_rate_limit():
            return
        path = self.path.split("?")[0]

        if path == "/agent_version.json":
            info = build_version_info("agent")
            self._send_json(info or {"error": "not configured"})

        elif path == "/unified_version.json":
            info = build_version_info("unified")
            self._send_json(info or {"error": "not configured"})

        elif path == "/chat_version.json":
            info = build_version_info("chat")
            self._send_json(info or {"error": "not configured"})

        elif path == "/health":
            self._send_json({"status": "ok", "version": "3.0", "timestamp": int(time.time())})

        elif path.startswith("/") and len(path) > 1:
            fname = path[1:]
            if ".." in fname:
                self._send_json({"error": "invalid filename"}, 400)
                return
            # modules.json 始终允许（热更新核心配置文件）
            if fname == "modules/modules.json":
                fpath = os.path.join(config.UPDATE_DIR, "modules.json")
                if os.path.exists(fpath) and os.path.isfile(fpath):
                    self._send_file(fpath, "modules.json")
                else:
                    self.send_error(404)
                return
            # 白名单检查：只允许下载已知文件
            allowed = set()
            for cfg in VERSIONS.values():
                for fc in cfg["files"].values():
                    allowed.add(fc["name"])
            if fname not in allowed:
                self._send_json({"error": "file not in whitelist"}, 403)
                return
            fpath = os.path.join(config.UPDATE_DIR, fname)
            if os.path.exists(fpath) and os.path.isfile(fpath):
                self._send_file(fpath, fname)
            else:
                self.send_error(404)

        else:
            self._send_json({
                "service": "Hermes Update Server",
                "version": "3.0",
                "endpoints": [
                    "/agent_version.json",
                    "/unified_version.json",
                    "/chat_version.json",
                    "/health",
                    "/upload (POST, auth required)",
                    "/version (POST, auth required)",
                ],
            })

    def do_POST(self):
        if not self._check_rate_limit():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        # 大小限制
        if content_length > config.UPLOAD_MAX_SIZE:
            self._send_json({"error": f"payload too large (max {config.UPLOAD_MAX_SIZE} bytes)"}, 413)
            return
        body_bytes = self.rfile.read(content_length)
        path = self.path.split("?")[0]

        if path == "/upload":
            if not self._check_api_key():
                return
            self._handle_upload(body_bytes)
        elif path == "/version":
            if not self._check_api_key():
                return
            self._handle_version_update(body_bytes)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_upload(self, raw):
        try:
            content_type = self.headers.get("Content-Type", "")
            files_info = []

            if "multipart/form-data" in content_type:
                boundary_match = re.search(r"boundary=(.+?)(?:;|$)", content_type)
                if not boundary_match:
                    self._send_json({"error": "no boundary"}, 400)
                    return

                boundary = boundary_match.group(1).strip().encode()
                parts = raw.split(b"--" + boundary)

                for part in parts:
                    part = part.strip()
                    if not part or part == b"--":
                        continue
                    header_end = part.find(b"\r\n\r\n")
                    if header_end < 0:
                        continue
                    headers_raw = part[:header_end].decode("utf-8", errors="replace")
                    body = part[header_end + 4:]
                    if body.endswith(b"\r\n"):
                        body = body[:-2]

                    fn_match = re.search(r'filename="([^"]+)"', headers_raw)
                    fname = fn_match.group(1) if fn_match else "upload.bin"

                    # 白名单检查
                    if fname not in config.UPLOAD_WHITELIST:
                        files_info.append({"filename": fname, "error": "not in whitelist"})
                        continue
                    filepath = os.path.join(config.UPDATE_DIR, fname)
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    # 原子写入：先写临时文件，验证后重命名
                    tmp_path = filepath + ".tmp"
                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(body)
                        # 验证写入完整性（至少非空且大小匹配）
                        if os.path.getsize(tmp_path) != len(body):
                            raise OSError("Write incomplete")
                        # 安全重命名
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        os.rename(tmp_path, filepath)
                    except Exception:
                        # 清理临时文件
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                        raise

                    sha = file_sha256(filepath)
                    size = os.path.getsize(filepath)
                    files_info.append({
                        "filename": fname,
                        "size": size,
                        "sha256": sha,
                    })
                    print(f"[Upload] {fname} ({size} bytes, sha256={sha[:16]}...)")
            else:
                filename = self.headers.get("X-Filename", "upload.bin")
                if filename not in config.UPLOAD_WHITELIST:
                    self._send_json({"error": f"{filename} not in whitelist"}, 403)
                    return
                filepath = os.path.join(config.UPDATE_DIR, filename)
                # 原子写入：先写临时文件，验证后重命名
                tmp_path = filepath + ".tmp"
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(raw)
                    if os.path.getsize(tmp_path) != len(raw):
                        raise OSError("Write incomplete")
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    os.rename(tmp_path, filepath)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise
                sha = file_sha256(filepath)
                size = os.path.getsize(filepath)
                files_info.append({
                    "filename": filename,
                    "size": size,
                    "sha256": sha,
                })
                print(f"[Upload] {filename} ({size} bytes)")

            self._send_json({"status": "ok", "files": files_info})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_version_update(self, body_bytes):
        try:
            body = json.loads(body_bytes)
            product = body.get("product")
            new_ver = body.get("version")

            if not product or not new_ver:
                self._send_json({"error": "need product + version"}, 400)
                return

            if product not in VERSIONS:
                self._send_json({"error": f"unknown product: {product}"}, 400)
                return

            old_ver = VERSIONS[product]["version"]
            VERSIONS[product]["version"] = new_ver
            if "changelog" in body:
                VERSIONS[product]["changelog"] = body["changelog"]

            # 持久化到 versions.json
            _save_versions()

            info = build_version_info(product)
            print(f"[Version] {product}: {old_ver} → {new_ver}")
            self._send_json({
                "status": "ok",
                "product": product,
                "old_version": old_ver,
                "new_version": new_ver,
                "info": info,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        origin = self.headers.get("Origin", "")
        if origin in ("https://localhost", "https://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename, X-Api-Key")
        self.end_headers()

if __name__ == "__main__":
    PORT = config.UPDATE_PORT

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        allow_reuse_port = True

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), UpdateHandler)
    print(f"Update Server v3.0 | 0.0.0.0:{PORT}")
    print(f"  Agent:   {config.public_url('/agent_version.json')}")
    print(f"  Unified: {config.public_url('/unified_version.json')}")
    print(f"  Chat:    {config.public_url('/chat_version.json')}")
    print(f"  Upload:  POST {config.public_url('/upload')} (X-Api-Key required)")
    print(f"  Version: POST {config.public_url('/version')} (X-Api-Key required)")
    # 不再打印 API Key 的任何部分，防止信息泄露
    key_status = "configured" if config.UPDATE_API_KEY else "MISSING"
    print(f"  Auth:    X-Api-Key = {key_status}")
    httpd.serve_forever()
