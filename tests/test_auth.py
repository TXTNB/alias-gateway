
"""core.auth 的单元测试：密码哈希与会话表。

密码相关的用例集中在"存进去的必须是哈希、认得出的必须只有正确密码"；
会话相关的用例覆盖签发、校验、吊销与过期。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth


class HashPasswordTest(unittest.TestCase):
    """哈希串的格式与随机性。"""

    def test_format(self):
        stored = auth.hash_password("admin")
        parts = stored.split("$")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "pbkdf2_sha256")
        self.assertEqual(parts[1], str(auth.ITERATIONS))
        self.assertEqual(len(parts[2]), auth.SALT_BYTES * 2)
        self.assertEqual(len(parts[3]), auth.DIGEST_BYTES * 2)

    def test_never_stores_plaintext(self):
        stored = auth.hash_password("admin")
        self.assertNotIn("admin", stored)

    def test_salt_is_random(self):
        first = auth.hash_password("admin")
        second = auth.hash_password("admin")
        self.assertNotEqual(first, second)

    def test_unicode_password(self):
        stored = auth.hash_password("猫娘密码喵")
        self.assertTrue(auth.verify_password("猫娘密码喵", stored))
        self.assertFalse(auth.verify_password("猫娘密码", stored))


class VerifyPasswordTest(unittest.TestCase):
    """密码校验：正确放行，错误与脏数据一律拒绝且不抛异常。"""

    def test_accepts_correct_password(self):
        stored = auth.hash_password("admin")
        self.assertTrue(auth.verify_password("admin", stored))

    def test_rejects_wrong_password(self):
        stored = auth.hash_password("admin")
        self.assertFalse(auth.verify_password("admin1", stored))
        self.assertFalse(auth.verify_password("", stored))

    def test_rejects_garbage_stored_value(self):
        for bad in ("", None, "plaintext", "pbkdf2_sha256$abc$x$y",
                    "md5$1$aa$bb", "pbkdf2_sha256$200000$zz$zz"):
            self.assertFalse(auth.verify_password("admin", bad), msg=repr(bad))

    def test_short_hash_length_is_handled(self):
        import hashlib
        salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        digest = hashlib.pbkdf2_hmac("sha256", b"admin", salt, 1000, dklen=16)
        stored = "pbkdf2_sha256$1000$%s$%s" % (salt.hex(), digest.hex())
        self.assertTrue(auth.verify_password("admin", stored))
        self.assertFalse(auth.verify_password("nope", stored))


class VerifyFormatTest(unittest.TestCase):
    """只校验格式，不校验内容——配置加载时用它做快速检查。"""

    def test_valid_format(self):
        self.assertTrue(auth.verify_format(auth.hash_password("admin")))

    def test_invalid_formats(self):
        for bad in ("", None, "pbkdf2_sha256", "pbkdf2_sha256$200000$aa",
                    "pbkdf2_sha256$0$aa$bb", "sha1$200000$aa$bb"):
            self.assertFalse(auth.verify_format(bad), msg=repr(bad))


class IsRegisteredTest(unittest.TestCase):
    """注册状态判定：空哈希 = 未注册，这是首次启动的正常状态。"""

    def test_unregistered_is_empty_hash(self):
        self.assertFalse(auth.is_registered(""))
        self.assertFalse(auth.is_registered(None))

    def test_any_set_password_counts_as_registered(self):
        self.assertTrue(auth.is_registered(auth.hash_password("something-else")))
        self.assertTrue(auth.is_registered(auth.hash_password("admin")))

    def test_garbage_is_not_registered(self):
        self.assertFalse(auth.is_registered("plaintext"))


class SessionStoreTest(unittest.TestCase):
    """会话表：签发 / 校验 / 吊销 / 过期。"""

    def test_create_and_validate(self):
        store = auth.SessionStore()
        token = store.create()
        self.assertTrue(token)
        self.assertTrue(store.validate(token))

    def test_unknown_token_is_invalid(self):
        store = auth.SessionStore()
        self.assertFalse(store.validate("nope"))
        self.assertFalse(store.validate(""))
        self.assertFalse(store.validate(None))

    def test_tokens_are_unique(self):
        store = auth.SessionStore()
        tokens = {store.create() for _ in range(50)}
        self.assertEqual(len(tokens), 50)

    def test_revoke(self):
        store = auth.SessionStore()
        token = store.create()
        self.assertTrue(store.revoke(token))
        self.assertFalse(store.validate(token))
        self.assertFalse(store.revoke(token))

    def test_revoke_all(self):
        store = auth.SessionStore()
        tokens = [store.create() for _ in range(3)]
        store.revoke_all()
        for token in tokens:
            self.assertFalse(store.validate(token))
        self.assertEqual(len(store), 0)

    def test_expiry(self):
        store = auth.SessionStore(ttl=1)
        token = store.create()
        self.assertTrue(store.validate(token))
        time.sleep(1.05)
        self.assertFalse(store.validate(token))

    def test_default_ttl_is_eight_hours(self):
        self.assertEqual(auth.DEFAULT_SESSION_TTL, 8 * 3600)
        self.assertEqual(auth.SessionStore().ttl, 8 * 3600)


if __name__ == "__main__":
    unittest.main()
