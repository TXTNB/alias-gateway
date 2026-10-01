
"""core.router 的单元测试：客户端 Key 匹配、模型名解析与候选上游排序。

重点覆盖"降级"与"排序"两条规则：

* ``prefer`` 指定的上游要排到最前，但它不拥有该模型时不能硬塞，要回退；
* 清单拉不到时（``require_membership=False``）不做成员校验，按配置顺序返回。

最后一组用例拿 router 的冲突判定与 models.plan_merge 做交叉验证，
确保预热和请求两条路径对同一个模型给出同样的结论。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import router as router_mod
from core.errors import (
    AuthError,
    BadRequestError,
    ModelConflictError,
    UpstreamError,
)


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


class ExtractClientKeyTest(unittest.TestCase):
    """从 Authorization 头里取 Key：Bearer 前缀可有可无。"""

    def test_bearer_header(self):
        self.assertEqual(router_mod.extract_client_key("Bearer sk-abc"), "sk-abc")

    def test_bearer_is_case_insensitive(self):
        self.assertEqual(router_mod.extract_client_key("bearer sk-abc"), "sk-abc")

    def test_bare_key(self):
        self.assertEqual(router_mod.extract_client_key("sk-abc"), "sk-abc")

    def test_extra_spaces_are_trimmed(self):
        self.assertEqual(router_mod.extract_client_key("  Bearer   sk-abc  "), "sk-abc")

    def test_empty_inputs(self):
        for value in (None, "", "   "):
            self.assertIsNone(router_mod.extract_client_key(value))


class MatchRouteTest(unittest.TestCase):
    """客户端 Key → route：必须精确匹配，不允许前缀命中。"""

    def test_matches_exact_key(self):
        routes = [_route(client_key="sk-aaa"), _route(client_key="sk-bbb")]
        self.assertEqual(router_mod.match_route(routes, "sk-bbb")["client_key"], "sk-bbb")

    def test_missing_key_raises_auth_error(self):
        with self.assertRaises(AuthError):
            router_mod.match_route([_route()], "")

    def test_unknown_key_raises_auth_error(self):
        with self.assertRaises(AuthError):
            router_mod.match_route([_route()], "sk-nope")

    def test_no_routes_raises_auth_error(self):
        with self.assertRaises(AuthError):
            router_mod.match_route([], "sk-testkey1234")

    def test_key_is_not_prefix_matched(self):
        with self.assertRaises(AuthError):
            router_mod.match_route([_route()], "sk-testkey123")


class ResolveModelTest(unittest.TestCase):
    """客户端模型名 → 上游真名。"""

    def test_alias_applied(self):
        route = _route(aliases={"deepseek-flash": "deepseek-v4.1-flash"})
        self.assertEqual(
            router_mod.resolve_model(route, "deepseek-flash"), "deepseek-v4.1-flash"
        )

    def test_unknown_name_passes_through(self):
        self.assertEqual(router_mod.resolve_model(_route(), "gpt-6"), "gpt-6")


class CandidateUpstreamsTest(unittest.TestCase):
    """候选上游的排序与过滤，这是故障切换顺序的来源。"""

    def test_declared_order_when_no_prefer(self):
        route = _route()
        model_map = {"wb": ["m"], "deepseek": ["m"]}
        picked = router_mod.candidate_upstreams(route, "m", model_map)
        self.assertEqual([u["name"] for u in picked], ["wb", "deepseek"])

    def test_prefer_moves_upstream_to_front(self):
        route = _route(prefer={"m": "deepseek"})
        model_map = {"wb": ["m"], "deepseek": ["m"]}
        picked = router_mod.candidate_upstreams(route, "m", model_map)
        self.assertEqual([u["name"] for u in picked], ["deepseek", "wb"])

    def test_prefer_key_is_upstream_raw_name(self):
        route = _route(aliases={"client-m": "raw-m"}, prefer={"raw-m": "deepseek"})
        model_map = {"wb": ["raw-m"], "deepseek": ["raw-m"]}
        picked = router_mod.candidate_upstreams(route, "client-m", model_map)
        self.assertEqual([u["name"] for u in picked], ["deepseek", "wb"])

    def test_membership_filter_keeps_only_owners(self):
        route = _route()
        model_map = {"wb": ["m"], "deepseek": ["other"]}
        picked = router_mod.candidate_upstreams(route, "m", model_map)
        self.assertEqual([u["name"] for u in picked], ["wb"])

    def test_model_absent_everywhere_raises_bad_request(self):
        route = _route()
        with self.assertRaises(BadRequestError):
            router_mod.candidate_upstreams(route, "ghost", {"wb": ["m"]})

    def test_upstream_without_model_list_is_skipped(self):
        route = _route()
        picked = router_mod.candidate_upstreams(route, "m", {"wb": ["m"]})
        self.assertEqual([u["name"] for u in picked], ["wb"])

    def test_require_membership_false_ignores_model_map(self):
        route = _route()
        picked = router_mod.candidate_upstreams(route, "whatever", None, False)
        self.assertEqual([u["name"] for u in picked], ["wb", "deepseek"])

    def test_no_upstreams_raises_upstream_error(self):
        route = _route(upstreams=[])
        with self.assertRaises(UpstreamError):
            router_mod.candidate_upstreams(route, "m", {})

    def test_prefer_to_missing_upstream_raises(self):
        route = _route(prefer={"m": "ghost"})
        with self.assertRaises(UpstreamError):
            router_mod.candidate_upstreams(route, "m", {"wb": ["m"], "deepseek": ["m"]})

    def test_prefer_upstream_not_owning_model_falls_back(self):
        route = _route(prefer={"m": "deepseek"})
        picked = router_mod.candidate_upstreams(route, "m", {"wb": ["m"], "deepseek": ["x"]})
        self.assertEqual([u["name"] for u in picked], ["wb"])


class PickUpstreamTest(unittest.TestCase):
    """只取首选上游的便捷函数。"""

    def test_returns_first_candidate(self):
        route = _route(prefer={"m": "deepseek"})
        model_map = {"wb": ["m"], "deepseek": ["m"]}
        picked = router_mod.pick_upstream(route, "m", model_map)
        self.assertEqual(picked["name"], "deepseek")


class PlanTest(unittest.TestCase):
    """plan() 一次返回真名 + 候选列表，供转发路径使用。"""

    def test_returns_raw_name_and_candidates(self):
        route = _route(aliases={"client-m": "raw-m"})
        model_map = {"wb": ["raw-m"], "deepseek": ["raw-m"]}
        raw, candidates = router_mod.plan(route, "client-m", model_map)
        self.assertEqual(raw, "raw-m")
        self.assertEqual([u["name"] for u in candidates], ["wb", "deepseek"])


class DetectRequestConflictTest(unittest.TestCase):
    """请求时的冲突检测：只有真撞名且没配 prefer 才拦。"""

    def test_single_owner_no_conflict(self):
        route = _route()
        router_mod.detect_request_conflict(route, "m", {"wb": ["m"], "deepseek": ["x"]})

    def test_two_owners_without_prefer_raises(self):
        route = _route()
        with self.assertRaises(ModelConflictError) as ctx:
            router_mod.detect_request_conflict(route, "m", {"wb": ["m"], "deepseek": ["m"]})
        self.assertIn("m", str(ctx.exception))
        self.assertIn('"prefer"', str(ctx.exception))

    def test_prefer_resolves_conflict(self):
        route = _route(prefer={"m": "deepseek"})
        router_mod.detect_request_conflict(route, "m", {"wb": ["m"], "deepseek": ["m"]})

    def test_conflict_of_another_model_does_not_block(self):
        route = _route()
        router_mod.detect_request_conflict(
            route, "safe", {"wb": ["safe", "dup"], "deepseek": ["dup"]}
        )

    def test_conflict_detected_after_rename(self):
        route = _route(aliases={"client-m": "raw-a"})
        with self.assertRaises(ModelConflictError):
            router_mod.detect_request_conflict(
                route, "client-m", {"wb": ["raw-a"], "deepseek": ["client-m"]}
            )

    def test_empty_model_map_is_noop(self):
        router_mod.detect_request_conflict(_route(), "m", {})
        router_mod.detect_request_conflict(_route(), "m", None)

    def test_duplicate_within_one_upstream_is_not_conflict(self):
        route = _route()
        router_mod.detect_request_conflict(route, "m", {"wb": ["m", "m"]})


class ConflictConsistencyTest(unittest.TestCase):
    """交叉验证：请求时判定与预热判定必须给出同样的结论。"""

    def test_matches_models_plan_merge(self):
        from core import models as models_mod

        cases = [
            (_route(), {"wb": ["a", "dup"], "deepseek": ["dup", "b"]}),
            (_route(prefer={"dup": "wb"}), {"wb": ["dup"], "deepseek": ["dup"]}),
            (
                _route(aliases={"client-dup": "raw-dup"}),
                {"wb": ["raw-dup"], "deepseek": ["client-dup"]},
            ),
            (_route(), {"wb": ["only"]}),
            (_route(), {}),
        ]
        for route, model_map in cases:
            with self.subTest(model_map=model_map, prefer=route.get("prefer")):
                _selected, conflicts = models_mod.plan_merge(route, model_map)
                conflict_models = {c.get("model") for c in conflicts}
                names = {r for ids in model_map.values() for r in ids}
                for raw in sorted(names):
                    resolved = router_mod.resolve_model(route, raw)
                    try:
                        router_mod.detect_request_conflict(route, raw, model_map)
                        blocked = False
                    except ModelConflictError:
                        blocked = True
                    self.assertEqual(
                        blocked,
                        resolved in conflict_models,
                        "route=%s raw=%s resolved=%s conflicts=%s"
                        % (route.get("client_key"), raw, resolved, conflict_models),
                    )


if __name__ == "__main__":
    unittest.main()
