#!/usr/bin/env bash
# T3 派发链 e2e（F3/F4/F6/F7/F11）：真 opencode（tmux 驻留）＋真 agorad 全链十三场景。
# 隔离同 T6 口径：HOME/XDG_*/AGORA_HOME 全沙箱，绝不触碰本机会话服务；产物 /tmp/opencode。
# 驱动形态（T6 裁定沿袭＋T7 收敛）：serve/agorad 各驻 tmux；服务侧动作经 agorad HTTP
# （root 牌＋task_dispatch 回传的 worker 牌）；worker 真会话干活＝目标即指令（goal 窄化，
# 模型自主落盘/提交），验收＝服务侧状态＋git 事实＋消息库/文件事实，不依赖模型自述，
# 亦不与其自发续作竞速（run7/16 实证：自发性是契约行为，脚本须状态驱动收敛）。
# 场景：S1 全链 / S2 跨仓顺序（B7 拒＋相对路径见新主）/ S3 过程沟通（问答一轮）＋
# 在线 ≤5s 计时＋连续 10 条按序 / 杀 agorad 重启续 / 杀 opencode 服务重启 / 会话死亡
# 重注入（牌重绑链边可达）/ done 崩溃窗口（重启重排不双死）/ 门内 cancel 排队 /
# 非 root enforce 403 / verify 驳回起重。残留断言＋反需求语义扫（F5 弱锚收口）。
# 用法：bash tests/e2e/t3_chain.sh
# Req: F3/F4/F6/F7/F11（锚逐断言标注）
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STAMP="$(date +%m%d%H%M)-$$"
SANDBOX="/tmp/opencode/t3-$STAMP"
WORK="/tmp/agora-t3-$STAMP"
OC_BIN="${OC_BIN:-$HOME/.opencode/bin/opencode}"
S1_NAME="t3-oc-$STAMP"; AD_NAME="t3-ad-$STAMP"

note() { echo "[t3] $*"; }

kill_from_json() {  # kill_from_json <service.json>——pid 字段杀进程（缺失/陈旧静默）
  [ -f "$1" ] && kill "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['pid'])" "$1" 2>/dev/null)" 2>/dev/null
  return 0
}

cleanup() {
  local rc=$?
  tmux kill-session -t "$S1_NAME" 2>/dev/null
  tmux kill-session -t "$AD_NAME" 2>/dev/null
  kill_from_json "$SANDBOX/state/opencode/service.json"
  kill_from_json "$SANDBOX/plaza/.agora/service.json"
  sleep 1
  if [ "$rc" -eq 0 ]; then
    rm -rf "$SANDBOX" "$WORK"
  else
    note "非绿保留现场：$SANDBOX $WORK（查 serve/agorad/install 日志）"
  fi
  note "清理完成（rc=$rc）"
}
trap cleanup EXIT

# ---- 0) 模型凭据（本机自适应；无凭据 SKIP=3，同 T6）----
mkdir -p "$SANDBOX" "$WORK"
chmod 700 "$SANDBOX"   # 红跑留存（cleanup 跳过 rm）时沙箱内真实 API key 仅本机本用户可读
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
OC_KEY="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['key'])" "$CFG_JSON")"
PROV="$(python3 -c "import json;print(json.load(open('$CFG_JSON'))['prov'])")"
MODEL="$(python3 -c "import json;print(json.load(open('$CFG_JSON'))['model'])")"
BASEURL="$(python3 -c "import json;print(json.load(open('$CFG_JSON'))['baseURL'])")"
note "model=$MODEL"

# ---- 1) 隔离 opencode 配置＋插件（install.sh 稍后并入 mcp 段＋root 牌）----
mkdir -p "$SANDBOX"/{home,data,state,cache}
mkdir -p "$SANDBOX"/plaza/state/messages "$SANDBOX"/plaza/wiki "$SANDBOX"/plaza/.agora
# 账本夹具必须先于 agorad 首启：Store 启动扫 messages/ 恢复 msg-id 计数（max+1），
# 后写夹具侵占 id 序→write_message FileExistsError→通知链 400（T6 排障志②）
echo "msg-fixture" > "$SANDBOX/plaza/state/messages/00000001.md"
echo "wiki-fixture" > "$SANDBOX/plaza/wiki/p.md"
touch "$SANDBOX/plaza/events.jsonl"
OC_DIR="$SANDBOX/home/.config/opencode"   # opencode 按 XDG_CONFIG_HOME 解析（T6 排障志①）
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

# ---- 2) serve（tmux 驻留；首轮 port 0，F6-b 重启复用记录端口——探针实证 password 稳定）----
tmux new-session -d -s "$S1_NAME" "env HOME='$SANDBOX/home' XDG_CONFIG_HOME='$OC_DIR' \
XDG_STATE_HOME='$SANDBOX/state' XDG_DATA_HOME='$SANDBOX/data' XDG_CACHE_HOME='$SANDBOX/cache' \
AGORA_HOME='$SANDBOX/plaza' ZAI_API_KEY='$OC_KEY' AGORAD_SCRIPT='/nonexistent/t3-no-resurrect' \
'$OC_BIN' serve --service --hostname 127.0.0.1 --port 0 > '$SANDBOX/serve.log' 2>&1"
for i in $(seq 1 60); do [ -f "$SANDBOX/state/opencode/service.json" ] && break; sleep 0.5; done
python3 - "$SANDBOX/state/opencode/service.json" <<'PY' || exit 1
import base64, json, sys, time, urllib.request
sj = json.load(open(sys.argv[1]))
auth = "Basic " + base64.b64encode(f"opencode:{sj['password']}".encode()).decode()
for _ in range(60):
    try:
        urllib.request.urlopen(urllib.request.Request(sj["url"] + "/api/info",
                               headers={"Authorization": auth}), timeout=2)
        print("serve ready", sj["url"]); sys.exit(0)
    except SystemExit: raise
    except Exception: time.sleep(0.5)
sys.exit("serve 30s 未就绪")
PY
OC1_URL="$(python3 -c "import json;print(json.load(open('$SANDBOX/state/opencode/service.json'))['url'])")"
OC1_PW="$(python3 -c "import json;print(json.load(open('$SANDBOX/state/opencode/service.json'))['password'])")"
note "serve $OC1_URL"

