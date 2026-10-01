#!/usr/bin/env bash
# 本地联调：起假上游 + 起网关，并打一遍冒烟测试。
#
#   bash .e2e/dev.sh          # 前台起网关（Ctrl+C 退出）
#   bash .e2e/dev.sh smoke    # 起后台 -> 跑冒烟 -> 关掉
#
# 只用于本机验证，别拿去对外服务。
#
# 所有运行产物（配置、日志、进程输出）都放临时文件，退出时清掉，
# 不会碰到项目根目录的 config.json 与 gateway.log。

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

UP_PORT="${UP_PORT:-9911}"
BACKUP_PORT="${BACKUP_PORT:-9912}"
GW_PORT="${GW_PORT:-8790}"
CLIENT_KEY="sk-local-test-0001"


# ---------------------------------------------------------------------------
# 端口探测
# ---------------------------------------------------------------------------

# 端口是否已被占用
port_open() {
    "$PY" -c '
import socket, sys
s = socket.socket(); s.settimeout(0.3)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
' "$1" 2>/dev/null
}

# 从 start 开始找一个空闲端口（最多试 50 个）
pick_port() {
    "$PY" -c '
import socket, sys
start = int(sys.argv[1])
for p in range(start, start + 50):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
    except OSError:
        s.close()
        continue
    s.close()
    print(p)
    break
else:
    sys.exit(1)
' "$1"
}

# 网关端口被占就自动往后挪，避免"Address already in use"
if port_open "$GW_PORT"; then
    NEW_PORT="$(pick_port "$GW_PORT")" || { echo "找不到空闲端口"; exit 1; }
    echo "== 端口 ${GW_PORT} 已被占用，自动改用 ${NEW_PORT}"
    GW_PORT="$NEW_PORT"
fi

# 上游端口同理：本机可能已经有别人的假上游在跑（比如另一个 dev.sh）
if port_open "$UP_PORT"; then
    NEW_PORT="$(pick_port "$UP_PORT")" || { echo "找不到空闲端口"; exit 1; }
    echo "== 端口 ${UP_PORT} 已被占用，自动改用 ${NEW_PORT}"
    UP_PORT="$NEW_PORT"
fi
if port_open "$BACKUP_PORT"; then
    NEW_PORT="$(pick_port "$BACKUP_PORT")" || { echo "找不到空闲端口"; exit 1; }
    echo "== 端口 ${BACKUP_PORT} 已被占用，自动改用 ${NEW_PORT}"
    BACKUP_PORT="$NEW_PORT"
fi

BASE="http://127.0.0.1:${GW_PORT}"

CFG="$(mktemp -t alias-gw-cfg-XXXXXX.json)"
LOG="$(mktemp -t alias-gw-gw-XXXXXX.log)"
UP_LOG="$(mktemp -t alias-gw-up-XXXXXX.log)"
UP2_LOG="$(mktemp -t alias-gw-up2-XXXXXX.log)"

cleanup() {
    [ -n "${GW_PID:-}" ] && kill "$GW_PID" 2>/dev/null
    [ -n "${UP_PID:-}" ] && kill "$UP_PID" 2>/dev/null
    [ -n "${UP2_PID:-}" ] && kill "$UP2_PID" 2>/dev/null
    wait 2>/dev/null
    rm -f "$CFG"
}
trap cleanup EXIT INT TERM


# ---------------------------------------------------------------------------
# 准备配置：拿模板换掉端口，喂给临时副本
# ---------------------------------------------------------------------------

# 模板缺失时自动补一份（避免"sed: can't read"这种没头没脑的失败）
if [ ! -f "$HERE/config.test.json" ]; then
    echo "== .e2e/config.test.json 缺失，自动生成模板"
    "$PY" "$HERE/make_test_config.py" "$HERE/config.test.json" || exit 1
fi

# 网关端口和两个上游端口都按实际值替换，模板里的默认值只是占位
sed -e "s/\"listen_port\": 8789/\"listen_port\": ${GW_PORT}/" \
    -e "s|127.0.0.1:9911/v1|127.0.0.1:${UP_PORT}/v1|" \
    -e "s|127.0.0.1:9912/v1|127.0.0.1:${BACKUP_PORT}/v1|" \
    "$HERE/config.test.json" > "$CFG"


# ---------------------------------------------------------------------------
# 起假上游
# ---------------------------------------------------------------------------

echo "== 启动假上游 :${UP_PORT}"
"$PY" "$HERE/fake_upstream.py" --port "$UP_PORT" --label fake >"$UP_LOG" 2>&1 &
UP_PID=$!

# 第二个上游：与第一个共享 shared-model，且 prefer 为空 —— 故意留一个未解决冲突
echo "== 启动假上游 :${BACKUP_PORT}"
"$PY" "$HERE/fake_upstream.py" --port "$BACKUP_PORT" --label backup \
    --models "shared-model,backup-only" >"$UP2_LOG" 2>&1 &
UP2_PID=$!

# 等假上游就绪
for _ in $(seq 1 40); do
    if port_open "$UP_PORT" && port_open "$BACKUP_PORT"; then break; fi
    sleep 0.15
done

if ! port_open "$UP_PORT" || ! port_open "$BACKUP_PORT"; then
    echo "!! 假上游没起来，日志："
    cat "$UP_LOG" "$UP2_LOG"
    exit 1
