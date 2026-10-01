"""模型名别名的双向映射。

aliases 的结构是 ``{客户端可见名: 上游真实名}``：

* 请求方向 —— :func:`to_upstream` 把客户端名换成上游真名再转发；
* 响应方向 —— :func:`to_client` 把上游真名换回客户端名。

别名允许多对一：几个客户端名可以指向同一个上游模型。响应方向只能挑一个
名字返回，具体选谁由 :func:`ambiguous_upstreams` 报出来提醒用户收敛配置。
"""


def to_upstream(name, aliases):
    """客户端模型名 → 上游真实模型名；没配别名时原样返回。"""
    if not name or not aliases:
        return name
    return aliases.get(name, name)


def to_client(name, aliases):
    """上游真实模型名 → 客户端模型名。

    多个客户端名映射到同一上游名时，按 ``aliases`` 的插入顺序取第一个命中的，
    保证同一份配置每次返回的结果稳定。
    """
    if not name or not aliases:
        return name
    for client_name, upstream_name in aliases.items():
        if upstream_name == name:
            return client_name
    return name


def reverse_map(aliases):
    """``{客户端名: 上游名}`` → ``{上游名: 客户端名}``。

    多对一时后者不会覆盖前者，保留最先出现的那个名字。
    """
    result = {}
    if not aliases:
        return result
    for client_name, upstream_name in aliases.items():
        result.setdefault(upstream_name, client_name)
    return result


def ambiguous_upstreams(aliases):
    """找出被多个客户端名同时指向的上游名，返回 ``{上游名: [客户端名, ...]}``。

    这类配置不算错，但响应方向只能回一个名字，值得在保存时给用户提个醒。
    """
    seen = {}
    for client_name, upstream_name in (aliases or {}).items():
        seen.setdefault(upstream_name, []).append(client_name)
    return {up: names for up, names in seen.items() if len(names) > 1}


def apply_to_model_object(model, aliases):
    """改单个模型对象的 ``id``，并把原名记到 ``upstream_model`` 便于溯源。"""
    if not isinstance(model, dict):
        return model
    item = dict(model)
    raw_id = item.get("id")
    item["id"] = to_client(raw_id, aliases)
    if raw_id and raw_id != item["id"]:
        item["upstream_model"] = raw_id
    return item


def apply_to_model_list(models, aliases):
    """批量改名，输入为空时返回空列表而不是 None。"""
    if not models:
        return []
    return [apply_to_model_object(m, aliases) for m in models]