#!/usr/bin/env python3
"""验收用假上游：模拟一个 OpenAI 兼容服务。

网关要转发请求、改写模型名、切换上游、透传流式响应 —— 这些行为都得有个
真的上游来对打，但又不能依赖外部服务。这个文件就是那个"靶子"。

支持的接口：

    GET  /v1/models             返回固定模型清单（可用 --models 覆盖）
    POST /v1/chat/completions   非流式返回 JSON，流式返回 SSE

两个常用开关：

    --label   标记自己是哪个上游，会写进模型对象的 owned_by，便于区分
    --fail    让本上游直接返回 503，用来测故障切换与降级路径
"""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 默认清单：故意留了 shared-model（用来测跨上游撞名）和
# secret-model（用来测黑名单过滤）
MODELS = ["deepseek-v4.1-flash", "shared-model", "secret-model"]


class Handler(BaseHTTPRequestHandler):
    """一个极简的 OpenAI 兼容 handler。

    只实现验收需要的两个接口，其余路径一律 404 —— 网关不该依赖
    上游的额外行为。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        """屏蔽默认的 stderr 访问日志，验收输出才干净。"""
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            if self.server.fail:
                return self._json(503, {"error": {"message": "upstream down"}})
            data = [
                {"id": m, "object": "model", "owned_by": self.server.label}
                for m in self.server.models
            ]
            return self._json(200, {"object": "list", "data": data})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        model = body.get("model")

        if self.server.fail:
            return self._json(503, {"error": {"message": "upstream down"}})

        if body.get("stream"):
            # 手动写 chunked 编码：网关侧要按行切分再转发，
            # 这里必须吐出标准的 SSE 分块，不能整段返回。
            chunks = [
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": "你"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"content": "好"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for c in chunks:
                line = b"data: " + json.dumps(c).encode("utf-8") + b"\n\n"
                self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                self.wfile.flush()
            end = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(end) + end + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        # 非流式：回一个最小但结构完整的 chat.completion。
        # model 原样回传，这样验收就能检查网关有没有把响应改回客户端名。
        return self._json(200, {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def main():
    """解析参数、把三个开关挂到 server 上，然后一直服务下去。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9911)
    parser.add_argument("--label", default="fake")
    parser.add_argument("--models", default="")
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()

    models = [m for m in args.models.split(",") if m] or list(MODELS)

    # 三个开关挂在 server 对象上，handler 通过 self.server 取用
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.label = args.label
    server.models = models
    server.fail = args.fail
    sys.stderr.write("fake upstream on %d label=%s models=%s fail=%s\n"
                     % (args.port, args.label, models, args.fail))
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()