fi


# ---------------------------------------------------------------------------
# 起网关
# ---------------------------------------------------------------------------

echo "== 启动网关 :${GW_PORT}"
# 夹具里故意留了一个未解决的同名冲突，跳过预热才能起来（否则退出码 3）
"$PY" "$ROOT/app.py" --config "$CFG" --log "$LOG" --no-preheat >/dev/null 2>&1 &
GW_PID=$!

for _ in $(seq 1 60); do
    if curl -s -o /dev/null "${BASE}/health" 2>/dev/null; then break; fi
    sleep 0.2
done

if ! curl -s -o /dev/null "${BASE}/health"; then
    echo "!! 网关没起来，日志："
    cat "$LOG"
    exit 1
fi


# ---------------------------------------------------------------------------
# 冒烟：逐项打勾，任意一项失败就整体退出非 0
# ---------------------------------------------------------------------------

if [ "${1:-}" = "smoke" ]; then
    AUTH="Authorization: Bearer ${CLIENT_KEY}"
    fail=0
    say() { printf '%-42s %s\n' "$1" "$2"; }

    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/v1/models" -H "$AUTH")
    [ "$code" = "200" ] && say "GET /v1/models" "OK" || { say "GET /v1/models" "FAIL($code)"; fail=1; }

    body=$(curl -s "${BASE}/v1/models" -H "$AUTH")
    case "$body" in
        *deepseek-flash*) say "别名改名 (deepseek-flash)" "OK" ;;
        *) say "别名改名 (deepseek-flash)" "FAIL"; fail=1 ;;
    esac

    # 两个上游各自的独有模型都要出现在清单里，证明多上游是合并而不是覆盖
    case "$body" in
        *backup-only*) say "多上游合并 (backup-only)" "OK" ;;
        *) say "多上游合并 (backup-only)" "FAIL"; fail=1 ;;
    esac

    # shared-model 被两个上游同时提供且没配 prefer，应当只出现在 conflicts 里
    case "$body" in
        *'"conflicts"'*shared-model*) say "同名冲突被标记 (shared-model)" "OK" ;;
        *) say "同名冲突被标记 (shared-model)" "FAIL"; fail=1 ;;
    esac

    body=$(curl -s "${BASE}/v1/chat/completions" -H "$AUTH" \
        -H 'Content-Type: application/json' \
        -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}]}')
    case "$body" in
        *'"model": "deepseek-flash"'*) say "非流式 + 响应改名" "OK" ;;
        *) say "非流式 + 响应改名" "FAIL: $body"; fail=1 ;;
    esac

    body=$(curl -sN "${BASE}/v1/chat/completions" -H "$AUTH" \
        -H 'Content-Type: application/json' \
        -d '{"model":"deepseek-flash","stream":true,"messages":[{"role":"user","content":"hi"}]}')
    case "$body" in
        *"[DONE]"*) say "流式 SSE" "OK" ;;
        *) say "流式 SSE" "FAIL"; fail=1 ;;
    esac

    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/v1/models" -H 'Authorization: Bearer wrong')
    [ "$code" = "401" ] && say "错误 Key -> 401" "OK" || { say "错误 Key -> 401" "FAIL($code)"; fail=1; }

    body=$(curl -s "${BASE}/panel/login" -H 'Content-Type: application/json' -d '{"password":"admin"}')
    case "$body" in
        *'"token"'*) say "管理接口登录（已注册）" "OK" ;;
        *) say "管理接口登录（已注册）" "FAIL: $body"; fail=1 ;;
    esac

    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/panel/register" \
        -H 'Content-Type: application/json' -d '{"password":"whatever"}')
    [ "$code" = "400" ] && say "已注册后拒绝重复注册" "OK" \
        || { say "已注册后拒绝重复注册" "FAIL($code)"; fail=1; }

    if "$PY" "$ROOT/tui.py" --url "$BASE" -p admin </dev/null >/dev/null 2>&1; then
        say "终端界面 tui.py 可启动" "OK"
    else
        # 非交互环境下 tui.py 读到 EOF 会正常退出，退出码 0；非 0 才算失败
        code=$?
        [ "$code" = "0" ] && say "终端界面 tui.py 可启动" "OK" \
            || { say "终端界面 tui.py 可启动" "FAIL($code)"; fail=1; }
    fi

    echo
    [ "$fail" = "0" ] && echo "全部通过 ✅" || echo "有失败项 ❌"
    exit "$fail"
fi


# ---------------------------------------------------------------------------
# 非 smoke 模式：前台起网关，把接入信息打出来
# ---------------------------------------------------------------------------

cat <<EOF

==========================================================
  网关已就绪
==========================================================
  管理界面    终端里运行： python3 ${ROOT}/tui.py --port ${GW_PORT}
              （首次进入会要求设置管理密码；本测试配置里密码已是 admin）

  客户端 Base URL   ${BASE}/v1
  客户端 API Key    ${CLIENT_KEY}

  curl 试一下：
    curl ${BASE}/v1/models -H 'Authorization: Bearer ${CLIENT_KEY}'

    curl ${BASE}/v1/chat/completions \\
      -H 'Authorization: Bearer ${CLIENT_KEY}' \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"你好"}]}'

  日志        ${LOG}
  Ctrl+C 退出（两个假上游会一起关掉）
==========================================================

EOF

wait "$GW_PID"