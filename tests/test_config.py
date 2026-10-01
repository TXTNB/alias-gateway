
"""core.config 的单元测试：首次生成、校验、归一化、冲突检测、打码与合并。

几处容易踩坑的约定，用例特意钉住了：

* 首次生成的配置是空骨架（``routes: []``），且不含任何预填上游；
* ``routes`` 为空是合法状态，但单条 route 没有 upstream 是错误；
* 管理端提交时，打码或空白的上游 Key 必须回填成原值，不能被写坏；
* ``apply_overrides`` 校验失败要回滚，不能留下半改的状态。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth
from core import config as config_mod
from core.errors import ConfigError


def _route(**overrides):
    """一条含两个上游的完整路由，用作夹具。"""
    route = {
        "client_key": "sk-testkey1234",
        "upstreams": [
            {
                "name": "wb",
                "base_url": "https://upstream-a.example/v1",
                "auth": {"type": "bearer", "key": "key-a"},
                "extra_headers": {},
                "timeout": 300,
            },
            {
                "name": "deepseek",
                "base_url": "https://upstream-b.example/v1",
                "auth": {"type": "bearer", "key": "key-b"},
                "extra_headers": {},
                "timeout": 300,
            },
        ],
        "aliases": {"deepseek-flash": "deepseek-v4.1-flash"},
        "prefer": {},
        "whitelist": [],
        "blacklist": [],
    }
    route.update(overrides)
    return route


def _cfg(**overrides):
    """一份校验通过的完整配置，用作夹具。"""
    cfg = {
        "listen_port": 8789,
        "listen_host": "127.0.0.1",
        "panel_password_hash": auth.hash_password("admin"),
        "routes": [_route()],
    }
    cfg.update(overrides)
    return cfg


def _write_config_file(path, **overrides):
    """把夹具写到指定路径。

    空骨架成为默认之后，"需要已有 route"的用例必须显式播种，
    否则读到的会是空的。
    """
    cfg = _cfg(**overrides)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    return cfg


class GenerateClientKeyTest(unittest.TestCase):
    """客户端 Key 的格式：sk- 前缀 + 64 位小写十六进制。"""

    def test_prefix_and_length(self):
        key = config_mod.generate_client_key()
        self.assertTrue(key.startswith("sk-"))
        self.assertEqual(len(key), 3 + 64)

    def test_is_random(self):
        keys = {config_mod.generate_client_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_is_hex_after_prefix(self):
        key = config_mod.generate_client_key()
        int(key[3:], 16)

    def test_matches_validation_regex(self):
        for _ in range(20):
            self.assertRegex(config_mod.generate_client_key(), config_mod.CLIENT_KEY_RE)


class MaskSecretTest(unittest.TestCase):
    """上游 Key 打码：保留前几位，短值整体遮住。"""

    def test_keeps_head_and_masks_rest(self):
        masked = config_mod.mask_secret("sk-abcdefghijklmnop")
        self.assertTrue(masked.startswith("sk-a"))
        self.assertIn(config_mod.MASK, masked)
        self.assertNotIn("bcdefghijklmnop", masked)

    def test_short_value_is_fully_masked(self):
        self.assertEqual(config_mod.mask_secret("ab"), config_mod.MASK)

    def test_empty_and_none(self):
        self.assertEqual(config_mod.mask_secret(""), "")
        self.assertEqual(config_mod.mask_secret(None), "")


class DefaultConfigTest(unittest.TestCase):
    """首次生成：必须是空骨架，不含任何占位上游。"""

    def test_defaults(self):
        cfg = config_mod.default_config()
        self.assertEqual(cfg["listen_port"], 8789)
        self.assertEqual(cfg["listen_host"], "127.0.0.1")

    def test_default_has_no_route(self):
        cfg = config_mod.default_config()
        self.assertEqual(cfg["routes"], [])

    def test_default_is_unregistered(self):
        cfg = config_mod.default_config()
        self.assertEqual(cfg["panel_password_hash"], "")
        self.assertFalse(auth.is_registered(cfg["panel_password_hash"]))

    def test_no_placeholder_upstream(self):
        cfg = config_mod.default_config()
        self.assertNotIn("example.invalid", json.dumps(cfg))

    def test_default_config_passes_validation(self):
        self.assertEqual(config_mod.validate_config(config_mod.default_config()), [])

    def test_init_messages(self):
        cfg = config_mod.default_config()
        store = config_mod.ConfigStore(os.path.join(tempfile.gettempdir(), "alias-gw-init-msg.json"))
        store._config = cfg
        lines = config_mod.init_messages(store)
        self.assertEqual(len(lines), 3)
        self.assertIn("config.json created", lines[0])
        self.assertIn("not registered", lines[1])
        self.assertIn("no route", lines[2])

    def test_init_messages_with_route(self):
        cfg = _cfg()
        store = config_mod.ConfigStore(os.path.join(tempfile.gettempdir(), "alias-gw-init-msg2.json"))
        store._config = cfg
        lines = config_mod.init_messages(store)
        self.assertIn(cfg["routes"][0]["client_key"], lines[2])


class ValidateConfigTest(unittest.TestCase):
    """校验规则：空 routes 合法，单条 route 的字段错误都要报出来。"""

    def test_valid_config(self):
        self.assertEqual(config_mod.validate_config(_cfg()), [])

    def test_port_out_of_range(self):
        errors = config_mod.validate_config(_cfg(listen_port=0))
        self.assertTrue(any("listen_port" in e for e in errors))
        errors = config_mod.validate_config(_cfg(listen_port=70000))
        self.assertTrue(any("listen_port" in e for e in errors))

    def test_empty_host(self):
        errors = config_mod.validate_config(_cfg(listen_host=""))
        self.assertTrue(any("listen_host" in e for e in errors))

    def test_bad_password_hash(self):
        errors = config_mod.validate_config(_cfg(panel_password_hash="plaintext"))
        self.assertTrue(any("panel_password_hash" in e for e in errors))

    def test_no_routes(self):
        errors = config_mod.validate_config(_cfg(routes=[]))
        self.assertEqual(errors, [])

    def test_duplicate_client_key(self):
        cfg = _cfg(routes=[_route(), _route()])
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("client_key 重复" in e for e in errors))

    def test_bad_client_key(self):
        cfg = _cfg(routes=[_route(client_key="a b")])
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("client_key 非法" in e for e in errors))

    def test_route_without_upstream(self):
        cfg = _cfg(routes=[_route(upstreams=[])])
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("至少要有一个 upstream" in e for e in errors))

    def test_duplicate_upstream_name(self):
        route = _route()
        route["upstreams"][1]["name"] = route["upstreams"][0]["name"]
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("name 重复" in e for e in errors))

    def test_base_url_must_end_with_v1(self):
        route = _route()
        route["upstreams"][0]["base_url"] = "https://example.com/api"
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("/v1" in e for e in errors))

    def test_base_url_scheme(self):
        route = _route()
        route["upstreams"][0]["base_url"] = "ftp://example.com/v1"
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("http:// 或 https://" in e for e in errors))

    def test_auth_type_and_key(self):
        route = _route()
        route["upstreams"][0]["auth"] = {"type": "bearer", "key": ""}
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("没填 key" in e for e in errors))

        route = _route()
        route["upstreams"][0]["auth"] = {"type": "magic", "key": "x"}
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("auth.type" in e for e in errors))

    def test_header_auth_needs_header_name(self):
        route = _route()
        route["upstreams"][0]["auth"] = {"type": "header", "key": "x", "header": ""}
        errors = config_mod.validate_config(_cfg(routes=[route]))
        self.assertTrue(any("没填 header 名" in e for e in errors))

    def test_auth_none_needs_no_key(self):
        route = _route()
        route["upstreams"][0]["auth"] = {"type": "none", "key": ""}
        self.assertEqual(config_mod.validate_config(_cfg(routes=[route])), [])

    def test_prefer_must_point_to_existing_upstream(self):
        cfg = _cfg(routes=[_route(prefer={"deepseek-v4.1-flash": "ghost"})])
        errors = config_mod.validate_config(cfg)
        self.assertTrue(any("指向不存在的 upstream" in e for e in errors))

    def test_prefer_ok(self):
        cfg = _cfg(routes=[_route(prefer={"deepseek-v4.1-flash": "wb"})])
        self.assertEqual(config_mod.validate_config(cfg), [])


class NormalizeTest(unittest.TestCase):
    """归一化：补默认值、去空白、把类型不对的字段兜住。"""

    def test_upstream_defaults(self):
        up = config_mod.normalize_upstream({"base_url": "https://a.example/v1"})
        self.assertEqual(up["name"], "upstream-1")
        self.assertEqual(up["auth"]["type"], "bearer")
        self.assertEqual(up["timeout"], config_mod.DEFAULT_UPSTREAM_TIMEOUT)

    def test_upstream_strips_trailing_slash(self):
        up = config_mod.normalize_upstream({"base_url": "https://a.example/v1/"})
        self.assertEqual(up["base_url"], "https://a.example/v1")

    def test_upstream_drops_unknown_fields(self):
        up = config_mod.normalize_upstream({"base_url": "https://a.example/v1", "junk": 1})
        self.assertNotIn("junk", up)

    def test_upstream_bad_timeout_falls_back(self):
        up = config_mod.normalize_upstream({"base_url": "https://a.example/v1", "timeout": "abc"})
        self.assertEqual(up["timeout"], config_mod.DEFAULT_UPSTREAM_TIMEOUT)
        up = config_mod.normalize_upstream({"base_url": "https://a.example/v1", "timeout": -5})
        self.assertEqual(up["timeout"], config_mod.DEFAULT_UPSTREAM_TIMEOUT)

    def test_route_drops_blank_alias_entries(self):
        route = config_mod.normalize_route({"aliases": {"a": "b", "": "c", "d": ""}})
        self.assertEqual(route["aliases"], {"a": "b"})

    def test_route_list_fields_must_be_lists(self):
        with self.assertRaises(ConfigError):
            config_mod.normalize_route({"whitelist": "not-a-list"})

    def test_non_dict_route_raises(self):
        with self.assertRaises(ConfigError):
            config_mod.normalize_route("nope")

    def test_non_dict_config_raises(self):
        with self.assertRaises(ConfigError):
            config_mod.normalize_config([])


class DetectConflictsTest(unittest.TestCase):
    """冲突检测：跨上游撞名才报，prefer 能消解。"""

    def setUp(self):
        self.cfg = _cfg()

    def test_no_conflict_when_models_are_distinct(self):
        model_map = {"sk-testkey1234": {"wb": ["deepseek-v4.1-flash"], "deepseek": ["glm-5.3"]}}
        self.assertEqual(config_mod.detect_conflicts(self.cfg, model_map), [])

    def test_conflict_when_same_raw_name_in_two_upstreams(self):
        model_map = {
            "sk-testkey1234": {
                "wb": ["deepseek-v4.1-flash"],
                "deepseek": ["deepseek-v4.1-flash"],
            }
        }
        conflicts = config_mod.detect_conflicts(self.cfg, model_map)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["model"], "deepseek-v4.1-flash")
        self.assertEqual(conflicts[0]["route"], "sk-testkey1234")
        self.assertEqual(
            sorted(f["name"] for f in conflicts[0]["found_in"]), ["deepseek", "wb"]
        )

    def test_prefer_resolves_conflict(self):
        cfg = _cfg(routes=[_route(prefer={"deepseek-v4.1-flash": "wb"})])
        model_map = {
            "sk-testkey1234": {
                "wb": ["deepseek-v4.1-flash"],
                "deepseek": ["deepseek-v4.1-flash"],
            }
        }
        self.assertEqual(config_mod.detect_conflicts(cfg, model_map), [])

    def test_conflict_after_alias_rename(self):
        route = _route(aliases={"shared-name": "alpha", "shared-name-2": "beta"})
        cfg = _cfg(routes=[route])
        model_map = {"sk-testkey1234": {"wb": ["alpha"], "deepseek": ["beta"]}}
        self.assertEqual(config_mod.detect_conflicts(cfg, model_map), [])

    def test_conflict_when_two_raws_share_one_client_name(self):
        route = _route(aliases={"client-x": "raw-a"})
        cfg = _cfg(routes=[route])
        model_map = {"sk-testkey1234": {"wb": ["raw-a"], "deepseek": ["client-x"]}}
        conflicts = config_mod.detect_conflicts(cfg, model_map)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["client_name"], "client-x")

    def test_missing_route_in_model_map_is_skipped(self):
        self.assertEqual(config_mod.detect_conflicts(self.cfg, {}), [])

    def test_route_keys_filter(self):
        model_map = {
            "sk-testkey1234": {"wb": ["m"], "deepseek": ["m"]},
            "sk-other": {"wb": ["m"], "deepseek": ["m"]},
        }
        cfg = _cfg(routes=[_route(), _route(client_key="sk-other")])
        conflicts = config_mod.detect_conflicts(cfg, model_map, route_keys=["sk-other"])
        self.assertEqual([c["route"] for c in conflicts], ["sk-other"])

    def test_same_upstream_listed_twice_is_not_conflict(self):
        model_map = {"sk-testkey1234": {"wb": ["m", "m"], "deepseek": ["n"]}}
        self.assertEqual(config_mod.detect_conflicts(self.cfg, model_map), [])


class ConflictReportTest(unittest.TestCase):
    """冲突报告：要给出可照抄的 prefer 修法。"""

    def test_report_contains_fix_hint(self):
        conflicts = [
            {
                "route": "sk-abc",
                "model": "deepseek-v4.1-flash",
                "client_name": None,
                "found_in": [
                    {"name": "wb", "base_url": "https://a.example/v1", "raw": "deepseek-v4.1-flash"},
                    {"name": "deepseek", "base_url": "https://b.example/v1", "raw": "deepseek-v4.1-flash"},
                ],
            }
        ]
        report = config_mod.conflict_report(conflicts)
        self.assertIn("[FATAL]", report)
        self.assertIn("sk-abc", report)
        self.assertIn("deepseek-v4.1-flash", report)
        self.assertIn("upstream 'wb'", report)
        self.assertIn('"prefer": {"deepseek-v4.1-flash": "wb"}', report)
        self.assertIn("Total: 1 conflict in 1 route.", report)
        self.assertIn("Exit.", report)


class ConfigStoreTest(unittest.TestCase):
    """配置读写：首次生成、原子落盘、命令行覆盖与回滚。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gateway-test-")
        self.path = os.path.join(self.tmp, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_load_creates_file(self):
        store = config_mod.ConfigStore.load(self.path)
        self.assertTrue(store.created)
        self.assertTrue(os.path.isfile(self.path))

    def test_second_load_is_not_created(self):
        config_mod.ConfigStore.load(self.path)
        store = config_mod.ConfigStore.load(self.path)
        self.assertFalse(store.created)

    def test_generated_file_is_valid_json(self):
        store = config_mod.ConfigStore.load(self.path)
        with open(self.path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        self.assertEqual(raw["listen_port"], 8789)
        self.assertEqual(raw["routes"], [])
        self.assertEqual(store.validate(), [])

    def test_generated_skeleton_has_no_upstream(self):
        config_mod.ConfigStore.load(self.path)
        with open(self.path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        self.assertNotIn("example.invalid", json.dumps(raw))
        self.assertEqual(raw["panel_password_hash"], "")

    def test_seeded_file_loads_without_creating(self):
        _write_config_file(self.path)
        store = config_mod.ConfigStore.load(self.path)
        self.assertFalse(store.created)
        self.assertEqual(len(store.routes()), 1)

    def test_password_survives_reload(self):
        store = config_mod.ConfigStore.load(self.path)
        stored = store.password_hash()
        again = config_mod.ConfigStore.load(self.path)
        self.assertEqual(again.password_hash(), stored)

    def test_broken_json_raises_config_error(self):
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("{not json")
        with self.assertRaises(ConfigError):
            config_mod.ConfigStore.load(self.path)

    def test_invalid_config_raises_config_error(self):
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump({"routes": "nope"}, fp)
        with self.assertRaises(ConfigError):
            config_mod.ConfigStore.load(self.path)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        store = config_mod.ConfigStore.load(self.path)
        store.save()
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".config-")]
        self.assertEqual(leftovers, [])

    def test_snapshot_is_a_copy(self):
        _write_config_file(self.path)
        store = config_mod.ConfigStore.load(self.path)
        snap = store.snapshot()
        snap["routes"][0]["client_key"] = "mutated"
        self.assertNotEqual(store.routes()[0]["client_key"], "mutated")

    def test_get_route(self):
        _write_config_file(self.path)
        store = config_mod.ConfigStore.load(self.path)
        key = store.routes()[0]["client_key"]
        self.assertIsNotNone(store.get_route(key))
        self.assertIsNone(store.get_route("sk-nope"))

    def test_upstreams_are_flattened_with_route_key(self):
        _write_config_file(self.path)
        store = config_mod.ConfigStore.load(self.path)
        key = store.routes()[0]["client_key"]
        ups = store.upstreams()
        self.assertEqual(len(ups), 2)
        self.assertEqual(ups[0]["route_key"], key)

    def test_apply_overrides_persists_port(self):
        store = config_mod.ConfigStore.load(self.path)
        changed, errors = store.apply_overrides(listen_port=9100)
        self.assertTrue(changed)
        self.assertEqual(errors, [])
        again = config_mod.ConfigStore.load(self.path)
        self.assertEqual(again.config["listen_port"], 9100)

    def test_apply_overrides_none_is_noop(self):
        store = config_mod.ConfigStore.load(self.path)
        changed, errors = store.apply_overrides(listen_host=None, listen_port=None)
        self.assertFalse(changed)
        self.assertEqual(errors, [])

    def test_apply_overrides_rejects_invalid_and_rolls_back(self):
        store = config_mod.ConfigStore.load(self.path)
        changed, errors = store.apply_overrides(listen_port=70000)
        self.assertFalse(changed)
        self.assertTrue(errors)
        self.assertEqual(store.config["listen_port"], 8789)


class MaskedTest(unittest.TestCase):
    """给管理端的副本：上游 Key 打码、客户端 Key 明文、哈希不外泄。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gateway-test-")
        self.path = os.path.join(self.tmp, "config.json")
        _write_config_file(self.path, panel_password_hash="")
        self.store = config_mod.ConfigStore.load(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upstream_keys_are_masked_but_client_key_is_not(self):
        masked = self.store.masked()
        self.assertEqual(masked["panel_password_hash"], "")
        self.assertFalse(masked["panel_registered"])
        self.assertTrue(masked["routes"])
        for route in masked["routes"]:
            self.assertFalse(route["client_key"].startswith(config_mod.MASK))
            self.assertNotIn("client_key_masked", route)
            for up in route["upstreams"]:
                if up["auth"].get("key"):
                    self.assertIn(config_mod.MASK, up["auth"]["key"])

    def test_real_config_is_untouched(self):
        self.store.masked()
        self.assertNotEqual(self.store.password_hash(), config_mod.MASK)

    def test_registered_flag_reflects_registration(self):
        self.assertFalse(self.store.masked()["panel_registered"])
        self.store.register_password("newpass")
        self.assertTrue(self.store.masked()["panel_registered"])


class ApplyPanelUpdateTest(unittest.TestCase):
    """管理端提交：Key 回填规则与空骨架下新增路由。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gateway-test-")
        self.path = os.path.join(self.tmp, "config.json")
        _write_config_file(self.path)
        self.store = config_mod.ConfigStore.load(self.path)
        self.key = self.store.routes()[0]["client_key"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_route_from_empty_skeleton(self):
        empty_path = os.path.join(self.tmp, "empty.json")
        store = config_mod.ConfigStore.load(empty_path)
        self.assertEqual(store.routes(), [])
        payload = store.masked()
        payload["routes"] = [{
            "client_key": "",
            "upstreams": [{
                "name": "primary",
                "base_url": "https://api.example.com/v1",
                "auth": {"type": "bearer", "key": "real-key"},
                "extra_headers": {},
                "timeout": 300,
            }],
            "aliases": {},
            "prefer": {},
            "whitelist": [],
            "blacklist": [],
        }]
        ok, errors, _ = store.apply_panel_update(payload)
        self.assertTrue(ok, errors)
        key = store.routes()[0]["client_key"]
        self.assertTrue(key.startswith("sk-"))
        self.assertEqual(len(key), 67)

    def test_masked_key_is_preserved(self):
        masked = self.store.masked()
        masked["routes"][0]["upstreams"][0]["base_url"] = "https://changed.example/v1"
        ok, errors, _ = self.store.apply_panel_update(masked)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.routes()[0]["client_key"], self.key)

    def test_masked_upstream_key_is_preserved(self):
        payload = self.store.masked()
        payload["routes"][0]["upstreams"][0]["auth"] = {"type": "bearer", "key": "real-secret"}
        ok, errors, _ = self.store.apply_panel_update(payload)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.routes()[0]["upstreams"][0]["auth"]["key"], "real-secret")

        masked = self.store.masked()
        ok, errors, _ = self.store.apply_panel_update(masked)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.routes()[0]["upstreams"][0]["auth"]["key"], "real-secret")

    def test_new_upstream_key_is_written(self):
        masked = self.store.masked()
        masked["routes"][0]["upstreams"][0]["auth"] = {"type": "bearer", "key": "fresh-key"}
        ok, errors, _ = self.store.apply_panel_update(masked)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.routes()[0]["upstreams"][0]["auth"]["key"], "fresh-key")

    def test_password_hash_never_changes_via_config_save(self):
        before = self.store.password_hash()
        masked = self.store.masked()
        masked["panel_password_hash"] = "hacked"
        ok, _errors, _ = self.store.apply_panel_update(masked)
        self.assertTrue(ok)
        self.assertEqual(self.store.password_hash(), before)

    def test_invalid_payload_returns_errors_and_keeps_config(self):
        before = self.store.routes()[0]["client_key"]
        ok, errors, _ = self.store.apply_panel_update(
            {"routes": [{"client_key": "bad key!", "upstreams": [], "aliases": {},
                         "prefer": {}, "whitelist": [], "blacklist": []}]}
        )
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertEqual(self.store.routes()[0]["client_key"], before)

    def test_non_dict_payload(self):
        ok, errors, _ = self.store.apply_panel_update(["nope"])
        self.assertFalse(ok)
        self.assertTrue(errors)

    def test_ambiguous_alias_produces_warning(self):
        masked = self.store.masked()
        masked["routes"][0]["aliases"] = {"a": "shared", "b": "shared"}
        ok, errors, warnings = self.store.apply_panel_update(masked)
        self.assertTrue(ok, errors)
        self.assertTrue(any("shared" in w for w in warnings))

    def test_listen_change_is_applied(self):
        masked = self.store.masked()
        masked["listen_port"] = 9999
        masked["listen_host"] = "0.0.0.0"
        ok, errors, _ = self.store.apply_panel_update(masked)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.config["listen_port"], 9999)
        self.assertEqual(self.store.config["listen_host"], "0.0.0.0")


