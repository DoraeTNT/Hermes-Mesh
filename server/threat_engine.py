"""
Hermes 威胁情报引擎 v1.0
  ClamAV + YARA + 在线威胁源查询
"""
import json, os, time, subprocess, threading, urllib.request, logging, sys, hmac, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes.threat")

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

BASE = os.path.dirname(os.path.abspath(__file__))
YARA_DIR = os.path.join(BASE, "..", "threat-intel", "yara")
FEEDS_DIR = os.path.join(BASE, "..", "threat-intel", "feeds")
PORT = int(os.environ.get("THREAT_PORT", "8893"))

# ── 扫描路径白名单（防止路径遍历攻击）──
SCAN_WHITELIST = [
    "/home/",
    "/tmp/",
    "/var/log/",
    "/etc/",
]

os.makedirs(FEEDS_DIR, exist_ok=True)

class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def clamav_scan(path):
    """ClamAV 扫描"""
    try:
        r = subprocess.run(["clamscan", "--no-summary", path],
            capture_output=True, text=True, timeout=60)
        threats = [l.strip() for l in r.stdout.split("\n") if "FOUND" in l]
        return {"threats": threats, "clean": len(threats) == 0}
    except Exception as e:
        return {"error": str(e)}

def yara_scan(path):
    """YARA 快速扫描（只跑恶意软件相关规则）"""
    matches = []
    rules_dir = os.path.join(YARA_DIR, "signature-base", "yara")
    if not os.path.isdir(rules_dir):
        return {"matches": [], "total": 0, "error": "rules not found"}
    try:
        for f in sorted(os.listdir(rules_dir)):
            if f.endswith((".yar", ".yara")):
                r = subprocess.run(["yara", "-s", os.path.join(rules_dir, f), path],
                    capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    matches.append({"rule": f, "match": r.stdout.strip()[:200]})
    except Exception as e:
        return {"error": str(e)}
    return {"matches": matches, "total": len(matches)}

def query_malwarebazaar(sha256):
    """查询 MalwareBazaar"""
    try:
        body = json.dumps({"query": "get_info", "hash": sha256}).encode()
        req = urllib.request.Request("https://mb-api.abuse.ch/api/v1/",
            data=body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("query_status") == "hash_found":
            d = data["data"][0]
            return {"found": True, "file_type": d.get("file_type","?"),
                    "tags": d.get("tags",[]), "first_seen": d.get("first_seen",""),
                    "signature": d.get("signature",""), "source": "MalwareBazaar"}
    except: pass
    return {"found": False}

def query_urlhaus(url):
    """查询 URLhaus"""
    try:
        import urllib.parse
        data = urllib.parse.urlencode({"url": url}).encode()
        req = urllib.request.Request("https://urlhaus-api.abuse.ch/v1/url/", data=data)
        resp = urllib.request.urlopen(req, timeout=10)
        r = json.loads(resp.read())
        if r.get("query_status") == "ok":
            return {"found": True, "threat": r.get("threat","?"), "url_status": r.get("url_status","?"),
                    "tags": r.get("tags",[]), "source": "URLhaus"}
    except: pass
    return {"found": False}

class ThreatHandler(BaseHTTPRequestHandler):
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


    def log_message(self, *a): pass

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

    def _check_auth(self):
        """验证 API 认证（X-Api-Key 头 或 api_key 查询参数），使用 hmac.compare_digest 防时序攻击"""
        api_key = self.headers.get("X-Api-Key", "")
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p) if qs else {}
        if not api_key:
            api_key = params.get("api_key", "")
        # 使用 THREAT_API_KEY 而不是 CHAT_API_KEY，隔离威胁引擎认证
        if not hmac.compare_digest(api_key, config.THREAT_API_KEY or config.CHAT_API_KEY):
            self._json({"error": "unauthorized"}, 401)
            return False
        return True

    def _validate_scan_path(self, raw_path):
        """验证扫描路径：解析真实路径，检查是否在白名单内，拒绝路径遍历"""
        # 拒绝包含危险字符的路径
        if any(c in raw_path for c in ("..", "~", "$", "`", ";", "|", "&", "\n", "\r", "\\0")):
            return None
        try:
            real = os.path.realpath(raw_path)
        except (ValueError, OSError):
            return None
        # 拒绝符号链接（防止 symlink 攻击）
        if os.path.islink(real):
            logger.warning("Symlink rejected: %s", raw_path)
            return None
        if not os.path.isfile(real):
            return None
        # 检查是否在白名单目录内
        allowed = any(real.startswith(d) for d in SCAN_WHITELIST)
        if not allowed:
            logger.warning("Path not in whitelist: %s → %s", raw_path, real)
            return None
        return real

    def do_GET(self):
        if not self._check_rate_limit():
            return
        path = self.path.split("?")[0]
        qs = dict(p.split("=", 1) for p in (self.path.split("?")[1] if "?" in self.path else "").split("&") if "=" in p)

        # ── 所有端点需要认证 ──
        if not self._check_auth():
            return

        if path == "/api/stats":
            self._json({
                "status": "ok",
                "yara_rules": len([f for f in os.listdir(os.path.join(YARA_DIR,"signature-base","yara")) if f.endswith((".yar",".yara"))]) if os.path.isdir(os.path.join(YARA_DIR,"signature-base","yara")) else 0,
            })

        elif path == "/api/check/hash":
            sha = qs.get("hash", "")
            if not sha or not re.match(r'^[a-fA-F0-9]{32,64}$', sha):
                self._json({"error": "valid hash required (MD5/SHA256 hex)"}, 400); return
            result = query_malwarebazaar(sha)
            self._json(result)

        elif path == "/api/check/url":
            url = qs.get("url", "")
            if not url:
                self._json({"error": "url required"}, 400); return
            result = query_urlhaus(url)
            self._json(result)

        elif path == "/api/scan/local":
            fp = qs.get("path", "")
            # 使用加固后的路径验证
            safe_path = self._validate_scan_path(fp)
            if not safe_path:
                self._json({"error": "invalid or unauthorized path"}, 400); return
            self._json({
                "file": safe_path,
                "clamav": clamav_scan(safe_path),
                "yara": yara_scan(safe_path),
            })

        elif path == "/api/sync":
            t = threading.Thread(target=self._do_sync, daemon=True)
            t.start()
            self._json({"status": "started"})

        else:
            self._json({"error": "not found", "endpoints": [
                "/api/stats", "/api/check/hash?hash=xxx",
                "/api/check/url?url=xxx", "/api/scan/local?path=/xxx",
                "/api/sync"
            ]}, 404)

    def _do_sync(self):
        """后台下载威胁源"""
        feeds = {
            "urlhaus.csv": "https://urlhaus.abuse.ch/downloads/csv_online/",
            "feodo.csv": "https://feodotracker.abuse.ch/downloads/ipblocklist.csv",
        }
        for name, url in feeds.items():
            try:
                urllib.request.urlretrieve(url, os.path.join(FEEDS_DIR, name))
                logger.info("synced: %s", name)
            except Exception as e:
                logger.error("sync failed: %s - %s", name, e)

if __name__ == "__main__":
    logger.info("威胁情报引擎 :8893")
    httpd = ThreadingServer(("0.0.0.0", PORT), ThreatHandler)
    httpd.serve_forever()
