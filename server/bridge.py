"""
Hermes GUI Bridge v5.0 — Thin Transport Layer
  TCP 25917 | HTTP 9123 | HMAC-SHA256 | Multi-device

v5.0 重构 (from v4.1):
  1. 集成 hermes_packet 统一数据包规范
  2. ACK 两阶段确认（ACK → Done）
  3. Python logging 统一日志
  4. Agent Capability 握手
  5. 向后兼容旧版 Agent（无 ACK 时降级为单阶段）
  6. Bridge 只做传输，不理解业务语义
"""

import asyncio, json, struct, time, os, hashlib, hmac, secrets, threading, signal, sys, logging, ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from hermes import server_config as config
from hermes import packet as pkt

# ── Stream 处理线程池（避免 feed_stream 阻塞事件循环）──
_stream_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stream")
# ── TS 接收计数器（诊断用）──
_ts_bytes_total = 0
_ts_pkt_total = 0
_ts_last_report = time.time()

# ── 统一日志 ──
# 日志配置由 config.py 统一初始化，此处只需获取 logger
logger = logging.getLogger("hermes.bridge")

# ── Config ──
SHARED_SECRET    = config.SHARED_SECRET
TCP_PORT         = config.BRIDGE_TCP_PORT
HTTP_PORT        = config.BRIDGE_HTTP_PORT
PING_INTERVAL    = int(os.environ.get("PING_INTERVAL", "30"))
PING_TIMEOUT     = int(os.environ.get("PING_TIMEOUT", "180"))
CMD_TIMEOUT      = int(os.environ.get("CMD_TIMEOUT", "120"))
ACK_TIMEOUT      = int(os.environ.get("ACK_TIMEOUT", "5"))
HTTP_MAX_THREADS = int(os.environ.get("HTTP_MAX_THREADS", "8"))
HTTP_REQUEST_TIMEOUT = int(os.environ.get("HTTP_REQUEST_TIMEOUT", "30"))
bridge_started   = time.time()

# ── 设备黑名单 ──
BLACKLIST_DEVICES = set()

# ── HMAC ──
def sign_payload(body: bytes) -> str:
    return hmac.new(SHARED_SECRET.encode(), body, hashlib.sha256).hexdigest()

def verify_signature(body: bytes, sig: str) -> bool:
    expected = sign_payload(body)
    return hmac.compare_digest(expected, sig or "")


# ── Pending 命令追踪 ──
class PendingCommand:
    """追踪一条命令的生命周期：ACK → Done"""
    __slots__ = ("packet_id", "ack_future", "done_future", "sent_at", "action")

    def __init__(self, packet_id: str, action: str = ""):
        self.packet_id = packet_id
        self.action = action
        self.sent_at = time.time()
        self.ack_future = None   # asyncio.Future → True/False
        self.done_future = None  # asyncio.Future → result dict


# ── Agent 连接 ──
class AgentConnection:
    def __init__(self, reader, writer, addr):
        self.reader, self.writer, self.addr = reader, writer, addr
        self.lock = threading.Lock()
        self.pending = {}   # packet_id → PendingCommand
        self.last_pong = time.time()
        self.connected_at = time.time()
        self.authenticated = False
        self.hostname = "?"
        self.device_id = ""
        self.device_name = ""
        self.capabilities = []    # Agent 上报的能力列表
        self.agent_version = ""
        self.cmd_count = 0
        self.last_action = ""
        self.closing = False

    def stats(self):
        return {
            "name": self.device_name,
            "host": self.hostname,
            "addr": str(self.addr),
            "pong_age": round(time.time() - self.last_pong, 1),
            "online": round(time.time() - self.connected_at),
            "cmds": self.cmd_count,
            "last": self.last_action,
            "pending": len(self.pending),
            "capabilities": self.capabilities,
            "agent_version": self.agent_version,
        }

    def drain_pending(self, error_msg="connection closed"):
        """Resolve all pending futures with error"""
        with self.lock:
            for pid, pc in list(self.pending.items()):
                if pc.ack_future and not pc.ack_future.done():
                    try:
                        pc.ack_future.set_result(False)
                    except (asyncio.InvalidStateError, RuntimeError):
                        pass
                if pc.done_future and not pc.done_future.done():
                    try:
                        pc.done_future.set_result({"error": error_msg})
                    except (asyncio.InvalidStateError, RuntimeError):
                        pass
            self.pending.clear()


