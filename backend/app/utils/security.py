"""安全认证工具。"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import get_authsettings


def hash_password(password: str) -> str:
    """使用 bcrypt 计算密码散列值。

    Args:
        password: 密码明文。

    Returns:
        str: 含盐的散列值。

    Note:
        本函数每次调用都会生成新盐, 同一密码多次调用的散列结果不同。
        因此校验必须使用 verify_password 函数, 不能直接比对散列字符串。
    """
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码明文与存储的散列值是否匹配。

    Args:
        password: 密码明文。
        password_hash: 数据库中保存的密码散列值。

    Returns:
        bool: 是否匹配。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(uid: int) -> str:
    """签发访问令牌。

    Args:
        uid: 用户uid。

    Returns:
        str: 编码后的JWT。
    """
    settings = get_authsettings()
    payload = {
        "sub": str(uid),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload=payload, key=settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> int:
    """校验并解析访问令牌, 取出令牌内的uid。

    Args:
        token: 编码后的 JWT 令牌。

    Returns:
        int: 令牌携带的用户uid。

    Raises:
        jwt.PyJWTError: 签名无效、令牌过期或格式错误。
        KeyError: 载荷缺少 sub 声明。
        ValueError: sub 无法转换为整数。

    Note:
        此函数不会自动处理异常, 应由调用方捕获异常并进行处理。
    """
    settings = get_authsettings()
    payload = jwt.decode(jwt=token, key=settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return int(payload["sub"])
