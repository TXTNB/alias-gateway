
"""core.models 的单元测试：模型清单的合并、去重与冲突判定。

上游请求被 FakeUpstreams 打桩替换，所以这些用例不联网。
最值得关注的是 plan_merge 的判定边界：只有**跨上游**撞名才算冲突，
同一个上游返回重复项、或者两个客户端名映射到不同原始名，都不算。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import models as models_mod


def _upstream(name, url=None):
    return {
        "name": name,
        "base_url": url or ("https://%s.example/v1" % name),
        "auth": {"type": "bearer", "key": "k-" + name},
        "extra_headers": {},
        "timeout": 300,
    }


def _route(**overrides):
    route = {
        "client_key": "sk-testkey1234",
        "upstreams": [_upstream("wb"), _upstream("deepseek")],
        "aliases": {},
        "prefer": {},
        "whitelist": [],
        "blacklist": [],
    }
    route.update(overrides)
    return route


class FakeUpstreams:
    """临时替换 fetch_models 的上下文管理器，让用例完全不碰网络。"""

    def __init__(self, table, errors=None):
        self.table = table
        self.errors = errors or {}

    def __enter__(self):
        self.original = models_mod.upstream_mod.fetch_models
        table = self.table
        errors = self.errors

        def fake(upstream):
            name = upstream.get("name")
            if name in errors:
                return False, [], errors[name]
            return True, list(table.get(name, [])), "OK"

        models_mod.upstream_mod.fetch_models = fake
        return self

    def __exit__(self, *exc):
        models_mod.upstream_mod.fetch_models = self.original
        return False


class CollectModelsTest(unittest.TestCase):
    """拉取清单：单个上游失败不影响其它上游，错误单独收集。"""

    def test_collects_each_upstream(self):
        with FakeUpstreams({"wb": ["a", "b"], "deepseek": ["c"]}):
            per_upstream, errors = models_mod.collect_models(_route())
        self.assertEqual(per_upstream, {"wb": ["a", "b"], "deepseek": ["c"]})
        self.assertEqual(errors, {})

    def test_records_errors_separately(self):
        with FakeUpstreams({"wb": ["a"]}, errors={"deepseek": "connection timeout"}):
            per_upstream, errors = models_mod.collect_models(_route())
        self.assertEqual(per_upstream, {"wb": ["a"]})
        self.assertEqual(errors, {"deepseek": "connection timeout"})


class PlanMergeTest(unittest.TestCase):
    """合并决策：谁能提供哪个可见模型名。"""

    def test_single_owner(self):
        selected, conflicts = models_mod.plan_merge(
            _route(), {"wb": ["alpha"], "deepseek": ["beta"]}
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(selected["alpha"], {"upstream": "wb", "raw": "alpha"})
        self.assertEqual(selected["beta"], {"upstream": "deepseek", "raw": "beta"})

    def test_alias_rename_applied(self):
        route = _route(aliases={"client-name": "raw-name"})
        selected, conflicts = models_mod.plan_merge(route, {"wb": ["raw-name"]})
        self.assertEqual(conflicts, [])
        self.assertEqual(selected["client-name"], {"upstream": "wb", "raw": "raw-name"})

    def test_conflict_without_prefer(self):
        selected, conflicts = models_mod.plan_merge(
            _route(), {"wb": ["same"], "deepseek": ["same"]}
        )
        self.assertNotIn("same", selected)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["model"], "same")

    def test_prefer_resolves(self):
        route = _route(prefer={"same": "deepseek"})
        selected, conflicts = models_mod.plan_merge(route, {"wb": ["same"], "deepseek": ["same"]})
        self.assertEqual(conflicts, [])
        self.assertEqual(selected["same"]["upstream"], "deepseek")

    def test_prefer_pointing_to_unrelated_upstream_is_ignored(self):
        route = _route(prefer={"same": "ghost"})
        _selected, conflicts = models_mod.plan_merge(route, {"wb": ["same"], "deepseek": ["same"]})
        self.assertEqual(len(conflicts), 1)

    def test_duplicate_within_one_upstream_is_not_conflict(self):
        selected, conflicts = models_mod.plan_merge(_route(), {"wb": ["m", "m"]})
        self.assertEqual(conflicts, [])
        self.assertIn("m", selected)

    def test_two_client_names_one_upstream_is_not_conflict(self):
        route = _route(aliases={"c1": "r1", "c2": "r2"})
        selected, conflicts = models_mod.plan_merge(route, {"wb": ["r1", "r2"]})
        self.assertEqual(conflicts, [])
        self.assertEqual(sorted(selected.keys()), ["c1", "c2"])

    def test_empty_input(self):
        selected, conflicts = models_mod.plan_merge(_route(), {})
        self.assertEqual(selected, {})
        self.assertEqual(conflicts, [])


class BuildCatalogTest(unittest.TestCase):
    """组装清单：改名、去重、黑白名单过滤与错误透传。"""

    def test_merges_and_dedupes(self):
        route = _route()
        per_upstream = {"wb": ["alpha", "beta"], "deepseek": ["gamma"]}
        catalog = models_mod.build_catalog(route, per_upstream, {}, fetch=False)
        self.assertEqual(sorted(catalog.ids), ["alpha", "beta", "gamma"])
        self.assertEqual(catalog.sources["gamma"], "deepseek")

    def test_renames_to_client_name(self):
        route = _route(aliases={"deepseek-flash": "deepseek-v4.1-flash"})
        catalog = models_mod.build_catalog(route, {"wb": ["deepseek-v4.1-flash"]}, {}, fetch=False)
        self.assertEqual(catalog.ids, ["deepseek-flash"])
        self.assertEqual(catalog.models[0]["upstream_model"], "deepseek-v4.1-flash")
        self.assertEqual(catalog.raw_names["deepseek-flash"], "deepseek-v4.1-flash")

    def test_dedupes_same_model_from_two_upstreams_when_no_conflict_possible(self):
        route = _route()
        catalog = models_mod.build_catalog(route, {"wb": ["x"], "deepseek": ["y"]}, {}, fetch=False)
        self.assertEqual(len(catalog.ids), len(set(catalog.ids)))

    def test_conflicting_model_is_excluded(self):
        route = _route()
        catalog = models_mod.build_catalog(route, {"wb": ["same"], "deepseek": ["same"]}, {}, fetch=False)
        self.assertEqual(catalog.ids, [])
        self.assertEqual(len(catalog.conflicts), 1)

    def test_prefer_keeps_model(self):
        route = _route(prefer={"same": "wb"})
        catalog = models_mod.build_catalog(route, {"wb": ["same"], "deepseek": ["same"]}, {}, fetch=False)
        self.assertEqual(catalog.ids, ["same"])
        self.assertEqual(catalog.conflicts, [])
        self.assertEqual(catalog.models[0]["upstream"], "wb")

    def test_whitelist_filters(self):
        route = _route(whitelist=["keep-me"])
        catalog = models_mod.build_catalog(
            route, {"wb": ["keep-me", "drop-me"]}, {}, fetch=False
        )
        self.assertEqual(catalog.ids, ["keep-me"])
        self.assertNotIn("drop-me", catalog.sources)

    def test_blacklist_filters(self):
        route = _route(blacklist=["gpt-6-astra"])
        catalog = models_mod.build_catalog(
            route, {"wb": ["gpt-6-astra", "keep-me"]}, {}, fetch=False
        )
        self.assertEqual(catalog.ids, ["keep-me"])

    def test_blacklist_wins_over_whitelist(self):
        route = _route(whitelist=["a", "b"], blacklist=["b"])
        catalog = models_mod.build_catalog(route, {"wb": ["a", "b"]}, {}, fetch=False)
        self.assertEqual(catalog.ids, ["a"])

    def test_whitelist_uses_client_visible_name(self):
        route = _route(aliases={"client-name": "raw-name"}, whitelist=["client-name"])
        catalog = models_mod.build_catalog(route, {"wb": ["raw-name"]}, {}, fetch=False)
        self.assertEqual(catalog.ids, ["client-name"])

    def test_errors_are_carried_through(self):
        route = _route()
        catalog = models_mod.build_catalog(
            route, {"wb": ["a"]}, {"deepseek": "timeout"}, fetch=False
        )
        self.assertEqual(catalog.errors, {"deepseek": "timeout"})

    def test_fetch_false_does_not_touch_network(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("fetch_models 不应被调用")

        original = models_mod.upstream_mod.fetch_models
        models_mod.upstream_mod.fetch_models = boom
        try:
            catalog = models_mod.build_catalog(_route(), fetch=False)
        finally:
            models_mod.upstream_mod.fetch_models = original
        self.assertEqual(catalog.ids, [])

    def test_fetch_true_pulls_models(self):
        with FakeUpstreams({"wb": ["a"], "deepseek": ["b"]}):
            catalog = models_mod.build_catalog(_route())
        self.assertEqual(sorted(catalog.ids), ["a", "b"])

    def test_model_objects_have_openai_shape(self):
        catalog = models_mod.build_catalog(_route(), {"wb": ["m"]}, {}, fetch=False)
        model = catalog.models[0]
        self.assertEqual(model["object"], "model")
        self.assertEqual(model["id"], "m")
        self.assertIn("upstream", model)

    def test_to_payload(self):
        catalog = models_mod.build_catalog(_route(), {"wb": ["m"]}, {}, fetch=False)
        payload = catalog.to_payload()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(len(payload["data"]), 1)

    def test_empty_per_upstream(self):
        catalog = models_mod.build_catalog(_route(), {}, {}, fetch=False)
        self.assertEqual(catalog.ids, [])
        self.assertEqual(catalog.conflicts, [])


class FilterModelsTest(unittest.TestCase):
    """黑白名单过滤：先白后黑，所以黑名单总能压过白名单。"""

    def test_no_filters_keeps_everything(self):
        models = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(models_mod.filter_models(models, [], []), models)

    def test_whitelist_only(self):
        models = [{"id": "a"}, {"id": "b"}]
        self.assertEqual([m["id"] for m in models_mod.filter_models(models, ["b"], [])], ["b"])

    def test_blacklist_only(self):
        models = [{"id": "a"}, {"id": "b"}]
        self.assertEqual([m["id"] for m in models_mod.filter_models(models, [], ["a"])], ["b"])

    def test_empty_input(self):
        self.assertEqual(models_mod.filter_models([], ["a"], []), [])
        self.assertEqual(models_mod.filter_models(None, [], []), [])


class ExposedIdsTest(unittest.TestCase):
    """已算好的 catalog 要复用，不要重新拉一遍清单。"""

    def test_uses_given_catalog(self):
        catalog = models_mod.build_catalog(_route(), {"wb": ["m"]}, {}, fetch=False)
        self.assertEqual(models_mod.exposed_ids(_route(), catalog), {"m"})


class SummarizeTest(unittest.TestCase):
    """日志摘要的格式：各上游数量 + 去重后的可见数量。"""

    def test_format(self):
        route = _route()
        text = models_mod.summarize(route, {"wb": ["a", "b"], "deepseek": ["c"]})
        self.assertIn("wb:2", text)
        self.assertIn("deepseek:1", text)
        self.assertIn("= 3 → 3", text)

    def test_counts_after_rename(self):
        route = _route(aliases={"c1": "r1", "c2": "r2"})
        text = models_mod.summarize(route, {"wb": ["r1", "r2"]})
        self.assertIn("= 2 → 2", text)

    def test_empty(self):
        self.assertEqual(models_mod.summarize(_route(), {}), "- = 0 → 0")


if __name__ == "__main__":
    unittest.main()
