# agorad unit 加固：门构建 cgroup 隔离（ag-67650e，oomd 连坐修复）

2026-11-23 事故：验收门 verify 构建作为 daemon 子进程跑在同一 cgroup
（agorad.service 单元 5.8G），systemd-oomd 按 PSI 压力击杀整单元（70 进程），
一条在途 /task_dispatch 蒸发。两层修复：门构建经 `systemd-run --user --scope`
落独立 slice（`agorad/core/gate.py` `_scoped_cmd`：systemd-run 缺失或 user bus
不可达自动回退直跑，verify 语义/退出码分类不变）；宿主 unit 加资源顶——本文
即模板，宿主 unit 更新由维护者合并后执行（任务 env 禁改 ~/.config/systemd）。

## agorad.service（追加以下行，既有 ExecStart/Restart 不动）

```ini
[Service]
MemoryMax=1500M
ManagedOOMMemoryPressure=kill
ManagedOOMMemoryPressureLimit=50%
```

`MemoryMax=1500M` 起评（daemon 常态 ~1.2G，峰值 10.3G 全来自门构建）：调顶前
先看 peak 构成——若仍来自门构建，说明 scope 隔离没生效，别一味放宽。
`ManagedOOMMemoryPressure=kill` 需 systemd-oomd 用户态启用
（`systemctl status systemd-oomd`）。

## agora-gate.slice（门构建独立资源域，oomd 只杀构建不杀 daemon）

```ini
# ~/.config/systemd/user/agora-gate.slice
[Slice]
ManagedOOMMemoryPressure=kill
ManagedOOMMemoryPressureLimit=50%
```

scope 由 gate 运行时创建（`--user --scope --slice=agora-gate.slice`），构建超压
时 oomd 只杀 scope——verify 失败照常路由派发方，daemon 与在飞请求不受连坐。
slice 不预设 MemoryMax：构建真实吃内存，顶死 slice 等于把偶发连坐改成必杀
构建；按机器内存自评再加。注意两层都管不到 kernel OOM killer 兜底路径——
整机硬耗尽时内核仍可能选中 daemon 进程，隔离语义＝oomd 连坐修复而非绝对豁免。

## 应用（维护者手动）

```bash
systemctl --user daemon-reload && systemctl --user restart agorad.service
# 重启窗口在飞请求会蒸发——挑空档执行
```
