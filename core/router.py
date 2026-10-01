"""客户端 Key → route → 候选上游的选择逻辑。

一次请求要经过三步：

1. :func:`match_route` —— 拿客户端 Key 找到对应的 route，找不到就 401；
2. :func:`candidate_upstreams` —— 按 ``prefer`` 优先、配置顺序兜底排出候选列表；
3. 逐个尝试，失败就换下一个。

第 2 步的 ``require_membership`` 是"降级开关"：模型清单拉不到时（比如上游刚挂），
网关不做成员校验、按配置顺序直接转发，免得整条链路因为清单拿不到就不可用。
"""

from . import alias as alias_mod
from .errors import AuthError, BadRequestError, ModelConflictError, UpstreamError, format_conflict_message


def extract_client_key(authorization):
    """从 ``Authorization`` 头里取客户端 Key。

    既接受标准的 ``Bearer <key>``，也接受直接把 Key 当整头发过来的写法。
    """
    if not authorization:
        return None
    text = str(authorization).strip()
    if not text:
        return None
    parts = text.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return text


def match_route(routes, client_key):
    """按客户端 Key 精确匹配 route，匹配不到就抛 :class:`AuthError`。"""
    if not client_key:
        raise AuthError("Missing client key. Send it as 'Authorization: Bearer <client_key>'.")
    for route in routes or []:
        if route.get("client_key") == client_key:
            return route
    raise AuthError("Invalid client key.")


def resolve_model(route, client_model):
    """把客户端模型名换成上游真实名。"""
    return alias_mod.to_upstream(client_model, route.get("aliases") or {})


def candidate_upstreams(route, client_model, model_map=None, require_membership=True):
    """排出这次请求可以尝试的上游顺序。

    ``prefer`` 指定的上游排在最前，其余按配置顺序跟在后面；
    ``model_map`` 有值时再过滤掉清单里没有该模型的上游，全部落空就 400。
    """
    upstreams = route.get("upstreams") or []
    if not upstreams:
        raise UpstreamError("route '%s' 没有配置任何上游" % route.get("client_key"))

    by_name = {u.get("name"): u for u in upstreams}
    raw_model = resolve_model(route, client_model)
    prefer = route.get("prefer") or {}

    ordered = []

    preferred_name = prefer.get(raw_model)
    if preferred_name:
        owner = by_name.get(preferred_name)
        if owner is None:
            raise UpstreamError(
                "route '%s' 的 prefer['%s'] 指向不存在的上游 '%s'"
                % (route.get("client_key"), raw_model, preferred_name)
            )
        ordered.append(owner)

    for up in upstreams:
        if up.get("name") == preferred_name:
            continue
        ordered.append(up)

    if model_map is None or not require_membership:
        return ordered

    matched = []
    for up in ordered:
        ids = model_map.get(up.get("name"))
        if ids is None:
            continue
        if raw_model in ids:
            matched.append(up)

    if not matched:
        raise BadRequestError(
            "The model '%s' does not exist or is not available on route '%s'."
            % (client_model, route.get("client_key"))
        )
    return matched


def detect_request_conflict(route, client_model, model_map):
    """请求时再查一次同名冲突 —— 预热之后配置可能被改过。"""
    if not model_map:
        return
    raw_model = resolve_model(route, client_model)

    from . import models as models_mod

    _selected, conflicts = models_mod.plan_merge(route, model_map)
    if not conflicts:
        return

    for conflict in conflicts:
        if conflict.get("model") == raw_model:
            raise ModelConflictError(
                format_conflict_message([conflict], route.get("client_key"))
            )


def pick_upstream(route, client_model, model_map=None, require_membership=True):
    """只取首选上游（调试/测试用）。"""
    return candidate_upstreams(route, client_model, model_map, require_membership)[0]


def plan(route, client_model, model_map=None, require_membership=True):
    """返回 ``(上游真实模型名, 候选上游列表)``，一次拿齐转发所需信息。"""
    candidates = candidate_upstreams(route, client_model, model_map, require_membership)
    return resolve_model(route, client_model), candidates