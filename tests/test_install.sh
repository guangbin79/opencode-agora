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

teardown() {
  local d p
  for d in "${A1:-}" "${A2:-}" "${A4:-}" "${A5:-}"; do  # 按夹具 service.json pid 补杀，避免孤儿 agorad（gt-71b6af）
    [ -n "$d" ] || continue
    p="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pid",""))' "$d/.agora/service.json" 2>/dev/null)" || continue
    [ -n "$p" ] && kill "$p" 2>/dev/null
  done
  [ -n "$FIX" ] && rm -rf "$FIX"
  return 0
}
trap teardown EXIT

newfix() {
  FIX="$(mktemp -d /tmp/opencode/t4.XXXXXX)"
  mkdir -p "$FIX/home"
}

# ---------- 场景 1：首装全件在位 ----------
newfix
H1="$FIX/home"; A1="$FIX/plaza"; OCJ="$H1/.config/opencode/opencode.json"

if out=$(run_install "$H1" "$A1" 2>&1); then ok "首装退出 0 (Req: F1)"; else bad "首装退出 0 (Req: F1)（$out）"; fi

# ---- setsid 脱会话回归（gt-71b6af）：install 收尾停自拉实例，装后以同形态自拉安装位
# 载荷，断言夹具 agorad 存活＋会话脱离测试 shell（确定性观察，无装中竞态）----
OLDPID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pid",""))' "$A1/.agora/service.json" 2>/dev/null || true)"
MYSESS="$(ps -o sess= -p $$ | tr -d ' ')"
AD_ENV=(env AGORA_HOME="$A1" AGORA_OC_URL="http://127.0.0.1:4096" PYTHONDONTWRITEBYTECODE=1)  # 占位同 install 缺省（oc 不可达不挡服务）；不写字节码免污载荷目录断言
"${AD_ENV[@]}" setsid python3 "$A1/.agora/agorad/main.py" >"$FIX/ad1.log" 2>&1 &
AD1_PID=$!
GOT=""
for _ in {1..100}; do  # 5s：等新实例覆写 service.json（pid 换新）且端口可连
  snap="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("pid",""),d.get("port",""))' "$A1/.agora/service.json" 2>/dev/null || true)"
  read -r adpid adport <<EOF
${snap:- }
EOF
  if [ -n "$adpid" ] && [ "$adpid" != "$OLDPID" ] && timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$adport" 2>/dev/null; then
    GOT=1; break
  fi
  sleep 0.05
done
[ -n "$GOT" ] || kill "$AD1_PID" 2>/dev/null || true
chk "夹具 agorad 存活（service.json 端口可连，gt-71b6af）" [ -n "$GOT" ]
chk "install 拉起行 setsid nohup 在位（源级锁，gt-71b6af/gt-e38e41）" grep -qF 'exec setsid nohup python3 "$REPO/agorad/main.py"' "$REPO/install.sh"
chk "依赖检查不含 setsid（macOS 兼容，Req: F27）" bash -c '! grep -qE "for c in [^;]*setsid" "$1"' _ "$REPO/install.sh"
chk "无 setsid 时 nohup 兜底分支在位（源级锁，Req: F27）" grep -qF 'exec nohup python3 "$REPO/agorad/main.py"' "$REPO/install.sh"
chk "凭据经子壳 export 进 environ（正向锁——删行即断凭据投递，task-bb4ad2）" grep -qF 'export AGORA_OC_PASSWORD="$OC_PASSWORD"' "$REPO/install.sh"
chk "AD_ENV 数组形不回归（整体禁标识符：两行拆分形也落本锁，task-bb4ad2）" bash -c '! grep -q "AD_ENV" "$1"' _ "$REPO/install.sh"
chk "凭据不进 argv（无未注释行同时携 OC_PASSWORD 赋值与启动器记号）" bash -c '! grep -qE "^[^#]*AGORA_OC_PASSWORD=.*(env|setsid|nohup|python3)" "$1"' _ "$REPO/install.sh"
chk "启动失败转储抹牌行（流式 sed，task-bb4ad2）" grep -qF "sed '/^AGORA_ROOT_TOKEN: /d' \"\$LOG\" >&2" "$REPO/install.sh"
chk "cleanup 连 .1 一起抹（前轮信号中断残留，task-bb4ad2）" grep -qF 'for f in "$LOG" "$LOG.1"' "$REPO/install.sh"
chk ".bak 残牌无条件扫尾在位（AD_LOG 键，与 step6 同源防漂移，task-bb4ad2）" grep -qF 'rm -f "$AD_LOG.bak" "$AD_LOG.1.bak"' "$REPO/install.sh"
ADSESS=""
[ -n "$GOT" ] && ADSESS="$(ps -o sess= -p "$adpid" 2>/dev/null | tr -d ' ')"
chk "夹具 agorad 脱会话（sess≠测试 shell，gt-71b6af）" bash -c '[ -n "$1" ] && [ "$1" != "$2" ]' _ "$ADSESS" "$MYSESS"

