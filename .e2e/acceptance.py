#!/usr/bin/env python3
"""端到端验收：真起假上游 + 真起网关，逐条核对验收标准。

不依赖任何第三方库。用 python3 .e2e/acceptance.py 运行。

所有服务都跑在临时目录里（配置、日志、进程输出都隔离），
跑完自动清理，不会碰项目根目录下的 config.json 与 gateway.log。
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import auth as auth_mod

PY = sys.executable


def free_port(start):
    """从 ``start`` 起找一个没人监听的端口。

    端口写死会和本机正在跑的 dev.sh / 网关撞车（"Address already in use"），
    验收不该因为别人开着服务就跑不起来。
    """
    for port in range(start, start + 200):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            probe.close()
            continue
        probe.close()
        return port
    raise RuntimeError("找不到空闲端口（从 %d 起试了 200 个）" % start)


FAKE_A_PORT = free_port(9911)
FAKE_B_PORT = free_port(FAKE_A_PORT + 1)
GW_PORT = free_port(8791)
BASE = "http://127.0.0.1:%d" % GW_PORT

RESULTS = []


def check(name, ok, detail=""):
    """记一条结果并立刻打印，失败项最后汇总。"""
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  <- " + detail) if detail else ""))


def http(method, path, body=None, headers=None, timeout=30, raw=False):
    """发一个请求，返回 ``(状态码, 响应体, 响应头)``。

    4xx/5xx 不当异常 —— 验收本来就要断言"错误 Key 返回 401"这类行为，
    所以状态码原样返回给调用方判断。``raw=True`` 时响应体保持 bytes
    （流式 SSE 需要看原始分块）。
    """
    url = path if path.startswith("http") else BASE + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else _maybe_json(payload)), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, (payload if raw else _maybe_json(payload)), dict(exc.headers)


def _maybe_json(payload):
    """能解析成 JSON 就解析，否则退回文本，避免一处非 JSON 响应炸掉整轮验收。"""
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return payload.decode("utf-8", "replace")


def wait_port(port, timeout=20.0):
    """轮询 ``/health`` 直到服务就绪。

    连上就算就绪（HTTPError 也算），因为只要能收到响应就说明端口已经在听。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.15)
    return False


