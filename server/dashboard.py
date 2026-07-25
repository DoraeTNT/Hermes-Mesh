"""
Hermes GUI Agent Dashboard v1.0
  后端: HTTP API + 看板页面
  数据源: Bridge (9123) + Chat Server (8891) + systemd + journal
"""
import json, os, time, hmac, hashlib, urllib.request, subprocess, re, logging, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes.dashboard")

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

PORT = int(os.environ.get("DASHBOARD_PORT", "8892"))
BRIDGE_URL = f"http://127.0.0.1:{config.BRIDGE_HTTP_PORT}"
CHAT_URL = f"http://127.0.0.1:{config.CHAT_PORT}"

def bridge_call(endpoint="/status", timeout=10):
    """通用 Bridge HTTP 调用（不需要签名）"""
    try:
        resp = urllib.request.urlopen(f"{BRIDGE_URL}{endpoint}", timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def bridge_signed_call(action, params=None, device_id=None, timeout=15):
    """带签名的 Bridge POST 调用（用于系统动作如 disconnect_device）"""
    body_dict = {"action": action, "params": params or {}}
    if device_id:
        body_dict["device_id"] = device_id
    body = json.dumps(body_dict).encode()
    sig = hmac.new(config.SHARED_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/", data=body,
        headers={"X-Signature": sig, "Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def chat_call(endpoint="/health", timeout=10):
    try:
        req = urllib.request.Request(f"{CHAT_URL}{endpoint}",
            headers={"X-Api-Key": config.CHAT_API_KEY})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def systemd_status(service):
    try:
        r = subprocess.run(["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except:
        return "unknown"

def get_process_mem(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024  # MB
    except:
        pass
    return 0

def bridge_log_events(limit=50):
    """解析 bridge journal 获取连接/断开事件"""
    try:
        r = subprocess.run(
            ["journalctl", "-u", "hermes-bridge.service", "--no-pager", "-n", str(limit),
             "-o", "short-iso"],
            capture_output=True, text=True, timeout=10
        )
        events = []
        for line in r.stdout.strip().split("\n"):
            if "✓" in line or "Disconnected" in line or "Replaced" in line:
                parts = line.split("hermes-bridge", 1)
                ts = parts[0].strip()[:19] if len(parts) > 1 else ""
                msg = parts[1].strip() if len(parts) > 1 else line
                evt_type = "connect" if "✓" in msg else "disconnect" if "Disconnected" in msg else "replace"
                # 提取设备名
                dev_match = re.search(r'✓\s+(\S+)', msg)
                if not dev_match:
                    dev_match = re.search(r'Disconnected:\s+(\S+)', msg)
                if not dev_match:
                    dev_match = re.search(r'Replaced old connection for\s+(\S+)', msg)
                device = dev_match.group(1) if dev_match else "?"
                events.append({"time": ts, "type": evt_type, "device": device, "raw": msg[:120]})
        return events[-30:]
    except:
        return []

def get_system_health():
    """服务器系统健康"""
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        mem = r.stdout.strip()
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        disk = r.stdout.strip().split("\n")[-1]
    except:
        mem, disk = "N/A", "N/A"
    return {"memory": mem, "disk": disk}

class DashboardHandler(BaseHTTPRequestHandler):
    def _check_rate_limit(self):
        """检查请求频率，超过限制返回 429 Too Many Requests"""
        client_ip = self.client_address[0]
        # 本地回环和私有地址绕过速率限制（nginx 反代时所有请求都来自 127.0.0.1）
        if client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("192.168.") or client_ip.startswith("10."):
            return True
        if not rate_limiter.is_allowed(client_ip):
            self.send_error(429, "Too Many Requests")
            return False
        return True


    def log_message(self, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # CORS: 限制来源，防止任意网站跨域访问
        origin = self.headers.get("Origin", "")
        if origin in ("https://localhost", "https://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        # 否则不发送 CORS 头，浏览器会阻止跨域
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        """验证 API 认证，/api/health 和主页免认证"""
        path_base = self.path.split("?")[0]
        if path_base in ("/", "/index.html", "/api/health", "/api/devices", "/api/events", "/video", "/flow", "/project"):
            return True
        api_key = self.headers.get("X-Api-Key", "")
        # 也支持 URL 查询参数
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p) if qs else {}
        if not api_key:
            api_key = params.get("api_key", "")
        if not hmac.compare_digest(api_key, config.DASHBOARD_API_KEY or config.CHAT_API_KEY):
            self._json({"error": "unauthorized"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._check_rate_limit():
            return
        path_base = self.path.split("?")[0]

        # ── 所有页面和 API 需要认证（使用 hmac.compare_digest 防时序攻击）──
        if not self._check_auth():
            return

        if path_base == "/" or path_base == "/index.html":
            self._html(DASHBOARD_HTML)
            return
        elif path_base == "/flow":
            flow_path = os.path.join(os.path.dirname(__file__), "screenshots", "flow-diagram.html")
            try:
                with open(flow_path) as f:
                    self._html(f.read())
            except:
                self._json({"error": "flow diagram not found"}, 404)
            return
        elif path_base == "/project":
            proj_path = os.path.join(os.path.dirname(__file__), "screenshots", "project-flow.html")
            try:
                with open(proj_path) as f:
                    self._html(f.read())
            except:
                self._json({"error": "project diagram not found"}, 404)
            return
        elif path_base == "/video":
            video_path = os.path.join(os.path.dirname(__file__), "video.html")
            try:
                with open(video_path) as f:
                    self._html(f.read())
            except:
                self._json({"error": "video page not found"}, 404)
            return

        if path_base == "/api/devices":
            data = bridge_call("/status")
            self._json(data)
        elif path_base == "/api/events":
            self._json({"events": bridge_log_events()})
        elif path_base == "/api/health":
            bridge = bridge_call("/health")
            chat = chat_call("/health")
            services = {
                "bridge": systemd_status("hermes-bridge.service"),
                "update-server": systemd_status("hermes-update-server.service"),
                "chat-server": systemd_status("hermes-chat.service"),
                "threat-engine": systemd_status("hermes-threat.service"),
            }
            sys_health = get_system_health()
            self._json({
                "bridge": bridge,
                "chat": chat,
                "services": services,
                "system": sys_health,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        elif path_base.startswith("/api/screenshot"):
            # 实时截图: GET /api/screenshot?device_id=xxx
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=") for p in qs.split("&") if "=" in p) if qs else {}
            device_id = params.get("device_id", "")
            quality = int(params.get("quality", 30))
            result = bridge_signed_call("screenshot", {"quality": quality}, device_id=device_id if device_id else None, timeout=15)
            if "data" in result:
                import base64
                img_bytes = base64.b64decode(result["data"])
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(img_bytes)
            else:
                self._json(result)
        elif path_base == "/api/stream-start":
            # 下发 stream_start 命令给 Agent
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=") for p in qs.split("&") if "=" in p) if qs else {}
            device_id = params.get("device_id", "")
            fps = params.get("fps", "20")
            bitrate = params.get("bitrate", "2M")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            # 下发命令给 Agent（bridge 的 feed_stream 会自动启动服务端 FFmpeg）
            result = bridge_signed_call("stream_start", {
                "fps": int(fps),
                "bitrate": bitrate,
            }, device_id=device_id, timeout=10)
            self._json(result)
        elif path_base == "/api/stream-stop":
            # 下发 stream_stop 命令给 Agent
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=") for p in qs.split("&") if "=" in p) if qs else {}
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            result = bridge_signed_call("stream_stop", {}, device_id=device_id, timeout=10)
            self._json(result)
        elif path_base == "/api/stream-meta":
            # 代理到 Bridge（stream_manager 状态在 Bridge 进程中）
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            meta_url = f"{BRIDGE_URL}/stream-meta?{qs}"
            try:
                resp = urllib.request.urlopen(meta_url, timeout=10)
                data = json.loads(resp.read())
                self._json(data)
            except Exception as e:
                self._json({"error": str(e)}, 502)
        elif path_base == "/api/sse-test":
            # 简单 SSE 测试：每秒发一个计数器
            import time as _time
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            count = 0
            try:
                while count < 30:
                    self.wfile.write(f"data: test message {count}\n\n".encode())
                    self.wfile.flush()
                    count += 1
                    _time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path_base == "/api/stream-sse":
            # SSE 代理到 Bridge（raw socket）
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            import socket as _sock
            s = None
            try:
                s = _sock.create_connection(("127.0.0.1", config.BRIDGE_HTTP_PORT), timeout=10)
                s.sendall(f"GET /stream-sse?{qs} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
                # ... (bridge communication is internal)
                # 响应浏览器时使用 HTTP/1.1
                self.protocol_version = "HTTP/1.1"
                # 读取 bridge 响应头
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk: break
                    buf += chunk
                header_end = buf.index(b"\r\n\r\n") + 4
                body_tail = buf[header_end:]
                # 发送 SSE 响应头
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                # 立刻发送一个心跳确保连接
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                # 转发 body
                if body_tail:
                    self.wfile.write(body_tail)
                    self.wfile.flush()
                s.settimeout(0.5)  # 500ms 超时避免永久阻塞
                while True:
                    try:
                        chunk = s.recv(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except _sock.timeout:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    except (BrokenPipeError, ConnectionResetError):
                        break
            except Exception as e:
                self._json({"error": str(e)}, 502)
            finally:
                if s:
                    try: s.close()
                    except: pass
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_rate_limit():
            return
        path_base = self.path.split("?")[0]
        if not self._check_auth():
            return

        if path_base == "/api/disconnect":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                device_id = body.get("device_id", "")
                if not device_id:
                    self._json({"error": "device_id required"}, 400)
                    return
                result = bridge_signed_call("disconnect_device", device_id=device_id)
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ══════════════════════════════════════════════════
#  看板 HTML (内联)
# ══════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes GUI Agent 看板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI','Microsoft YaHei',sans-serif;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:20px;color:#58a6ff}
.header .time{font-size:13px;color:#8b949e}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;padding:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.card-header{padding:12px 16px;font-size:14px;font-weight:600;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center}
.card-body{padding:12px 16px}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.status-online{background:#3fb950}
.status-offline{background:#f85149}
.status-warn{background:#f0883e}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-online{background:#1a3a2a;color:#3fb950}
.badge-offline{background:#3a1a1a;color:#f85149}
.badge-warn{background:#3a301a;color:#f0883e}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500;font-size:11px;text-transform:uppercase}
.event-item{padding:6px 0;border-bottom:1px solid #21262d;font-size:12px;display:flex;gap:8px}
.event-connect{color:#3fb950}
.event-disconnect{color:#f85149}
.event-replace{color:#f0883e}
.event-time{color:#484f58;min-width:50px}
.stat{text-align:center;padding:8px}
.stat-value{font-size:28px;font-weight:bold;color:#58a6ff}
.stat-label{font-size:11px;color:#8b949e;margin-top:4px}
.health-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.health-item{background:#0d1117;border-radius:6px;padding:10px;text-align:center}
.health-item .label{font-size:11px;color:#8b949e}
.health-item .value{font-size:18px;font-weight:bold;margin-top:2px}
.health-item .value.ok{color:#3fb950}
.health-item .value.err{color:#f85149}
.health-item .value.warn{color:#f0883e}
.log-view{max-height:300px;overflow-y:auto;font-family:'Consolas','Courier New',monospace;font-size:11px;color:#8b949e;line-height:1.6}
.refresh-indicator{font-size:11px;color:#484f58;text-align:right;padding:4px}
.full-width{grid-column:1/-1}
.mem-bar{height:6px;border-radius:3px;background:#21262d;margin-top:6px;overflow:hidden}
.mem-bar-fill{height:100%;border-radius:3px;background:#58a6ff;transition:width .5s}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:16px">
    <h1> Hermes GUI Agent </h1>
    <a href="video" style="color:#58a6ff;text-decoration:none;font-size:13px;padding:4px 12px;border:1px solid #30363d;border-radius:4px"> Stream</a>
    <a href="flow" style="color:#8b949e;text-decoration:none;font-size:13px;padding:4px 12px;border:1px solid #30363d;border-radius:4px">Flow</a>
    <a href="project" style="color:#8b949e;text-decoration:none;font-size:13px;padding:4px 12px;border:1px solid #30363d;border-radius:4px">Project</a>
  </div>
  <div class="time" id="serverTime">...</div>
</div>

<div class="grid">

  <!-- 设备连接状态 -->
  <div class="card">
    <div class="card-header">
      <span>📡 设备连接</span>
      <span id="deviceCount" style="color:#58a6ff;font-size:18px;font-weight:bold">-</span>
    </div>
    <div class="card-body">
      <table id="deviceTable">
        <thead><tr><th>设备名</th><th>IP</th><th>在线</th><th>Pong</th><th>命令数</th><th>操作</th></tr></thead>
        <tbody><tr><td colspan="6" style="text-align:center;color:#484f58">加载中...</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- 服务健康 -->
  <div class="card">
    <div class="card-header">⚙️ 服务状态</div>
    <div class="card-body">
      <div class="health-grid" id="serviceHealth">
        <div class="health-item"><div class="label">Bridge</div><div class="value warn">...</div></div>
        <div class="health-item"><div class="label">Chat Server</div><div class="value warn">...</div></div>
        <div class="health-item"><div class="label">Update Server</div><div class="value warn">...</div></div>
        <div class="health-item"><div class="label">Dashboard</div><div class="value ok">✅</div></div>
      </div>
      <div id="serverMem" style="margin-top:12px;font-size:12px;color:#8b949e"></div>
    </div>
  </div>

  <!-- 系统统计 -->
  <div class="card">
    <div class="card-header">📊 统计</div>
    <div class="card-body" style="display:flex;justify-content:space-around">
      <div class="stat"><div class="stat-value" id="statDevices">0</div><div class="stat-label">在线设备</div></div>
      <div class="stat"><div class="stat-value" id="statCmds">0</div><div class="stat-label">总命令数</div></div>
      <div class="stat"><div class="stat-value" id="statErrors">0</div><div class="stat-label">错误数</div></div>
      <div class="stat"><div class="stat-value" id="statUptime">0h</div><div class="stat-label">Bridge 运行</div></div>
    </div>
  </div>

  <!-- 实时画面 -->
  <div class="card">
    <div class="card-header">
      <span>📺 实时画面</span>
      <select id="liveDevice" onchange="startLiveView()" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:4px 8px;font-size:12px;font-family:inherit">
        <option value="">选择设备...</option>
      </select>
      <span id="liveFPS" style="color:#484f58;font-size:11px;margin-left:8px"></span>
      <button id="livePauseBtn" onclick="togglePause()" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:3px 10px;font-size:12px;cursor:pointer;font-family:inherit;display:none">⏸ 暂停</button>
    </div>
    <div class="card-body" style="text-align:center;min-height:200px">
      <video id="liveVideo" style="max-width:100%;max-height:500px;display:none;border-radius:4px;border:1px solid #30363d" muted autoplay playsinline></video>
      <div id="livePlaceholder" style="color:#484f58;padding:60px 0">选择设备后开始 H.264 实时视频流<br><small style="color:#30363d">需要 FFmpeg (gdigrab + MSE)</small></div>
      <div id="liveError" style="color:#f85149;display:none;padding:20px;font-size:13px"></div>
    </div>
  </div>

  <!-- 威胁扫描 -->
  <div class="card">
    <div class="card-header">
      <span>🛡️ 威胁扫描</span>
      <span id="threatStatus" style="font-size:11px;color:#484f58">ClamAV 3.6M+ · YARA 1.3K</span>
    </div>
    <div class="card-body">
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input id="threatInput" placeholder="SHA256 哈希 或 文件路径" style="flex:1;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-size:12px;font-family:inherit" onkeydown="if(event.key==='Enter')runThreatScan()">
        <button onclick="runThreatScan()" style="background:#238636;color:#fff;border:none;border-radius:4px;padding:6px 16px;font-size:12px;cursor:pointer;font-family:inherit;white-space:nowrap">扫描</button>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <select id="threatDevice" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:4px 8px;font-size:11px;font-family:inherit">
          <option value="">本地服务器</option>
        </select>
        <span style="font-size:11px;color:#484f58">选择远程设备扫描文件</span>
      </div>
      <div id="threatResult" style="font-size:12px;max-height:200px;overflow-y:auto;font-family:Consolas,monospace;color:#8b949e"></div>
    </div>
  </div>

  <!-- 连接事件日志 -->
  <div class="card">
    <div class="card-header">📋 连接事件</div>
    <div class="card-body">
      <div class="log-view" id="eventLog">加载中...</div>
    </div>
  </div>

</div>
<!-- v20260704-2 -->
<div class="refresh-indicator" id="refreshInfo">自动刷新: 5s | 上次: --</div>

<script>
const DEFAULT_API_KEY = "";
const BASE = window.location.pathname.replace(/\/+$/, '');
const API = BASE + '/api';
// 从 URL 参数读取 API Key（例如 /dashboard/?key=xxx）
const urlParams = new URLSearchParams(window.location.search);
const API_KEY = urlParams.get('api_key') || (typeof DEFAULT_API_KEY!=='undefined'?DEFAULT_API_KEY:'');

async function fetchJSON(url) {
  try {
    const opts = {};
    if (API_KEY) opts.headers = {'X-Api-Key': API_KEY};
    // URL 也附带 api_key
    const sep = url.includes('?') ? '&' : '?';
    const r = await fetch(API_KEY ? url + sep + 'api_key=' + encodeURIComponent(API_KEY) : url, opts);
    return await r.json();
  }
  catch(e) { return {error: String(e)}; }
}

function fmtTime(seconds) {
  if (!seconds || seconds < 0) return '0s';
  const h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60), s = seconds%60;
  return h>0 ? `${h}h${m}m` : m>0 ? `${m}m${s}s` : `${s}s`;
}

async function disconnectDevice(id, name) {
  if (!confirm(`确定断开 ${name} (${id}) 的连接？`)) return;
  try {
    const headers = {'Content-Type': 'application/json'};
    if (API_KEY) headers['X-Api-Key'] = API_KEY;
    const url = API_KEY ? API+'/disconnect?api_key=' + encodeURIComponent(API_KEY) : API+'/disconnect';
    const r = await fetch(url, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({device_id: id})
    });
    const data = await r.json();
    if (data.status === 'disconnected') {
      refresh();  // 立即刷新看板
    } else {
      alert('断开失败: ' + (data.error || '未知错误'));
    }
  } catch(e) {
    alert('请求失败: ' + e);
  }
}

// ── 实时画面 (H.264 MSE 视频流) ──
let liveDeviceId = '';
let mediaSource = null;
let sourceBuffer = null;
let eventSource = null;
let initSegment = null;
let frameCount = 0;
let fpsTimer = 0;
let mseReady = false;

function startLiveView() {
  const sel = document.getElementById('liveDevice');
  const newId = sel.value;
  if (!newId) { stopLiveView(); return; }

  if (liveDeviceId && liveDeviceId !== newId) {
    stopLiveView();
  }
  liveDeviceId = newId;

  const video = document.getElementById('liveVideo');
  const placeholder = document.getElementById('livePlaceholder');
  const errEl = document.getElementById('liveError');
  const fpsEl = document.getElementById('liveFPS');
  const pauseBtn = document.getElementById('livePauseBtn');

  // 显示进度
  placeholder.innerHTML = '正在启动视频流...<br><small style="color:#30363d">Step 1/4: 发送 stream_start 命令</small>';
  placeholder.style.display = 'block';
  errEl.style.display = 'none';

  // 发送 stream_start 命令给 Agent
  const streamStartUrl = API + '/stream-start?device_id=' + newId + '&fps=20&bitrate=2M' + (API_KEY ? '&api_key=' + encodeURIComponent(API_KEY) : '');
  fetch(streamStartUrl).catch(() => {});

  // 获取 init segment（带重试，因为 -c copy 的 init 需要等待 TS 数据来修补 avc1）
  placeholder.innerHTML = '正在启动视频流...<br><small style="color:#30363d">Step 2/4: 获取 init segment</small>';
  const metaUrl = API + '/stream-meta?device_id=' + newId + (API_KEY ? '&api_key=' + encodeURIComponent(API_KEY) : '');
  
  // Retry fetch: init segment may need time for TS-based avc1 patching
  let metaRetries = 0;
  const maxMetaRetries = 80;  // 80 * 250ms = 20 seconds max
  
  function fetchMeta() {
    return fetch(metaUrl).then(r => r.json()).then(meta => {
      if (meta.error) {
        metaRetries++;
        if (metaRetries < maxMetaRetries) {
          placeholder.innerHTML = '正在启动视频流...<br><small style="color:#30363d">Step 2/4: 等待 init segment (' + metaRetries + '/' + maxMetaRetries + ')</small>';
          return new Promise(resolve => setTimeout(resolve, 250)).then(fetchMeta);
        }
        throw new Error(meta.error);
      }
      return meta;
    });
  }
  
  fetchMeta()
    .then(meta => {
      if (meta.error) {
        placeholder.style.display = 'none';
        errEl.textContent = 'Stream error: ' + meta.error;
        errEl.style.display = 'block';
        throw new Error(meta.error);
      }
      placeholder.innerHTML = '正在启动视频流...<br><small style="color:#30363d">Step 3/4: 初始化 MSE (' + (meta.init_b64 ? meta.init_b64.length : 0) + ' bytes init)</small>';
      return setupMSE(video, meta);
    })
    .then(() => {
      placeholder.innerHTML = '正在启动视频流...<br><small style="color:#30363d">Step 4/4: 连接 SSE 视频流</small>';
      // 连接 SSE 获取 fMP4 segments
      const sseUrl = API + '/stream-sse?device_id=' + newId + (API_KEY ? '&api_key=' + encodeURIComponent(API_KEY) : '');
      eventSource = new EventSource(sseUrl);
      eventSource.onopen = () => {
        fpsEl.textContent = 'SSE open';
        placeholder.innerHTML = '视频流已连接，等待数据...<br><small style="color:#3fb950">SSE connected, waiting for segments</small>';
      };
      eventSource.onmessage = (e) => {
        try {
          fpsEl.textContent = 'got msg';
          if (!sourceBuffer || sourceBuffer.updating) { fpsEl.textContent = 'sb busy'; return; }
          if (!mseReady) { fpsEl.textContent = 'waiting MSE'; return; }
          let binary = atob(e.data);
          let bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          sourceBuffer.appendBuffer(bytes);
          frameCount++;
          fpsEl.textContent = 'Frames: ' + frameCount;
          if (!fpsTimer) fpsTimer = performance.now();
          if (video.paused) video.play().catch(() => {});
          if (placeholder.style.display !== 'none') {
            video.style.display = 'block';
            placeholder.style.display = 'none';
            pauseBtn.style.display = 'inline-block';
            pauseBtn.textContent = '⏸ 暂停';
          }
          // Force seek to buffered range if video is stuck at t=0 with data elsewhere
          if (video.readyState < 2 && sourceBuffer && sourceBuffer.buffered.length > 0) {
            const start = sourceBuffer.buffered.start(0);
            if (Math.abs(video.currentTime - start) > 0.5) {
              video.currentTime = start;
            }
            // Explicitly trigger play after seek
            video.play().catch(() => {});
          }
        } catch(ex) {
          errEl.textContent = 'Segment error: ' + ex.message;
          errEl.style.display = 'block';
          placeholder.style.display = 'none';
        }
      };
      eventSource.onerror = (ev) => {
        placeholder.style.display = 'none';
        errEl.textContent = 'SSE connection lost — reconnecting...';
        errEl.style.display = 'block';
      };

      // FPS 计数器
      setInterval(() => {
        if (frameCount > 0 && fpsTimer) {
          const elapsed = (performance.now() - fpsTimer) / 1000;
          const fps = Math.round(frameCount / elapsed);
          fpsEl.textContent = '~' + fps + ' FPS';
        }
      }, 2000);
    })
    .catch(err => {
      placeholder.style.display = 'none';
      errEl.innerHTML = '<b>H.264 stream failed:</b><br>' + (err.message || err) +
        '<br><small style="color:#484f58">Check: FFmpeg on Windows client, stream active on server.</small>';
      errEl.style.display = 'block';
    });
}

async function setupMSE(video, meta) {
  const initB64 = meta.init_b64;
  if (!initB64 || !window.MediaSource) {
    throw new Error('Browser does not support MediaSource Extensions');
  }
  const binary = atob(initB64);
  initSegment = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) initSegment[i] = binary.charCodeAt(i);

  return new Promise((resolve, reject) => {
    mediaSource = new MediaSource();
    video.src = URL.createObjectURL(mediaSource);
    video.load();  // 强制开始加载
    let resolved = false;
    mediaSource.addEventListener('sourceopen', () => {
      try {
        // Comprehensive H.264 codec list — video-only + audio variants
        const codecs = [
          'video/mp4; codecs="avc1.42E01E"',
          'video/mp4; codecs="avc1.42c028"',
          'video/mp4; codecs="avc1.4D401E"',
          'video/mp4; codecs="avc1.64001E"',
          'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
          'video/mp4; codecs="avc1.42c028, mp4a.40.2"',
          'video/mp4; codecs="avc1.4D401E, mp4a.40.2"',
          'video/mp4; codecs="avc1.64001E, mp4a.40.2"',
        ];
        let mime = null;
        for (const c of codecs) {
          if (MediaSource.isTypeSupported(c)) { mime = c; break; }
        }
        if (!mime) {
          reject(new Error('No supported H.264 codec found'));
          return;
        }
        sourceBuffer = mediaSource.addSourceBuffer(mime);
        sourceBuffer.mode = 'sequence';
        sourceBuffer.addEventListener('updateend', () => {
          mseReady = true;
          if (sourceBuffer && sourceBuffer.buffered.length > 0) {
            const end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
            const start = sourceBuffer.buffered.start(0);
            if (end - start > 5) {
              try { sourceBuffer.remove(start, end - 3); } catch(e) {}
            }
          }
          // resolve only after init segment is fully processed
          if (!resolved) { resolved = true; resolve(); }
        });
        sourceBuffer.addEventListener('error', (e) => {
          const errEl = document.getElementById('liveError');
          errEl.textContent = 'MSE sourceBuffer error — codec or init segment mismatch';
          errEl.style.display = 'block';
          console.error('sourceBuffer error:', e);
          if (!resolved) { resolved = true; reject(new Error('sourceBuffer error')); }
        });
        sourceBuffer.appendBuffer(initSegment);
        // NOTE: do NOT resolve here — wait for 'updateend' to ensure MSE is ready
      } catch(e) {
        if (!resolved) { resolved = true; reject(e); }
      }
    });
    mediaSource.addEventListener('sourceclose', () => {
      mseReady = false;
      mediaSource = null;
      sourceBuffer = null;
    });
    mediaSource.addEventListener('sourceended', () => {
      mseReady = false;
    });
    // Timeout: if sourceopen doesn't fire in 10s, reject
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        reject(new Error('MediaSource sourceopen timeout'));
      }
    }, 10000);
  });
}

function stopLiveView() {
  liveDeviceId = '';

  if (eventSource) { eventSource.close(); eventSource = null; }
  if (mediaSource && mediaSource.readyState === 'open') {
    try { mediaSource.endOfStream(); } catch(e) {}
  }
  mediaSource = null;
  sourceBuffer = null;
  initSegment = null;
  mseReady = false;

  const video = document.getElementById('liveVideo');
  video.src = '';
  video.style.display = 'none';
  document.getElementById('livePlaceholder').style.display = 'block';
  document.getElementById('liveError').style.display = 'none';
  document.getElementById('liveFPS').textContent = '';
  document.getElementById('livePauseBtn').style.display = 'none';
}

function togglePause() {
  const video = document.getElementById('liveVideo');
  const btn = document.getElementById('livePauseBtn');
  if (!video.paused) {
    video.pause();
    btn.textContent = '▶ 播放';
  } else {
    video.play();
    btn.textContent = '⏸ 暂停';
  }
}

async function refresh() {
  const [devices, events, health] = await Promise.all([
    fetchJSON(API+'/devices'),
    fetchJSON(API+'/events'),
    fetchJSON(API+'/health'),
  ]);

  // ── Server time ──
  document.getElementById('serverTime').textContent = health.server_time || '--';

  // ── Devices table ──
  const devs = devices.devices || {};
  const ids = Object.keys(devs);
  document.getElementById('deviceCount').textContent = ids.length;
  const tbody = document.querySelector('#deviceTable tbody');
  if (ids.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#484f58">无设备连接</td></tr>';
  } else {
    tbody.innerHTML = ids.map(id => {
      const d = devs[id];
      const pongOk = d.pong_age < 60;
      return `<tr>
        <td><span class="status-dot ${pongOk?'status-online':'status-warn'}"></span>${d.name}</td>
        <td style="color:#8b949e">${d.addr}</td>
        <td>${fmtTime(d.online)}</td>
        <td style="color:${pongOk?'#3fb950':'#f0883e'}">${d.pong_age}s</td>
        <td>${d.cmds}</td>
        <td><button onclick="disconnectDevice('${id}','${d.name}')" style="background:#3a1a1a;color:#f85149;border:1px solid #f85149;border-radius:4px;padding:3px 10px;font-size:12px;cursor:pointer;font-family:inherit">断开</button></td>
      </tr>`;
    }).join('');
  }

  // ── Stats ──
  let totalCmds = 0, totalErrors = 0;
  ids.forEach(id => { totalCmds += devs[id].cmds; });
  document.getElementById('statDevices').textContent = ids.length;
  document.getElementById('statCmds').textContent = totalCmds;
  document.getElementById('statErrors').textContent = totalErrors;
  document.getElementById('statUptime').textContent = fmtTime(devices.uptime || 0);

  // ── 更新实时画面设备列表 ──
  const liveSel = document.getElementById('liveDevice');
  const curLive = liveDeviceId;
  liveSel.innerHTML = '<option value="">选择设备...</option>' +
    ids.map(id => `<option value="${id}" ${id===curLive?'selected':''}>${devs[id].name} (${id.slice(0,8)})</option>`).join('');
  if (curLive && !ids.includes(curLive)) {
    stopLiveView();
  }

  // ── 更新威胁扫描设备列表 ──
  const threatSel = document.getElementById('threatDevice');
  threatSel.innerHTML = '<option value="">本地服务器</option>' +
    ids.map(id => `<option value="${id}">${devs[id].name}</option>`).join('');

  // ── Services ──
  const svcs = health.services || {};
  const icons = { active: '✅', inactive: '❌', unknown: '⚠️' };
  const grid = document.getElementById('serviceHealth');
  grid.innerHTML = `
    <div class="health-item"><div class="label">Bridge</div><div class="value ${svcs.bridge==='active'?'ok':'err'}">${icons[svcs.bridge]||'⚠️'} ${svcs.bridge||'?'}</div></div>
    <div class="health-item"><div class="label">Update Server</div><div class="value ${svcs['update-server']==='active'?'ok':'err'}">${icons[svcs['update-server']]||'⚠️'} ${svcs['update-server']||'?'}</div></div>
    <div class="health-item"><div class="label">Chat Server</div><div class="value ${svcs['chat-server']==='active'?'ok':'err'}">${icons[svcs['chat-server']]||'⚠️'} ${svcs['chat-server']||'?'}</div></div>
    <div class="health-item"><div class="label">Threat Engine</div><div class="value ${svcs['threat-engine']==='active'?'ok':'err'}">${icons[svcs['threat-engine']]||'⚠️'} ${svcs['threat-engine']||'?'}</div></div>
    <div class="health-item"><div class="label">Dashboard</div><div class="value ok">✅ active</div></div>
  `;

  // ── Server memory ──
  const memInfo = (health.system && health.system.memory) || '';
  // Parse "Mem:   total   used   free   shared   buff/cache   available"
  const parts = memInfo.split('\n')[1] || '';  // skip header line
  const nums = (parts.match(/\d+/g) || []).map(Number);
  if (nums.length >= 2) {
    const total = nums[0], used = nums[1];
    const pct = Math.round(used/total*100);
    document.getElementById('serverMem').innerHTML = `服务器内存: ${used}MB / ${total}MB (${pct}%)
      <div class="mem-bar"><div class="mem-bar-fill" style="width:${pct}%"></div></div>`;
  }

  // ── Events log ──
  const evts = events.events || [];
  document.getElementById('eventLog').innerHTML = evts.length === 0
    ? '<span style="color:#484f58">暂无事件</span>'
    : evts.map(e => `
      <div class="event-item">
        <span class="event-time">${e.time}</span>
        <span class="event-${e.type}">${e.type==='connect'?'✅':e.type==='disconnect'?'❌':'🔄'} ${e.device}</span>
      </div>
    `).join('');

  // ── Refresh info ──
  const now = new Date().toLocaleTimeString('zh-CN');
  document.getElementById('refreshInfo').textContent = `自动刷新: 5s | 上次: ${now}`;
}

refresh();
setInterval(refresh, 5000);

// ── 威胁扫描 ──
async function runThreatScan() {
  const input = document.getElementById('threatInput').value.trim();
  const resultDiv = document.getElementById('threatResult');
  if (!input) { resultDiv.innerHTML = '<span style="color:#f85149">请输入哈希或文件路径</span>'; return; }
  
  resultDiv.innerHTML = '<span style="color:#f0883e">⏳ 扫描中...</span>';
  
  // 判断是哈希还是路径（哈希=64位hex）
  if (/^[a-fA-F0-9]{64}$/.test(input)) {
    try {
      const r = await fetch(BASE + '/threat/api/check/hash?hash=' + input);
      const d = await r.json();
      if (d.found) {
        resultDiv.innerHTML = `<span style="color:#f85149">⚠️ 恶意!</span>
          <br>类型: ${d.file_type}
          <br>标签: ${(d.tags||[]).join(', ')}
          <br>首次发现: ${d.first_seen}
          <br>来源: ${d.source}`;
      } else {
        resultDiv.innerHTML = '<span style="color:#3fb950">✅ 未在特征库中命中</span><br><span style="color:#484f58">该哈希不在已知恶意库中</span>';
      }
    } catch(e) {
      resultDiv.innerHTML = '<span style="color:#f85149">查询失败: ' + e + '</span>';
    }
  } else {
    // 文件路径扫描
    const deviceId = document.getElementById('threatDevice').value;
    const apiPath = deviceId 
      ? BASE + '/threat/api/scan/remote?device_id=' + deviceId + '&path=' + encodeURIComponent(input)
      : BASE + '/threat/api/scan/local?path=' + encodeURIComponent(input);
    try {
      const r = await fetch(apiPath);
      const d = await r.json();
      let html = '';
      if (d.clamav) {
        html += d.clamav.clean 
          ? '<span style="color:#3fb950">🟢 ClamAV: 干净</span><br>'
          : '<span style="color:#f85149">🔴 ClamAV: ' + d.clamav.threats.join(', ') + '</span><br>';
      }
      if (d.yara) {
        html += d.yara.total > 0
          ? '<span style="color:#f85149">🔴 YARA: ' + d.yara.total + ' 条命中</span><br>'
          : '<span style="color:#3fb950">🟢 YARA: 干净</span><br>';
        d.yara.matches.forEach(m => {
          html += '<span style="color:#f0883e;font-size:10px">  ⚡ ' + m.rule + ': ' + m.match.slice(0,80) + '</span><br>';
        });
      }
      if (d.hash_lookup && d.hash_lookup.found) {
        html += '<span style="color:#f85149">⚠️ 哈希已知恶意: ' + (d.hash_lookup.tags||[]).join(',') + '</span><br>';
      }
      resultDiv.innerHTML = html || '<span style="color:#3fb950">✅ 扫描完成</span>';
    } catch(e) {
      resultDiv.innerHTML = '<span style="color:#f85149">扫描失败: ' + e + '</span>';
    }
  }
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)
    httpd = ThreadingServer(("0.0.0.0", PORT), DashboardHandler)
    logger.info("Dashboard: http://0.0.0.0:%d", PORT)
    logger.info("API:       http://0.0.0.0:%d/api/", PORT)
    httpd.serve_forever()
