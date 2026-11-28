#!/usr/bin/env bash
# verify.sh（Req: F182）——容器级六项验证（V1-V4＋P4R＋P6B），可复跑。
# 每项＝独立容器回合：mock-agorad（隔离 AGORA_HOME，三端点）先起 → 容器内
# 真实 dsh headless 会话（GLM 动力）挂 shim＋真 agora_mcp.py → 断言后写
# dsh-shim/VERIFY.md 六行结论；原始证据（events.jsonl/stderr/mock 账本）归档
# dsh-shim/verify-out/<phase>/（运行目录在 $F182_VERIFY_RUN，缺省 /tmp 下）。
# 凭据纪律：GLM token 从 opencode 凭据库提取后只进 docker -e 进程 env，
# 全程零落盘；收尾 grep 守卫防意外回流。
# 预算：docker build ≤1 次/回跑；每回合超时 420s。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHIM="$REPO_ROOT/dsh-shim"
IMAGE="${IMAGE:-opencode-agora/f182-dsh-worker:1}"
OUT="$SHIM/verify-out"
RUN_ROOT="${F182_VERIFY_RUN:-/tmp/opencode/ag-2e75a4-verify}"
TIMEOUT="${F182_VERIFY_TIMEOUT:-420}"

say() { printf '[verify] %s\n' "$*"; }

# GLM 动力（只进进程 env——禁打印禁落盘）；提取失败/为空＝诚实早失败
# （B 册 R1：空 key 会让末尾守卫 grep -qF "" 恒命中→假报「泄漏」吞真实原因）
if [ -z "${GLM_API_KEY:-}" ]; then
  GLM_API_KEY="$(python3 -c 'import sqlite3,json,os; c=sqlite3.connect("file:"+os.path.expanduser("~/.local/share/opencode/opencode.db")+"?mode=ro",uri=True); print(json.loads(c.execute("SELECT value FROM credential WHERE integration_id=\"zai-coding-plan\" AND active=1").fetchone()[0])["key"])')"
  export GLM_API_KEY
fi
if [ -z "${GLM_API_KEY:-}" ]; then
  echo "[verify] FAIL GLM key 提取为空（opencode 凭据库不可用）" >&2
  exit 3
fi

# 共通容器内引导（各 phase 的 in-container.sh 首行 source 它）
mkdir -p "$RUN_ROOT" "$OUT"
cat > "$RUN_ROOT/.boot.sh" <<'BOOT'
set -u
ENFORCE="$1"
start_mock() {
  python3 /work/repo/dsh-shim/mock-agorad.py --home /work/agora-home \
    --env-dir /work/task-env --enforce "$ENFORCE" \
    >/work/out/mock-boot.log 2>&1 &
  MOCK_PID=$!
  for _ in $(seq 1 40); do
    [ -f /work/agora-home/.agora/service.json ] && break; sleep 0.25
  done
}
run_dsh() {  # run_dsh "<prompt>"（events→/work/out/events.jsonl，stderr 另档）
  local prompt="$1"; shift
  cd /work/task-env
  dsh --profile headless --patch /work/repo/dsh-shim/cordis.worker.yml \
    --json "$@" "$prompt" >/work/out/events.jsonl 2>/work/out/stderr.log
}
BOOT