# ---- 3) install.sh（隔离装配＋root 牌接管＋冒烟）----
HOME="$SANDBOX/home" AGORA_HOME="$SANDBOX/plaza" \
AGORA_OC_URL="$OC1_URL" AGORA_OC_PASSWORD="$OC1_PW" bash "$REPO/install.sh" \
  > "$SANDBOX/install.log" 2>&1 || { tail -5 "$SANDBOX/install.log"; exit 1; }
ROOT_TOKEN="$(cat "$SANDBOX/plaza/.agora/root.token")"
note "install.sh 完成"

# ---- 4) 常驻 agorad（health tick 2s——探活/重注入场景的确定性节奏）----
ad_cmd() { echo "env AGORA_HOME='$SANDBOX/plaza' AGORA_OC_URL='$OC1_URL' AGORA_OC_PASSWORD='$OC1_PW' \
AGORA_HEALTH_INTERVAL=2 python3 '$REPO/agorad/main.py' > '$SANDBOX/agorad.log' 2>&1"; }
tmux new-session -d -s "$AD_NAME" "$(ad_cmd)"
for i in $(seq 1 120); do grep -q "^agorad ready" "$SANDBOX/agorad.log" 2>/dev/null && break; sleep 0.5; done
grep -q "^agorad ready" "$SANDBOX/agorad.log" || { tail -5 "$SANDBOX/agorad.log"; exit 1; }
AD_PORT="$(python3 -c "import json;print(json.load(open('$SANDBOX/plaza/.agora/service.json'))['port'])")"
note "agorad 常驻 port=$AD_PORT（tick=2s）"

# ---- 5) git 夹具（9 仓：S1/S2A/S2B/F6×2/F6重注入/F7崩溃窗/F11门内cancel/R25A拍人；仓级身份免 -c）----
mk_repo() { git -C "$WORK" init -q "$1" && git -C "$WORK/$1" config user.email w@t \
  && git -C "$WORK/$1" config user.name w && git -C "$WORK/$1" commit -q --allow-empty -m init; }
mk_repo proj; mk_repo repoA; mk_repo repoB; mk_repo repoD; mk_repo repoE
mk_repo repoR; mk_repo repoX; mk_repo repoY; mk_repo repoM
note "夹具 9 仓就绪"

# ---- 6) 十三场景（python 驱动）----
T3_OC_KEY="$OC_KEY" T3_PROV="$PROV" python3 -u - "$SANDBOX" "$WORK" "$REPO" "$ROOT_TOKEN" \
  "$SANDBOX/state/opencode/service.json" "$OC1_URL" "$OC1_PW" "$OC_BIN" "$STAMP" <<'PY'
import base64, json, os, pathlib, sqlite3, subprocess, sys, time, urllib.error, urllib.request

SANDBOX, WORK, REPO, ROOT, SVC, OC_URL, OC_PW, OC_BIN, STAMP = sys.argv[1:10]
OC_KEY = os.environ["T3_OC_KEY"]
PROV = os.environ["T3_PROV"]
PLAZA = f"{SANDBOX}/plaza"
AD_LOG = f"{SANDBOX}/agorad.log"
AD_NAME = f"t3-ad-{STAMP}"
OC_NAME = f"t3-oc-{STAMP}"
FAIL = []

def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)
    return cond

# ---- HTTP 面 ----
def oc_auth():
    sj = json.load(open(SVC))  # 每调用重读：serve 重启可轮换 password（排障志#2）
    return sj["url"], "Basic " + base64.b64encode(f"opencode:{sj['password']}".encode()).decode()

def api(p, obj=None, base=None, auth=None, method=None):
    if base is None:
        base, auth = oc_auth()
    hdrs = {"Authorization": auth}
    if obj is not None:
        hdrs["Content-Type"] = "application/json"  # 空 body 带 Content-Type 的 DELETE→400（实测）
    req = urllib.request.Request(base + "/api" + p,
        data=None if obj is None else json.dumps(obj).encode(),
        headers=hdrs,
        method=method or ("GET" if obj is None else "POST"))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")

def ad_port():
    return json.load(open(f"{PLAZA}/.agora/service.json"))["port"]

def ad_pid():
    return json.load(open(f"{PLAZA}/.agora/service.json"))["pid"]

def agora(p, obj=None, token=ROOT):
    """agorad JSON 口：(code, body)——错误码也是值（403/409/401 断言面）。"""
    req = urllib.request.Request(f"http://127.0.0.1:{ad_port()}" + p,
        data=None if obj is None else json.dumps(obj).encode(),
        headers={"X-Agora-Token": token, "Content-Type": "application/json"},
        method="GET" if p == "/health" else "POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# ---- 进程面（tmux/kill 由驱动直管——F6 场景本体）----
def start_agorad():
    subprocess.run(["tmux", "new-session", "-d", "-s", AD_NAME,
                    f"env AGORA_HOME='{PLAZA}' AGORA_OC_URL='{OC_URL}' AGORA_OC_PASSWORD='{OC_PW}' "
                    f"AGORA_HEALTH_INTERVAL=2 python3 '{REPO}/agorad/main.py' > '{AD_LOG}' 2>&1"])
    dl = time.time() + 60
    while time.time() < dl:
        ready = False
        try:
            ready = "agorad ready" in open(AD_LOG).read()
        except OSError:
            pass
        if ready:  # service.json 在 ready 行前已重写（main.py 序）
            try:
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{ad_port()}/health",
                                           headers={"X-Agora-Token": ROOT}), timeout=2)
                return
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("agorad 60s 未就绪")

def kill_agorad():
    os.kill(ad_pid(), 9)
    subprocess.run(["tmux", "kill-session", "-t", AD_NAME])
    try:
        os.unlink(AD_LOG)  # 防旧日志 ready 行误判新实例就绪（T6 排障志③同族）
    except OSError:
        pass
    time.sleep(0.5)

def proc_gone(pid, timeout=5):
    """杀后进程死亡判定：/proc 消失或 stat 变 Z（SIGKILL 生效后非死即 zombie 待收割——
    tmux 收割延展会拖住 /proc 消失，run9 实证）。"""
    dl = time.time() + timeout
    while time.time() < dl:
        if not os.path.exists(f"/proc/{pid}"):
            return True
        try:
            state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return True
        except (OSError, IndexError):
            return True
        time.sleep(0.2)
    return False

