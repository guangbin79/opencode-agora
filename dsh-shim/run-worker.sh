#!/usr/bin/env bash
# run-worker.sh（Req: F182）——dsh worker 容器运行配方。
# 形态（任务契约）：--network=host（GLM 代理与 agorad 均 127.0.0.1）＋-u 对齐
# 宿主 UID（写产物归宿主用户，容器内 root 仅用于免 sudo 装 bwrap）＋dsh-home
# 独立目录＋AGORA_HOME 隔离目录。挂载位：本仓 /work/repo（shim 源/共享核心/
# agora_mcp.py，只读）；任务 worktree 与隔离 AGORA_HOME 由调用方注入（F183 e2e
# 预留位：<task-env>/<agora-home>，见 verify.sh 的实参用法）。
# 凭据纪律：GLM_API_KEY 只从调用方环境透传（docker -e），本脚本零落盘零回显。
set -euo pipefail

IMAGE="${IMAGE:-opencode-agora/f182-dsh-worker:1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

build() {
  docker build -t "$IMAGE" "$REPO_ROOT/dsh-shim"
}

# 用法：
#   ./run-worker.sh build
#   GLM_API_KEY=... ./run-worker.sh run <task-env-dir> <agora-home-dir> [-- extra docker args...] -- dsh 内层参数...
#     内层缺省＝--profile headless --patch /work/repo/dsh-shim/cordis.worker.yml --json "<prompt>"
cmd="${1:-}"
case "$cmd" in
  build) build; exit 0 ;;
  run) shift ;;
  *) echo "usage: $0 build | run <task-env-dir> <agora-home-dir> [-- docker-args] -- [dsh-args]" >&2; exit 2 ;;
esac

TASK_ENV_HOST="$1"; shift
AGORA_HOME_HOST="$1"; shift

docker_args=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do docker_args+=("$1"); shift; done
[ "$#" -gt 0 ] && shift || true

# AGORA_HOME 只读挂载＝信任锚保护（C 册 R1）：service.json/root.token 若可被容器内
# worker 改写，即可把 lift 指向自建假 agorad 关闸（enforce off/env_dir=/）——shim
# 只读消费该目录；mock 验证流的读写挂载归 verify.sh 自管。
exec docker run --rm \
  --network=host \
  -u "$(id -u):$(id -g)" \
  -e GLM_API_KEY \
  -e HOME=/work/dsh-home \
  -e DSH_HOME=/work/dsh-home \
  -e AGORA_HOME=/work/agora-home \
  -e AGORAD_SCRIPT=/dev/null \
  -v "$REPO_ROOT":/work/repo:ro \
  -v "$(readlink -f "$TASK_ENV_HOST")":/work/task-env \
  -v "$(readlink -f "$AGORA_HOME_HOST")":/work/agora-home:ro \
  -v "${F182_DSH_HOME_HOST:-$(mktemp -d)}":/work/dsh-home \
  ${docker_args[@]+"${docker_args[@]}"} \
  "$IMAGE" \
  dsh --profile headless --patch /work/repo/dsh-shim/cordis.worker.yml --json "$@"
