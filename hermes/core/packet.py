# -*- coding: utf-8 -*-
"""
hermes.core.packet — 统一数据包规范
所有 Bridge ↔ Agent 通信使用此格式。
"""

import uuid
import time
import json
import logging

logger = logging.getLogger("hermes.core.packet")

PROTOCOL_VERSION = 1

class PacketType:
    # 命令相关
    COMMAND   = "command"    # Bridge → Agent: 执行命令
    ACK       = "ack"        # Agent → Bridge: 已收到命令
    DONE      = "done"       # Agent → Bridge: 命令执行完成
    ERROR     = "error"      # Agent → Bridge: 命令执行失败

    # 心跳
    PING      = "ping"       # Bridge → Agent
    PONG      = "pong"       # Agent → Bridge

    # 握手
    CHALLENGE      = "challenge"       # Bridge → Agent
    AUTH_REPLY     = "auth_response"   # Agent → Bridge
    BLACKLISTED    = "blacklisted"     # Bridge → Agent

class Capability:
    SCREENSHOT = "screenshot"
    CLICK      = "click"
    MOVE       = "move"
    DRAG       = "drag"
    KEYBOARD   = "keyboard"
    TYPE_TEXT   = "type_text"
    KEY        = "key"
    PRESS      = "press"
    SCROLL     = "scroll"
    CMD        = "cmd"
    CLIPBOARD  = "clipboard"
    DOWNLOAD   = "download"
    OPEN_URL   = "open_url"
    FILE       = "file"

    # 平台标识
    WINDOWS    = "platform:windows"
    LINUX      = "platform:linux"
    MACOS      = "platform:macos"
    ANDROID    = "platform:android"

def new_packet_id() -> str:
    return uuid.uuid4().hex[:12]

def make_command(action: str, params: dict = None, session: str = "", expire_seconds: int = 300) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.COMMAND,
        "packet_id": new_packet_id(),
        "action": action,
        "params": params or {},
        "session": session,
        "timestamp": time.time(),
        "expire": time.time() + expire_seconds,
        "retry": 0,
    }

def make_ack(packet_id: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.ACK,
        "packet_id": packet_id,
        "timestamp": time.time(),
    }

def make_done(packet_id: str, result: dict) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.DONE,
        "packet_id": packet_id,
        "result": result,
        "timestamp": time.time(),
    }

def make_error(packet_id: str, error: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.ERROR,
        "packet_id": packet_id,
        "error": error,
        "timestamp": time.time(),
    }

def make_ping() -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.PING,
        "timestamp": time.time(),
    }

def make_challenge(challenge_hex: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.CHALLENGE,
        "challenge": challenge_hex,
    }

def make_blacklisted(reason: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": PacketType.BLACKLISTED,
        "reason": reason,
    }

def encode_packet(packet: dict) -> bytes:
    return json.dumps(packet, ensure_ascii=False).encode()

def parse_packet(raw: bytes) -> dict:
    return json.loads(raw)

def get_packet_type(packet: dict) -> str:
    return packet.get("type", "")
