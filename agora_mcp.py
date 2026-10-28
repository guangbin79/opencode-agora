#!/usr/bin/env python3
"""agora MCP 壳：stdio newline-JSON-RPC ↔ agorad HTTP 薄代理（零自身逻辑）。

独立单文件（Python 3 stdlib ONLY，不进 agorad 包——opencode 拉起的独立
stdio 进程）。每行一请求一应答；通知（无 id）不应答；stderr 留诊断。
11 工具→`POST /<tool_name>` 根级路由；arguments 内 `_token` 剥除→
X-Agora-Token 头。服务地址：env AGORAD_URL 优先，缺省读
`$AGORA_HOME/.agora/service.json` 的 port（lift 惰性拉起后每调用重读）。
错误映射（负数区，message 服务原文）：400→-32602、401→-32001、403→-32003、
404→-32004、409→-32009、500→-32603；未知工具/缺牌壳内直错；服务不可达→
-32000（默认拒，安全方向）。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
_VERSION = "0.1.0"

_STR = {"type": "string"}
_TID = {"type": "string",
        "description": "任务 id（gt-xxxxxx，task_create/task_dispatch 返回）"}
_TOOLS = [
    {"name": "task_create", "description": "登记任务（todo）（dispatcher/root）",
     "inputSchema": {"type": "object", "properties": {
         "goal": _STR, "project_dir": _STR,
         "after": {"type": "string", "description": "前驱任务 id（gt-xxxxxx，B7 链）"},
         "target_branch": {"type": "string",
                           "description": "合入目标分支（外部工作流；缺省＝仓默认支）"},
         "title": {"type": "string", "description": "简短任务描述（缺省截取 goal 首句 40 字）"}},
         "required": ["goal", "project_dir"]}},
    {"name": "task_dispatch", "description": "派发任务给 worker（回传凭据）",
     "inputSchema": {"type": "object", "properties": {"task": _TID},
                     "required": ["task"]}},
    {"name": "task_update", "description": "登记 verify 命令（仅派发方/root；worker 不可用）",
     "inputSchema": {"type": "object", "properties": {
         "task": _TID, "verify_cmd": _STR},
         "required": ["task", "verify_cmd"]}},
    {"name": "task_cancel", "description": "中止任务（级联下级）（派发方/root；worker 不可用）",
     "inputSchema": {"type": "object", "properties": {"task": _TID},
                     "required": ["task"]}},
    {"name": "post", "description": "链边发消息（方向服务按角色定）",
     "inputSchema": {"type": "object", "properties": {
         "task": _TID,
         "kind": {"type": "string",
                  "enum": ["note", "question", "answer", "done", "verify",
                           "verify_reject"],
                  "description": "note=附言；question=问；answer=答；done=完成回填；"
                                 "verify=提案验收命令；verify_reject=驳回提案"},
         "body": {"type": "string",
                  "description": "done 时＝纯 40 位完整 commit sha（无前缀无附言）；"
                                 "其余 kind＝正文"}},
         "required": ["task", "kind", "body"]}},
    {"name": "check_mail", "description": "拉取本会话收件",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "board", "description": "看板快照（给 task 返回单任务详情）（dispatcher/root；worker 不可用）",
     "inputSchema": {"type": "object", "properties": {"task": _TID}}},
    {"name": "doctor", "description": "诊断视图（年龄排名＋环境残留）（dispatcher/root；worker 不可用）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wiki_read", "description": "读 wiki 页",
     "inputSchema": {"type": "object", "properties": {"path": _STR},
                     "required": ["path"]}},
    {"name": "wiki_write", "description": "写 wiki 页（wiki/tasks/ 前缀服务独占）",
     "inputSchema": {"type": "object", "properties": {"path": _STR,
                                                      "text": _STR},
                     "required": ["path", "text"]}},
    {"name": "enforce", "description": "拨强制旗（仅 root；on 须布尔）",
     "inputSchema": {"type": "object", "properties": {
         "on": {"type": "boolean"}}, "required": ["on"]}},
    {"name": "wait", "description": "本地休眠指定秒数（Code Mode 无 setTimeout 的补偿；上限 120s）。"
                                    "本地休眠，非任务条件等待——终态等待用 CLI agora wait <task>",
     "inputSchema": {"type": "object", "properties": {
         "seconds": {"type": "integer", "minimum": 0, "maximum": 120}}}},
]
_NAMES = frozenset(t["name"] for t in _TOOLS)
_ERROR_CODES = {400: -32602, 401: -32001, 403: -32003, 404: -32004,
                409: -32009, 500: -32603}
# 测试锚（常量别名）；映射全档负数区且互异
ERROR_CODES = _ERROR_CODES


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def resolve_url():
    """AGORAD_URL 优先；缺省读 $AGORA_HOME/.agora/service.json 的 port。"""
    url = os.environ.get("AGORAD_URL")
    if url:
        return url.rstrip("/")
    home = os.environ.get("AGORA_HOME") or str(Path.home() / "plaza")
    path = Path(home) / ".agora" / "service.json"
    try:
        port = json.loads(path.read_text(encoding="utf-8")).get("port")
    except (OSError, ValueError, AttributeError):
        port = None
    if not port:
        raise OSError(f"agorad 未发现（{path} 无 port 且未设 AGORAD_URL）")
    return f"http://127.0.0.1:{port}"


def http_call(name, arguments):
    """POST /<name>：_token/_sid 剥除→头（_sid＝R25-A 调用者真实会话 sid，服务绑主会话）；
    返回 (status, body)。HTTPError 亦为值。"""
    args = dict(arguments)
    token = args.pop("_token", None)
    sid = args.pop("_sid", None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agora-Token"] = token
    if sid:
        headers["X-Agora-Sid"] = sid
    req = urllib.request.Request(
        f"{resolve_url()}/{name}",
        data=json.dumps(args, ensure_ascii=False).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def handle(msg):
    """一条请求→一条应答 dict；通知（无 id 的非 initialize 面）返回 None。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "请求须为 JSON 对象")
    rid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agora", "version": _VERSION}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        return _tools_call(rid, msg.get("params") or {})
    if rid is None:  # 其余通知：静默
        return None
    if method is None:
        return _err(rid, -32600, "缺 method")
    return _err(rid, -32601, f"未知方法: {method}")


