"""
Hermes Chat Server v3 — 安全对话 + 远程操控
  - ThreadingHTTPServer（多线程，不阻塞）
  - API Key 认证
  - SHARED_SECRET 从环境变量读取
  - screenshots 目录自动创建
  - 公网 URL 从配置生成
  - open_url 做 URL 校验防注入
"""

import os, json, time, hmac, hashlib, threading, re, logging, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes.chat")

import config
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

AUDIT_LOG = os.path.join(config.UPDATE_DIR, "audit.log")
AUDIT_LOCK = threading.Lock()

def _audit(device_id, action, detail):
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | device={device_id or '?'} | {action}: {detail[:300]}\n"
    with AUDIT_LOCK:
        try:
            with open(AUDIT_LOG, "a") as f:
                f.write(line)
        except Exception:
            pass  # 审计写入失败不要打断主流程
    logger.info("AUDIT %s", line.rstrip())
conversations = {}
conv_lock = threading.Lock()
MAX_HISTORY = 30

# ── Bridge 工具调用 ──
def bridge_call(action, params, timeout=30, device_id=None):
    if not config.SHARED_SECRET:
        return {"error": "Bridge secret not configured"}
    body_dict = {"action": action, "params": params}
    if device_id:
        body_dict["device_id"] = device_id
    body = json.dumps(body_dict).encode()
    sig = hmac.new(config.SHARED_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://127.0.0.1:{config.BRIDGE_HTTP_PORT}/",
        data=body,
        headers={"X-Signature": sig},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def bridge_status():
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{config.BRIDGE_HTTP_PORT}/status", timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ── 工具定义 ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取 Windows 桌面屏幕截图，返回图片描述。用于查看当前屏幕状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "quality": {"type": "integer", "description": "JPEG 质量 1-100，默认 40"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在 Windows 上执行命令并返回输出。可用于运行程序、查看文件、系统操作等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 Windows 命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "在 Windows 桌面上点击指定坐标位置",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X 坐标"},
                    "y": {"type": "integer", "description": "Y 坐标"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "在当前焦点窗口输入文字（支持中文）",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文字"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "按下键盘按键",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}, "description": "按键列表，如 ['ctrl','c'] 或 ['enter']"}
                },
                "required": ["keys"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "在 Windows 浏览器中打开指定网址",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要打开的网址"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "滚动鼠标滚轮",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "滚动方向"},
                    "amount": {"type": "integer", "description": "滚动次数，默认 3"}
                },
                "required": ["direction"]
            }
        }
    },
]

SYSTEM_PROMPT = """你是 Hermes AI，运行在云端服务器上，可以通过 GUI Agent 远程操控用户的 Windows 桌面。

你有以下能力：
- screenshot: 截取屏幕截图并分析
- run_command: 在 Windows 上执行命令
- click: 点击屏幕坐标
- type_text: 输入文字
- press_key: 按键盘按键
- open_url: 打开网址
- scroll: 滚动页面

使用规则：
1. 用户要求操作桌面时，先截图确认当前状态，再执行操作
2. 需要点击时，先截图找到目标位置的坐标
3. 执行操作后截图验证结果
4. 用中文回复，简洁明了
5. 如果用户只是聊天，正常回复即可，不需要调用工具
6. 操作完成后告诉用户结果"""

def _validate_url(url):
    """校验 URL 安全性，防止命令注入"""
    if not url:
        return False
    # 只允许 http/https 开头
    if not re.match(r'^https?://', url):
        return False
    # 禁止 shell 特殊字符
    dangerous = set(';&|`$(){}[]!#~')
    if any(c in url for c in dangerous):
        return False
    return True

def execute_tool(tool_name, args, device_id=None):
    if tool_name == "screenshot":
        result = bridge_call("screenshot", {"quality": args.get("quality", 40)}, timeout=30, device_id=device_id)
        if "data" in result:
            import base64
            ts = int(time.time() * 1000)
            fname = f"screenshot_{ts}.jpg"
            local_path = os.path.join(config.SCREENSHOTS_DIR, fname)
            img_bytes = base64.b64decode(result["data"])
            with open(local_path, "wb") as f:
                f.write(img_bytes)
            os.chmod(local_path, 0o644)
            img_url = config.public_url(f"/screenshots/{fname}")
            return json.dumps({
                "status": "screenshot_saved",
                "url": img_url,
                "width": result.get("width"),
                "height": result.get("height"),
                "size_kb": len(img_bytes) // 1024,
            }), [img_url]
        return json.dumps(result), []

    elif tool_name == "run_command":
        # 审计日志：记录所有远程命令执行
        cmd_str = args["command"]
        _audit(device_id, "run_command", cmd_str)
        result = bridge_call("cmd", {"cmd": cmd_str, "timeout": 30}, timeout=35, device_id=device_id)
        return json.dumps({
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("exit_code"),
        }), []

    elif tool_name == "click":
        result = bridge_call("click", {"x": args["x"], "y": args["y"]}, timeout=10, device_id=device_id)
        return json.dumps(result), []

    elif tool_name == "type_text":
        result = bridge_call("type_text", {"text": args["text"]}, timeout=10, device_id=device_id)
        return json.dumps(result), []

    elif tool_name == "press_key":
        result = bridge_call("press", {"keys": args["keys"]}, timeout=10, device_id=device_id)
        return json.dumps(result), []

    elif tool_name == "open_url":
        url = args.get("url", "")
        if not _validate_url(url):
            return json.dumps({"error": "invalid or unsafe URL"}), []
        # 用 Bridge 的 open_url action，不走 shell
        result = bridge_call("open_url", {"url": url}, timeout=15, device_id=device_id)
        return json.dumps({"status": "opened", "url": url}), []

    elif tool_name == "scroll":
        result = bridge_call("scroll", {
            "direction": args["direction"],
            "amount": args.get("amount", 3),
        }, timeout=10, device_id=device_id)
        return json.dumps(result), []

    return json.dumps({"error": f"unknown tool: {tool_name}"}), []