class ChangePasswordTest(unittest.TestCase):
    """改密码：旧密码要对，新密码有长度要求。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gateway-test-")
        self.path = os.path.join(self.tmp, "config.json")
        self.store = config_mod.ConfigStore.load(self.path)
        ok, message = self.store.register_password("admin")
        self.assertTrue(ok, message)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_change_password(self):
        ok, message = self.store.change_password("admin", "newpass")
        self.assertTrue(ok, message)
        self.assertTrue(auth.verify_password("newpass", self.store.password_hash()))
        self.assertFalse(auth.verify_password("admin", self.store.password_hash()))
        self.assertTrue(auth.is_registered(self.store.password_hash()))

    def test_wrong_current_password(self):
        ok, message = self.store.change_password("wrong", "newpass")
        self.assertFalse(ok)
        self.assertTrue(auth.verify_password("admin", self.store.password_hash()))

    def test_too_short_new_password(self):
        ok, message = self.store.change_password("admin", "abc")
        self.assertFalse(ok)
        self.assertIn("4", message)

    def test_new_salt_is_generated(self):
        before = self.store.password_hash()
        self.store.change_password("admin", "newpass")
        after = self.store.password_hash()
        self.assertNotEqual(before.split("$")[2], after.split("$")[2])

    def test_password_persists(self):
        self.store.change_password("admin", "newpass")
        again = config_mod.ConfigStore.load(self.path)
        self.assertTrue(auth.verify_password("newpass", again.password_hash()))


class RegisterTest(unittest.TestCase):
    """首次注册：只在未注册时可用，成功后写入哈希。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alias-gateway-test-")
        self.path = os.path.join(self.tmp, "config.json")
        self.store = config_mod.ConfigStore.load(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_store_is_unregistered(self):
        self.assertFalse(auth.is_registered(self.store.password_hash()))

    def test_register_sets_password(self):
        ok, message = self.store.register_password("newpass")
        self.assertTrue(ok, message)
        self.assertTrue(auth.verify_password("newpass", self.store.password_hash()))
        self.assertTrue(auth.is_registered(self.store.password_hash()))

    def test_register_persists(self):
        self.store.register_password("newpass")
        again = config_mod.ConfigStore.load(self.path)
        self.assertTrue(auth.verify_password("newpass", again.password_hash()))

    def test_second_register_is_rejected(self):
        self.store.register_password("newpass")
        ok, message = self.store.register_password("otherpass")
        self.assertFalse(ok)
        self.assertTrue(auth.verify_password("newpass", self.store.password_hash()))

    def test_too_short_password_is_rejected(self):
        ok, message = self.store.register_password("abc")
        self.assertFalse(ok)
        self.assertIn("4", message)
        self.assertFalse(auth.is_registered(self.store.password_hash()))

    def test_empty_password_is_rejected(self):
        ok, _message = self.store.register_password("")
        self.assertFalse(ok)
        self.assertFalse(auth.is_registered(self.store.password_hash()))


if __name__ == "__main__":
    unittest.main()
