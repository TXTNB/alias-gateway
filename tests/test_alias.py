
"""core.alias 的单元测试：别名的双向替换与辅助映射。

覆盖三类容易出错的地方：未配置的名字必须原样透传、多对一时的取值顺序要稳定、
以及改写模型对象时不能污染调用方传进来的原对象。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import alias


class ToUpstreamTest(unittest.TestCase):
    """客户端名 → 上游真名（请求方向）。"""

    def test_maps_known_name(self):
        aliases = {"deepseek-flash": "deepseek-v4.1-flash"}
        self.assertEqual(alias.to_upstream("deepseek-flash", aliases), "deepseek-v4.1-flash")

    def test_passes_through_unknown_name(self):
        aliases = {"deepseek-flash": "deepseek-v4.1-flash"}
        self.assertEqual(alias.to_upstream("gpt-6-astra", aliases), "gpt-6-astra")

    def test_empty_aliases_is_identity(self):
        self.assertEqual(alias.to_upstream("anything", {}), "anything")
        self.assertEqual(alias.to_upstream("anything", None), "anything")

    def test_falsy_name_is_returned_as_is(self):
        self.assertIsNone(alias.to_upstream(None, {"a": "b"}))
        self.assertEqual(alias.to_upstream("", {"a": "b"}), "")


class ToClientTest(unittest.TestCase):
    """上游真名 → 客户端名（响应方向）。"""

    def test_maps_known_name(self):
        aliases = {"deepseek-flash": "deepseek-v4.1-flash"}
        self.assertEqual(alias.to_client("deepseek-v4.1-flash", aliases), "deepseek-flash")

    def test_passes_through_unknown_name(self):
        aliases = {"deepseek-flash": "deepseek-v4.1-flash"}
        self.assertEqual(alias.to_client("glm-5.3", aliases), "glm-5.3")

    def test_many_to_one_picks_first_key(self):
        aliases = {"first": "shared", "second": "shared"}
        self.assertEqual(alias.to_client("shared", aliases), "first")

    def test_empty_aliases_is_identity(self):
        self.assertEqual(alias.to_client("anything", {}), "anything")
        self.assertEqual(alias.to_client("anything", None), "anything")


class RoundTripTest(unittest.TestCase):
    """一来一回要能回到原名字，否则客户端看到的模型名会漂移。"""

    def test_round_trip(self):
        aliases = {
            "deepseek-flash": "deepseek-v4.1-flash",
            "fast": "glm-5.3-air",
        }
        for client_name, upstream_name in aliases.items():
            self.assertEqual(alias.to_upstream(client_name, aliases), upstream_name)
            self.assertEqual(alias.to_client(upstream_name, aliases), client_name)


class ReverseMapTest(unittest.TestCase):
    """反查表：模型清单合并时用它把上游 id 折成客户端名。"""

    def test_builds_upstream_to_client(self):
        aliases = {"a": "x", "b": "y"}
        self.assertEqual(alias.reverse_map(aliases), {"x": "a", "y": "b"})

    def test_empty(self):
        self.assertEqual(alias.reverse_map({}), {})
        self.assertEqual(alias.reverse_map(None), {})

    def test_many_to_one_keeps_first(self):
        self.assertEqual(alias.reverse_map({"a": "x", "b": "x"}), {"x": "a"})


class AmbiguousUpstreamsTest(unittest.TestCase):
    """多对一检测：用于保存配置时给出提醒。"""

    def test_detects_many_to_one(self):
        result = alias.ambiguous_upstreams({"a": "x", "b": "x", "c": "y"})
        self.assertEqual(result, {"x": ["a", "b"]})

    def test_clean_mapping_is_empty(self):
        self.assertEqual(alias.ambiguous_upstreams({"a": "x", "b": "y"}), {})
        self.assertEqual(alias.ambiguous_upstreams({}), {})


class ModelObjectTest(unittest.TestCase):
    """改写模型对象：改名要记录原名，且不能改动入参。"""

    def test_renames_id_and_keeps_original(self):
        model = {"id": "deepseek-v4.1-flash", "object": "model"}
        result = alias.apply_to_model_object(model, {"deepseek-flash": "deepseek-v4.1-flash"})
        self.assertEqual(result["id"], "deepseek-flash")
        self.assertEqual(result["upstream_model"], "deepseek-v4.1-flash")
        self.assertEqual(model["id"], "deepseek-v4.1-flash")
        self.assertNotIn("upstream_model", model)

    def test_unchanged_name_has_no_upstream_model_field(self):
        model = {"id": "glm-5.3"}
        result = alias.apply_to_model_object(model, {"deepseek-flash": "deepseek-v4.1-flash"})
        self.assertEqual(result["id"], "glm-5.3")
        self.assertNotIn("upstream_model", result)

    def test_non_dict_passes_through(self):
        self.assertEqual(alias.apply_to_model_object("x", {}), "x")

    def test_list_helper(self):
        models = [{"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3"}]
        result = alias.apply_to_model_list(models, {"deepseek-flash": "deepseek-v4.1-flash"})
        self.assertEqual([m["id"] for m in result], ["deepseek-flash", "glm-5.3"])

    def test_list_helper_empty(self):
        self.assertEqual(alias.apply_to_model_list([], {"a": "b"}), [])
        self.assertEqual(alias.apply_to_model_list(None, {"a": "b"}), [])


if __name__ == "__main__":
    unittest.main()