# ── TCP Server ──
class TCPServer:
    def __init__(self):
        self.agents = {}
        self.lock = threading.Lock()

    def cleanup_stale_agents(self):
        now = time.time()
        with self.lock:
            stale_ids = []
            for device_id, conn in self.agents.items():
                elapsed = now - conn.last_pong
                if elapsed > PING_TIMEOUT + 30:
                    stale_ids.append(device_id)
                    logger.warning("清理死连接: %s (pong_age: %.0fs)", conn.device_name, elapsed)
                    conn.closing = True
                    conn.drain_pending("connection stale, cleaned up")
                    try:
                        conn.writer.close()
                    except Exception:
                        pass
            for did in stale_ids:
                self.agents.pop(did, None)

    def disconnect_device(self, device_id):
        with self.lock:
            conn = self.agents.get(device_id)
            if not conn:
                return {"error": f"Device '{device_id}' not found"}
            conn.closing = True
            conn.drain_pending("manually disconnected from dashboard")
            try:
                conn.writer.close()
            except Exception:
                pass
            self.agents.pop(device_id, None)
        logger.info("手动断开: %s (%s)", conn.device_name, device_id)
        return {"status": "disconnected", "device": conn.device_name, "device_id": device_id}

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        conn = AgentConnection(reader, writer, addr)
        ping_task = None
        try:
            # ── Challenge-Response Auth ──
            ch = secrets.token_hex(16)
            challenge_pkt = pkt.make_challenge(ch)
            raw = pkt.encode_packet(challenge_pkt)
            writer.write(struct.pack(">I", len(raw)) + raw)
            await writer.drain()

            hdr = await asyncio.wait_for(reader.readexactly(4), 15)
            data = await asyncio.wait_for(reader.readexactly(struct.unpack(">I", hdr)[0]), 15)
            msg = pkt.parse_packet(data)

            # 兼容新旧协议
            msg_type = pkt.get_packet_type(msg)
            if msg_type not in (pkt.PacketType.AUTH_REPLY, "auth_response"):
                logger.warning("Bad auth type from %s: %s", addr, msg_type)
                return

            exp = hmac.new(SHARED_SECRET.encode(), ch.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(exp, msg.get("response", "")):
                logger.warning("Auth failed from %s", addr)
                return

            conn.authenticated = True
            conn.hostname = msg.get("hostname", "?")
            conn.device_id = msg.get("device_id", conn.hostname)
            conn.device_name = msg.get("device_name", conn.hostname)
            conn.capabilities = msg.get("capabilities", [])
            conn.agent_version = msg.get("agent_version", "unknown")

            # ── 黑名单 ──
            if conn.device_id in BLACKLIST_DEVICES or conn.device_name in BLACKLIST_DEVICES:
                logger.warning("✖ 黑名单拦截: %s (%s) @ %s", conn.device_name, conn.device_id, addr)
                try:
                    reject = pkt.encode_packet(pkt.make_blacklisted(
                        f"Device '{conn.device_name}' is blacklisted"))
                    writer.write(struct.pack(">I", len(reject)) + reject)
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
                return

            with self.lock:
                old = self.agents.pop(conn.device_id, None)
                if old:
                    old.closing = True
                    old.drain_pending("replaced by new connection")
                    try:
                        old.writer.close()
                    except Exception:
                        pass
                    logger.info("Replaced old connection for %s", conn.device_name)
                self.agents[conn.device_id] = conn

            cap_str = ", ".join(conn.capabilities[:5]) + ("..." if len(conn.capabilities) > 5 else "")
            logger.info("✓ %s (%s) @ %s | caps=[%s] | ver=%s",
                        conn.device_name, conn.device_id, addr, cap_str, conn.agent_version)

            ping_task = asyncio.create_task(self._ping(conn))

            # ── 消息循环 ──
            while True:
                hdr = await reader.readexactly(4)
                length = struct.unpack(">I", hdr)[0]
                if length > 5 * 1024 * 1024:
                    logger.warning("Payload too large (%d bytes), closing", length)
                    break
                data = await reader.readexactly(length)
                msg = pkt.parse_packet(data)
                msg_type = msg.get("type", "")

                # 心跳
                if msg.get("type") == pkt.PacketType.PONG:
                    conn.last_pong = time.time()
                    continue

                # ── 视频流（无 packet_id，异步推流）──
                if msg.get("type") == "stream":
                    try:
                        import base64 as _b64
                        from hermes.stream_manager import feed_stream
                        global _ts_bytes_total, _ts_pkt_total, _ts_last_report
                        stream_data = _b64.b64decode(msg.get("data", ""))
                        subtype = msg.get("subtype", "segment")
                        did = conn.device_id
                        # 计数器（诊断）
                        _ts_bytes_total += len(stream_data)
                        _ts_pkt_total += 1
                        now = time.time()
                        if now - _ts_last_report >= 2.0:
                            rate = _ts_bytes_total / (now - _ts_last_report) / 1024
                            logger.info("[BRIDGE] TS in: %d pkt/s  %.1f KB/s  total=%d",
                                       _ts_pkt_total, rate, _ts_pkt_total)
                            _ts_bytes_total = 0
                            _ts_pkt_total = 0
                            _ts_last_report = now
                        # 线程池执行
                        _stream_executor.submit(feed_stream, did, stream_data, subtype)
                        if _ts_pkt_total == 1:
                            logger.info("[BRIDGE] First fMP4 %s: %d bytes (decoded)", subtype, len(stream_data))
                    except Exception as e:
                        logger.warning("STREAM error: %s", e)
                    continue

                # 协议包处理
                packet_id = msg.get("packet_id")

                # 调试：记录非 stream/ack/done 消息类型
                if msg_type not in ("ack", "done", ""):
                    logger.info("MSG type=%s pid=%s", msg_type,
                               (packet_id or "None")[:8])

                if not packet_id:
                    continue

                with conn.lock:
                    pc = conn.pending.get(packet_id)

                if not pc:
                    logger.debug("收到未知 packet_id: %s", packet_id)
                    continue

                # 新协议类型处理（明确的协议类型）
                if msg_type == pkt.PacketType.ACK:
                    if pc.ack_future and not pc.ack_future.done():
                        pc.ack_future.set_result(True)
                    logger.debug("ACK %s %s", packet_id[:8], pc.action)
                    continue

                if msg_type in (pkt.PacketType.DONE, pkt.PacketType.ERROR):
                    result = msg.get("result", {})
                    if msg_type == pkt.PacketType.ERROR:
                        result = {"error": msg.get("error", "unknown")}
                    if pc.done_future and not pc.done_future.done():
                        pc.done_future.set_result(result)
                    continue

                # 未知类型回包（不支持旧协议）
                logger.warning("Unknown msg type %s from %s, ignoring", msg_type, conn.device_name)

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.TimeoutError:
            logger.warning("Auth timeout from %s", addr)
        except Exception as e:
            logger.error("Error from %s: %s", conn.device_name, e, exc_info=True)
        finally:
            conn.closing = True
            if ping_task:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass
            conn.drain_pending(f"disconnected: {conn.device_name}")
            removed = False
            with self.lock:
                if self.agents.get(conn.device_id) is conn:
                    del self.agents[conn.device_id]
                    removed = True
            # 清理该设备的视频流，避免 FFmpeg 进程与读取线程泄漏
            if removed:
                try:
                    from hermes.stream_manager import stop_stream
                    stop_stream(conn.device_id)
                except Exception as e:
                    logger.warning("Stream cleanup failed for %s: %s", conn.device_name, e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Disconnected: %s", conn.device_name)

    async def _ping(self, conn):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if conn.closing:
                return
            elapsed = time.time() - conn.last_pong
            if elapsed > PING_TIMEOUT:
                logger.warning("Ping timeout (%.0fs) for %s, closing", elapsed, conn.device_name)
                conn.closing = True
                conn.drain_pending("ping timeout")
                try:
                    conn.writer.close()
                except Exception:
                    pass
                return
            try:
                ping_pkt = pkt.make_ping()
                raw = pkt.encode_packet(ping_pkt)
                conn.writer.write(struct.pack(">I", len(raw)) + raw)
                await conn.writer.drain()
            except Exception:
                return

    async def send(self, action, params=None, timeout=None, device_id=None):
        """发送命令到 Agent，支持 ACK 两阶段确认"""
        timeout = timeout or CMD_TIMEOUT
        conn = None
        with self.lock:
            if device_id:
                conn = self.agents.get(device_id)
                if not conn:
                    return {"error": f"Device '{device_id}' not found. Available: {list(self.agents.keys())}"}
            else:
                if not self.agents:
                    return {"error": "No agent connected"}
                elif len(self.agents) == 1:
                    conn = list(self.agents.values())[0]
                else:
                    devices = {did: c.device_name for did, c in self.agents.items()}
                    return {"error": f"Multiple devices connected. Please specify device_id. Devices: {devices}"}

        if conn.closing:
            return {"error": "Agent is disconnecting"}

        # 构建命令包
        command_pkt = pkt.make_command(action, params or {}, expire_seconds=timeout)
        packet_id = command_pkt["packet_id"]
        pc = PendingCommand(packet_id, action)

        loop = asyncio.get_running_loop()
        pc.ack_future = loop.create_future()
        pc.done_future = loop.create_future()

        try:
            with conn.lock:
                conn.pending[packet_id] = pc
                conn.last_action = action

            # 发送命令
            raw = pkt.encode_packet(command_pkt)
            conn.writer.write(struct.pack(">I", len(raw)) + raw)
            await conn.writer.drain()

            # ACK → Done 两阶段确认
            logger.info("SEND %s %s → waiting ACK (caps=%d)",
                        action, packet_id[:8], len(conn.capabilities))
            ack_start = time.time()
            try:
                ack_ok = await asyncio.wait_for(pc.ack_future, ACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("ACK timeout: %s %s (%s)", action, packet_id[:8], conn.device_name)
                return {"error": "agent did not acknowledge", "action": action,
                        "packet_id": packet_id, "phase": "ack_timeout"}

            if not ack_ok:
                logger.warning("ACK rejected: %s %s (ack_future.result=%s, done_future.done=%s)",
                              action, packet_id[:8], pc.ack_future.result(), pc.done_future.done())
                return {"error": "ACK rejected", "action": action, "packet_id": packet_id}

            # ACK 收到，基于实际耗时计算剩余超时
            ack_elapsed = time.time() - ack_start
            remaining = timeout - ack_elapsed
            result = await asyncio.wait_for(pc.done_future, max(remaining, 1))

            conn.cmd_count += 1
            # 给结果附加 packet_id 方便追踪
            if isinstance(result, dict):
                result["_packet_id"] = packet_id
            return result

        except asyncio.TimeoutError:
            logger.warning("Command timeout: %s (%ds) for %s", action, timeout, conn.device_name)
            return {"error": "timeout", "action": action, "packet_id": packet_id}
        except Exception as e:
            logger.error("Send error: %s", e, exc_info=True)
            return {"error": f"send failed: {e}"}
        finally:
            with conn.lock:
                conn.pending.pop(packet_id, None)


# ── 事件循环引用（由 main_sync 初始化）──
loop = None

tcp_server = TCPServer()


# ── HTTP API ──
class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 通用：解析 query string
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p) if qs else {}
        path_base = self.path.split("?")[0]

        if path_base == "/health":
            uptime = int(time.time() - bridge_started)
            with tcp_server.lock:
                count = len(tcp_server.agents)
            self._json({"status": "ok", "uptime": uptime, "agents": count,
                        "version": "5.0", "protocol": pkt.PROTOCOL_VERSION})
        elif path_base in ("/status", "/devices"):
            with tcp_server.lock:
                devs = {did: conn.stats() for did, conn in tcp_server.agents.items()}
            self._json({
                "connected": len(devs) > 0,
                "count": len(devs),
                "devices": devs,
                "device_ids": list(devs.keys()),
                "uptime": int(time.time() - bridge_started),
                "version": "5.0",
                "protocol": pkt.PROTOCOL_VERSION,
            })
        elif path_base == "/stream-meta":
            # Stream status
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            from hermes.stream_manager import get_stream_status
            self._json(get_stream_status(device_id))
        elif path_base == "/stream-fmp4":
            # fMP4 live — <video> tag plays natively, no MSE needed
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            from hermes.stream_manager import get_or_create_stream
            session = get_or_create_stream(device_id)

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            session.client_count += 1
            init_sent = False
            # New viewers should start near the live edge, not replay the
            # complete in-memory history (which adds several seconds of lag).
            last_seq = session.live_start_sequence(history=3)
            try:
                while session.active or len(session._segments) > 0:
                    if not init_sent:
                        init = session.get_init()
                        if init:
                            self.wfile.write(init)
                            self.wfile.flush()
                            init_sent = True
                            continue
                    if init_sent:
                        segs = session.get_segments_since(last_seq)
                        for seq, data in segs:
                            try:
                                self.wfile.write(data)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                raise
                            last_seq = max(last_seq, seq)
                        if not segs:
                            time.sleep(0.05)
                    else:
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                session.client_count = max(0, session.client_count - 1)
        elif path_base == "/stream-sse":
            # SSE — raw TS chunks (base64)
            import base64
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            from hermes.stream_manager import get_or_create_stream
            session = get_or_create_stream(device_id)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            session.client_count += 1
            # Keep only a brief keyframe warm-up for MSE, then follow live.
            last_seq = session.live_start_sequence(history=3)
            try:
                while session.active or len(session.chunks) > 0:
                    chunks = session.get_chunks_since(last_seq)
                    for seq, data in chunks:
                        b64 = base64.b64encode(data).decode()
                        self.wfile.write(f"id: {seq}\ndata: {b64}\n\n".encode())
                        self.wfile.flush()
                        last_seq = max(last_seq, seq)
                    if not chunks:
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                session.client_count = max(0, session.client_count - 1)
        elif path_base == "/stream-mjpeg":
            # MJPEG multipart — works on every browser (no MSE required)
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            from hermes.stream_manager import get_or_create_stream
            session = get_or_create_stream(device_id)

            boundary = b"--hermesmjpeg"
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=hermesmjpeg")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            session.client_count += 1
            last_id = 0
            try:
                while session.active or len(session.frames) > 0:
                    frames = session.get_frames_since(last_id)
                    for fid, data in frames:
                        hdr = (b"--hermesmjpeg\r\n"
                               b"Content-Type: image/jpeg\r\n"
                               b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
                        try:
                            self.wfile.write(hdr + data + b"\r\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            raise
                        last_id = max(last_id, fid)
                    if not frames:
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                session.client_count = max(0, session.client_count - 1)
        elif path_base == "/stream-ts":
            # Raw TS relay — <video> tag plays on some browsers natively
            device_id = params.get("device_id", "")
            if not device_id:
                self._json({"error": "device_id required"}, 400)
                return
            from hermes.stream_manager import get_or_create_stream
            session = get_or_create_stream(device_id)

            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            session.client_count += 1
            last_seq = 0
            try:
                while session.active or len(session._chunks) > 0:
                    chunks = session.get_chunks_since(last_seq)
                    for seq, data in chunks:
                        try:
                            self.wfile.write(data)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            raise
                        last_seq = max(last_seq, seq)
                    if not chunks:
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                session.client_count = max(0, session.client_count - 1)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)

        sig = self.headers.get("X-Signature", "")
        device_id_from_url = None
        if self.path not in ("", "/"):
            if "?" in self.path:
                params_str = self.path.split("?", 1)[1]
                params_dict = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
                device_id_from_url = params_dict.get("device_id")
            else:
                self._json({"error": "POST only to /"}, 404)
                return

        if not verify_signature(body_bytes, sig):
            self._json({"error": "invalid or missing X-Signature"}, 403)
            return

        body = json.loads(body_bytes)
        action = body.get("action")
        params = body.get("params", {})
        timeout = body.get("timeout", CMD_TIMEOUT)
        device_id = body.get("device_id") or self.headers.get("X-Device-Id") or device_id_from_url

        if action == "disconnect_device":
            if not device_id:
                self._json({"error": "device_id required"})
                return
            result = tcp_server.disconnect_device(device_id)
            self._json(result)
            return

        try:
            if loop is None:
                self._json({"error": "bridge not ready"}, 503)
                return
            logger.info("HTTP POST action=%s loop.closed=%s loop.running=%s",
                       action, loop.is_closed(), loop.is_running())
            if loop.is_closed():
                self._json({"error": "bridge is shutting down"}, 503)
                return
            fut = asyncio.run_coroutine_threadsafe(
                tcp_server.send(action, params, timeout, device_id), loop
            )
            result = fut.result(timeout=min(timeout + 15, HTTP_REQUEST_TIMEOUT + timeout))
            if action == "stream_stop" and device_id and not result.get("error"):
                from hermes.stream_manager import stop_stream
                stop_stream(device_id)
            self._json(result)
        except RuntimeError as e:
            logger.error("HTTP RuntimeError: %s", e, exc_info=True)
            if "shutdown" in str(e).lower() or "closed" in str(e).lower():
                self._json({"error": "bridge is shutting down"}, 503)
            else:
                self._json({"error": f"call failed: {e}"})
        except Exception as e:
            logger.error("HTTP Exception: %s", e, exc_info=True)
            self._json({"error": f"call failed: {e}"})


class BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    _pool = ThreadPoolExecutor(max_workers=HTTP_MAX_THREADS, thread_name_prefix="http")

    def process_request(self, request, client_address):
        self._pool.submit(self.process_request_thread, request, client_address)


# ── 后台任务 ──
async def cleanup_loop():
    while True:
        await asyncio.sleep(60)
        try:
            tcp_server.cleanup_stale_agents()
        except Exception as e:
            logger.error("Cleanup error: %s", e)

async def health_watchdog():
    while True:
        await asyncio.sleep(120)
        try:
            with tcp_server.lock:
                count = len(tcp_server.agents)
                total_pending = sum(len(c.pending) for c in tcp_server.agents.values())
                caps_summary = {}
                for c in tcp_server.agents.values():
                    for cap in c.capabilities:
                        caps_summary[cap] = caps_summary.get(cap, 0) + 1
            uptime_h = (time.time() - bridge_started) / 3600
            logger.info("HEALTH uptime=%.1fh agents=%d pending=%d caps=%s",
                        uptime_h, count, total_pending, caps_summary)
        except Exception as e:
            logger.error("Watchdog error: %s", e)


# ── Main ──
async def _setup():
    """初始化所有服务"""
    global loop
    loop = asyncio.get_running_loop()

    # ── TLS 上下文 ──
    _ssl_ctx = None
    if config.TLS_ENABLED:
        _ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        _ssl_ctx.check_hostname = False
        _ssl_ctx.load_cert_chain(config.TLS_CERT_FILE, config.TLS_KEY_FILE)
        logger.info("TLS enabled (cert=%s)", config.TLS_CERT_FILE)

    logger.info("Bridge v5.0 | TCP:%d HTTP:%d | Protocol v%d", TCP_PORT, HTTP_PORT, pkt.PROTOCOL_VERSION)
    logger.info("Ping: %ds interval, %ds timeout | ACK timeout: %ds", PING_INTERVAL, PING_TIMEOUT, ACK_TIMEOUT)
    logger.info("HTTP threads: %d max", HTTP_MAX_THREADS)

    tcp_server.cleanup_stale_agents()
    logger.info("已清理残留连接")

    httpd = BoundedThreadingHTTPServer(("127.0.0.1", HTTP_PORT), APIHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http-main").start()
    logger.info("HTTP Listening 127.0.0.1:%d (max %d threads)", HTTP_PORT, HTTP_MAX_THREADS)

    srv = await asyncio.start_server(tcp_server.handle_client, "0.0.0.0", TCP_PORT,
                                     ssl=_ssl_ctx if config.TLS_ENABLED else None)
    if config.TLS_ENABLED:
        logger.info("TLS Listening 0.0.0.0:%d (cert=%s)", TCP_PORT, config.TLS_CERT_FILE)
    else:
        logger.info("TCP Listening 0.0.0.0:%d (plaintext)", TCP_PORT)

    asyncio.create_task(cleanup_loop())
    asyncio.create_task(health_watchdog())
    logger.info("Ready. Waiting for connections...")

    # 返回 httpd 和 srv，供关闭时使用
    return httpd, srv


def main_sync():
    """同步入口：run_forever 保持 loop 永不关闭"""
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    # 初始化
    httpd, srv = _loop.run_until_complete(_setup())

    # 信号处理：收到 SIGTERM 时停止
    def _on_signal():
        logger.info("Shutting down...")
        _loop.stop()

    for s in (signal.SIGINT, signal.SIGTERM):
        _loop.add_signal_handler(s, _on_signal)

    logger.info("main_sync: loop.is_running=%s, loop.is_closed=%s, global_loop=%s",
                _loop.is_running(), _loop.is_closed(), loop is _loop)

    # 永久运行（直到信号触发 loop.stop）
    try:
        _loop.run_forever()
    except Exception as e:
        logger.error("run_forever exited with: %s", e, exc_info=True)
    finally:
        logger.info("main_sync: finally block, loop.is_closed=%s", _loop.is_closed())
        httpd.shutdown()
        with tcp_server.lock:
            for conn in tcp_server.agents.values():
                conn.drain_pending("bridge shutting down")
                try:
                    conn.writer.close()
                except Exception:
                    pass
        logger.info("Bridge stopped.")


if __name__ == "__main__":
    main_sync()
