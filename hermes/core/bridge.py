# -*- coding: utf-8 -*-
"""
hermes.core.bridge — 异步 TCP 桥接引擎
负责与 Windows Agent 维护长连接，实现指令调度与状态管理。
"""

import asyncio
import struct
import time
import logging
from typing import Dict, List, Optional, Any

from hermes.core.packet import (
    PacketType, encode_packet, parse_packet, 
    make_ping, make_challenge, make_blacklisted, make_command
)
from hermes.core.security import SecurityManager
from hermes import config

logger = logging.getLogger("hermes.core.bridge")

class PendingCommand:
    """追踪一条命令的生命周期：ACK -> Done"""
    __slots__ = ("packet_id", "ack_future", "done_future", "sent_at", "action")

    def __init__(self, packet_id: str, action: str = ""):
        self.packet_id = packet_id
        self.action = action
        self.sent_at = time.time()
        self.ack_future: Optional[asyncio.Future] = None
        self.done_future: Optional[asyncio.Future] = None

class AgentConnection:
    """代表一个已认证的 Agent 连接"""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, addr: tuple):
        self.reader = reader
        self.writer = writer
        self.addr = addr
        self.lock = asyncio.Lock()
        self.pending: Dict[str, PendingCommand] = {}
        self.last_pong = time.time()
        self.connected_at = time.time()
        self.hostname = "?"
        self.device_id = ""
        self.device_name = ""
        self.capabilities: List[str] = []
        self.agent_version = ""
        self.cmd_count = 0
        self.last_action = ""
        self.closing = False

    def stats(self) -> dict:
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

    async def drain_pending(self, error_msg: str = "connection closed"):
        async with self.lock:
            for pid, pc in list(self.pending.items()):
                if pc.ack_future and not pc.ack_future.done():
                    pc.ack_future.set_result(False)
                if pc.done_future and not pc.done_future.done():
                    pc.done_future.set_result({"error": error_msg})
            self.pending.clear()