def kill_serve():
    os.kill(json.load(open(SVC))["pid"], 9)
    subprocess.run(["tmux", "kill-session", "-t", OC_NAME])
    try:
        os.unlink(f"{SANDBOX}/serve.log")
    except OSError:
        pass
    time.sleep(1)

def start_serve(port):
    subprocess.run(["tmux", "new-session", "-d", "-s", OC_NAME,
                    f"env HOME='{SANDBOX}/home' XDG_CONFIG_HOME='{SANDBOX}/home/.config' "
                    f"XDG_STATE_HOME='{SANDBOX}/state' XDG_DATA_HOME='{SANDBOX}/data' "
                    f"XDG_CACHE_HOME='{SANDBOX}/cache' AGORA_HOME='{PLAZA}' "
                    f"ZAI_API_KEY='{OC_KEY}' AGORAD_SCRIPT='/nonexistent/t3-no-resurrect' "
                    f"'{OC_BIN}' serve --service --hostname 127.0.0.1 --port {port} "
                    f"> '{SANDBOX}/serve.log' 2>&1"])
    dl = time.time() + 60
    while time.time() < dl:
        try:
            u, a = oc_auth()
            api("/info", base=u, auth=a)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("serve 60s 未就绪")

# ---- 会话/任务驱动 ----
def seed_credential():
    """模型凭据注入沙箱凭据库：无凭据 provider 被 opencode 静默回退免费网关
    （big-pickle），配额 429 中途断链（run12 实证 provider.free-limit）。
    ro 附读全局库（被本会话持锁）；只拷目标 provider 一行（密钥最小面：红跑留存沙箱
    不携带其他 provider 的 key），全列拷贝（schema 不摸索，run13 实证 NOT NULL label）。"""
    g = os.path.expanduser("~/.local/share/opencode/opencode.db")
    src = sqlite3.connect(f"file:{g}?mode=ro", uri=True, timeout=5)
    cols = [r[1] for r in src.execute("PRAGMA table_info(credential)")]
    rows = src.execute(f"SELECT {','.join(cols)} FROM credential WHERE integration_id=?",
                       (PROV,)).fetchall()
    src.close()
    dst = sqlite3.connect(f"{SANDBOX}/data/opencode/opencode.db", timeout=5)
    ph = ",".join("?" * len(cols))
    dst.executemany(f"INSERT OR REPLACE INTO credential ({','.join(cols)}) VALUES ({ph})", rows)
    dst.commit()
    dst.close()

def user_texts(sid, needle):
    msgs = [m for m in api(f"/session/{sid}/message")["data"]
            if m.get("type") == "user" and needle in str(m.get("text", ""))]
    msgs.reverse()  # API 列表新在前（run2 实测 [9..0]）——先反转再稳定排序，并列 ms 保持注入序
    msgs.sort(key=lambda m: (m.get("time") or {}).get("created", 0))
    return [str(m.get("text", "")) for m in msgs]

def wait_idle(sid, after_ms, timeout=120):
    """等会话收束（idle 部件晚于 after_ms）——prompt 排队期与模型自发续作解耦。"""
    dl = time.time() + timeout
    while time.time() < dl:
        for m in api(f"/session/{sid}/message")["data"]:
            if m.get("type") == "idle" and (m.get("time") or {}).get("created", 0) > after_ms:
                return True
        time.sleep(1)
    return False

def wait_file(path, content, timeout=240):
    """目标即指令的验收原语：worker 真会话自主落盘（作者/轮次无关），只认文件事实。"""
    dl = time.time() + timeout
    while time.time() < dl:
        if os.path.exists(path):
            try:
                if open(path).read().strip() == content:
                    return True
            except OSError:
                pass
        time.sleep(2)
    return False

def wait_state(tid, states, timeout=150):
    dl = time.time() + timeout
    while time.time() < dl:
        if row(tid)["state"] in states:
            break
        time.sleep(1)
    return row(tid)

def wait_in_gate(tid, timeout=20):
    dl = time.time() + timeout
    while time.time() < dl:
        if row(tid).get("in_gate"):
            return True
        time.sleep(0.5)
    return False

def detail(tid):
    return agora("/board", {"task": tid})[1]

def row(tid):
    return detail(tid)["task"]

def evs(tid):
    return [e["event"] for e in detail(tid)["events"]]

def subseq(need, have):
    it = iter(have)
    return all(x in it for x in need)

def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)

def git(args, cwd):
    r = sh(["git", *args], cwd)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()

def commit_anchor(env, msg):
    """驱动侧 git 机械（确定性）：提交 env 变更＋写评审锚（未跟踪文件，锚=分支 HEAD）。
    无变更可提交（worker 落盘已单独断言把关）不在此硬崩，保后续场景可跑。"""
    git(["add", "-A"], env)
    subprocess.run(["git", "commit", "-qm", msg], cwd=env, capture_output=True, text=True)
    sha = git(["rev-parse", "HEAD"], env)
    os.makedirs(f"{env}/.review-loop", exist_ok=True)
    with open(f"{env}/.review-loop/.last-reviewed", "w") as f:
        f.write(sha)
    return sha

def post_done(tid, token, sha):
    """done 受理（state 守卫：worker 模型自发抢跑已 done 则不再发——应答归一 accepted，
    收尾断言由状态事实把关）。"""
    if row(tid)["state"] == "done":
        return 200, {"result": "accepted"}
    return agora("/post", {"task": tid, "kind": "done", "body": sha}, token)

def env_of(tid):
    return row(tid)["env_dir"]

def git_clean(repo):
    wt = len(git(["worktree", "list"], repo).splitlines())
    br = git(["branch", "--list", "*__wt-task-*"], repo)
    return wt == 1 and not br

def dispatch(goal, repo, after=None):
    """建＋派一体的场景件（goal 即 worker 指令——提示词窄化走任务页）：回 (tid, wtok, env, wsid)。"""
    body = {"goal": goal, "project_dir": f"{WORK}/{repo}", "verify_pending": True}
    if after:
        body["after"] = after
    code, body = agora("/task_create", body)
    assert code == 200, body
    tid = body["task"]
    code, body = agora("/task_dispatch", {"task": tid})
    assert code == 200, f"dispatch {tid}: {body}"
    return tid, body["token"], env_of(tid), row(tid)["worker_sid"]