# docker_phase <phase> <enforce> <sandbox-mode> <script-src>
# 运行目录 $RUN_ROOT/<phase>（rm -rf 重建）；容器退出码落 rc。
docker_phase() {
  local phase="$1" enforce="$2" perm="$3" script="$4"
  local d="$RUN_ROOT/$phase"
  rm -rf "$d"; mkdir -p "$d/task-env" "$d/foreign-repo/.git" "$d/agora-home" "$d/dsh-home" "$d/out"
  printf 'foreign fixture\n' > "$d/foreign-repo/README.txt"
  cp "$RUN_ROOT/.boot.sh" "$d/out/.boot.sh"
  cp "$script" "$d/out/in-container.sh"
  timeout "$TIMEOUT" docker run --rm \
    --network=host \
    -u "$(id -u):$(id -g)" \
    -e GLM_API_KEY -e HOME=/work/dsh-home -e DSH_HOME=/work/dsh-home \
    -e AGORA_HOME=/work/agora-home -e AGORAD_SCRIPT=/dev/null \
    -e DSH_PERMISSION_MODE="$perm" -e AGORA_HOOK_DEBUG=1 \
    -v "$REPO_ROOT":/work/repo:ro \
    -v "$d/task-env":/work/task-env \
    -v "$d/foreign-repo":/work/foreign-repo \
    -v "$d/agora-home":/work/agora-home \
    -v "$d/dsh-home":/work/dsh-home \
    -v "$d/out":/work/out \
    "$IMAGE" \
    bash "/work/out/in-container.sh" "$enforce" \
    >"$d/container.log" 2>&1
  echo $? > "$d/rc"
}

# 归档证据到仓内 verify-out/<phase>/（脚本＋输出留档）
archive() {
  local phase="$1"
  local d="$RUN_ROOT/$phase" o="$OUT/$phase"
  rm -rf "$o"; mkdir -p "$o"
  cp -f "$d/rc" "$d/container.log" "$o/" 2>/dev/null || true
  for f in in-container.sh events.jsonl stderr.log mock-boot.log kill.ts dsh.rc; do
    cp -f "$d/out/$f" "$o/" 2>/dev/null || true
  done
  cp -f "$d/agora-home/mock-agorad.jsonl" "$o/" 2>/dev/null || true
  cp -f "$d/foreign-repo/allowed.txt" "$o/" 2>/dev/null || true
  cp -f "$d/task-env/p6b-ww.txt" "$o/" 2>/dev/null || true
}

# python3 断言助手：events.jsonl / mock 账本（JSONL）解析
PY() { python3 - "$@" <<'PYEOF'
import json, sys
def rows(p):
    out=[]
    try:
        for line in open(p, encoding="utf-8"):
            line=line.strip()
            if line:
                try: out.append(json.loads(line))
                except ValueError: pass
    except OSError: pass
    return out
cmd, *a = sys.argv[1:]
if cmd == "session":
    for e in rows(a[0]):
        if e.get("type")=="session": print(e.get("sessionId") or ""); break
    else: print("")
elif cmd == "calls":
    print(sum(1 for e in rows(a[0]) if e.get("type")=="tool_call" and e.get("tool")==a[1]))
elif cmd == "denytext":
    print("HIT" if any(
        e.get("type")=="tool_result" and e.get("status")=="error" and a[1] in str(e.get("result") or "")
        for e in rows(a[0])) else "MISS")
elif cmd == "toolok":
    ids={e.get("callId") for e in rows(a[0]) if e.get("type")=="tool_call" and e.get("tool")==a[1]}
    print(sum(1 for e in rows(a[0]) if e.get("type")=="tool_result" and e.get("callId") in ids and e.get("status")!="error"))
elif cmd == "mock":
    kind, field, cond = a[1], a[2], a[3]
    print(sum(1 for e in rows(a[0]) if e.get("kind")==kind and str(e.get(field) or "")==cond))
elif cmd == "mockcount":
    print(sum(1 for e in rows(a[0]) if e.get("kind")==a[1]))
elif cmd == "mocklastts":
    ts=[e.get("ts") for e in rows(a[0]) if e.get("kind")==a[1]]
    print(ts[-1] if ts else 0)
elif cmd == "mockfield":
    for e in rows(a[0]):
        if e.get("kind")==a[1]:
            print(e.get(a[2]) or ""); break
    else: print("")
PYEOF
}

