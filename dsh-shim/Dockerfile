# dsh worker 容器配方（Req: F182）——node24（dsh 0.2.2-rc.5）＋python3（跑
# agora_mcp.py / mock-agorad）＋bwrap（容器内 root 免宿主 sudo 的沙箱后端）。
# 仓内载荷（shim 源码/共享核心/agora_mcp.py）不烧镜像——按任务契约经挂载位
# 注入（见 run-worker.sh：/work/repo）。
FROM node:24-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates bubblewrap procps \
    && rm -rf /var/lib/apt/lists/* \
    && bwrap --version

RUN npm install -g --omit=dev @deepseek-ai/dsh@0.2.2-rc.5

WORKDIR /work
CMD ["bash"]
