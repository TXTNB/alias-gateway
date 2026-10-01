
"""server 层与 app 的单元测试：导入完整性、响应封装、SSE 改名、面板逻辑。

这组用例只测"纯函数 + 不依赖网络的部分"：

* 导入冒烟顺带扫一遍越界相对导入，防止 core/server 里出现 ``from ..``；
* SSE 改名用例覆盖 data 行、[DONE]、坏 JSON 与嵌套结构；
* 面板用例在临时目录里播种一份配置，验证注册 / 登录 / 打码 / 空骨架行为。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
from core import auth as auth_mod
from core import config as config_mod
from server import sse as sse_mod
from server.handlers import Gateway, Response, error_response


class ImportSmokeTest(unittest.TestCase):
    """模块能导入、版本可读、且没有越界的相对导入。"""

    def test_app_imports(self):
        self.assertTrue(callable(app_mod.main))

    def test_server_modules_import(self):
        from server import handlers, http, sse

        self.assertTrue(callable(http.build_server))

    def test_no_relative_beyond_top_level(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for folder in ("server", "core"):
            target = os.path.join(root, folder)
            for name in sorted(os.listdir(target)):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(target, name)
                with open(path, "r", encoding="utf-8") as fp:
                    for lineno, line in enumerate(fp, 1):
                        stripped = line.strip()
                        if stripped.startswith("from ..") or stripped.startswith("import .."):
                            offenders.append("%s/%s:%d %s" % (folder, name, lineno, stripped))
        self.assertEqual(offenders, [], "越界相对导入：%s" % offenders)

    def test_gateway_version_is_reported(self):
        from server import VERSION

        self.assertTrue(VERSION)


class ResponseTest(unittest.TestCase):
    """响应封装与错误转响应。"""

    def test_json_response(self):
        resp = Response.json({"ok": True})
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.content_type)
        self.assertIn(b'"ok"', resp.body)

    def test_error_response_shape(self):
        from core.errors import AuthError

        resp = error_response(AuthError("bad key"))
        self.assertEqual(resp.status, 401)
        self.assertIn(b"bad key", resp.body)


class SseRewriteTest(unittest.TestCase):
    """SSE 行改写：只动该动的，解析不了的原样放行。"""

    def test_rewrites_model_in_data_line(self):
        line = b'data: {"model":"raw-name","choices":[]}'
        out = sse_mod.rewrite_data_line(line, {"client-name": "raw-name"})
        self.assertIn(b'"model": "client-name"', out)

    def test_non_data_line_untouched(self):
        line = b"event: ping"
        self.assertEqual(sse_mod.rewrite_data_line(line, {"a": "b"}), line)

    def test_done_sentinel_untouched(self):
        line = b"data: [DONE]"
        self.assertEqual(sse_mod.rewrite_data_line(line, {"a": "b"}), line)

    def test_broken_json_untouched(self):
        line = b"data: {not json"
        self.assertEqual(sse_mod.rewrite_data_line(line, {"a": "b"}), line)

    def test_nested_model_field_rewritten(self):
        line = b'data: {"response":{"model":"raw-name"}}'
        out = sse_mod.rewrite_data_line(line, {"client-name": "raw-name"})
        self.assertIn(b'"model": "client-name"', out)

    def test_json_body_rewrite(self):
        payload = {"model": "raw-name", "choices": [{"message": {"model": "raw-name"}}]}
        sse_mod.rewrite_json_body(payload, {"client-name": "raw-name"})
        self.assertEqual(payload["model"], "client-name")
        self.assertEqual(payload["choices"][0]["message"]["model"], "client-name")


class GatewayPanelTest(unittest.TestCase):
    """面板接口：注册 / 登录 / 打码 / 空骨架。

    每个用例在独立临时目录里播种配置，避免相互污染。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gw-test-")
        self.path = os.path.join(self.tmp, "config.json")
        self._seed_config()
        self.store = config_mod.ConfigStore.load(self.path)
        self.gw = Gateway(self.store, log_path=os.path.join(self.tmp, "g.log"),
                          console=False)

    def _seed_config(self):
        """播种一份"已有一条 route"的配置：多数用例需要它。"""
        import json

        cfg = {
            "listen_port": 8789,
            "listen_host": "127.0.0.1",
            "panel_password_hash": "",
            "routes": [
                {
                    "client_key": "sk-testkey1234",
                    "upstreams": [
                        {
                            "name": "up-1",
                            "base_url": "https://upstream.example/v1",
                            "auth": {"type": "bearer", "key": "real-secret"},
                            "extra_headers": {},
                            "timeout": 300,
                        }
                    ],
                    "aliases": {},
                    "prefer": {},
                    "whitelist": [],
                    "blacklist": [],
                }
            ],
        }
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, password="admin"):
        import json

        resp = self.gw.panel_register(json.dumps({"password": password}).encode("utf-8"))
        self.assertEqual(resp.status, 200)
        return json.loads(resp.body.decode("utf-8"))["token"]

    def test_panel_requires_login(self):
        from core.errors import PanelAuthError

        with self.assertRaises(PanelAuthError):
            self.gw.panel_config_get("not-a-token")

    def test_register_then_use_token(self):
        token = self._register("admin")
        self.assertEqual(self.gw.panel_config_get(token).status, 200)

    def test_register_rejected_when_already_registered(self):
        import json

        self._register("admin")
        resp = self.gw.panel_register(b'{"password":"other"}')
        self.assertEqual(resp.status, 400)
        self.assertFalse(json.loads(resp.body.decode("utf-8"))["ok"])

    def test_register_rejects_short_password(self):
        resp = self.gw.panel_register(b'{"password":"ab"}')
        self.assertEqual(resp.status, 400)

    def test_login_before_register_is_rejected(self):
        from core.errors import PanelAuthError

        with self.assertRaises(PanelAuthError):
            self.gw.panel_login(b'{"password":"admin"}')

    def test_status_reports_unregistered_then_registered(self):
        import json

        resp = self.gw.panel_status("")
        self.assertFalse(json.loads(resp.body.decode("utf-8"))["panel_registered"])
        self._register("admin")
        resp = self.gw.panel_status("")
        self.assertTrue(json.loads(resp.body.decode("utf-8"))["panel_registered"])

    def test_panel_login_then_access(self):
        self._register("admin")
        resp = self.gw.panel_login(b'{"password":"admin"}')
        self.assertEqual(resp.status, 200)
        import json

        token = json.loads(resp.body.decode("utf-8"))["token"]
        masked = self.gw.panel_config_get(token)
        self.assertEqual(masked.status, 200)

    def test_masked_config_hides_secrets(self):
        import json

        token = self._register("admin")
        payload = json.loads(self.gw.panel_config_get(token).body.decode("utf-8"))
        stored = self.store.password_hash()
        self.assertNotIn(stored, json.dumps(payload))
        self.assertEqual(payload["routes"][0]["client_key"],
                         self.store.routes()[0]["client_key"])
        self.assertIn(config_mod.MASK, payload["routes"][0]["upstreams"][0]["auth"]["key"])

    def test_panel_models_reports_empty_when_no_route(self):
        import json

        empty_path = os.path.join(self.tmp, "empty.json")
        empty_store = config_mod.ConfigStore.load(empty_path)
        gw = Gateway(empty_store, log_path=os.path.join(self.tmp, "e.log"), console=False)
        token = json.loads(
            gw.panel_register(b'{"password":"admin"}').body.decode("utf-8")
        )["token"]
        payload = json.loads(gw.panel_models(token).body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["routes"], [])

    def test_overview_on_empty_skeleton(self):
        import json

        empty_path = os.path.join(self.tmp, "empty2.json")
        empty_store = config_mod.ConfigStore.load(empty_path)
        gw = Gateway(empty_store, log_path=os.path.join(self.tmp, "e2.log"), console=False)
        token = json.loads(
            gw.panel_register(b'{"password":"admin"}').body.decode("utf-8")
        )["token"]
        payload = json.loads(gw.panel_overview(token).body.decode("utf-8"))
        self.assertEqual(payload["routes"], 0)
        self.assertEqual(payload["upstreams"], 0)

    def test_overview_has_no_default_password_flag(self):
        import json

        token = self._register("admin")
        payload = json.loads(self.gw.panel_overview(token).body.decode("utf-8"))
        self.assertNotIn("default_password", payload)
        self.assertTrue(payload["panel_registered"])

    def test_wrong_password_rejected(self):
        from core.errors import PanelAuthError

        self._register("admin")
        with self.assertRaises(PanelAuthError):
            self.gw.panel_login(b'{"password":"nope"}')


if __name__ == "__main__":
    unittest.main()
