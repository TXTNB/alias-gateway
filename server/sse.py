"""SSE 流式转发，以及响应方向上的模型名回改。

上游把真实模型名写在 SSE 的 ``data:`` 行里（``{"model": "deepseek-v4.1-flash"}``），
客户端只认自己起的名字，所以每一行都要改写后再发出去。

改写是递归的：模型名可能藏在 ``choices``、``data``、``output`` 等不同层级里，
不同上游的响应结构并不统一，所以按 :data:`_MODEL_CARRIERS` 里的键逐层下钻，
碰到 ``model`` 字段就改。解析失败的行原样透传，不会因为一行脏数据掐断整条流。
"""

import json

from core import alias as alias_mod

DONE_SENTINEL = b"[DONE]"
LINE_SEP = b"\n"

# 模型名可能出现的容器键，覆盖 chat/completions、responses 等常见结构
_MODEL_CARRIERS = ("choices", "data", "response", "message", "delta", "error", "output")


def rewrite_data_line(line, aliases):
    """改一行 SSE ``data:``。

    非 data 行、``[DONE]``、以及解析不出来的内容都原样返回。
    """
    if not line.startswith(b"data:"):
        return line

    payload = line[5:].strip()
    if not payload or payload == DONE_SENTINEL:
        return line

    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return line

    if not _rewrite_model(obj, aliases):
        return line

    try:
        encoded = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return line
    return b"data: " + encoded


def _rewrite_model(obj, aliases):
    """递归找 ``model`` 字段并改回客户端名，返回是否真的改动了。"""
    changed = False
    if isinstance(obj, dict):
        value = obj.get("model")
        if isinstance(value, str):
            new_value = alias_mod.to_client(value, aliases)
            if new_value != value:
                obj["model"] = new_value
                changed = True
        for key in _MODEL_CARRIERS:
            if key in obj and _rewrite_model(obj[key], aliases):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _rewrite_model(item, aliases):
                changed = True
    return changed


def iter_stream(upstream_response, aliases, chunk_size=4096):
    """按行切分上游流并逐行改写，边收边发。

    上游的 chunk 边界不一定落在换行处，所以这里自己维护一个 buffer，
    只有攒够一整行才往外吐。
    """
    buffer = b""
    for chunk in upstream_response.iter_chunks(chunk_size):
        if not chunk:
            continue
        buffer += chunk
        while True:
            index = buffer.find(LINE_SEP)
            if index < 0:
                break
            line = buffer[:index]
            buffer = buffer[index + 1:]
            yield rewrite_data_line(line, aliases) + LINE_SEP

    # 收尾：最后一行可能没有换行符
    if buffer:
        yield rewrite_data_line(buffer, aliases)


def rewrite_json_body(payload, aliases):
    """非流式响应就地改模型名（调用方拿到的是同一个对象）。"""
    if isinstance(payload, dict):
        _rewrite_model(payload, aliases)
    return payload