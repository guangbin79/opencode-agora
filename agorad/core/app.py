"""装配容器（R2）：字段即依赖图——main 独占构造，transport 只 import 本模块。"""
from dataclasses import dataclass


@dataclass
class App:
    """聚合根：core 服务＋注入的 access 件＋装配元数据（main_sid/root 之外零逻辑）。"""
    store: object
    state: object
    auth: object
    router: object
    task: object
    gate: object
    health: object
    recover: object
    report: object
    oc: object
    gitx: object
    main_sid: str
