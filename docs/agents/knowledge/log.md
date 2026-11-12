# Knowledge Log

## [2026-11-10] ingest | knowledge-index
- Added F70 stalled worker self-check prompt to architecture decisions.
## [2026-11-11] ingest | knowledge-index
- Added F73 board manual worker prompt to architecture decisions.
## [2026-11-11] ingest | agora-dispatch-routing
- 记录 F75 CLI 派发错址修复与补交语义
- Types: Decision/Bug
## [2026-11-12] ingest | stability-performance
- 记录 F84-F93 写闸双路径、凭据/消息并发、账本热读缓存与 e2e 裸拉 MCP 的 HOME 沙箱坑位
- Types: Decision/Bug/Strategy
## [2026-11-12] ingest | knowledge-index
- 记录 F97 根级未跟踪隐藏目录自动豁免边界与名册/clean-ignore 通道分工
- Types: Decision
## [2026-11-12] ingest | knowledge-index
- Added F99 verify command hygiene gate to architecture decisions.
- Types: Decision
## [2026-11-13] ingest | knowledge-index
- Added delivery-authority split (F98/F100/F101/F102): remote authority with origin, local CAS authority without origin, external dispatcher authority for external_branch, instance-owned cleanup; e2e proof at tests/e2e/multi_instance_delivery.py.
- Types: Decision