def store_msgs():
    """消息库直读（账本面）：milestone 投递对象按设计断言（worker 侧，不经补拉游标）。"""
    out = []
    d = pathlib.Path(PLAZA) / "state" / "messages"
    for p in sorted(d.glob("*.md")):
        head, _, body = p.read_text().partition("\n\n")
        fm = dict(x.split(": ", 1) for x in head.splitlines() if ": " in x)
        if "kind" not in fm:  # 夹具/撕裂件无头——同 Store 读容错跳过
            continue
        fm["body"] = body
        out.append(fm)
    return out

# ---- 模型凭据种子＋serve/agorad 重启（必须在首个任务前）----
seed_credential()
_p = OC_URL.rsplit(":", 1)[1]
kill_serve()
start_serve(_p)
OC_PW = json.load(open(SVC))["password"]  # serve 重启可轮换 password（repro 实测）——agorad 同步重启
kill_agorad()
start_agorad()

# ============================================================
# S0 F23 派发硬闸：无 verify 无旗 dispatch 必拒＋专属审计行（快速路见 S1 后的原子登记对照）
# ============================================================
code, body = agora("/task_create", {"goal": "F23 gate probe",
                                    "project_dir": f"{WORK}/repoA"})
assert code == 200, body  # 同 dispatch() 口径：建单失败带因断言，非 KeyError 裸栈
tidG = body["task"]
code, body = agora("/task_dispatch", {"task": tidG})
check(code == 403 and "verify 未登记" in body.get("error", ""),
      f"F23 硬闸：verify 未登记派发必拒（{code} {body.get('error', '')[:40]}） # Req: F23")
check("dispatch_rejected_verify_missing" in evs(tidG), "F23 拒因审计留行 # Req: F23")
check(row(tidG)["state"] == "todo" and not row(tidG).get("env_dir")
      and "dispatched" not in evs(tidG),
      "F23 拒绝零副作用（未建环境未派发） # Req: F23")
code, _ = agora("/task_cancel", {"task": tidG})
check(code == 200 and row(tidG)["state"] == "cancelled",
      f"S0 收尾：gate 探针任务取消净（{code} {row(tidG)['state']}） # Req: F23")

# ============================================================
# S1 全链：建→派→worker 真活→verify 提案/登记→done→门→merged→closed
# ============================================================
tid1, wtok1, env1, wsid1 = dispatch(
    "Create a file named feature.md containing exactly one line: s1-feature. Commit it with git.",
    "proj")
check(tid1.startswith("gt-") and wtok1 and env1 and wsid1
      and row(tid1)["state"] == "dispatched" and os.path.isdir(env1),
      "S1 dispatch（worker 牌回传＋env worktree＋真会话登记） # Req: F3")
check(api(f"/session/{wsid1}")["data"]["id"] == wsid1,
      "S1 worker 会话真实存在于 opencode # Req: F3")

file_ok = wait_file(f"{env1}/feature.md", "s1-feature")
check(file_ok, "S1 worker 真会话 env 内写盘（goal 即指令，自主落盘） # Req: F3")
check(not os.path.exists(f"{WORK}/proj/feature.md"),
      "S1 主检出未污染（隔离环境内作业） # Req: F3")
try:  # harness 自检：免费网关回退已根治（providerID 落在配置的 zai，run12 实证 429 断链）
    _db = sqlite3.connect(f"file:{SANDBOX}/data/opencode/opencode.db?mode=ro", uri=True)
    _row = _db.execute("SELECT data FROM session_message WHERE session_id=? AND type='assistant' LIMIT 1",
                       (wsid1,)).fetchone()
    _db.close()
    _prov = (json.loads(_row[0]).get("model") or {}).get("providerID") if _row else None
except Exception:
    _prov = None
check(_prov == PROV, f"沙箱模型 provider=配置值（实际 {_prov}）")

agora("/post", {"task": tid1, "kind": "verify", "body": "test -f feature.md"}, wtok1)
mail = agora("/check_mail")[1]["messages"]
check(any(m["kind"] == "verify" and "test -f feature.md" in m["body"] for m in mail),
      "S1 worker verify 提案落库，main 补拉可见（main 非 ses 注入走留库口径） # Req: F2")
agora("/task_update", {"task": tid1, "verify_cmd": "test -f feature.md"})
check(row(tid1)["verify"]["state"] == "registered",
      "S1 verify 登记（registered＋锁定） # Req: F2")

sha1 = commit_anchor(env1, "s1 work")
post_done(tid1, wtok1, sha1)
t = wait_state(tid1, {"closed"})
check(subseq(["task_created", "dispatched", "running", "done", "merged", "closed"], evs(tid1)),
      f"S1 审计事件链全（实际：{evs(tid1)}） # Req: F8")
check(t["state"] == "closed" and not os.path.exists(env1) and git_clean(f"{WORK}/proj"),
      "S1 门走完 closed＋env 回收＋worktree/分支净 # Req: F7")
mev = [e for e in detail(tid1)["events"] if e["event"] == "merged"]
check(git(["rev-parse", "HEAD"], f"{WORK}/proj") == mev[-1].get("commit")
      and sh(["git", "show", "HEAD:feature.md"], f"{WORK}/proj").stdout.strip() == "s1-feature",
      "S1 ff 合并 git 事实（HEAD==merged commit＋字节相符） # Req: F7")
check(bool(mev) and mev[-1].get("commit") == t.get("commit"),
      "S1 merged 事件带 commit 证据 # Req: F8")
mail = agora("/check_mail")[1]["messages"]
check(any(m["kind"] == "done" and m.get("from") == wsid1 for m in mail),
      "S1 派发方收 done 回执（worker 上行链边→留库补拉面） # Req: F4")
ms = [m for m in store_msgs()
      if m["kind"] == "milestone" and m.get("task") == tid1
      and m.get("to") == wsid1 and sha1 in m["body"]]
check(bool(ms) or any(m["kind"] == "milestone" and m.get("task") == tid1
                      and m.get("to") == wsid1 and (t.get("commit") or "") in m["body"]
                      for m in store_msgs()),
      "S1 merged 里程碑按设计投 worker（设计 §4.5 绿→通知 worker） # Req: F7")

