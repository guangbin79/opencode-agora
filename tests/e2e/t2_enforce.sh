#!/usr/bin/env bash
# T2 插件强制 e2e（F1）：tmux 开真 opencode（独立实例——HOME/XDG_*/AGORA_HOME 全隔离，
# 绝不触碰本机会话服务），插件经 install.sh 装入隔离配置目录；agorad 独立实例＋root 牌；
# 七例场景＋agorad 侧断言＋残留自清理。
# 用法：bash tests/e2e/t2_enforce.sh
#   产物 /tmp/opencode/t2-<stamp>/（约束根）；git 夹具在 /tmp/agora-t2-<stamp>/——判定链把
#   /tmp/opencode 整体豁免（B3），拒例目标必须放豁免根之外。
# 驱动形态（实测裁定，T5 探针同源）：真 opencode serve 进程驻 tmux；注入经 /api/session/{sid}/prompt
# （与 TUI send-keys 同一 prompt 链路，插件 session.prompt/tool.execute.before 全程在真进程内触发）；
# 断言＝消息面工具部件（state.status/content）＋盘面文件＋agorad HTTP，避 capture-pane 渲染噪声。
# 模型凭据：读本机 ~/.config/opencode/opencode.json provider/baseURL＋opencode.db credential（缺则 SKIP=3）。
# Req: F1
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STAMP="$(date +%m%d%H%M)-$$"
SANDBOX="/tmp/opencode/t2-$STAMP"
WORK="/tmp/agora-t2-$STAMP"
OC_BIN="${OC_BIN:-$HOME/.opencode/bin/opencode}"
S1_NAME="t2-oc1-$STAMP"; S2_NAME="t2-oc2-$STAMP"; AD_NAME="t2-ad-$STAMP"
PID1="$SANDBOX/serve1.pid"; PID2="$SANDBOX/serve2.pid"; PIDA="$SANDBOX/agorad.pid"

note() { echo "[t2] $*"; }

cleanup() {
  local rc=$?
  tmux kill-session -t "$S1_NAME" 2>/dev/null
  tmux kill-session -t "$S2_NAME" 2>/dev/null
  tmux kill-session -t "$AD_NAME" 2>/dev/null
  for f in "$PID1" "$PID2" "$PIDA"; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
  sleep 1
  if [ "$rc" -eq 0 ]; then
    rm -rf "$SANDBOX" "$WORK"
  else
    note "非绿保留现场：$SANDBOX $WORK（查 serve1/serve2/agorad/install 日志）"
  fi
  note "清理完成（rc=$rc）"
}
trap cleanup EXIT

# ---- 0) 模型凭据（本机自适应；无凭据 SKIP）----
mkdir -p "$SANDBOX" "$WORK"
CFG_JSON="$SANDBOX/ocfg.json"
python3 - "$CFG_JSON" <<'PY' || { note "SKIP：本机无 opencode provider 配置/凭据（e2e 依赖真模型）"; exit 3; }
import json, os, sqlite3, sys
cfg = json.load(open(os.path.expanduser("~/.config/opencode/opencode.json")))
prov = next(p for p, v in cfg["providers"].items() if v.get("settings", {}).get("baseURL"))
data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
db = sqlite3.connect(f"file:{data_home}/opencode/opencode.db?mode=ro", uri=True)
key = json.loads(db.execute("SELECT value FROM credential WHERE integration_id=?",
                            (prov,)).fetchone()[0])["key"]
json.dump({"prov": prov, "model": cfg["model"],
           "baseURL": cfg["providers"][prov]["settings"]["baseURL"], "key": key},
          open(sys.argv[1], "w"))
PY
read -r PROV MODEL BASEURL < <(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d['prov'],d['model'],d['baseURL'])" "$CFG_JSON")
OC_KEY="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['key'])" "$CFG_JSON")"
note "provider=$PROV model=$MODEL"