# ───────────────────────── V1 越界写被拒（enforce on） ─────────────────────────
v1() {
  cat > "$RUN_ROOT/v1.script" <<'EOF'
source /work/out/.boot.sh
start_mock
run_dsh "用 write 工具把文本 hi 写入绝对路径 /work/foreign-repo/evil.txt（必须用 write 工具真调用一次），然后说明结果并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
EOF
  docker_phase v1 on danger-full-access "$RUN_ROOT/v1.script"
  archive v1
  local d="$RUN_ROOT/v1"
  local rc deny gd
  rc="$(cat "$d/rc")"
  deny="$(PY denytext "$d/out/events.jsonl" '被拦：写入目标 /work/foreign-repo/evil.txt 在任务 env /work/task-env 外')"
  gd="$(PY mockcount "$d/agora-home/mock-agorad.jsonl" gate_deny)"
  if [ "$rc" = 0 ] && [ "$deny" = HIT ] && [ ! -e "$d/foreign-repo/evil.txt" ] && [ "${gd:-0}" -ge 1 ]; then
    echo "V1: PASS — enforce on 下越界写被拒（events.jsonl tool_result 含 wrongDoorHint 全文，与 opencode 版同源），目标未落盘，/gate_deny 审计落账 ${gd} 条"
  else
    echo "V1: FAIL — rc=$rc deny=$deny 文件存在=$([ -e "$d/foreign-repo/evil.txt" ] && echo yes || echo no) gate_deny=$gd（证据：dsh-shim/verify-out/v1/）"
  fi
}

# ───────────────────────── V2 enforce off 全放行 ─────────────────────────
v2() {
  cat > "$RUN_ROOT/v2.script" <<'EOF'
source /work/out/.boot.sh
start_mock
run_dsh "用 write 工具把文本 hi 写入绝对路径 /work/foreign-repo/allowed.txt（必须用 write 工具真调用一次），然后说明结果并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
EOF
  docker_phase v2 off danger-full-access "$RUN_ROOT/v2.script"
  archive v2
  local d="$RUN_ROOT/v2"
  local rc gd
  rc="$(cat "$d/rc")"
  gd="$(PY mockcount "$d/agora-home/mock-agorad.jsonl" gate_deny)"
  if [ "$rc" = 0 ] && [ -f "$d/foreign-repo/allowed.txt" ] && grep -q hi "$d/foreign-repo/allowed.txt" && [ "${gd:-0}" = 0 ]; then
    echo "V2: PASS — enforce off 下同一越界写全放行（allowed.txt 实落盘），零 gate_deny"
  else
    echo "V2: FAIL — rc=$rc 文件=$([ -f "$d/foreign-repo/allowed.txt" ] && echo yes || echo no) gate_deny=$gd（证据：dsh-shim/verify-out/v2/）"
  fi
}

# ───────────────────────── V3 附牌实收 _token/_sid ─────────────────────────
v3() {
  cat > "$RUN_ROOT/v3.script" <<'EOF'
source /work/out/.boot.sh
start_mock
run_dsh "调用工具 mcp__agora__check_mail（空参数即可，必须真调用），然后报告工具返回并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
EOF
  docker_phase v3 on danger-full-access "$RUN_ROOT/v3.script"
  archive v3
  local d="$RUN_ROOT/v3"
  local rc sid pulls toolok tokok msid
  rc="$(cat "$d/rc")"
  sid="$(PY session "$d/out/events.jsonl")"
  pulls="$(PY mock "$d/agora-home/mock-agorad.jsonl" cred_for_sid sid "$sid")"
  toolok="$(PY toolok "$d/out/events.jsonl" mcp__agora__check_mail)"
  tokok="$(PY mock "$d/agora-home/mock-agorad.jsonl" tool token_ok True)"
  msid="$(PY mockfield "$d/agora-home/mock-agorad.jsonl" tool x_agora_sid)"
  if [ "$rc" = 0 ] && [ "${pulls:-0}" -ge 1 ] && [ "${toolok:-0}" -ge 1 ] && [ "${tokok:-0}" -ge 1 ] && [ -n "$sid" ] && [ "$msid" = "$sid" ]; then
    echo "V3: PASS — mcp__agora__check_mail 调用达 agorad（mock 面实收 X-Agora-Token＝签发 worker 牌、X-Agora-Sid＝会话 sid，与 /cred_for_sid 拉取同 sid）"
  else
    echo "V3: FAIL — rc=$rc pulls=$pulls toolok=$toolok tokok=$tokok sid=$sid msid=$msid（证据：dsh-shim/verify-out/v3/）"
  fi
}