def start(cmd, log_path):
    """后台起一个进程，输出重定向到日志文件。

    日志句柄挂在 proc 上，:func:`stop` 负责关闭，避免文件描述符泄漏。
    """
    log = open(log_path, "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    proc._log = log
    return proc


def stop(proc):
    """先 SIGTERM 优雅退出，超时再 SIGKILL。

    验收里会反复起停服务，端口释放得干净很重要 —— 所以这里必须等进程真的
    退出，不能只发信号就走。
    """
    if proc is None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc._log.close()
    except Exception:
        pass


def write_config(path, routes, password="admin", port=GW_PORT):
    """写一份验收用配置。

    密码在写入时哈希，所以每次生成的配置内容都不同 —— 这不影响断言，
    因为用例从不比对配置文件原文。
    """
    cfg = {
        "listen_port": port,
        "listen_host": "127.0.0.1",
        "panel_password_hash": auth_mod.hash_password(password),
        "routes": routes,
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    return cfg


def upstream(name, port, key="k"):
    """一条指向本机假上游的上游配置。"""
    return {
        "name": name,
        "base_url": "http://127.0.0.1:%d/v1" % port,
        "auth": {"type": "bearer", "key": key},
        "extra_headers": {},
        "timeout": 30,
    }


def main():
    """跑完整轮验收：起服务 -> 逐条断言 -> 收尾清理。

    所有临时文件都在 mkdtemp 出来的目录里，finally 里删掉，
    不碰项目根目录的任何产物。
    """
    work = tempfile.mkdtemp(prefix="alias-gw-e2e-")
    fake_a = fake_b = gw = None
    try:
        fake_a = start([PY, os.path.join(ROOT, ".e2e", "fake_upstream.py"),
                        "--port", str(FAKE_A_PORT), "--label", "a"],
                       os.path.join(work, "fake-a.log"))
        fake_b = start([PY, os.path.join(ROOT, ".e2e", "fake_upstream.py"),
                        "--port", str(FAKE_B_PORT), "--label", "b",
                        "--models", "deepseek-v4.1-pro,shared-model"],
                       os.path.join(work, "fake-b.log"))
        if not (wait_port(FAKE_A_PORT) and wait_port(FAKE_B_PORT)):
            print("假上游启动失败")
            return 1
        check("假上游就绪", True)

        cfg1 = os.path.join(work, "config1.json")
        write_config(cfg1, [{
            "client_key": "sk-e2etestkey0001",
            "upstreams": [upstream("a", FAKE_A_PORT), upstream("b", FAKE_B_PORT)],
            "aliases": {"deepseek-flash": "deepseek-v4.1-flash"},
            "prefer": {"shared-model": "a"},
            "whitelist": [],
            "blacklist": ["secret-model"],
        }])

        gw = start([PY, "app.py", "--config", cfg1,
                    "--log", os.path.join(work, "gw1.log")],
                   os.path.join(work, "gw1.out"))
        if not wait_port(GW_PORT):
            print(open(os.path.join(work, "gw1.out")).read()[-3000:])
            return 1

        check("1 网关启动并监听", True)

        with open(cfg1, "r", encoding="utf-8") as fp:
            stored = json.load(fp)["panel_password_hash"]
        check("2a 密码以哈希存储（非明文）",
              stored != "admin" and stored.startswith("pbkdf2_sha256$") and "admin" not in stored,
              stored[:32] + "...")
        st, body, _ = http("POST", "/panel/login", {"password": "admin"})
        check("2b 已注册的密码可登录管理端", st == 200 and body.get("ok") and body.get("token"),
              "status=%s" % st)
        token = body.get("token") if isinstance(body, dict) else None

        st, body, _ = http("POST", "/panel/register", {"password": "other"})
        check("2c 已注册后拒绝重复注册", st == 400 and not body.get("ok"), "status=%s" % st)

        st, body, _ = http("GET", "/v1/models",
                           headers={"Authorization": "Bearer sk-e2etestkey0001"})
        ids = sorted([m["id"] for m in body.get("data", [])]) if isinstance(body, dict) else []
        check("3a /v1/models 合并两个上游", st == 200 and len(ids) == 3, "ids=%s" % ids)
        check("3b aliases 改名生效（deepseek-flash）", "deepseek-flash" in ids)
        check("3c 未配置的名字原样透传（deepseek-v4.1-pro）", "deepseek-v4.1-pro" in ids)
        check("3d prefer 让 shared-model 保留且归属 a",
              any(m["id"] == "shared-model" and m.get("upstream") == "a"
                  for m in body.get("data", [])),
              str([(m["id"], m.get("upstream")) for m in body.get("data", [])]))

        check("9 blacklist 排除 secret-model", "secret-model" not in ids)

        st, body, _ = http("POST", "/v1/chat/completions",
                           {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}]},
                           {"Authorization": "Bearer sk-e2etestkey0001"})
        check("4a 请求方向改名 + 正常返回", st == 200 and body.get("choices"),
              "status=%s" % st)
        check("4b 响应方向改回客户端名",
              isinstance(body, dict) and body.get("model") == "deepseek-flash",
              "model=%s" % (body.get("model") if isinstance(body, dict) else body))

        st, text, headers = http("POST", "/v1/chat/completions",
                                 {"model": "deepseek-flash", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]},
                                 {"Authorization": "Bearer sk-e2etestkey0001"}, raw=True)
        stream_text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
        check("10a 流式返回 SSE",
              st == 200 and "data:" in stream_text and "[DONE]" in stream_text,
              "status=%s len=%d" % (st, len(stream_text)))
        check("10b 流式内模型名已改回客户端名",
              "deepseek-flash" in stream_text and "deepseek-v4.1-flash" not in stream_text)

        st, body, _ = http("GET", "/v1/models", headers={"Authorization": "Bearer sk-wrong"})
        check("11 错误客户端 Key 返回 401", st == 401, "status=%s" % st)

        st, body, _ = http("POST", "/v1/chat/completions",
                           {"model": "secret-model", "messages": []},
                           {"Authorization": "Bearer sk-e2etestkey0001"})
        check("12 请求被排除的模型返回 400", st == 400, "status=%s" % st)

        stop(fake_a)
        fake_a = None
        time.sleep(0.4)
        st, body, _ = http("POST", "/v1/chat/completions",
                           {"model": "shared-model", "messages": [{"role": "user", "content": "x"}]},
                           {"Authorization": "Bearer sk-e2etestkey0001"})
        check("5 首选上游不可用时自动切到备用上游",
              st == 200 and isinstance(body, dict) and body.get("choices"), "status=%s" % st)

        fake_a = start([PY, os.path.join(ROOT, ".e2e", "fake_upstream.py"),
                        "--port", str(FAKE_A_PORT), "--label", "a"],
                       os.path.join(work, "fake-a2.log"))
        wait_port(FAKE_A_PORT)

        if token:
            st, masked, _ = http("GET", "/panel/config", headers={"X-Panel-Token": token})
            ok = st == 200 and isinstance(masked, dict)
            check("管理端可读取配置（打码）", ok, "status=%s" % st)
            if ok:
                payload = dict(masked)
                payload["routes"][0]["whitelist"] = ["deepseek-flash"]
                st, body, _ = http("POST", "/panel/config", payload,
                                   {"X-Panel-Token": token})
                check("管理端保存白名单成功", st == 200 and body.get("ok"),
                      "status=%s body=%s" % (st, body))
                st, body, _ = http("GET", "/v1/models",
                                   headers={"Authorization": "Bearer sk-e2etestkey0001"})
                ids2 = [m["id"] for m in body.get("data", [])]
                check("8 whitelist 只保留列出的模型", ids2 == ["deepseek-flash"], "ids=%s" % ids2)
                payload["routes"][0]["whitelist"] = []
                http("POST", "/panel/config", payload, {"X-Panel-Token": token})
        else:
            check("管理端可读取配置（打码）", False, "没有 token")
            check("管理端保存白名单成功", False, "没有 token")
            check("8 whitelist 只保留列出的模型", False, "没有 token")

        cfg2 = os.path.join(work, "config2.json")
        write_config(cfg2, [{
            "client_key": "sk-conflict00000001",
            "upstreams": [upstream("a", FAKE_A_PORT), upstream("b", FAKE_B_PORT)],
            "aliases": {},
            "prefer": {},
            "whitelist": [],
            "blacklist": [],
        }], port=GW_PORT)
        out = subprocess.run([PY, "app.py", "--config", cfg2,
                              "--log", os.path.join(work, "gw2.log")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        report = (out.stderr or "") + (out.stdout or "")
        check("6a 冲突时预热退出码为 3", out.returncode == 3,
              "rc=%s" % out.returncode)
        check("6b 冲突报告列出模型与修法",
              "shared-model" in report and "prefer" in report,
              report[-400:].replace("\n", " / "))

        cfg3 = os.path.join(work, "config3.json")
        write_config(cfg3, [{
            "client_key": "sk-resolved0000001",
            "upstreams": [upstream("a", FAKE_A_PORT), upstream("b", FAKE_B_PORT)],
            "aliases": {},
            "prefer": {"shared-model": "b"},
            "whitelist": [],
            "blacklist": [],
        }], port=GW_PORT + 1)
        gw3 = start([PY, "app.py", "--config", cfg3,
                     "--log", os.path.join(work, "gw3.log")],
                    os.path.join(work, "gw3.out"))
        ok3 = wait_port(GW_PORT + 1)
        check("7a prefer 解决冲突后正常启动", ok3)
        if ok3:
            st, body, _ = http("POST", "http://127.0.0.1:%d/v1/chat/completions" % (GW_PORT + 1),
                               {"model": "shared-model", "messages": []},
                               {"Authorization": "Bearer sk-resolved0000001"})
            check("7b prefer 指定上游被采用", st == 200, "status=%s" % st)
        stop(gw3)

        modules = ["core/alias.py", "core/auth.py", "core/config.py", "core/errors.py",
                   "core/logs.py", "core/models.py", "core/router.py", "core/upstream.py",
                   "server/handlers.py", "server/http.py", "server/sse.py", "app.py",
                   "tui.py"]
        missing = [m for m in modules if not os.path.isfile(os.path.join(ROOT, m))]
        biggest = max(os.path.getsize(os.path.join(ROOT, m)) for m in modules)
        check("14 代码按模块拆分（13 个模块齐全，单文件 < 100KB）",
              not missing and biggest < 100 * 1024,
              "missing=%s biggest=%d" % (missing, biggest))

        import importlib

        tui_mod = importlib.import_module("tui")
        check("13a 终端管理界面 tui.py 存在且可导入",
              os.path.isfile(os.path.join(ROOT, "tui.py")) and callable(tui_mod.main))

        tui_client = tui_mod.PanelClient(BASE)
        tui_health = tui_client.health()
        tui_login = tui_client.login("admin")
        tui_ov = tui_client.overview()
        tui_cfg = tui_client.config_masked()
        tui_logs = tui_client.logs(limit=5)
        check("13b TUI 客户端可登录并读取总览/配置/日志",
              bool(tui_login.get("token")) and tui_ov.get("ok")
              and isinstance(tui_cfg, dict) and isinstance(tui_logs.get("logs"), list),
              "health=%s routes=%s logs=%s" % (tui_health.get("service"),
                                               tui_ov.get("routes"), tui_logs.get("total")))

        masked_dump = json.dumps(tui_cfg, ensure_ascii=False)
        check("13c TUI 读到的配置：上游 Key 已打码、客户端 Key 明文",
              "••••••••" in masked_dump and "sk-e2etestkey0001" in masked_dump
              and '"client_key_masked"' not in masked_dump)

        tui_test = tui_client.test_upstreams()
        check("13d TUI 可触发上游连通性测试",
              tui_test.get("ok") and isinstance(tui_test.get("results"), list)
              and len(tui_test.get("results")) == 2
              and "••••••••" not in json.dumps(tui_test, ensure_ascii=False),
              "results=%d" % len(tui_test.get("results") or []))

        tui_client.logout()
        st, root_body, _ = http("GET", "/")
        check("13e 根路径不再返回 HTML，改为服务信息 JSON",
              st == 200 and isinstance(root_body, dict) and root_body.get("service"),
              "status=%s" % st)

        check("13f Web 静态资源目录已移除",
              not os.path.isdir(os.path.join(ROOT, "web")))

        gen_cfg = os.path.join(work, "regen-config.test.json")
        regen = subprocess.run(
            [PY, os.path.join(ROOT, ".e2e", "make_test_config.py"), gen_cfg],
            cwd=ROOT, capture_output=True)
        regen_ok = False
        if regen.returncode == 0 and os.path.isfile(gen_cfg):
            with open(gen_cfg, "r", encoding="utf-8") as fp:
                regen_data = json.load(fp)
            regen_ok = (regen_data.get("listen_port") == 8789
                        and regen_data["routes"][0]["client_key"] == "sk-local-test-0001"
                        and regen_data["routes"][0]["upstreams"][0]["base_url"]
                        == "http://127.0.0.1:9911/v1")
        check("13f2 联调模板可重新生成（dev.sh 缺模板时自愈）",
              regen_ok, "rc=%s" % regen.returncode)

        # 13f3) 联调模板本身是一条路由挂两个上游，冒烟才能覆盖多上游路径
        # 夹具缺失时先按 dev.sh 的自愈逻辑补回来，避免因外部误删导致整轮验收中断
        test_cfg_path = os.path.join(ROOT, ".e2e", "config.test.json")
        if not os.path.isfile(test_cfg_path):
            subprocess.run([PY, os.path.join(ROOT, ".e2e", "make_test_config.py"),
                            test_cfg_path], cwd=ROOT, capture_output=True)
        with open(test_cfg_path, "r", encoding="utf-8") as fp:
            test_cfg = json.load(fp)
        test_ups = (test_cfg.get("routes") or [{}])[0].get("upstreams") or []
        check("13f3 联调模板是一条路由挂两个上游",
              len(test_ups) == 2 and [u.get("name") for u in test_ups] == ["fake", "backup"],
              "upstreams=%s" % [u.get("name") for u in test_ups])

        check("13g TUI 有清屏重绘（真终端下覆盖上次输出）",
              callable(getattr(tui_mod, "_clear", None)))
        check("13h TUI 新增与修改共用同一套路由表单",
              callable(getattr(tui_mod, "_route_form", None))
              and "action_route_add" not in dir(tui_mod)
              and tui_mod.ACTIONS.get("3") is tui_mod.action_config_edit)

        # 13h2) 一条路由可以挂多个上游：表单里有专门的上游列表循环
        check("13h2 TUI 支持一条路由挂多个上游",
              callable(getattr(tui_mod, "_upstreams_loop", None))
              and callable(getattr(tui_mod, "_upstream_form", None))
              and callable(getattr(tui_mod, "_conflict_hint", None)))

        # 13h5) 表单分两段，上游那段回车只结束本段，不会让人以为整个表单完了
        import inspect
        loop_src = inspect.getsource(tui_mod._upstreams_loop)
        route_src = inspect.getsource(tui_mod._route_form)
        check("13h5 路由表单分两段（上游列表 / 路由信息）",
              "① 上游列表" in loop_src and "② 路由信息" in route_src
              and "本段完成" in loop_src,
              "loop=%s route=%s" % ("①" in loop_src, "②" in route_src))

        import io
        import contextlib

        # 13h3) 模型清单发现同名冲突时可就地选归属：写入 prefer 后冲突消失
        cfg_fix = os.path.join(work, "config-fix.json")
        write_config(cfg_fix, [{
            "client_key": "sk-conflictfix0001",
            "upstreams": [upstream("a", FAKE_A_PORT), upstream("b", FAKE_B_PORT)],
            "aliases": {},
            "prefer": {},
            "whitelist": [],
            "blacklist": [],
        }], port=GW_PORT + 4)
        gw6 = start([PY, "app.py", "--config", cfg_fix, "--no-preheat",
                     "--log", os.path.join(work, "gw6.log"),
                     "--port", str(GW_PORT + 4)],
                    os.path.join(work, "gw6.out"))
        if wait_port(GW_PORT + 4, timeout=20):
            fix_client = tui_mod.PanelClient("http://127.0.0.1:%d" % (GW_PORT + 4))
            fix_client.login("admin")
            listing = fix_client.models(force=True)
            before = (listing.get("routes") or [{}])[0].get("conflicts") or []

            real_ask = tui_mod.ask
            # 冲突列表里选第 1 个上游作为归属
            tui_mod.ask = lambda prompt="", default="": "1"
            try:
                with contextlib.redirect_stdout(io.StringIO()) as fix_buf:
                    tui_mod._fix_conflicts(fix_client, listing.get("routes") or [])
            finally:
                tui_mod.ask = real_ask

            prefer_after = ((fix_client.config_masked().get("routes") or [{}])[0]
                            .get("prefer") or {})
            after = ((fix_client.models(force=True).get("routes") or [{}])[0]
                     .get("conflicts") or [])
            check("13h3 模型清单可就地修冲突（选归属写入 prefer 后冲突消失）",
                  len(before) == 1 and prefer_after.get("shared-model") == "a" and not after,
                  "before=%d prefer=%s after=%d out=%s"
                  % (len(before), prefer_after, len(after),
                     fix_buf.getvalue().replace("\n", " / ")[:120]))

            # 13h4) 菜单动作整体跑一遍不炸：曾经有残留调用让 action_models 抛 NameError
            tui_mod.ask = lambda prompt="", default="": ""
            try:
                with contextlib.redirect_stdout(io.StringIO()) as models_buf:
                    tui_mod.action_models(fix_client)
            except Exception as exc:  # noqa: BLE001 - 验收就是要抓任何异常
                models_err = "%s: %s" % (type(exc).__name__, exc)
            else:
                models_err = ""
            finally:
                tui_mod.ask = real_ask
            models_out = models_buf.getvalue()
            check("13h4 菜单动作 action_models 可完整跑通",
                  not models_err and "模型清单" in models_out,
                  "err=%s out=%s" % (models_err, models_out.replace("\n", " / ")[:120]))
            fix_client.logout()
        else:
            check("13h3 模型清单可就地修冲突（选归属写入 prefer 后冲突消失）",
                  False, "网关未起来")
            check("13h4 菜单动作 action_models 可完整跑通", False, "网关未起来")
        stop(gw6)

        buf = io.StringIO()
        view_client = tui_mod.PanelClient(BASE)
        view_client.login("admin")
        real_ask = tui_mod.ask
        # 归属编辑会读输入；这里直接回车（不修改），只看渲染结果
        tui_mod.ask = lambda prompt="", default="": ""
        try:
            with contextlib.redirect_stdout(buf):
                tui_mod.action_config_view(view_client)
        finally:
            tui_mod.ask = real_ask
        view_out = buf.getvalue()
        check("13i 配置查看输出人读格式且不含 JSON 大括号",
              "当前配置" in view_out and "{" not in view_out and "}" not in view_out,
              view_out.replace("\n", " / ")[:160])

        # 13i2) 每个上游下面要列出它实际提供的模型，而不是只有地址
        check("13i2 配置查看在每个上游下列出模型",
              "模型" in view_out and "deepseek-v4.1-flash" in view_out
              and "deepseek-v4.1-pro" in view_out,
              view_out.replace("\n", " / ")[:200])

        # 13i3) 别名/prefer 的键值之间留空格，长模型名才看得清对应关系
        check("13i3 配置查看的映射用「键 = 值」格式",
              "deepseek-flash = deepseek-v4.1-flash" in view_out,
              view_out.replace("\n", " / ")[:200])

        # 13i4) 配了 prefer 的模型要说明"采用哪个上游的模型"
        check("13i4 配置查看说明 prefer 采用了谁的模型",
              "采用" in view_out and "shared-model" in view_out,
              view_out.replace("\n", " / ")[:200])

        # 13i5) 归属可改：选模型 → 选上游，写进 prefer 并重新渲染
        edit_client = tui_mod.PanelClient(BASE)
        edit_client.login("admin")
        before_prefer = ((edit_client.config_masked().get("routes") or [{}])[0]
                         .get("prefer") or {})
        current_owner = before_prefer.get("shared-model") or "a"
        other = "b" if current_owner == "a" else "a"
        # 第 1 个可调模型 → 选另一个上游（编号 1/2 对应上游出现顺序）
        answers = ["1", "1" if other == "a" else "2"]
        real_ask = tui_mod.ask
        tui_mod.ask = lambda prompt="", default="": answers.pop(0) if answers else ""
        try:
            with contextlib.redirect_stdout(io.StringIO()) as edit_buf:
                tui_mod._edit_prefer(edit_client, edit_client.models(force=True).get("routes") or [])
        finally:
            tui_mod.ask = real_ask
        after_prefer = ((edit_client.config_masked().get("routes") or [{}])[0]
                        .get("prefer") or {})
        edit_client.logout()
        check("13i5 配置查看里可改模型归属（写进 prefer）",
              after_prefer.get("shared-model") == other,
              "before=%s after=%s out=%s"
              % (before_prefer, after_prefer,
                 edit_buf.getvalue().replace("\n", " / ")[:140]))
        view_client.logout()

        tui_mod._take_error()
        tui_mod.good("ok")
        after_good = tui_mod._take_error()
        tui_mod.bad("boom")
        after_bad = tui_mod._take_error()
        consumed = tui_mod._take_error()
        check("13j 报错会保留在屏幕上（bad 置标记、正常输出不置）",
              after_good is False and after_bad is True and consumed is False,
              "good=%s bad=%s consumed=%s" % (after_good, after_bad, consumed))

        class _RunClient:
            base_url = BASE

        def _ok_action(client):
            print("结果标记OK")

        buf2 = io.StringIO()
        real_actions = tui_mod.ACTIONS
        real_menu = tui_mod.MENU
        real_ask = tui_mod.ask
        real_pause = tui_mod._pause
        answers = ["9", "0"]
        pause_calls = []
        tui_mod.ACTIONS = {"9": _ok_action}
        tui_mod.MENU = [("9", "测试", _ok_action)]
        tui_mod.ask = lambda prompt="", default="": answers.pop(0)
        tui_mod._pause = lambda prompt="": pause_calls.append(prompt)
        try:
            with contextlib.redirect_stdout(buf2):
                tui_mod.run(_RunClient())
        finally:
            tui_mod.ACTIONS = real_actions
            tui_mod.MENU = real_menu
            tui_mod.ask = real_ask
            tui_mod._pause = real_pause
        run_out = buf2.getvalue()
        first_menu = run_out.find("=== alias-gateway")
        result_at = run_out.find("结果标记OK")
        second_menu = run_out.find("=== alias-gateway", first_menu + 1)
        check("13k 每个菜单动作都清屏：结果页暂停后返回菜单",
              first_menu < result_at < second_menu
              and run_out.count("=== alias-gateway") == 2
              and pause_calls == [""],
              run_out.replace("\n", " / ")[:200])

        fresh = os.path.join(work, "fresh", "config.json")
        os.makedirs(os.path.dirname(fresh), exist_ok=True)
        fresh_log = os.path.join(work, "fresh.out")
        fresh_proc = start([PY, "app.py", "--config", fresh, "--no-preheat",
                            "--log", os.path.join(work, "gw4.log"),
                            "--port", str(GW_PORT + 2)],
                           fresh_log)
        ok_fresh = wait_port(GW_PORT + 2, timeout=20)
        stop(fresh_proc)
        with open(fresh_log, "r", encoding="utf-8", errors="replace") as fp:
            fresh_out = fp.read()
        check("1b 首次启动自动生成 config.json",
              os.path.isfile(fresh) and ok_fresh, "path=%s" % fresh)
        if os.path.isfile(fresh):
            with open(fresh, "r", encoding="utf-8") as fp:
                gen = json.load(fp)
            check("1c 生成的是空骨架（无预填上游），且 --port 已写回配置文件",
                  gen["routes"] == []
                  and gen["listen_port"] == GW_PORT + 2
                  and gen["panel_password_hash"] == ""
                  and "example.invalid" not in json.dumps(gen),
                  "routes=%s port=%s" % (gen["routes"], gen["listen_port"]))
        init_lines = [ln for ln in fresh_out.splitlines() if ln.startswith("[init]")]
        check("1d 首次启动打印三行 [init] 提示", len(init_lines) == 3,
              " / ".join(init_lines) or fresh_out[:200].replace("\n", " / "))

        gw5 = start([PY, "app.py", "--config", fresh, "--no-preheat",
                     "--log", os.path.join(work, "gw5.log"),
                     "--port", str(GW_PORT + 3)],
                    os.path.join(work, "gw5.out"))
        if wait_port(GW_PORT + 3, timeout=20):
            base5 = "http://127.0.0.1:%d" % (GW_PORT + 3)
            st, body, _ = http("GET", base5 + "/panel/status")
            check("1e 未注册时 status 报告 panel_registered=false",
                  st == 200 and body.get("panel_registered") is False, "body=%s" % body)
            st, body, _ = http("POST", base5 + "/panel/register", {"password": "firstpass"})
            check("1f 首次进入可注册并直接拿到会话",
                  st == 200 and body.get("ok") and body.get("token"), "status=%s" % st)
            st, body2, _ = http("POST", base5 + "/panel/register", {"password": "again"})
            check("1g 注册后重复注册被拒绝", st == 400 and not body2.get("ok"),
                  "status=%s" % st)
            st, body3, _ = http("POST", base5 + "/panel/login", {"password": "firstpass"})
            check("1h 注册的密码可用于登录",
                  st == 200 and body3.get("ok") and body3.get("token"), "status=%s" % st)

            st, token_body, _ = http("POST", base5 + "/panel/login", {"password": "firstpass"})
            tok = token_body.get("token") if isinstance(token_body, dict) else None
            st, models_body, _ = http("GET", base5 + "/panel/models",
                                      headers={"X-Panel-Token": tok or ""})
            check("1i 空骨架下 /panel/models 返回空路由列表",
                  st == 200 and models_body.get("ok") and models_body.get("routes") == [],
                  "status=%s routes=%s" % (st, models_body.get("routes")))
            st, ov_body, _ = http("GET", base5 + "/panel/overview",
                                  headers={"X-Panel-Token": tok or ""})
            check("1j 空骨架下总览计数为 0",
                  st == 200 and ov_body.get("routes") == 0 and ov_body.get("upstreams") == 0,
                  "status=%s routes=%s ups=%s" % (st, ov_body.get("routes"),
                                                  ov_body.get("upstreams")))
            st, masked_body, _ = http("GET", base5 + "/panel/config",
                                      headers={"X-Panel-Token": tok or ""})
            payload = dict(masked_body) if isinstance(masked_body, dict) else {}
            payload["routes"] = [{
                "client_key": "",
                "upstreams": [{
                    "name": "primary",
                    "base_url": "http://127.0.0.1:%d/v1" % FAKE_A_PORT,
                    "auth": {"type": "bearer", "key": "k"},
                    "extra_headers": {},
                    "timeout": 30,
                }],
                "aliases": {},
                "prefer": {},
                "whitelist": [],
                "blacklist": [],
            }]
            st, saved, _ = http("POST", base5 + "/panel/config", payload,
                                {"X-Panel-Token": tok or ""})
            check("1k 空骨架下可新增路由（客户端 Key 自动生成）",
                  st == 200 and saved.get("ok"), "status=%s body=%s" % (st, saved))
            st, after, _ = http("GET", base5 + "/panel/config",
                                headers={"X-Panel-Token": tok or ""})
            new_key = ((after.get("routes") or [{}])[0] or {}).get("client_key", "")
            check("1l 新增的客户端 Key 形如 sk- + 64 位十六进制",
                  new_key.startswith("sk-") and len(new_key) == 67, "key=%s" % new_key)
        else:
            check("1e 未注册时 status 报告 panel_registered=false", False, "网关未起来")
            check("1f 首次进入可注册并直接拿到会话", False, "网关未起来")
            check("1g 注册后重复注册被拒绝", False, "网关未起来")
            check("1h 注册的密码可用于登录", False, "网关未起来")
            check("1i 空骨架下 /panel/models 返回空路由列表", False, "网关未起来")
            check("1j 空骨架下总览计数为 0", False, "网关未起来")
            check("1k 空骨架下可新增路由（客户端 Key 自动生成）", False, "网关未起来")
            check("1l 新增的客户端 Key 形如 sk- + 64 位十六进制", False, "网关未起来")
        stop(gw5)

    finally:
        stop(gw)
        stop(fake_a)
        stop(fake_b)
        shutil.rmtree(work, ignore_errors=True)

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print("\n%d/%d 通过" % (passed, total))
    failed = [n for n, ok, _d in RESULTS if not ok]
    if failed:
        print("失败项：" + "、".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
