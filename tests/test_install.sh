#!/usr/bin/env bash
# install.sh 自验收（T4）：临时 HOME/AGORA_HOME 夹具，断言全过退出 0。
# 不真改 ~/.config/opencode——一切经 HOME 重定向。每断言带 Req: F1 锚。
set -u
cd "$(dirname "$0")/.."
REPO="$PWD"
PASS=0; FAIL=0

ok()   { echo "ok  - $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL- $1"; FAIL=$((FAIL+1)); }
chk()  { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }

run_install() { HOME="$1" AGORA_HOME="$2" bash "$REPO/install.sh"; }
FIX=""

teardown() { [ -n "$FIX" ] && rm -rf "$FIX"; }
trap teardown EXIT

newfix() {
  FIX="$(mktemp -d /tmp/opencode/t4.XXXXXX)"
  mkdir -p "$FIX/home"
}

# ---------- 场景 1：首装全件在位 ----------
newfix
H1="$FIX/home"; A1="$FIX/plaza"; OCJ="$H1/.config/opencode/opencode.json"

if out=$(run_install "$H1" "$A1" 2>&1); then ok "首装退出 0 (Req: F1)"; else bad "首装退出 0 (Req: F1)（$out）"; fi

chk "骨架 state/messages (Req: F1)" [ -d "$A1/state/messages" ]
chk "骨架 wiki (Req: F1)"           [ -d "$A1/wiki" ]
chk "骨架 .agora (Req: F1)"         [ -d "$A1/.agora" ]

chk "MCP 注册 agora＋指向本仓壳 (Req: F1)" python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
a = cfg["mcp"]["agora"]
assert a["type"] == "local", a
assert a["command"][1] == sys.argv[2], a' "$OCJ" "$REPO/agora_mcp.py"

chk "插件复制 index.ts (Req: F1)" cmp -s "$REPO/agora-plugin/index.ts" "$H1/.config/opencode/plugins/agora/index.ts"
chk "插件复制 lift.ts (Req: F1)"  cmp -s "$REPO/agora-plugin/lift.ts"  "$H1/.config/opencode/plugins/agora/lift.ts"
chk "插件复制 hook/cred/inject (Req: F1)" bash -c '
for f in hook cred inject; do
  cmp -s "$1/agora-plugin/$f.ts" "$2/plugins/agora/$f.ts" || exit 1
done' _ "$REPO" "$H1/.config/opencode"
chk "测试件不随装（无 *.test.ts）(Req: F1)" bash -c '
[[ -z "$(find "$1/plugins/agora" -name "*.test.ts")" ]]' _ "$H1/.config/opencode"

chk "root.token 在位非空 (Req: F1)" [ -s "$A1/.agora/root.token" ]
chk "root.token 0600 (Req: F1)"     [ "$(stat -c %a "$A1/.agora/root.token")" = 600 ]
chk "AGORA_OC_URL 占位 env (Req: F1)" grep -q "AGORA_OC_URL" "$A1/.agora/env"
chk "agorad 载荷随装（lift 独立安装解析位，Req: F4）" [ -f "$A1/.agora/agorad/main.py" ]
chk "载荷含子包（漏包＝ModuleNotFoundError，Req: F4）" bash -c '
for f in access/oc.py access/store.py core/app.py core/task.py transport/httpd.py; do
  [ -f "$1/.agora/agorad/$f" ] || exit 1
done
[[ -z "$(find "$1/.agora/agorad" -name "__pycache__")" ]]' _ "$A1"
chk "载荷可执行到 env 检查处（Req: F4）" bash -c '
d="$(mktemp -d)"; mkdir -p "$d/state"  # 空 XDG_STATE_HOME：隔离真机 service.json（T5 发现源）
out="$(AGORA_HOME="$1/nonexist-home" XDG_STATE_HOME="$d/state" timeout 10 python3 "$1/.agora/agorad/main.py" 2>&1)"; rc=$?
rm -rf "$d"
[ "$rc" = 2 ] && echo "$out" | grep -q "AGORA_OC_URL"' _ "$A1"

chk "冒烟不开 enforce OFF 窗（无 enforce_off 审计——复用实例安全，Req: F1）" bash -c '
[[ ! -e "$1/events.jsonl" ]] || ! grep -q "\"event\": \"enforce_off\"" "$1/events.jsonl"' _ "$A1"
# /health 只读冒烟可达由首装退出 0 蕴含（冒烟不过即非零）

TOK1="$(cat "$A1/.agora/root.token")"

# ---------- 场景 2：二装幂等（他 server 不覆盖＋不重复注册）----------
newfix
H2="$FIX/home"; A2="$FIX/plaza"; OCJ2="$H2/.config/opencode/opencode.json"
mkdir -p "$H2/.config/opencode"
cat > "$OCJ2" <<'EOF'
{"theme": "dark", "mcp": {"other": {"type": "local", "command": ["echo", "hi"]}}}
EOF

run_install "$H2" "$A2" >/dev/null 2>&1 || bad "二装场景首跑退出 0 (Req: F1)"
cp "$OCJ2" "$FIX/oc-run1.json"
cp "$A2/.agora/root.token" "$FIX/token-run1"

run_install "$H2" "$A2" >/dev/null 2>&1 || bad "二装退出 0 (Req: F1)"

chk "二装 opencode.json 逐字节不变 (Req: F1)" cmp -s "$FIX/oc-run1.json" "$OCJ2"
chk "他 server other 保留 (Req: F1)" python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg["theme"] == "dark"
assert cfg["mcp"]["other"]["command"] == ["echo", "hi"]' "$OCJ2"
chk "agora 不重复注册 (Req: F1)" python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
names = [k for k in cfg["mcp"] if k == "agora"]
assert len(names) == 1, names' "$OCJ2"
chk "二装 root.token 不变（牌持久复用）(Req: F1)" cmp -s "$FIX/token-run1" "$A2/.agora/root.token"

# ---------- 场景 3：缺依赖退出码＋提示＋早退 ----------
newfix
H3="$FIX/home"; A3="$FIX/plaza"; mkdir -p "$H3" "$FIX/shim"
ln -sf "$(command -v python3)" "$FIX/shim/python3"
ln -sf "$(command -v git)"     "$FIX/shim/git"

out3=$(HOME="$H3" AGORA_HOME="$A3" PATH="$FIX/shim:/usr/bin:/bin" \
       bash "$REPO/install.sh" 2>&1)
rc3=$?
chk "缺依赖退出非零 (Req: F1)" [ "$rc3" -ne 0 ]
chk "缺依赖提示含缺件名 (Req: F1)" bash -c '[[ "$1" == *"缺依赖"* && "$1" == *"bun"* ]]' _ "$out3"
chk "缺依赖早退（骨架未建）(Req: F1)" bash -c '[[ ! -e "$1/.agora" ]]' _ "$A3"

# ---------- 场景 4：带 OC password 安装（展开值不作赋值前缀回归）----------
newfix
H4="$FIX/home"; A4="$FIX/plaza"; mkdir -p "$H4"
run_install_pw() { HOME="$H4" AGORA_HOME="$A4" AGORA_OC_PASSWORD="pw has space" bash "$REPO/install.sh"; }
run_install_pw >/dev/null 2>&1 || bad "带 password 安装退出 0 (Req: F1)"
chk "带 password 安装 root.token 在位 (Req: F1)" [ -s "$A4/.agora/root.token" ]

# ---------- 汇总 ----------
echo "----------------------------------------"
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