# ───────────────────────── V4 shadow 动态取定义连调两次 ─────────────────────────
v4() {
  cat > "$RUN_ROOT/v4.script" <<'EOF'
source /work/out/.boot.sh
start_mock
run_dsh "连续两次调用工具 mcp__agora__check_mail（每次都是真实独立调用，共两次），两次都完成后输出 DONE 并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
EOF
  docker_phase v4 on danger-full-access "$RUN_ROOT/v4.script"
  archive v4
  local d="$RUN_ROOT/v4"
  local rc calls toolok mockn
  rc="$(cat "$d/rc")"
  calls="$(PY calls "$d/out/events.jsonl" mcp__agora__check_mail)"
  toolok="$(PY toolok "$d/out/events.jsonl" mcp__agora__check_mail)"
  mockn="$(PY mockcount "$d/agora-home/mock-agorad.jsonl" tool)"
  if [ "$rc" = 0 ] && [ "${calls:-0}" -ge 2 ] && [ "${toolok:-0}" -ge 2 ] && [ "${mockn:-0}" -ge 2 ]; then
    echo "V4: PASS — 同会话连续 ${calls} 次调用均走 shadow 动态取定义（${toolok} 次成功、mock 面实收 ${mockn} 次），无 stale 捕获"
  else
    echo "V4: FAIL — rc=$rc calls=$calls ok=$toolok mock=$mockn（证据：dsh-shim/verify-out/v4/）"
  fi
}

# ───────────────────────── P4R 全局定义换代后 shadow 仍可用 ─────────────────────────
p4r() {
  cat > "$RUN_ROOT/p4r.script" <<'EOF'
source /work/out/.boot.sh
start_mock
LOG=/work/agora-home/mock-agorad.jsonl
( run_dsh "第一步：调用一次工具 mcp__agora__check_mail。第二步：MCP 服务随后会被外部重启，期间的调用可能报错——请等 3 秒后再调用一次 mcp__agora__check_mail（失败就再等 3 秒重试，直到成功）。两次都成功后输出 DONE 并结束。"
  echo $? > /work/out/dsh.rc ) &
DSH=$!
for _ in $(seq 1 120); do
  grep -q '"kind": "tool"' "$LOG" 2>/dev/null && break; sleep 1
done
sleep 2   # 首调稳定落账后动手：杀 MCP 子进程→客户端重连→工具重注册→shim 重同步
date +%s > /work/out/kill.ts
pkill -f 'agora_mcp.py' 2>/dev/null || true
wait $DSH
kill $MOCK_PID 2>/dev/null
exit "$(cat /work/out/dsh.rc)"
EOF
  docker_phase p4r on danger-full-access "$RUN_ROOT/p4r.script"
  archive p4r
  local d="$RUN_ROOT/p4r"
  local rc killts lastts toolok after
  rc="$(cat "$d/rc")"
  killts="$(cat "$d/out/kill.ts" 2>/dev/null || echo 0)"
  lastts="$(PY mocklastts "$d/agora-home/mock-agorad.jsonl" tool)"
  toolok="$(PY toolok "$d/out/events.jsonl" mcp__agora__check_mail)"
  after="no"; awk -v a="$lastts" -v b="$killts" 'BEGIN{exit !(a>b)}' && after="yes"
  if [ "$rc" = 0 ] && [ "${toolok:-0}" -ge 2 ] && [ "$after" = yes ]; then
    echo "P4R: PASS — MCP 子进程被杀触发重同步（全局定义换代）后 shadow 再调成功（kill@$killts 后仍有 tool 落账@${lastts}，共 ${toolok} 次成功）"
  else
    echo "P4R: FAIL — rc=$rc ok=$toolok after=$after kill=$killts last=$lastts（证据：dsh-shim/verify-out/p4r/）"
  fi
}