# ---- 1) 隔离 opencode 配置＋插件（install.sh 稍后幂等并入 mcp 段＋载荷＋root 牌）----
mkdir -p "$SANDBOX"/{home,data,state,state2,cache}
mkdir -p "$SANDBOX"/plaza/state/messages "$SANDBOX"/plaza/wiki "$SANDBOX"/plaza/.agora "$SANDBOX"/plaza2
# 账本夹具必须在 agorad 首启前落位：Store 启动扫 messages/ 目录恢复 msg-id 计数（max+1），
# 后写夹具会侵占 id 序（write_message "x" 模式 FileExistsError→通知链 400，run5/run6 实证）
echo "msg-fixture" > "$SANDBOX/plaza/state/messages/00000001.md"
echo "wiki-fixture" > "$SANDBOX/plaza/wiki/p.md"
touch "$SANDBOX/plaza/events.jsonl"
OC_DIR="$SANDBOX/home/.config/opencode"   # opencode 按 XDG_CONFIG_HOME 解析（HOME 不够，实测）
mkdir -p "$OC_DIR/plugins/agora"
for f in "$REPO"/agora-plugin/*.ts; do case "$f" in *.test.ts) continue ;; esac; cp -f "$f" "$OC_DIR/plugins/agora/"; done
python3 - "$OC_DIR/opencode.json" "$REPO/agora_mcp.py" "$SANDBOX/plaza" "$PROV" "$MODEL" "$BASEURL" <<'PY'
import json, sys
p, mcp_py, home, prov, model, base = sys.argv[1:7]
cfg = {"$schema": "https://opencode.ai/config.json", "model": model,
       "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
       "providers": {prov: {"settings": {"baseURL": base}}},
       "mcp": {"agora": {"type": "local", "command": ["python3", mcp_py],
                         "environment": {"AGORA_HOME": home}}}}
open(p, "w").write(json.dumps(cfg, ensure_ascii=False, indent=1))
PY

# ---- 2) serve#1（主实例）tmux 拉起；就绪判据＝/api/info（/health 是 SPA 兜底 200，T5 实证不可用）----
tmux new-session -d -s "$S1_NAME" "env HOME='$SANDBOX/home' XDG_CONFIG_HOME='$SANDBOX/home/.config' \
XDG_STATE_HOME='$SANDBOX/state' XDG_DATA_HOME='$SANDBOX/data' XDG_CACHE_HOME='$SANDBOX/cache' \
AGORA_HOME='$SANDBOX/plaza' ZAI_API_KEY='$OC_KEY' AGORAD_SCRIPT='/nonexistent/t2-no-resurrect' \
'$OC_BIN' serve --service --hostname 127.0.0.1 --port 0 > '$SANDBOX/serve1.log' 2>&1"
for i in $(seq 1 60); do [ -f "$SANDBOX/state/opencode/service.json" ] && break; sleep 0.5; done
python3 -c "import json;print(json.load(open('$SANDBOX/state/opencode/service.json'))['pid'])" > "$PID1"
python3 - "$SANDBOX/state/opencode/service.json" <<'PY' || exit 1
import base64, json, sys, time, urllib.request
sj = json.load(open(sys.argv[1]))
auth = "Basic " + base64.b64encode(f"opencode:{sj['password']}".encode()).decode()
for _ in range(60):
    try:
        urllib.request.urlopen(urllib.request.Request(sj["url"] + "/api/info",
                               headers={"Authorization": auth}), timeout=2)
        print("serve1 ready", sj["url"]); sys.exit(0)
    except SystemExit: raise
    except Exception: time.sleep(0.5)
sys.exit("serve1 30s 未就绪")
PY
OC1_URL="$(python3 -c "import json;print(json.load(open('$SANDBOX/state/opencode/service.json'))['url'])")"
OC1_PW="$(python3 -c "import json;print(json.load(open('$SANDBOX/state/opencode/service.json'))['password'])")"
note "serve#1 $OC1_URL"

# ---- 3) install.sh（隔离 HOME/AGORA_HOME/显式 OC）：插件＋agorad 载荷＋root 牌接管＋冒烟 ----
HOME="$SANDBOX/home" AGORA_HOME="$SANDBOX/plaza" \
AGORA_OC_URL="$OC1_URL" AGORA_OC_PASSWORD="$OC1_PW" bash "$REPO/install.sh" \
  > "$SANDBOX/install.log" 2>&1 || { tail -5 "$SANDBOX/install.log"; exit 1; }
[ -s "$SANDBOX/plaza/.agora/root.token" ] || { note "root.token 缺失"; exit 1; }
ROOT_TOKEN="$(cat "$SANDBOX/plaza/.agora/root.token")"
note "install.sh 装配＋root 牌接管＋冒烟 完成"

# ---- 4) 常驻 agorad（install 冒烟实例已停；牌落盘复用；enforce 服务侧缺省 on）----
tmux new-session -d -s "$AD_NAME" "env AGORA_HOME='$SANDBOX/plaza' AGORA_OC_URL='$OC1_URL' \
AGORA_OC_PASSWORD='$OC1_PW' python3 '$REPO/agorad/main.py' > '$SANDBOX/agorad.log' 2>&1"
for i in $(seq 1 120); do grep -q "^agorad ready" "$SANDBOX/agorad.log" 2>/dev/null && break; sleep 0.5; done
grep -q "^agorad ready" "$SANDBOX/agorad.log" || { tail -5 "$SANDBOX/agorad.log"; exit 1; }
python3 -c "import json;print(json.load(open('$SANDBOX/plaza/.agora/service.json'))['pid'])" > "$PIDA"
AD_PORT="$(python3 -c "import json;print(json.load(open('$SANDBOX/plaza/.agora/service.json'))['port'])")"
python3 - "$AD_PORT" "$ROOT_TOKEN" <<'PY' || exit 1
import json, sys, urllib.request
req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/health",
                             headers={"X-Agora-Token": sys.argv[2]})
d = json.loads(urllib.request.urlopen(req, timeout=5).read())
assert d.get("enforce_on") is True, f"agorad enforce 缺省应 on: {d}"
print("agorad ready enforce_on=True")
PY
note "agorad 常驻 port=$AD_PORT"

# ---- 5) git 夹具（拒例目标全在 /tmp/opencode 豁免根之外；tmprepo 例外＝专压 tmp 豁免）----
git -C "$WORK" init -q denyrepo && git -C "$WORK/denyrepo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$WORK" init -q offrepo && git -C "$WORK/offrepo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$WORK" init -q proj && (cd "$WORK/proj" && echo base > a.md && git add a.md \
  && git -c user.email=t@t -c user.name=t commit -q -m init)
git -C "$SANDBOX" init -q tmprepo && git -C "$SANDBOX/tmprepo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
mkdir -p "$WORK/plain"

# ---- 6) serve#2（服务停机例专用：空 AGORA_HOME＝无 service.json 无载荷，lift 必败）----
tmux new-session -d -s "$S2_NAME" "env HOME='$SANDBOX/home' XDG_CONFIG_HOME='$SANDBOX/home/.config' \
XDG_STATE_HOME='$SANDBOX/state2' XDG_DATA_HOME='$SANDBOX/data' XDG_CACHE_HOME='$SANDBOX/cache' \
AGORA_HOME='$SANDBOX/plaza2' ZAI_API_KEY='$OC_KEY' \
'$OC_BIN' serve --service --hostname 127.0.0.1 --port 0 > '$SANDBOX/serve2.log' 2>&1"
for i in $(seq 1 60); do [ -f "$SANDBOX/state2/opencode/service.json" ] && break; sleep 0.5; done
python3 -c "import json;print(json.load(open('$SANDBOX/state2/opencode/service.json'))['pid'])" > "$PID2"
python3 - "$SANDBOX/state2/opencode/service.json" <<'PY' || exit 1
import base64, json, sys, time, urllib.request
sj = json.load(open(sys.argv[1]))
auth = "Basic " + base64.b64encode(f"opencode:{sj['password']}".encode()).decode()
for _ in range(60):
    try:
        urllib.request.urlopen(urllib.request.Request(sj["url"] + "/api/info",
                               headers={"Authorization": auth}), timeout=2)
        print("serve2 ready", sj["url"]); sys.exit(0)
    except SystemExit: raise
    except Exception: time.sleep(0.5)
sys.exit("serve2 30s 未就绪")
PY
note "serve#2 就绪（plaza2 空仓）"

# ---- 7) 七例场景（python 驱动：注入/结算/工具面与盘面断言）----
python3 -u - "$SANDBOX" "$WORK" "$REPO" "$AD_PORT" "$ROOT_TOKEN" \
  "$SANDBOX/state/opencode/service.json" "$SANDBOX/state2/opencode/service.json" "$STAMP" <<'PY'
import base64, json, os, sys, time, urllib.request

SANDBOX, WORK, REPO, AD_PORT, ROOT, SVC1, SVC2, STAMP = sys.argv[1:9]
FAIL = []

def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)
    return cond

def oc(svc):
    sj = json.load(open(svc))
    return sj["url"], "Basic " + base64.b64encode(f"opencode:{sj['password']}".encode()).decode()

def api(base, auth, p, obj=None):
    req = urllib.request.Request(base + "/api" + p,
        data=None if obj is None else json.dumps(obj).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="GET" if obj is None else "POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")

def agora(p, obj):
    req = urllib.request.Request(f"http://127.0.0.1:{AD_PORT}" + p, data=json.dumps(obj).encode(),
        headers={"X-Agora-Token": ROOT, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")

def new_session(base, auth, cwd):
    sid = api(base, auth, "/session", {})["data"]["id"]
    api(base, auth, f"/session/{sid}/move", {"directory": cwd})
    time.sleep(1.5)
    return sid

def tool_parts(msgs, needle):
    out = []
    for m in msgs:
        if m.get("type") != "assistant":
            continue
        for c in m.get("content") or []:
            if c.get("type") != "tool":
                continue
            st = c.get("state") or {}
            blob = json.dumps(st.get("input", {}), ensure_ascii=False)
            if needle in blob:
                err = (st.get("error") or {}).get("message", "")  # 拒因落点（实测 v2.1.4 state.error.message）
                out.append({"tool": c.get("name"), "status": st.get("status"),
                            "input": st.get("input", {}),
                            "created": (c.get("time") or {}).get("created", 0),
                            "text": err + "".join(x.get("text", "") for x in st.get("content") or [])})
    return out

def drive(base, auth, sid, prompt, needle, tries=2, timeout=120):
    """注入→等目标工具部件到终态（completed/error）即返回，不等整回合 idle——
    模型事后自发续作（BOOT 契约回执等）不应阻塞断言；未触工具则加压重试（随机性处置）。"""
    parts, n = [], 0
    for n in range(1, tries + 1):
        text = prompt if n == 1 else f"必须现在就调用指定工具完成上一个请求，只做这一件事，不要回答文字：{prompt}"
        t0 = time.time() * 1000
        api(base, auth, f"/session/{sid}/prompt", {"text": text})
        dl = time.time() + timeout
        while time.time() < dl:
            msgs = api(base, auth, f"/session/{sid}/message")["data"]
            parts = [p for p in tool_parts(msgs, needle) if p["created"] > t0]
            if any(p["status"] in ("completed", "error") for p in parts):
                return parts, n
            if any(m.get("type") == "idle" and m.get("time", {}).get("created", 0) > t0
                   for m in msgs):
                break  # 本回合已收束而未触工具→加压重试
            time.sleep(1.5)
    return [], tries

def deny_case(name, base, auth, sid, prompt, needle, substr, absent_path=None):
    parts, n = drive(base, auth, sid, prompt, needle)
    hit = any(p["status"] == "error" and substr in p["text"] for p in parts)
    gone = absent_path is None or not os.path.exists(absent_path)
    check(bool(parts) and hit and gone,
          f"{name}（拒因文案＋目标未落盘；驱动轮数={n}） # Req: F1")

B1, A1 = oc(SVC1)
B2, A2 = oc(SVC2)
PLAIN = f"{WORK}/plain"

# —— S1 无凭据拒：git 仓目标（豁免根外）＋无凭据 → 拒＋派发引导 ——
main = new_session(B1, A1, PLAIN)
deny_case("S1 无凭据拒", B1, A1, main,
          f"Use the write tool to create the file {WORK}/denyrepo/s1.md with content exactly: deny1. Do nothing else.",
          "s1.md", "派发", f"{WORK}/denyrepo/s1.md")

# —— S2 持凭据放行：真 dispatch（BOOT/credential 经 session.prompt 进 cred 缓存）→ env 内写放行 ——
tid = agora("/task_create", {"goal": "t2 e2e allow", "project_dir": f"{WORK}/proj",
                             "verify_pending": True})["task"]
agora("/task_dispatch", {"task": tid})
row = agora("/board", {"task": tid})["task"]
env_dir = row.get("env_dir") or ""
worker = row.get("worker_sid") or ""
check(bool(env_dir) and bool(worker) and os.path.isdir(env_dir)
      and row["state"] in ("dispatched", "running"),
      "S2 dispatch 登记（env_dir＋worker_sid＋状态飞行中） # Req: F1")
cred_in = False
for _ in range(30):
    for m in api(B1, A1, f"/session/{worker}/message")["data"]:
        if m.get("type") == "user" and "[agora] credential" in str(m.get("text", "")):
            cred_in = True
    if cred_in:
        break
    time.sleep(1)
check(cred_in, "S2 credential 消息抵达 worker 会话 # Req: F1")
parts, n = drive(B1, A1, worker, f"Use the write tool to create the file {env_dir}/note.md with content exactly: allow-s2. Do nothing else.", "note.md")
check(any(p["status"] == "completed" for p in parts)
      and os.path.exists(f"{env_dir}/note.md")
      and open(f"{env_dir}/note.md").read().strip() == "allow-s2",
      f"S2 持凭据 cwd∈env 写放行（文件落盘内容相符；驱动轮数={n}） # Req: F1")
row = agora("/board", {"task": tid})
check(row["task"]["state"] in ("dispatched", "running")
      and any(e.get("event") == "task_created" for e in row["events"])
      and any(e.get("event") == "dispatched" for e in row["events"]),
      "S2 agorad 侧断言（任务行＋审计事件链） # Req: F1")

# —— S3 豁免三例 ——
parts, n = drive(B1, A1, main, f"Use the write tool to create the file {PLAIN}/s3a.md with content exactly: allow3a. Do nothing else.", "s3a.md")
check(any(p["status"] == "completed" for p in parts) and os.path.exists(f"{PLAIN}/s3a.md"),
      f"S3a 非 git 目录豁免→放行（驱动轮数={n}） # Req: F1")
parts, n = drive(B1, A1, main, f"Use the write tool to create the file {SANDBOX}/tmprepo/s3b.md with content exactly: allow3b. Do nothing else.", "s3b.md")
check(any(p["status"] == "completed" for p in parts) and os.path.exists(f"{SANDBOX}/tmprepo/s3b.md"),
      f"S3b 临时目录 /tmp/opencode 豁免（git 仓亦放）→放行（驱动轮数={n}） # Req: F1")
agora("/enforce", {"on": False})
parts, n = drive(B1, A1, main, f"Use the write tool to create the file {WORK}/offrepo/s3c.md with content exactly: allow3c. Do nothing else.", "s3c.md")
check(any(p["status"] == "completed" for p in parts) and os.path.exists(f"{WORK}/offrepo/s3c.md"),
      f"S3c enforce off 豁免→放行（驱动轮数={n}） # Req: F1")
agora("/enforce", {"on": True})
ev = open(f"{SANDBOX}/plaza/events.jsonl").read()
check("enforce_off" in ev and "enforce_on" in ev, "S3c enforce 开/关均有审计行 # Req: F1")

# —— S4 自身仓不豁免：写 opencode-agora 仓自身 → 拒（自食狗粮）——
deny_case("S4 自身仓不豁免", B1, A1, main,
          f"Use the write tool to create the file {REPO}/t2-self-{STAMP}.md with content exactly: self. Do nothing else.",
          "t2-self", "派发", f"{REPO}/t2-self-{STAMP}.md")

# —— S5 服务停机默认拒：serve#2（空 AGORA_HOME，lift 必败）→ 拒＋lift 恢复线索 ——
s5 = new_session(B2, A2, PLAIN)
parts, n = drive(B2, A2, s5, f"Use the write tool to create the file {WORK}/denyrepo/s5.md with content exactly: deny5. Do nothing else.", "s5.md")
hit = any(p["status"] == "error" and "派发" in p["text"] and "resetLift" in p["text"] for p in parts)
check(bool(parts) and hit and not os.path.exists(f"{WORK}/denyrepo/s5.md"),
      f"S5 服务停机默认拒＋lift 线索（驱动轮数={n}） # Req: F1")

# —— S6 账本读引导：state/messages 直读→引导；events/wiki 裸读开放 ——
deny_case("S6a tasks.jsonl 直读拒", B1, A1, main,
          f"Use the read tool to read the file {SANDBOX}/plaza/state/tasks.jsonl. Do nothing else.",
          "tasks.jsonl", "账本面")
deny_case("S6b messages 直读拒", B1, A1, main,
          f"Use the read tool to read the file {SANDBOX}/plaza/state/messages/00000001.md. Do nothing else.",
          "00000001.md", "账本面")
parts, n = drive(B1, A1, main, f"Use the read tool to read the file {SANDBOX}/plaza/events.jsonl. Do nothing else.", "events.jsonl")
check(bool(parts) and all(p["status"] != "error" or "账本面" not in p["text"] for p in parts),
      f"S6c events.jsonl 裸读开放（驱动轮数={n}） # Req: F1")
parts, n = drive(B1, A1, main, f"Use the read tool to read the file {SANDBOX}/plaza/wiki/p.md. Do nothing else.", "wiki/p.md")
check(bool(parts) and all(p["status"] != "error" or "账本面" not in p["text"] for p in parts),
      f"S6d wiki 裸读开放（驱动轮数={n}） # Req: F1")

# —— S7 bash 不拦（接受面）：bash 直写 git 仓主检出不被拦，记录在案 ——
parts, n = drive(B1, A1, main, f"Use the bash tool to run exactly this command: echo allow-7 > {WORK}/denyrepo/s7.md. Do nothing else.", "s7.md")
check(any(p["status"] == "completed" for p in parts) and os.path.exists(f"{WORK}/denyrepo/s7.md"),
      f"S7 bash 直写主检出不拦（接受面记录在案；驱动轮数={n}） # Req: F1")

# —— 收尾：任务取消级联（env 回收）＋会话删除 ——
agora("/task_cancel", {"task": tid})
time.sleep(1)
check(not os.path.exists(env_dir), "收尾 task_cancel 级联回收任务 env # Req: F1")
for sid, b, a in ((main, B1, A1), (worker, B1, A1), (s5, B2, A2)):
    try:
        req = urllib.request.Request(b + "/api/session/" + sid,
                                     headers={"Authorization": a}, method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

print(f"T2 driver done: {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
PY
RC=$?

# ---- 8) 停（先行显式清理，残留断言才测得到净态；EXIT trap 兜底幂等）----
tmux kill-session -t "$S1_NAME" 2>/dev/null
tmux kill-session -t "$S2_NAME" 2>/dev/null
tmux kill-session -t "$AD_NAME" 2>/dev/null
for f in "$PID1" "$PID2" "$PIDA"; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
sleep 1.5

# ---- 9) 残留断言（进程/会话/worktree/仓面）----
if python3 - "$WORK/proj" <<'PY'
import subprocess, sys
wt = subprocess.run(["git", "worktree", "list"], cwd=sys.argv[1], capture_output=True, text=True).stdout.strip().splitlines()
br = subprocess.run(["git", "branch", "--list", "*__wt-*"], cwd=sys.argv[1], capture_output=True, text=True).stdout.strip()
sys.exit(0 if len(wt) == 1 and not br else 1)
PY
then note "PASS 残留：任务 worktree/分支清净（worktree list＝1）"; else note "FAIL 残留：worktree 残留"; RC=1; fi
if pgrep -f "agorad/main.py" >/dev/null 2>&1; then note "FAIL 残留：agorad 进程未净"; RC=1; else note "PASS 残留：pgrep agorad 净"; fi
if tmux ls 2>/dev/null | grep -q "t2-oc1\|t2-oc2\|t2-ad"; then note "FAIL 残留：tmux 会话未净"; RC=1; else note "PASS 残留：tmux 无残会话"; fi
if compgen -G "$REPO/t2-*" >/dev/null || [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null | grep '^??' | grep -v 'tests/\|.superpowers/\|.omo/\|.review-loop/' || true)" ]; then
  note "FAIL 残留：e2e 在本仓落盘（S4 拒例失效）"; RC=1; else note "PASS 残留：本仓无 e2e 落盘"; fi
if [ "$RC" -eq 0 ]; then note "ALL GREEN（七例＋残留）"; else note "RED（见上 FAIL 行）"; fi
exit "$RC"
