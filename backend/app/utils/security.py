"""安全认证工具"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_authsettings


def hash_password(password: str) -> str:
    """将密码哈希"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

def verify_password(password: str, password_hash: str) -> bool:
    """校验密码"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False

def create_access_token(uid: int) -> str:
    """签发JWT"""
    settings = get_authsettings()
    payload = {
        "sub": str(uid),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.jwt_expire_days)
    }
    return jwt.encode(payload=payload, key=settings.jwt_secret, algorithm=settings.jwt_algorithm)

def decode_access_token(token: str) -> int:
    """校验并解析JWT"""
    settings = get_authsettings()
    payload = jwt.decode(jwt=token, key=settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return int(payload["sub"])