# ───────────────────────── P6B bwrap 沙箱档位实测 ─────────────────────────
p6b() {
  local tier tag d script
  for mode in read-only:ro workspace-write:ww; do
    tier="${mode%%:*}"; tag="${mode##*:}"
    script="$RUN_ROOT/p6b-$tag.script"
    cat > "$script" <<EOF
source /work/out/.boot.sh
start_mock
run_dsh "用 bash 工具执行命令：echo p6b > /work/task-env/p6b-${tag}.txt（必须用 bash 工具真执行一次），然后报告命令结果并结束。"
RC=\$?
kill \$MOCK_PID 2>/dev/null
exit \$RC
EOF
    docker_phase "p6b-$tag" on "$tier" "$script"
    archive "p6b-$tag"
  done
  local rorc worc rofile wwfile
  rorc="$(cat "$RUN_ROOT/p6b-ro/rc")"; worc="$(cat "$RUN_ROOT/p6b-ww/rc")"
  rofile="$RUN_ROOT/p6b-ro/task-env/p6b-ro.txt"; wwfile="$RUN_ROOT/p6b-ww/task-env/p6b-ww.txt"
  if [ "$rorc" = 0 ] && [ ! -e "$rofile" ] && [ "$worc" = 0 ] && [ -f "$wwfile" ]; then
    echo "P6B: PASS — bwrap 在位：read-only 档 bash 写被拒（p6b-ro.txt 未落盘）＋workspace-write 档工作区内写放行（p6b-ww.txt 落盘）"
  else
    echo "P6B: FAIL — ro(rc=$rorc 文件=$([ -e "$rofile" ] && echo yes || echo no)) ww(rc=$worc 文件=$([ -f "$wwfile" ] && echo yes || echo no))（证据：dsh-shim/verify-out/p6b-*/）"
  fi
}

# ───────────────────────── 主流程 ─────────────────────────
say "docker build（缺镜像才建，预算 ≤1/回跑）"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" "$SHIM"

RESULTS=()
if [ "$#" -gt 0 ]; then PHASES=("$@"); else PHASES=(v1 v2 v3 v4 p4r p6b); fi
for phase in "${PHASES[@]}"; do
  say "phase $phase 开始"
  case "$phase" in
    v1) RESULTS+=("$(v1)") ;;
    v2) RESULTS+=("$(v2)") ;;
    v3) RESULTS+=("$(v3)") ;;
    v4) RESULTS+=("$(v4)") ;;
    p4r) RESULTS+=("$(p4r)") ;;
    p6b) RESULTS+=("$(p6b)") ;;
    *) echo "[verify] 未知 phase $phase" >&2; exit 2 ;;
  esac
done

# 凭据守卫：仓内 verify-out 不得出现 GLM key 任何片段
if grep -rqF "$GLM_API_KEY" "$OUT" 2>/dev/null; then
  echo "[verify] FAIL GLM key 泄入 verify-out——清除后重跑" >&2
  exit 3
fi

{
  echo "# F182 容器级验证结论（Req: F182）"
  echo
  echo "由 dsh-shim/verify.sh 生成（容器内真实 dsh headless 会话＋GLM 动力＋真 agora_mcp.py＋mock-agorad 三端点）。"
  echo "复跑：cd dsh-shim && ./verify.sh（证据归档 dsh-shim/verify-out/<phase>/）。"
  echo "生成时间：$(date '+%F %T')（宿主）"
  echo
  for r in "${RESULTS[@]}"; do echo "$r"; done
} > "$SHIM/VERIFY.md"

for r in "${RESULTS[@]}"; do echo "$r"; done
if grep -qE '^(V[1-4]|P4R|P6B): FAIL' "$SHIM/VERIFY.md"; then exit 1; fi
exit 0