# ============================================================
# F4/S3：≤5s 计时→worker 应答（链边上行）→连续 10 条按序（载体＝repoA worker）
# ============================================================
tidA, wtokA, envA, wsidA = dispatch(
    "Create a file named merged.md containing exactly one line: s2-marker. Commit it with git.",
    "repoA")

qbody = f"s3-q-{STAMP} what is the marker"
t0 = time.time() * 1000
agora("/post", {"task": tidA, "kind": "question", "body": qbody})
hit, elapsed = False, 999.0
while time.time() * 1000 - t0 < 30000:
    for m in api(f"/session/{wsidA}/message")["data"]:
        if m.get("type") == "user" and qbody in str(m.get("text", "")):
            # 到达时延＝opencode 消息 created 与注入前时刻差（ms 级，剔除轮询/取列开销）
            hit, elapsed = True, ((m.get("time") or {}).get("created", 0) - t0) / 1000.0
            break
    if hit:
        break
    time.sleep(0.2)
check(hit and elapsed <= 5.0, f"S3 问题在线注入实测 {elapsed:.2f}s ≤5s # Req: F4")

# worker 应答走服务面（与 MCP agora_post 同端点同牌语义）。注：opencode v2.1.4 的 MCP
# spawn 间歇缺失（run8 在场成功、run10/11 全会话无 agora 工具——排障志#9），链边/留库/
# 补拉语义由本断言把关；F1 附牌面由插件单测＋T5 探针锚定。
agora("/post", {"task": tidA, "kind": "answer", "body": "s3-answer-body"}, wtokA)
ans = []
dl = time.time() + 30
while time.time() < dl and not ans:
    ans = [m for m in agora("/check_mail")[1]["messages"]
           if m["kind"] == "answer" and m["body"] == "s3-answer-body"]
    time.sleep(1)
check(bool(ans) and ans[0]["from"] == wsidA,
      "S3 worker 应答→链边上行→留库→main 补拉（from==worker_sid） # Req: F4")

for i in range(10):
    agora("/post", {"task": tidA, "kind": "chat", "body": f"seq-{i}"})
dl = time.time() + 300
seqs = []
while time.time() < dl:
    seqs = []
    for x in user_texts(wsidA, f"[agora] task {tidA} chat from main"):
        for i in range(10):
            if f"seq-{i}" in x:
                seqs.append(i)
                break
    if seqs == list(range(10)):
        break
    time.sleep(2)
check(seqs == list(range(10)), f"连续 10 条按序全到（实际序：{seqs}） # Req: F4")

# ============================================================
# S2 跨仓顺序：B7 前驱拒→A merged→B 才派发→后仓相对路径见新主（驳回起重折入 B）
# ============================================================
code, body = agora("/task_create", {"goal": "Create a file named seen.md containing the exact content "
                                    "of the file ../repoA/merged.md (relative path from your current "
                                    "directory). Commit it with git.",
                                    "project_dir": f"{WORK}/repoB", "after": tidA,
                                    "verify_pending": True})
tidB = body["task"]
code, body = agora("/task_dispatch", {"task": tidB})
check(code == 403 and "after" in body.get("error", ""),
      f"S2 B7 前驱未终态派发必拒（{code} {body.get('error', '')[:40]}） # Req: F3")
check("dispatch_rejected" in evs(tidB), "S2 拒因审计留行 # Req: F3")

file_ok = wait_file(f"{envA}/merged.md", "s2-marker")
check(file_ok, f"S2-A worker env 写盘（goal 驱动自主落盘） # Req: F3")
agora("/post", {"task": tidA, "kind": "verify", "body": "test -f merged.md"}, wtokA)
agora("/check_mail")
agora("/task_update", {"task": tidA, "verify_cmd": "test -f merged.md"})
shaA = commit_anchor(envA, "s2a work")
post_done(tidA, wtokA, shaA)
wait_state(tidA, {"closed"})
check(row(tidA)["state"] == "closed" and os.path.exists(f"{WORK}/repoA/merged.md")
      and open(f"{WORK}/repoA/merged.md").read().strip() == "s2-marker",
      "S2-A merged：前仓主检出呈现新形态（git 事实） # Req: F3")

code, body = agora("/task_dispatch", {"task": tidB})
wtokB = body.get("token", "")
envB = env_of(tidB); wsidB = row(tidB)["worker_sid"]
merged_ts = [e["ts"] for e in detail(tidA)["events"] if e["event"] == "merged"][-1]
disp_ts = [e["ts"] for e in detail(tidB)["events"] if e["event"] == "dispatched"][-1]
check(code == 200 and disp_ts > merged_ts,
      "S2 前仓 merged 后后仓才 dispatched（时序事实） # Req: F3")

# —— verify 驳回起重（F2）——
agora("/post", {"task": tidB, "kind": "verify", "body": "test -f nothing"}, wtokB)
agora("/check_mail")
code, _ = agora("/post", {"task": tidB, "kind": "verify_reject",
                          "body": f"驳回：判据不成立，重报（{STAMP}）"})
check(code == 200, "S2 驳回下达（dispatcher→worker 下行） # Req: F2")
t_rej = time.time() * 1000
hit = False
dl = time.time() + 30
while time.time() < dl and not hit:
    hit = any(STAMP in x for x in user_texts(wsidB, "[agora]"))
    time.sleep(1)
check(hit, "S2 驳回实时注入 worker 会话 # Req: F2")
wait_idle(wsidB, t_rej)  # 等模型消化驳回再续——同刻排队的 prompt 曾诱发自发 done 竞态（run7 实证）
agora("/post", {"task": tidB, "kind": "verify", "body": "test -f seen.md"}, wtokB)
agora("/check_mail")
agora("/task_update", {"task": tidB, "verify_cmd": "test -f seen.md"})
check(row(tidB)["verify"]["state"] == "registered",
      "S2 重报后登记（驳回起重闭环） # Req: F2")
code, _ = agora("/task_update", {"task": tidB, "verify_cmd": "test -f seen.md"}, wtokB)
check(code == 403, "S2 worker 改 verify 必拒（登记锁定） # Req: F2")