def chat_with_tools(messages, max_rounds=5, device_id=None):
    all_images = []
    for round_num in range(max_rounds):
        payload = json.dumps({
            "model": config.LLM_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "temperature": 0.7,
            "max_tokens": 2000,
        }).encode()

        req = urllib.request.Request(
            f"{config.LLM_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.LLM_API_KEY}",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            result = json.loads(resp.read())
        except Exception as e:
            return f"[API Error] {e}", all_images

        choice = result["choices"][0]
        msg = choice["message"]

        if not msg.get("tool_calls"):
            return msg.get("content", ""), all_images

        messages.append(msg)

        for tool_call in msg["tool_calls"]:
            fn = tool_call["function"]
            fn_name = fn["name"]
            try:
                fn_args = json.loads(fn["arguments"])
            except:
                fn_args = {}

            tool_result, img_paths = execute_tool(fn_name, fn_args, device_id=device_id)
            all_images.extend(img_paths)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_result,
            })

    return "[达到最大工具调用轮次]", all_images

# ── HTTP Handler ──
class ChatHandler(BaseHTTPRequestHandler):
    def _check_rate_limit(self):
        """检查请求频率，超过限制返回 429 Too Many Requests"""
        client_ip = self.client_address[0]
        if not rate_limiter.is_allowed(client_ip):
            self.send_error(429, "Too Many Requests")
            return False
        return True


    def log_message(self, format, *args):
        pass

    def _send(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # CORS: 限制来源，防止任意网站跨域访问
        origin = self.headers.get("Origin", "")
        if origin in ("https://localhost", "https://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        # 否则不发送 CORS 头，浏览器会阻止跨域
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        api_key = self.headers.get("X-Api-Key", "")
        if not hmac.compare_digest(api_key, config.CHAT_API_KEY):
            self._send({"error": "unauthorized"}, 401)
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(200)
        origin = self.headers.get("Origin", "")
        if origin in ("https://localhost", "https://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, X-Device-Id")
        self.end_headers()

    def do_GET(self):
        if not self._check_rate_limit():
            return
        if self.path == "/health":
            if not self._check_auth():
                return
            status = bridge_status()
            self._send({
                "status": "ok",
                "model": config.LLM_MODEL,
                "bridge_connected": status.get("connected", False),
                "bridge_devices": status.get("count", 0),
            })
        elif self.path == "/devices":
            if not self._check_auth():
                return
            status = bridge_status()
            self._send({
                "devices": status.get("devices", {}),
                "device_ids": status.get("device_ids", []),
            })
        elif self.path.startswith("/screenshots/"):
            # 静态截图服务
            fname = self.path[len("/screenshots/"):]
            # 路径安全：只允许文件名，不允许 ..
            if ".." in fname or "/" in fname or "\\" in fname:
                self._send({"error": "invalid filename"}, 400)
                return
            fpath = os.path.join(config.SCREENSHOTS_DIR, fname)
            if os.path.exists(fpath) and os.path.isfile(fpath):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                fsize = os.path.getsize(fpath)
                self.send_header("Content-Length", str(fsize))
                origin = self.headers.get("Origin", "")
                if origin in ("https://localhost", "https://127.0.0.1"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                with open(fpath, "rb") as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)
            else:
                self._send({"error": "screenshot not found"}, 404)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_rate_limit():
            return
        if self.path == "/chat":
            if not self._check_auth():
                return
            self._handle_chat()
        elif self.path == "/clear":
            if not self._check_auth():
                return
            self._handle_clear()
        else:
            self._send({"error": "not found"}, 404)

    def _handle_chat(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            user_msg = body.get("message", "").strip()
            device_id = self.headers.get("X-Device-Id", body.get("device_id", "default"))
            target_device_id = body.get("target_device_id") or self.headers.get("X-Target-Device")

            if not user_msg:
                self._send({"error": "empty message"}, 400)
                return

            with conv_lock:
                if device_id not in conversations:
                    conversations[device_id] = [
                        {"role": "system", "content": SYSTEM_PROMPT}
                    ]
                history = conversations[device_id]
                history.append({"role": "user", "content": user_msg})
                if len(history) > MAX_HISTORY * 2 + 1:
                    history[:] = [history[0]] + history[-(MAX_HISTORY * 2):]

            messages = list(history)
            ai_reply, image_paths = chat_with_tools(messages, device_id=target_device_id)

            with conv_lock:
                conversations[device_id] = messages
                conversations[device_id].append({"role": "assistant", "content": ai_reply})

            self._send({
                "reply": ai_reply,
                "images": image_paths,
                "history_len": len(conversations[device_id]),
                "model": config.LLM_MODEL,
            })

        except Exception as e:
            self._send({"error": str(e)}, 500)

    def _handle_clear(self):
        device_id = self.headers.get("X-Device-Id", "default")
        with conv_lock:
            if device_id in conversations:
                del conversations[device_id]
        self._send({"status": "cleared"})

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    # 确保 screenshots 目录存在
    os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer(("0.0.0.0", config.CHAT_PORT), ChatHandler)
    logger.info("Hermes Chat Server v3 | Port: %s | Model: %s", config.CHAT_PORT, config.LLM_MODEL)
    logger.info("Auth: API Key | Bridge: %s | Screenshots: %s",
               'configured' if config.SHARED_SECRET else 'not configured', config.SCREENSHOTS_DIR)
    httpd.serve_forever()
