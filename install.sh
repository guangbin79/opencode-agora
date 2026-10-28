#!/usr/bin/env bash
# agora 三部件装配＋root 牌接管（F1，T4）。幂等：连跑两遍落盘结果不变。
# 流程：依赖检查→骨架（含 F12 名册）→MCP 注册（合并不覆盖）→插件/skills 复制→root 牌接管→持牌冒烟→停。
# 更新路径：git pull 后重跑本脚本——插件（~/.config/opencode/plugins/agora）与 agorad
# 载荷（$AGORA_HOME/.agora/agorad）是拷贝不是引用，拉新代码不重跑不生效。
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${AGORA_HOME:-}" ] && [ -s "$HOME/.agora-home" ]; then  # 复跑保既有装（评审环）：env > 指针 > 缺省
  AGORA_HOME="$(python3 -c 'import sys
try:
    print(open(sys.argv[1], encoding="utf-8").read().split("\n", 1)[0].strip())
except (OSError, ValueError):
    pass' "$HOME/.agora-home" 2>/dev/null || true)"  # 坏指针严格解码失败→空→缺省（读全量取首行，与 pointer_home 同契约，评审环）
fi
AGORA_HOME="${AGORA_HOME:-$HOME/plaza}"          # 与 agorad env_config 同缺省（设计 §3）
printf '%s\n' "$AGORA_HOME" > "$HOME/.agora-home"  # gt-32c051 指针：无 env 组件统一发现（MCP 壳启动亦写）
STATE_OC="$HOME/.local/state/opencode/service.json"  # T5 实证：serve --service 写此处 {url,password}
OC_URL="${AGORA_OC_URL:-}"
OC_PASSWORD="${AGORA_OC_PASSWORD:-}"
if [ -z "$OC_URL" ] && [ -r "$STATE_OC" ]; then  # T5 服务发现回填；读不到回落占位（oc 不可达不挡服务）
  OC_URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("url",""))' "$STATE_OC" 2>/dev/null || true)"
  OC_PASSWORD="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("password",""))' "$STATE_OC" 2>/dev/null || true)"
fi
OC_URL="${OC_URL:-http://127.0.0.1:4096}"        # 占位：仅当 service.json 缺失
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
# F12 名册空骨架：已有不覆盖（用户自录条目不可破坏）；0644 owner 可追加
[ -e "$AGORA_HOME/.agora/inactive-repos" ] || (umask 022; : > "$AGORA_HOME/.agora/inactive-repos")

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

# ---- 4a) skills 复制（主会话编排剧本；纯 md 整目录拷，同值覆写＝幂等）----
mkdir -p "$OC_DIR/skills"
for d in "$REPO"/skills/*/; do
  cp -rf "${d%/}" "$OC_DIR/skills/"
done

# ---- 4b) agorad 载荷随装（T5 追加项：lift 不再假定与插件同仓——独立安装场景从
#      $AGORA_HOME/.agora/agorad/main.py 解析；env AGORAD_SCRIPT 可覆写）。
#      递归拷贝含 access/core/transport 子包（漏子包＝ModuleNotFoundError，lift 必超时）----
mkdir -p "$AGORA_HOME/.agora"
rm -rf "$AGORA_HOME/.agora/agorad"
cp -r "$REPO/agorad" "$AGORA_HOME/.agora/"
cp -f "$REPO/board_tui.py" "$AGORA_HOME/.agora/"
chmod +x "$AGORA_HOME/.agora/board_tui.py"
mkdir -p "$HOME/.local/bin"
ln -sf "$AGORA_HOME/.agora/board_tui.py" "$HOME/.local/bin/agora-board"

# ---- 4c) agora CLI（F14：wait 阻塞谓词；bin 目录 4b 已建）----
cp -f "$REPO/agora" "$HOME/.local/bin/agora"
chmod +x "$HOME/.local/bin/agora"
find "$AGORA_HOME/.agora/agorad" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# ---- 5) AGORA_OC_URL 发现结果落档（lift 运行时自发现同源；此处只留档不作为消费面）----
cat > "$AGORA_HOME/.agora/env" <<EOF
# opencode 服务发现（T5 实证：\$XDG_STATE_HOME/opencode/service.json）
# AGORA_OC_URL=$OC_URL
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
  AD_ENV=(env AGORA_HOME="$AGORA_HOME" AGORA_OC_URL="$OC_URL")  # 展开值不作赋值前缀（bash 视为命令名）
  [ -n "$OC_PASSWORD" ] && AD_ENV+=("AGORA_OC_PASSWORD=$OC_PASSWORD")
  "${AD_ENV[@]}" python3 "$REPO/agorad/main.py" >"$CAP" 2>&1 &
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

# ---- 7) 冒烟：持 root 牌 /health 只读可达（不碰 enforce——复用既有实例的更新场景
#      若做 off→on 写冒烟，会在真实例上开毫秒级 enforce OFF 窗；enforce 面由插件/e2e 覆盖）----
python3 - "$SERVICE_JSON" "$ROOT_TOKEN" <<'PY'
import json, sys, urllib.request
port = json.load(open(sys.argv[1]))["port"]
token = open(sys.argv[2]).read().strip()
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/health",
    headers={"X-Agora-Token": token})
with urllib.request.urlopen(req, timeout=10) as r:
    d = json.loads(r.read() or b"{}")
assert d.get("ok"), f"冒烟失败: {d}"
PY

# ---- 8) 停（只停本脚本拉起的实例）----
if [ -n "$AD_PID" ]; then
  kill "$AD_PID" 2>/dev/null || true
  wait "$AD_PID" 2>/dev/null || true
  AD_PID=""
fi

echo "install: 完成（$AGORA_HOME）"
echo "install: 启用域名册 $AGORA_HOME/.agora/inactive-repos（空＝全启用；仓根路径一行一个，新会话生效）"
echo "install: 更新＝git pull 后重跑本脚本（插件与 agorad 载荷是拷贝不是引用，不重跑不生效）"
echo "install: lift 失败可重启会话或等 doctor；工具拒因携 resetLift 恢复指引"