# worker 落盘（goal 已含相对路径采集指令）＋确定性收尾
seen_ok = wait_file(f"{envB}/seen.md", "s2-marker", timeout=420)
check(seen_ok, "S2-B worker 经相对路径见前仓新主检出（内容相符） # Req: F3")
dl = time.time() + 180  # 收敛等待：模型自发 done→门，或由驱动提交后 done——状态驱动收尾（作者无关）
while time.time() < dl and row(tidB)["state"] == "running":
    time.sleep(2)
if row(tidB)["state"] in ("running", "dispatched", "verify_failed", "reopened"):
    shaB = commit_anchor(envB, "s2b work")
    post_done(tidB, wtokB, shaB)
wait_state(tidB, {"closed"}, timeout=240)
head_seen = sh(["git", "show", "HEAD:seen.md"], f"{WORK}/repoB").stdout.strip()
check(row(tidB)["state"] == "closed" and head_seen == "s2-marker"
      and not os.path.exists(envB),
      "S2-B merged/closed＋git 事实（HEAD:seen.md 字节相符＋env 回收） # Req: F3")

# —— 非 root enforce 403 ——
code, _ = agora("/enforce", {"on": False}, wtokB)
code2, hbody = agora("/health")
check(code == 403 and hbody.get("enforce_on") is True,
      f"非 root enforce 必拒且旗不动（{code}，enforce_on 仍 True） # Req: F1")
evfile = open(f"{PLAZA}/events.jsonl").read()
check('"tool": "enforce"' in evfile and "auth_denied" in evfile,
      "非 root enforce 拒绝审计留行 # Req: F1")

# ============================================================
# F6-a 杀 agorad 重启续：in-flight 跨重启状态一致＋续跑（存活不重注入）
# ============================================================
tidD, wtokD, envD, wsidD = dispatch(
    "Create a file named d_resume.md containing exactly one line: d-resume. Commit it with git.",
    "repoD")
file_ok = wait_file(f"{envD}/d_resume.md", "d-resume")
check(file_ok, "F6-a 重启前 worker 真活（goal 驱动自主落盘） # Req: F6")
old_pid = ad_pid()
kill_agorad()
check(proc_gone(old_pid), "F6-a agorad 已 kill -9 # Req: F6")
start_agorad()
check(agora("/health")[0] == 200 and row(tidD)["state"] in ("dispatched", "running")
      and row(tidD)["worker_sid"] == wsidD and os.path.isdir(envD),
      "F6-a 重启后状态一致（任务/worker/环境全恢复） # Req: F6")
check("reinjected" not in evs(tidD), "F6-a worker 存活→不重注入（探活口径） # Req: F6")
code, _ = agora("/post", {"task": tidD, "kind": "verify", "body": "x"}, wtokD)
check(code == 200, "F6-a worker 牌跨重启有效（持久化） # Req: F6")
agora("/task_update", {"task": tidD, "verify_cmd": "test -f d_resume.md"})
shaD = commit_anchor(envD, "d work")
post_done(tidD, wtokD, shaD)
wait_state(tidD, {"closed"})
check(row(tidD)["state"] == "closed" and sh(["git", "show", "HEAD:d_resume.md"],
      f"{WORK}/repoD").stdout.strip() == "d-resume",
      "F6-a in-flight 续跑至 merged/closed（git 事实） # Req: F6")

# ============================================================
# F6-b 杀 opencode 服务重启：同端口重启→tick 探测→链不散（会话持久化实测口径）
# ============================================================
tidE, wtokE, envE, wsidE = dispatch(
    "Create a file named resumed.md containing exactly one line: e-resume. Commit it with git.",
    "repoE")
port = OC_URL.rsplit(":", 1)[1]
kill_serve()
start_serve(port)
u, a = oc_auth()
check(OC_URL == u, f"F6-b serve 同 URL 重启（{u}） # Req: F6")
check(api(f"/session/{wsidE}", base=u, auth=a)["data"]["id"] == wsidE,
      "F6-b 会话持久化（v2.1.4 实测）：worker 会话跨重启存活 # Req: F6")
time.sleep(5)  # ≥2 个 health tick：探活判定面（旧 agorad 凭据若被轮换→异常免判，同安全向）
check("reinjected" not in evs(tidE) and row(tidE)["state"] in ("dispatched", "running"),
      "F6-b tick 探测存活→链不散、状态一致 # Req: F6")
# serve 重启可随机轮换 service password（实测两态）——运维面恢复＝agorad 重取发现凭据重启；
# 链本身靠持久化无损续跑（F6：状态一致＋in-flight 续跑）
OC_PW = json.load(open(SVC))["password"]
kill_agorad()
start_agorad()
api(f"/session/{wsidE}/prompt",
    {"text": "Continue your task now: create resumed.md containing exactly one line e-resume, then commit it with git."})
file_ok = wait_file(f"{envE}/resumed.md", "e-resume")
check(file_ok, "F6-b worker 会话重启后续作（续跑落盘） # Req: F6")
agora("/post", {"task": tidE, "kind": "verify", "body": "x"}, wtokE)
agora("/task_update", {"task": tidE, "verify_cmd": "test -f resumed.md"})
shaE = commit_anchor(envE, "e work")
post_done(tidE, wtokE, shaE)
wait_state(tidE, {"closed"})
check(row(tidE)["state"] == "closed" and sh(["git", "show", "HEAD:resumed.md"],
      f"{WORK}/repoE").stdout.strip() == "e-resume",
      "F6-b 链走完全程（merged/closed＋git 事实） # Req: F6")

# ============================================================
# F6-c 会话死亡重注入：删会话→tick 探活→reinject（新 sid＋牌重绑＋凭据重发）
# ============================================================
tidR, wtokR, envR, wsidR = dispatch(
    "Create a file named reinject.md containing exactly one line: reinject-ok. Commit it with git.",
    "repoR")
api(f"/session/{wsidR}", method="DELETE")   # 会话死亡（服务正常）
new_sid = wsidR
dl = time.time() + 20
while time.time() < dl:
    new_sid = row(tidR)["worker_sid"]
    if new_sid != wsidR and "reinjected" in evs(tidR):
        break
    time.sleep(1)
check(new_sid != wsidR and "reinjected" in evs(tidR),
      f"F6-c tick 探活死会话→reinject（{wsidR}→{new_sid}） # Req: F6")
