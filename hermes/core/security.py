# -*- coding: utf-8 -*-
"""
hermes.core.security — 安全认证管理
负责 HMAC-SHA256 签名、Challenge-Response 握手验证。
"""

import hmac
import hashlib
import secrets
import logging

logger = logging.getLogger("hermes.core.security")

class SecurityManager:
    def __init__(self, shared_secret: str):
        if not shared_secret:
            logger.warning("SecurityManager initialized with empty shared_secret!")
        self._secret = shared_secret.encode()

    def generate_challenge(self) -> str:
        """生成随机挑战字符串"""
        return secrets.token_hex(16)

    def verify_response(self, challenge: str, response: str) -> bool:
        """验证 Agent 回传的 HMAC 响应"""
        if not response:
            return False
        
        expected = hmac.new(
            self._secret, 
            challenge.encode(), 
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected, response)

    def sign_payload(self, body: bytes) -> str:
        """对 Payload 进行签名 (用于 HTTP API)"""
        return hmac.new(
            self._secret, 
            body, 
            hashlib.sha256
        ).hexdigest()

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """验证 Payload 签名"""
        if not signature:
            return False
        expected = self.sign_payload(body)
        return hmac.compare_digest(expected, signature)
