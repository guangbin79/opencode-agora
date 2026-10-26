#!/usr/bin/env bash
# agora 三部件装配＋root 牌接管（F1，T4）。幂等：连跑两遍落盘结果不变。
# 流程：依赖检查→骨架→MCP 注册（合并不覆盖）→插件复制→root 牌接管→持牌冒烟→停。
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
AGORA_HOME="${AGORA_HOME:-$HOME/plaza}"          # 与 agorad env_config 同缺省（设计 §3）
OC_URL="${AGORA_OC_URL:-http://127.0.0.1:4096}"  # 占位：T5 服务发现回填；oc 不可达不挡服务
OC_DIR="$HOME/.config/opencode"
PLUGIN_DIR="$OC_DIR/plugins/agora"
SERVICE_JSON="$AGORA_HOME/.agora/service.json"
ROOT_TOKEN="$AGORA_HOME/.agora/root.token"
AD_PID=""; CAP=""

fail() { echo "install: $*" >&2; exit 1; }
cleanup() {
  [ -n "$AD_PID" ] && kill "$AD_PID" 2>/dev/null || true
  [ -n "$CAP" ] && rm -f "$CAP"
  return 0
}
trap cleanup EXIT

# ---- 1) 依赖检查 ----
missing=""
for c in python3 git opencode bun; do
  command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
done
[ -z "$missing" ] || fail "缺依赖:$missing —— 安装后重试"

# ---- 2) 骨架 ----
mkdir -p "$AGORA_HOME/state/messages" "$AGORA_HOME/wiki" "$AGORA_HOME/.agora"

# ---- 3) MCP 注册（mcp 段合并不覆盖；注册形状 T5 对账）----
mkdir -p "$OC_DIR"
python3 - "$OC_DIR/opencode.json" "$REPO/agora_mcp.py" "$AGORA_HOME" <<'PY'
import json, sys
from pathlib import Path
p, mcp_py, home = (Path(a) for a in sys.argv[1:4])
cfg = {}
if p.exists():
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"install: {p} 非法 JSON——拒改，请手修后重试")
if not isinstance(cfg, dict):
    sys.exit(f"install: {p} 顶层须为 JSON 对象——拒改")
cfg.setdefault("mcp", {})["agora"] = {  # 重跑同值覆写＝幂等；他 server 原样保留
    "type": "local",
    "command": ["python3", str(mcp_py)],
    "environment": {"AGORA_HOME": str(home)},
}
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# ---- 4) 插件复制（opencode 只扫插件目录；整目录复制保相对导入；测试件不随装）----
mkdir -p "$PLUGIN_DIR"
for f in "$REPO"/agora-plugin/*.ts; do
  case "$f" in *.test.ts) continue ;; esac
  cp -f "$f" "$PLUGIN_DIR"/
done

# ---- 5) AGORA_OC_URL 占位（T5 服务发现回填）----
cat > "$AGORA_HOME/.agora/env" <<'EOF'
# AGORA_OC_URL= — opencode 服务地址（T5 服务发现回填；lift 拉起 agorad 需此 env）
EOF

# ---- 6) root 牌接管 ----
agorad_alive() {  # service.json port 可连＝在运行（任意应答含 401 同义）
  python3 - "$SERVICE_JSON" <<'PY'
import json, socket, sys
try:
    port = json.load(open(sys.argv[1]))["port"]
    socket.create_connection(("127.0.0.1", int(port)), timeout=0.5).close()
except Exception:
    sys.exit(1)
PY
}

if agorad_alive; then
  echo "install: agorad 已在运行——复用既有实例与 root.token"
else
  mkdir -p /tmp/opencode
  CAP="$(mktemp /tmp/opencode/install-agorad.XXXXXX)"
  AGORA_HOME="$AGORA_HOME" AGORA_OC_URL="$OC_URL" \
      python3 "$REPO/agorad/main.py" >"$CAP" 2>&1 &
  AD_PID=$!
  ready=""
  for _ in {1..100}; do  # 10s 上限：等就绪行（root 牌行在其前 flush）
    if grep -q "^agorad ready" "$CAP" 2>/dev/null; then ready=1; break; fi
    kill -0 "$AD_PID" 2>/dev/null || break
    sleep 0.1
  done
  [ -n "$ready" ] || { cat "$CAP" >&2; fail "agorad 启动失败（10s 未就绪，见上方输出）"; }
  tok="$(awk '/^AGORA_ROOT_TOKEN: /{print $2; exit}' "$CAP")"
  if [ -n "$tok" ]; then
    (umask 077; printf '%s\n' "$tok" > "$ROOT_TOKEN")  # 0600（F1 root 牌唯一落盘点）
  elif [ ! -s "$ROOT_TOKEN" ]; then
    fail "未捕获 root 牌且无既有 root.token——异常态，请清 $AGORA_HOME/tokens 后重装"
  fi  # else: 复装复用既有 root.token（牌持久化，重启不换）
fi
[ -s "$ROOT_TOKEN" ] || fail "root.token 缺失（运行中实例无牌可接管）"

# ---- 7) 冒烟：持 root 牌 doctor 可达＋enforce off→on ----
# （resetLift 语义不在此验：插件进程内函数，插件测试已覆盖）
python3 - "$SERVICE_JSON" "$ROOT_TOKEN" <<'PY'
import json, sys, urllib.request
port = json.load(open(sys.argv[1]))["port"]
token = open(sys.argv[2]).read().strip()
def call(path, obj=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if obj is None else json.dumps(obj).encode(),
        headers={"X-Agora-Token": token, "Content-Type": "application/json"},
        method="GET" if obj is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")
d = call("/health")  # doctor 同级查询面（may 矩阵）
assert d.get("ok"), f"doctor 冒烟失败: {d}"
assert call("/enforce", {"on": False})["enforce_on"] is False, "enforce off 失败"
assert call("/enforce", {"on": True})["enforce_on"] is True, "enforce on 失败"
PY

# ---- 8) 停（只停本脚本拉起的实例）----
if [ -n "$AD_PID" ]; then
  kill "$AD_PID" 2>/dev/null || true
  wait "$AD_PID" 2>/dev/null || true
  AD_PID=""
fi

echo "install: 完成（$AGORA_HOME）"
echo "install: lift 失败可重启会话或等 doctor；工具拒因携 resetLift 恢复指引"