cred = []
dl = time.time() + 30
while time.time() < dl and not cred:
    cred = [x for x in user_texts(new_sid, "[agora] credential") if "token:" in x]
    time.sleep(1)
check(bool(cred), "F6-c 新会话收到 BOOT＋凭据重发 # Req: F6")
file_ok = wait_file(f"{envR}/reinject.md", "reinject-ok")
check(file_ok, "F6-c 新会话持重发凭据 env 写盘（goal 驱动自主落盘） # Req: F6")
shaR = commit_anchor(envR, "r work")
agora("/task_update", {"task": tidR, "verify_cmd": "test -f reinject.md"})
code, body = post_done(tidR, wtokR, shaR)   # 旧牌（rebind 后 identify=新 sid）
t = wait_state(tidR, {"closed"})
check(code == 200 and body.get("result") == "accepted" and t["state"] == "closed",
      "F6-c 旧牌重绑链边可达：done 受理走完整门 # Req: F6")
mail = agora("/check_mail")[1]["messages"]
check(any(m["kind"] == "done" and m["from"] == new_sid for m in mail),
      "F6-c done 消息 from=新 sid（上行链边按新绑定解析） # Req: F4")

# ============================================================
# F7 done 崩溃窗口：门中 kill -9→重启重排→verify 幂等重跑→单次合并不双死
# ============================================================
tidX, wtokX, envX, wsidX = dispatch(
    "Create a file named win.md containing exactly one line: x-win. Commit it with git.",
    "repoX")
file_ok = wait_file(f"{envX}/win.md", "x-win")
check(file_ok, f"F7 窗前 worker 真活（goal 驱动自主落盘） # Req: F7")
agora("/task_update", {"task": tidX, "verify_cmd": "sleep 4 && test -f win.md"})
shaX = commit_anchor(envX, "x work")
post_done(tidX, wtokX, shaX)
check(row(tidX)["state"] == "done" and wait_in_gate(tidX),
      "F7 done 受理入门（in_gate 置位） # Req: F7")
kill_agorad()   # 门体（sleep 4 verify）运行中崩溃
start_agorad()
t = wait_state(tidX, {"closed"})
cnt = lambda name: evs(tidX).count(name)
check(t["state"] == "closed" and cnt("done") == 1 and cnt("merged") == 1 and cnt("closed") == 1
      and cnt("verify_failed") == 0 and cnt("cancelled") == 0,
      f"F7 重启重排门体幂等：单 done/merged/closed，零 verify_failed/cancelled"
      f"（done={cnt('done')} merged={cnt('merged')} closed={cnt('closed')}） # Req: F7")
check(sh(["git", "show", "HEAD:win.md"], f"{WORK}/repoX").stdout.strip() == "x-win"
      and not os.path.exists(envX),
      "F7 目标仓合并内容相符（无双死双并）＋env 回收 # Req: F7")

# ============================================================
# F11 门内 cancel 排队：done 在门→cancel 排队→门尾消费（verify 败→cancelled）
# ============================================================
tidY, wtokY, envY, wsidY = dispatch("Standby: no work required right now.", "repoY")
agora("/task_update", {"task": tidY, "verify_cmd": "sleep 6 && false"})
shaY = git(["rev-parse", "HEAD"], envY)   # 无 diff：锚=基线 HEAD 亦可入门
os.makedirs(f"{envY}/.review-loop", exist_ok=True)
with open(f"{envY}/.review-loop/.last-reviewed", "w") as f:
    f.write(shaY)
post_done(tidY, wtokY, shaY)
check(row(tidY)["state"] == "done" and wait_in_gate(tidY), "F11 done 入门 # Req: F11")
code, _ = agora("/task_cancel", {"task": tidY})
check(code == 200 and row(tidY).get("pending_cancel") is True,
      "F11 门临界段 cancel 排队（pending_cancel 置位，判定置位不撕裂） # Req: F11")
t = wait_state(tidY, {"cancelled"}, timeout=60)
check(t["state"] == "cancelled" and "verify_failed" in evs(tidY)
      and not os.path.exists(envY),
      "F11 门走完生效：verify 败后门尾结算 cancelled＋env 回收 # Req: F11")
code, _ = agora("/post", {"task": tidY, "kind": "note", "body": "hi"}, wtokY)
check(code == 401, "F11 cancelled 后旧凭据作废（401） # Req: F11")
check("merged" not in evs(tidY), "F11 未 merged（无逆转历史面） # Req: F11")

# ============================================================
# R25-A 拍人真链：主会话真 MCP 附牌绑定→3 连败 blocked→拍人注入真会话
# （绑定前任务面全走占位 sid 留库回退——本节为 R25 裁定的真链锚，置末不扰前场景）
# ============================================================
def mcp_call(name, args):
    """真 agora_mcp.py 子进程一调用：stdio newline-JSON-RPC（附牌链真面）。"""
    p = subprocess.Popen(["python3", f"{REPO}/agora_mcp.py"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True,
                         env=dict(os.environ, AGORA_HOME=PLAZA))
    out, _ = p.communicate(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}}) + "\n", timeout=60)
    return json.loads(out.strip().splitlines()[-1])

msid = api("/session", {})["data"]["id"]  # 真 opencode 会话扮演主会话（注入面可断言）
r = mcp_call("board", {"_token": ROOT, "_sid": msid})
check("result" in r and "buckets" in json.loads(r["result"]["content"][0]["text"]),
      "R25-A 主会话经真 MCP 调 board（root 牌附牌＋_sid 透传） # Req: F4")
check("main_sid_bound" in open(f"{PLAZA}/events.jsonl").read(),
      "R25-A 主会话绑定审计留行 # Req: F8")
tidM, wtokM, envM, wsidM = dispatch("Standby: no work required right now.", "repoM")
check(row(tidM)["dispatcher_sid"] == msid,
      f"R25-A 绑定后建任务 dispatcher＝真实主会话 sid（{row(tidM)['dispatcher_sid']}） # Req: F4")
agora("/task_update", {"task": tidM, "verify_cmd": "false"})
for i in range(3):
    if row(tidM)["state"] != "running":  # verify_failed/reopened 续修边：worker 调用即开工
        agora("/post", {"task": tidM, "kind": "note", "body": f"kick-{i}"}, wtokM)
    post_done(tidM, wtokM, commit_anchor(envM, f"m work {i}"))
    wait_state(tidM, {"verify_failed", "blocked"})