def _tools_call(rid, params):
    name = params.get("name")
    if name == "wait":  # 壳层本地休眠（Code Mode 无 setTimeout 的补偿）
        secs = max(0, min(params.get("arguments", {}).get("seconds", 30), 120))
        time.sleep(secs)
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps({"ok": True, "waited": secs})}]}}
    if name not in _NAMES:
        return _err(rid, -32602, f"未知工具: {name}")
    args = params.get("arguments")
    if not isinstance(args, dict) or not args.get("_token"):
        return _err(rid, -32001, "缺 _token（未附牌）——服务不可达默认拒同方向")
    try:
        status, body = http_call(name, args)
    except OSError as e:  # 连接拒/发现失败：lift 线索报因
        return _err(rid, -32000, f"服务不可达: {e}")
    print(f"[agora_mcp] {name} -> {status}", file=sys.stderr)
    if status != 200:
        return _err(rid, _ERROR_CODES.get(status, -32000),
                    str(body.get("error", body)) if isinstance(body, dict)
                    else str(body))
    text = json.dumps(body, ensure_ascii=False)
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}]}}


def serve(lines=None, out=None):
    """stdio 主循环：一行一请求一应答；EOF 即退；坏行报 -32700 不死循环。"""
    lines = sys.stdin if lines is None else lines
    out = sys.stdout if out is None else out
    while True:
        line = lines.readline()
        if not line:  # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except ValueError:
            resp = _err(None, -32700, f"坏 JSON 行: {line[:80]}")
        except Exception as e:  # 壳内兜底：不让单请求杀进程
            resp = _err(None, -32603, f"壳内错误: {e}")
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


def write_pointer():
    """启动桥（gt-32c051）：解析出的 AGORA_HOME 落 ~/.agora-home 指针（单行覆盖）——
    MCP 壳是配置 env 的唯一必有进程，写指针供插件/agorad/board 无 env 时统一发现。
    桥面失败（HOME 只读等）不阻壳启动。"""
    home = os.environ.get("AGORA_HOME") or str(Path.home() / "plaza")
    target = Path.home() / ".agora-home"
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(home + "\n", encoding="utf-8")
        os.replace(tmp, target)  # 原子替换：并发读者不见 truncate 空窗（评审环 B3）
    except (OSError, ValueError):  # ValueError＝env 含坏字节的 UnicodeEncodeError 同不阻壳
        pass  # 残留 tmp 无害：下次启动覆盖


def main():
    write_pointer()
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