class BridgeServer:
    """核心 TCP 服务端"""
    def __init__(self, shared_secret: str):
        self.agents: Dict[str, AgentConnection] = {}
        self.lock = asyncio.Lock()
        self.security = SecurityManager(shared_secret)
        self.blacklist = set()
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self, host: str = "0.0.0.0", port: int = 25917):
        self._server = await asyncio.start_server(self.handle_client, host, port)
        logger.info("Bridge TCP Server started on %s:%d", host, port)
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Bridge TCP Server stopped.")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        conn = AgentConnection(reader, writer, addr)
        ping_task = None
        
        try:
            # 1. Challenge-Response Auth
            ch = self.security.generate_challenge()
            challenge_pkt = make_challenge(ch)
            raw = encode_packet(challenge_pkt)
            writer.write(struct.pack(">I", len(raw)) + raw)
            await writer.drain()

            # Read response
            hdr = await asyncio.wait_for(reader.readexactly(4), 15)
            length = struct.unpack(">I", hdr)[0]
            data = await asyncio.wait_for(reader.readexactly(length), 15)
            msg = parse_packet(data)

            # Verify
            if not self.security.verify_response(ch, msg.get("response", "")):
                logger.warning("Auth failed from %s", addr)
                writer.close()
                return

            # Identity
            conn.hostname = msg.get("hostname", "?")
            conn.device_id = msg.get("device_id", conn.hostname)
            conn.device_name = msg.get("device_name", conn.hostname)
            conn.capabilities = msg.get("capabilities", [])
            conn.agent_version = msg.get("agent_version", "unknown")

            # Blacklist check
            if conn.device_id in self.blacklist or conn.device_name in self.blacklist:
                logger.warning("✖ Blacklisted: %s (%s)", conn.device_name, conn.device_id)
                reject = encode_packet(make_blacklisted(f"Device {conn.device_name} is blacklisted"))
                writer.write(struct.pack(">I", len(reject)) + reject)
                await writer.drain()
                writer.close()
                return

            # Register device
            async with self.lock:
                old = self.agents.pop(conn.device_id, None)
                if old:
                    old.closing = True
                    await old.drain_pending("replaced by new connection")
                    old.writer.close()
                    logger.info("Replaced old connection for %s", conn.device_name)
                self.agents[conn.device_id] = conn

            logger.info("✓ %s (%s) @ %s | ver=%s", conn.device_name, conn.device_id, addr, conn.agent_version)
            
            ping_task = asyncio.create_task(self._ping_loop(conn))

            # 2. Message Loop
            while True:
                hdr = await reader.readexactly(4)
                length = struct.unpack(">I", hdr)[0]
                if length > 5 * 1024 * 1024:
                    logger.warning("Payload too large (%d), closing", length)
                    break
                
                data = await reader.readexactly(length)
                msg = parse_packet(data)
                
                # Heartbeat
                if msg.get("type") == PacketType.PONG:
                    conn.last_pong = time.time()
                    continue

                packet_id = msg.get("packet_id")
                msg_type = msg.get("type", "")
                if not packet_id: continue

                async with conn.lock:
                    pc = conn.pending.get(packet_id)

                if not pc: continue

                # Two-stage confirmation logic
                if msg_type == PacketType.ACK:
                    if pc.ack_future and not pc.ack_future.done():
                        pc.ack_future.set_result(True)
                    continue

                if msg_type in (PacketType.DONE, PacketType.ERROR):
                    result = msg.get("result", {}) if msg_type == PacketType.DONE else {"error": msg.get("error", "unknown")}
                    if pc.done_future and not pc.done_future.done():
                        pc.done_future.set_result(result)
                    continue

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.TimeoutError:
            logger.warning("Auth timeout from %s", addr)
        except Exception as e:
            logger.error("Error from %s: %s", conn.device_name, e, exc_info=True)
        finally:
            conn.closing = True
            if ping_task: ping_task.cancel()
            await conn.drain_pending(f"disconnected: {conn.device_name}")
            async with self.lock:
                if self.agents.get(conn.device_id) is conn:
                    del self.agents[conn.device_id]
            writer.close()
            await writer.wait_closed()
            logger.info("Disconnected: %s", conn.device_name)

    async def _ping_loop(self, conn: AgentConnection):
        while not conn.closing:
            await asyncio.sleep(config.BRIDGE_PING_INTERVAL)
            if time.time() - conn.last_pong > config.BRIDGE_PING_TIMEOUT:
                logger.warning("Ping timeout for %s, closing", conn.device_name)
                conn.closing = True
                await conn.drain_pending("ping timeout")
                conn.writer.close()
                return
            try:
                raw = encode_packet(make_ping())
                conn.writer.write(struct.pack(">I", len(raw)) + raw)
                await conn.writer.drain()
            except Exception:
                return

    async def send(self, action: str, params: dict = None, timeout: int = None, device_id: str = None) -> dict:
        timeout = timeout or config.BRIDGE_CMD_TIMEOUT
        
        async with self.lock:
            if device_id:
                conn = self.agents.get(device_id)
                if not conn: return {"error": f"Device {device_id} not found"}
            else:
                if not self.agents: return {"error": "No agent connected"}
                if len(self.agents) == 1:
                    conn = list(self.agents.values())[0]
                else:
                    return {"error": "Multiple devices connected. Please specify device_id."}

        if conn.closing: return {"error": "Agent is disconnecting"}

        # Build and Send
        command_pkt = make_command(action, params or {}, expire_seconds=timeout)
        packet_id = command_pkt["packet_id"]
        pc = PendingCommand(packet_id, action)
        pc.ack_future = asyncio.get_running_loop().create_future()
        pc.done_future = asyncio.get_running_loop().create_future()

        async with conn.lock:
            conn.pending[packet_id] = pc
            conn.last_action = action

        try:
            raw = encode_packet(command_pkt)
            conn.writer.write(struct.pack(">I", len(raw)) + raw)
            await conn.writer.drain()

            # Stage 1: ACK
            try:
                ack_ok = await asyncio.wait_for(pc.ack_future, config.BRIDGE_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                return {"error": "agent did not acknowledge", "phase": "ack_timeout"}
            
            if not ack_ok: return {"error": "ACK rejected"}

            # Stage 2: DONE
            elapsed = time.time() - pc.sent_at
            result = await asyncio.wait_for(pc.done_future, max(timeout - elapsed, 1))
            conn.cmd_count += 1
            if isinstance(result, dict): result["_packet_id"] = packet_id
            return result

        except asyncio.TimeoutError:
            return {"error": "timeout", "action": action, "packet_id": packet_id}
        except Exception as e:
            return {"error": f"send failed: {e}"}
        finally:
            async with conn.lock:
                conn.pending.pop(packet_id, None)
