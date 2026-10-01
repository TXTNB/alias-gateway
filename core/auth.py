"""管理密码的哈希与内存会话表。

密码用 PBKDF2-HMAC-SHA256 存储，落盘格式固定为：

    pbkdf2_sha256$<迭代次数>$<盐 hex>$<摘要 hex>

空字符串表示"还没注册"，是合法状态 —— 首次打开管理界面时再设置密码。
会话只存在进程内存里，服务重启即全部失效。
"""

import hashlib
import hmac
import os
import secrets
import threading
import time

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200000
SALT_BYTES = 16
DIGEST_BYTES = 32

DEFAULT_SESSION_TTL = 8 * 3600
TOKEN_BYTES = 32


def hash_password(password, iterations=ITERATIONS, salt=None):
    """生成 ``pbkdf2_sha256$iters$salt$hash`` 形式的哈希串。

    ``salt`` 允许传入 hex 字符串，方便测试固定盐值。
    """
    if isinstance(password, (bytes, bytearray)):
        password = bytes(password)
    else:
        password = str(password).encode("utf-8")

    if salt is None:
        salt = os.urandom(SALT_BYTES)
    elif isinstance(salt, str):
        salt = bytes.fromhex(salt)
    salt = bytes(salt)

    digest = hashlib.pbkdf2_hmac("sha256", password, salt, int(iterations), dklen=DIGEST_BYTES)
    return "%s$%d$%s$%s" % (ALGORITHM, int(iterations), salt.hex(), digest.hex())


def verify_password(password, stored):
    """校验密码。

    任何解析异常都当作"不匹配"处理，不向上抛 —— 调用方只需要知道真假。
    摘要长度从存量哈希里反推，以便兼容以后调整 DIGEST_BYTES。
    """
    if not stored or not isinstance(stored, str):
        return False
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != ALGORITHM:
        return False
    try:
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    if iterations <= 0 or not salt:
        return False

    if isinstance(password, (bytes, bytearray)):
        raw = bytes(password)
    else:
        raw = str(password).encode("utf-8")
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, iterations, dklen=len(hash_hex) // 2 or DIGEST_BYTES)
    return hmac.compare_digest(digest.hex(), hash_hex)


def verify_format(stored):
    """只检查格式是否合法，不校验密码内容。"""
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations, salt_hex, hash_hex = parts
    if algorithm != ALGORITHM:
        return False
    try:
        if int(iterations) <= 0:
            return False
        bytes.fromhex(salt_hex)
        bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return bool(salt_hex) and bool(hash_hex)


def is_registered(stored):
    """是否已经注册（设置过管理密码）。空字符串表示还没注册。"""
    return bool(stored) and verify_format(stored)


class SessionStore:
    """内存会话表：重启即失效，token 默认 8 小时过期。"""

    def __init__(self, ttl=DEFAULT_SESSION_TTL):
        self.ttl = int(ttl)
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self):
        """签发一个新 token，顺带清掉已过期的旧会话。"""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            self._sessions[token] = now + self.ttl
        return token

    def validate(self, token):
        """token 是否有效；顺手清掉这条已过期的记录。"""
        if not token:
            return False
        now = time.time()
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires <= now:
                self._sessions.pop(token, None)
                return False
            return True

    def revoke(self, token):
        """踢掉单个会话，返回它原本是否存在。"""
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def revoke_all(self):
        """清空所有会话，用于改密码后强制重新登录。"""
        with self._lock:
            self._sessions.clear()

    def purge(self):
        """主动清理过期会话。"""
        with self._lock:
            self._purge_locked(time.time())

    def _purge_locked(self, now):
        expired = [t for t, exp in self._sessions.items() if exp <= now]
        for t in expired:
            self._sessions.pop(t, None)

    def __len__(self):
        """当前在线会话数，供总览页展示。"""
        with self._lock:
            return len(self._sessions)