check(row(tidM)["state"] == "blocked" and "escalate" in evs(tidM),
      "R25-A 3 连败→blocked＋escalate 审计 # Req: F5")
hit, dl = False, time.time() + 30
while time.time() < dl and not hit:
    hit = bool(user_texts(msid, "失控[quality]"))
    time.sleep(1)
check(hit, "R25-A 拍人注入真主会话（会话内见失控[quality]） # Req: F4/F5")
check(any(m.get("kind") == "escalate" and m.get("to") == msid for m in store_msgs()),
      "R25-A 拍人消息库行 to＝真实主会话 sid # Req: F4")
agora("/task_cancel", {"task": tidM})  # blocked→cancelled：env 回收（残留断言前置）
wait_state(tidM, {"cancelled"})
check(row(tidM)["state"] == "cancelled" and not os.path.exists(envM),
      "R25-A blocked 任务中止即环境回收 # Req: F11")

print(f"T3 driver done: {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
PY
RC=$?

# ---- 7) 停（显式清理先于残留断言；EXIT trap 兜底幂等）----
tmux kill-session -t "$S1_NAME" 2>/dev/null
tmux kill-session -t "$AD_NAME" 2>/dev/null
kill_from_json "$SANDBOX/state/opencode/service.json"
kill_from_json "$SANDBOX/plaza/.agora/service.json"
sleep 1.5

# ---- 8) 残留断言＋反需求语义扫（F5 弱锚收口）----
# ---- 8a) 密钥最小面自检（cleanup 前沙箱＝红跑留存现场态）：mode=700＋非目标 provider key 零留存 ----
if [ "$(stat -c %a "$SANDBOX")" = "700" ]; then
  note "PASS 密钥面：沙箱 mode=700（红跑留存仅本机本用户可读）"
else
  note "FAIL 密钥面：沙箱 mode=$(stat -c %a "$SANDBOX")≠700"; RC=1
fi
if python3 - "$SANDBOX" "$PROV" <<'PY'
import os, sqlite3, sys
sandbox, prov = sys.argv[1:3]
src = sqlite3.connect(f"file:{os.path.expanduser('~/.local/share/opencode/opencode.db')}?mode=ro", uri=True)
others = [(i, v) for i, v in src.execute("SELECT integration_id, value FROM credential") if i != prov]
src.close()
db = f"{sandbox}/data/opencode/opencode.db"
raw = open(db, "rb").read()
dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
ints = {r[0] for r in dst.execute("SELECT integration_id FROM credential")}
dst.close()
bad = [i for i, v in others if i in ints or v.encode() in raw]
if bad:
    print(f" leak: {bad}")
    sys.exit(1)
sys.exit(0)
PY
then note "PASS 密钥面：非目标 provider key 零留存（表＋原始字节双查，留存沙箱即红跑现场）"
else note "FAIL 密钥面：非目标 provider key 泄漏（见上）"; RC=1; fi
if python3 - "$WORK" <<'PY'
import subprocess, sys, glob
ok = True
for repo in sorted(glob.glob(sys.argv[1] + "/*")):
    wt = subprocess.run(["git", "worktree", "list"], cwd=repo,
                        capture_output=True, text=True).stdout.strip().splitlines()
    br = subprocess.run(["git", "branch", "--list", "*__wt-task-*"], cwd=repo,
                        capture_output=True, text=True).stdout.strip()
    if len(wt) != 1 or br:
        print(f" residue in {repo}: wt={len(wt)} br={br!r}")
        ok = False
sys.exit(0 if ok else 1)
PY
then note "PASS 残留：9 夹具仓 worktree/分支全净（worktree list＝1）"; else note "FAIL 残留：worktree 残留"; RC=1; fi
if pgrep -f "agorad/main.py" >/dev/null 2>&1; then note "FAIL 残留：agorad 进程未净"; RC=1; else note "PASS 残留：pgrep agorad 净"; fi
if tmux ls 2>/dev/null | grep -q "t3-oc\|t3-ad"; then note "FAIL 残留：tmux 会话未净"; RC=1; else note "PASS 残留：tmux 无残会话"; fi
if compgen -G "$REPO/t3-*" >/dev/null || [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null | grep '^??' | grep -v 'tests/\|.superpowers/\|.omo/\|.review-loop/' || true)" ]; then
  note "FAIL 残留：e2e 在本仓落盘"; RC=1; else note "PASS 残留：本仓无 e2e 落盘"; fi
if python3 - "$REPO" <<'PY'
# 反需求语义扫：cron/看门狗/stalled 处置零代码路径（t4-coverage F5 弱锚收口）。
# docstring/注释剔除——health.py 文档行自述"无巡检"为合法否定，不计数。
import ast, pathlib, re, sys
pat = re.compile(r"cron|crontab|\bstalled?\b|watchdog|看门狗|巡检|全局扫描", re.I)
hits = []
files = list(pathlib.Path(sys.argv[1], "agorad").rglob("*.py"))
files.append(pathlib.Path(sys.argv[1]) / "agora_mcp.py")
files += [p for p in pathlib.Path(sys.argv[1], "agora-plugin").glob("*.ts")
          if not p.name.endswith(".test.ts")]
for p in files:
    src = p.read_text(encoding="utf-8")
    skip = []
    if p.suffix == ".py":
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            first = body[0] if isinstance(body, list) and body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                skip.append((first.value.lineno, first.value.end_lineno))
    for i, line in enumerate(src.splitlines(), 1):
        if any(a <= i <= b for a, b in skip):
            continue
        s = line.lstrip()
        if s.startswith("#") or s.startswith("//") or s.startswith("*"):
            continue
        if pat.search(line):
            hits.append(f"{p.name}:{i}: {s.strip()[:60]}")
for h in hits:
    print(" semantic-hit", h)
sys.exit(1 if hits else 0)
PY
then note "PASS 语义扫：无 cron/巡检/看门狗/stalled 处置代码路径 # Req: F5"; else note "FAIL 语义扫：反需求路径命中"; RC=1; fi

if [ "$RC" -eq 0 ]; then note "ALL GREEN（十三场景＋残留＋密钥面＋语义扫）"; else note "RED（见上 FAIL 行）"; fi
exit "$RC"
