# -*- coding: utf-8 -*-
"""
crypto_loader — 运行时解密配置
  敏感数据（IP、密钥）不以明文存储，运行时动态解密。
  所有环境变量名称集中在此模块，客户端代码不直接引用。

  优先级：环境变量 > 加密配置文件 > 空字符串
"""
import base64
import os
import sys

# ── XOR 密钥（需与 obfuscate_config.py 一致）──
_KEY = b"hG7$kL9@mN2#qR5&wX8"

# ── 环境变量映射（key_name → env_var）──
_ENV_MAP = {
    "SERVER_HOST":   "BRIDGE_HOST",
    "SERVER_URL":    "HERMES_SERVER_URL",
    "UPDATE_URL":    "UPDATE_SERVER",
    "API_KEY":       "CHAT_API_KEY",
    "SHARED_SECRET": "SHARED_SECRET",
}


def _xor_decrypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _decode(encoded: str) -> str:
    try:
        raw = base64.b64decode(encoded.encode())
        return _xor_decrypt(raw, _KEY).decode("utf-8")
    except Exception:
        return ""


# ── 加载加密配置文件 ──
_config = {}
try:
    import _enc_config
    for key, value in _enc_config.__dict__.items():
        if not key.startswith("_") and isinstance(value, str):
            _config[key] = value
except ImportError:
    pass


def get_config(name: str, default: str = "") -> str:
    """获取配置值。优先级：环境变量 > 加密文件 > 默认值"""
    env_var = _ENV_MAP.get(name)
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    encoded = _config.get(name)
    if encoded:
        return _decode(encoded)

    return default


def get_api_key() -> str:
    return get_config("API_KEY", "")


def get_secret() -> str:
    return get_config("SHARED_SECRET", "")