# ---- 复用实例轮（agorad_alive→LOG 空）：cleanup 残牌扫尾实路径（task-bb4ad2）----
# AD1 仍活着：此轮走复用分支不拉起，seed 的残 .bak 只能由 AGORA_HOME 键扫尾收敛
printf 'AGORA_ROOT_TOKEN: seeded-reuse-bak\n' > "$A1/.agora/install-agorad.log.bak"
printf 'AGORA_ROOT_TOKEN: seeded-reuse-1\n留档行 keep me\n' > "$A1/.agora/install-agorad.log.1"
run_install "$H1" "$A1" >/dev/null 2>&1 || bad "复用轮退出 0 (Req: F1)"
chk "复用轮 .bak 残牌扫尾净（AGORA_HOME 键实路径，task-bb4ad2）" bash -c '[ ! -e "$1/.agora/install-agorad.log.bak" ]' _ "$A1"
chk "复用轮 .1 牌行抹净且留档行保留（AD_LOG.1 sed 实路径——删行即红，task-bb4ad2）" bash -c '
f="$1/.agora/install-agorad.log.1"
[ -f "$f" ] && ! grep -q "^AGORA_ROOT_TOKEN: " "$f" && grep -q "留档行 keep me" "$f"' _ "$A1"
chk "复用轮 .1.bak 消失（sed 换 inode 残壳扫净，task-bb4ad2）" bash -c '[ ! -e "$1/.agora/install-agorad.log.1.bak" ]' _ "$A1"
chk "复用轮存活实例不受累（AD1 仍可连）" bash -c 'exec 3<>/dev/tcp/127.0.0.1/$1' _ "$adport"

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

chk "skills 复制 dispatch＋ops (Req: F12)" bash -c '
for s in agora-dispatch agora-ops; do
  cmp -s "$1/skills/$s/SKILL.md" "$2/skills/$s/SKILL.md" || exit 1
done' _ "$REPO" "$H1/.config/opencode"
chk "inactive-repos 骨架在位且空 (Req: F12)" bash -c '
f="$1/.agora/inactive-repos"; [ -f "$f" ] && [ ! -s "$f" ]' _ "$A1"
chk "inactive-repos owner 可写 (Req: F12)" [ -w "$A1/.agora/inactive-repos" ]
chk "inactive-repos 0644 (Req: F12)" [ "$(stat -c %a "$A1/.agora/inactive-repos")" = 644 ]
chk "收尾输出携名册提示（路径 env 展开非字面量，Req: F12）" bash -c '
[[ "$1" == *"$2/.agora/inactive-repos"* ]]' _ "$out" "$A1"

chk "root.token 在位非空 (Req: F1)" [ -s "$A1/.agora/root.token" ]
chk "root.token 0600 (Req: F1)"     [ "$(stat -c %a "$A1/.agora/root.token")" = 600 ]
chk "AGORA_OC_URL 占位 env (Req: F1)" grep -q "AGORA_OC_URL" "$A1/.agora/env"
chk "install 稳定日志在位含 ready 行（不随 install 清理，gt-e38e41）" grep -q "^agorad ready" "$A1/.agora/install-agorad.log"
chk "信号死因留痕（冒烟毕 kill 记入稳定日志，gt-e38e41）" grep -q "agorad exit signal=SIGTERM" "$A1/.agora/install-agorad.log"
chk "install 稳定日志 0600（gt-e38e41）" [ "$(stat -c %a "$A1/.agora/install-agorad.log")" = 600 ]
chk "日志不留 root 牌行（取牌即抹，gt-e38e41）" bash -c '! grep -q "^AGORA_ROOT_TOKEN: " "$1/.agora/install-agorad.log"' _ "$A1"
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

chk "agora CLI 在位可执行且 help 可用 (Req: F14)" bash -c '
[ -x "$1/.local/bin/agora" ] && "$1/.local/bin/agora" --help >/dev/null && "$1/.local/bin/agora" wait --help | grep -q "退出码"' _ "$H1"

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
echo "$FIX/some/repo" >> "$A2/.agora/inactive-repos"   # 模拟用户自录名册（二装不可覆盖）
cp -r "$H2/.config/opencode/skills" "$FIX/skills-run1"
printf 'AGORA_ROOT_TOKEN: seeded-stale\n' >> "$A2/.agora/install-agorad.log"  # 模拟前轮中断残牌（.1 留档实路径，task-bb4ad2）
printf 'AGORA_ROOT_TOKEN: seeded-bak\n' > "$A2/.agora/install-agorad.log.bak"  # 模拟前轮 cleanup 被杀 mid-sed（.bak 扫尾实路径）

run_install "$H2" "$A2" >/dev/null 2>&1 || bad "二装退出 0 (Req: F1)"

chk "二装 .1 留档连残牌一并抹（取牌即抹实路径，task-bb4ad2）" bash -c '
for f in "$1/.agora/install-agorad.log" "$1/.agora/install-agorad.log.1"; do
  [ -f "$f" ] && grep -q "^AGORA_ROOT_TOKEN: " "$f" && exit 1
