# 安全模块：口令哈希（PBKDF2，stdlib）+ JWT 签发/校验（携带 role）
import hashlib
import hmac
import os
import time
from typing import Any

import jwt

from app.config import settings

# ---------------- 口令哈希 ----------------

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 哈希，格式: salt$hex_digest"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较，防时序攻击"""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 200_000
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False

# ---------------- JWT ----------------

def create_token(user_id: int, username: str, role: str) -> str:
    """签发 Bearer token，负载携带 role（对齐 PRD F-RBAC-02）"""
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": int(time.time()) + settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """校验并解码 token；无效/过期返回 None"""
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
