"""模型清单的拉取、改名、合并与去重。

核心是 :func:`plan_merge`：把每个上游返回的原始模型名按 ``aliases`` 折算成
客户端可见名，同名归到一个桶里。一个桶里出现多个不同上游就说明撞名了，
这时看 ``prefer`` 有没有指定归属，指定了就用它，没指定就记成冲突上报。

冲突检测只有这一处实现，启动预热（:func:`core.config.detect_conflicts`）和
请求时校验（:func:`core.router.detect_request_conflict`）都复用它，
保证两条路径判定一致。
"""

from . import alias as alias_mod
from . import upstream as upstream_mod
from .errors import format_conflict_message

SOURCE_UNKNOWN = "unknown"


class ModelCatalog:
    """一次合并的结果：可见模型列表 + 来源 + 原名 + 冲突 + 拉取失败信息。"""

    def __init__(self, models, sources, raw_names, conflicts, errors):
        self.models = models
        self.sources = sources
        self.raw_names = raw_names
        self.conflicts = conflicts
        self.errors = errors

    @property
    def ids(self):
        return [m.get("id") for m in self.models]

    def to_payload(self):
        return {
            "object": "list",
            "data": self.models,
            "sources": self.sources,
        }


def collect_models(route):
    """并发拉取一个 route 下所有上游的模型清单。

    返回 ``(per_upstream, errors)``：前者是 ``{上游名: [模型 id, ...]}``，
    后者收集拉取失败的上游及原因 —— 单个上游挂掉不影响其它上游。
    """
    per_upstream = {}
    errors = {}
    for up in route.get("upstreams") or []:
        name = up.get("name") or SOURCE_UNKNOWN
        ok, ids, message = upstream_mod.fetch_models(up)
        if ok:
            per_upstream[name] = ids
        else:
            errors[name] = message
    return per_upstream, errors


def plan_merge(route, per_upstream):
    """决定每个可见模型名最终由哪个上游提供。

    返回 ``(selected, conflicts)``：

    * ``selected`` —— ``{可见名: {"upstream": 上游名, "raw": 原始名}}``；
    * ``conflicts`` —— 撞名且没配 ``prefer`` 的条目，启动预热会据此退出。

    只有一个来源（或所有来源都是同一个上游）时不算冲突，
    直接采用；真撞名了才去查 ``prefer``。
    """
    aliases = route.get("aliases") or {}
    prefer = route.get("prefer") or {}
    reverse = alias_mod.reverse_map(aliases)
    url_by_name = {u.get("name"): u.get("base_url", "") for u in route.get("upstreams") or []}

    buckets = {}
    order = []
    for up_name, raw_ids in (per_upstream or {}).items():
        for raw in raw_ids or []:
            client_name = reverse.get(raw, raw)
            bucket = buckets.get(client_name)
            if bucket is None:
                bucket = []
                buckets[client_name] = bucket
                order.append(client_name)
            bucket.append({"name": up_name, "base_url": url_by_name.get(up_name, ""), "raw": raw})

    selected = {}
    conflicts = []

    for client_name in order:
        owners = buckets[client_name]
        distinct = {o["name"] for o in owners}

        if len(owners) < 2 or len(distinct) < 2:
            chosen = owners[0]
        else:
            chosen = None
            for owner in owners:
                preferred = prefer.get(owner["raw"])
                if preferred and preferred in distinct:
                    chosen = next(o for o in owners if o["name"] == preferred)
                    break
            if chosen is None:
                conflicts.append(
                    {
                        "route": route.get("client_key"),
                        "model": owners[0]["raw"],
                        "client_name": None if client_name == owners[0]["raw"] else client_name,
                        "found_in": [
                            {"name": o["name"], "base_url": o["base_url"], "raw": o["raw"]}
                            for o in owners
                        ],
                    }
                )
                continue

        selected[client_name] = {"upstream": chosen["name"], "raw": chosen["raw"]}

    return selected, conflicts


def build_catalog(route, per_upstream=None, errors=None, fetch=True):
    """组装最终的模型清单。

    ``fetch=False`` 表示清单已经在别处拉好了，直接复用，不再发请求
    （请求转发路径上就是这么用的，避免每个请求都打一遍上游）。
    """
    if per_upstream is None:
        if fetch:
            per_upstream, fetched_errors = collect_models(route)
            errors = dict(fetched_errors)
        else:
            per_upstream, errors = {}, {}
    errors = dict(errors or {})

    selected, conflicts = plan_merge(route, per_upstream)

    models = []
    sources = {}
    raw_names = {}

    for client_name in sorted(selected.keys()):
        info = selected[client_name]
        model = {"id": client_name, "object": "model", "owned_by": "gateway"}
        if client_name != info["raw"]:
            model["upstream_model"] = info["raw"]
        model["upstream"] = info["upstream"]
        models.append(model)
        sources[client_name] = info["upstream"]
        raw_names[client_name] = info["raw"]

    models = filter_models(models, route.get("whitelist"), route.get("blacklist"))
    kept = {m.get("id") for m in models}
    sources = {k: v for k, v in sources.items() if k in kept}
    raw_names = {k: v for k, v in raw_names.items() if k in kept}

    return ModelCatalog(models, sources, raw_names, conflicts, errors)


def filter_models(models, whitelist, blacklist):
    """先应用白名单再应用黑名单；两者都为空时原样返回。"""
    result = list(models or [])
    if whitelist:
        allowed = set(whitelist)
        result = [m for m in result if m.get("id") in allowed]
    if blacklist:
        denied = set(blacklist)
        result = [m for m in result if m.get("id") not in denied]
    return result


def exposed_ids(route, catalog=None):
    """这条 route 对外暴露的模型 id 集合，已复用的 catalog 不会重复拉取。"""
    if catalog is not None:
        return set(catalog.ids)
    return set(build_catalog(route).ids)


def conflict_message(conflicts, route_key):
    """冲突的一行描述（请求时用）。"""
    return format_conflict_message(conflicts, route_key)


def summarize(route, per_upstream):
    """给日志用的一句话摘要，形如 ``a:3 + b:2 = 5 → 4``。

    左边是各上游的原始数量，右边是去重改名后的可见数量。
    """
    parts = []
    total = 0
    for name, ids in (per_upstream or {}).items():
        count = len(ids or [])
        total += count
        parts.append("%s:%d" % (name, count))

    aliases = route.get("aliases") or {}
    unique = len(
        {
            alias_mod.to_client(i, aliases)
            for ids in (per_upstream or {}).values()
            for i in (ids or [])
        }
    )
    left = " + ".join(parts) if parts else "-"
    return "%s = %d → %d" % (left, total, unique)