done; exit 0' _ "$A2"
chk "二装 .bak 残牌扫尾净（断言不存在，task-bb4ad2）" bash -c '
for f in "$1/.agora/install-agorad.log.bak" "$1/.agora/install-agorad.log.1.bak"; do
  [ -e "$f" ] && exit 1
done; exit 0' _ "$A2"

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
chk "二装 skills 逐字节不变 (Req: F12)" diff -r "$FIX/skills-run1" "$H2/.config/opencode/skills" >/dev/null
chk "二装名册用户自录行不被覆盖 (Req: F12)" grep -qxF "$FIX/some/repo" "$A2/.agora/inactive-repos"

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

# ---------- 场景 5：pkg 模式（F31 PyPI 分发：AGORA_PKG_ROOT 数据面＋PATH agorad 入口）----------
newfix
H5="$FIX/home"; A5="$FIX/plaza"; PKGROOT="$FIX/pkgroot"; BIN5="$FIX/bin"
mkdir -p "$H5" "$PKGROOT/agora-plugin" "$PKGROOT/skills/agora-dispatch" "$PKGROOT/skills/agora-ops" "$BIN5" "$H5/.local/bin" "$BIN5/fakevenv"
ln -sf "$BIN5/fakevenv/agora-board" "$H5/.local/bin/agora-board"  # 模拟 pipx 同路径活链接（round-2 B 册回归锚）
for f in cred envPath hook index inject lift scope; do  # 只放 7 个非测试 ts（wheel 数据面同清单）
  cp "$REPO/agora-plugin/$f.ts" "$PKGROOT/agora-plugin/"
done
cp "$REPO/skills/agora-dispatch/SKILL.md" "$PKGROOT/skills/agora-dispatch/"
cp "$REPO/skills/agora-ops/SKILL.md" "$PKGROOT/skills/agora-ops/"
cp "$REPO/install.sh" "$PKGROOT/install.sh"
cp "$REPO/agora" "$PKGROOT/agora"
cat > "$BIN5/agorad" <<EOF  # PATH stub：模拟 pipx console script（触标留痕＋转跑真载荷）
#!/usr/bin/env bash
touch "$BIN5/agorad-called"
exec python3 "$REPO/agorad/main.py" "\$@"
EOF
chmod +x "$BIN5/agorad"

out5=$(HOME="$H5" AGORA_HOME="$A5" AGORA_PKG_ROOT="$PKGROOT" AGORA_AGORAD_SRC="$REPO/agorad" \
       PATH="$BIN5:$PATH" bash "$PKGROOT/install.sh" 2>&1)
rc5=$?
chk "pkg 模式 install 退出 0 (Req: F31)" [ "$rc5" -eq 0 ]
chk "pkg: 7 个非测试 ts 落插件目录且无测试件 (Req: F31)" bash -c '
[ "$(find "$1/plugins/agora" -name "*.ts" | wc -l)" -eq 7 ] && [[ -z "$(find "$1/plugins/agora" -name "*.test.ts")" ]]' _ "$H5/.config/opencode"
chk "pkg: skills 两剧本在位 (Req: F31)" bash -c '
for s in agora-dispatch agora-ops; do
  cmp -s "$1/skills/$s/SKILL.md" "$2/skills/$s/SKILL.md" || exit 1
done' _ "$PKGROOT" "$H5/.config/opencode"
chk "pkg: opencode.json command==[\"agora-mcp\"] (Req: F31)" python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
assert cfg["mcp"]["agora"]["command"] == ["agora-mcp"], cfg["mcp"]["agora"]' "$H5/.config/opencode/opencode.json"
chk "pkg: agorad 载荷在位（lift 契约位）(Req: F31)" [ -f "$A5/.agora/agorad/main.py" ]
chk "pkg: PATH stub agorad 确被调用 (Req: F31)" [ -f "$BIN5/agorad-called" ]
chk "pkg: root.token 在位 (Req: F31)" [ -s "$A5/.agora/root.token" ]
chk "pkg: pipx 形态活链接不被误删（round-2 B 册回归锚）" bash -c '
[ -L "$1/.local/bin/agora-board" ] && [ "$(readlink "$1/.local/bin/agora-board")" = "$2/fakevenv/agora-board" ]' _ "$H5" "$BIN5"
ln -sf "$A5/.agora/board_tui.py" "$H5/.local/bin/agora-board"  # repo 残留形态：指向载荷位
HOME="$H5" AGORA_HOME="$A5" AGORA_PKG_ROOT="$PKGROOT" AGORA_AGORAD_SRC="$REPO/agorad" \
  PATH="$BIN5:$PATH" bash "$PKGROOT/install.sh" >/dev/null 2>&1 || bad "pkg 二装退出 0 (Req: F31)"
chk "pkg: repo 残留形态链接被收敛（round-2 B 册回归锚）" bash -c '[ ! -L "$1" ]' _ "$H5/.local/bin/agora-board"

# ---------- 汇总 ----------
echo "----------------------------------------"
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
