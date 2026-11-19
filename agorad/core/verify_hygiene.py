"""Req: F99 verify 命令卫生纯静态检查器＋F123 shell 语法预检（登记面聚合）。

agora verify 在任务 env worktree 的 cwd 执行，但命令内绝对 cd 或主仓绝对路径
会绕开 env 隔离读改主检出（ag-df192c 假红先例）。本模块对单条 shell 命令做
纯文本分析：不做命令替换、不 source，仅 realpath 归一 project_dir/home 后做
词法判定；语法层经 /bin/sh -n -c 无执行预检（F123：与生产 runner shell=True
同方言——合法 &&/||/|/;/多行 shell 放行，只拒真语法错误）。

规则（finding 固定键 code/fragment/hint/blocking，按命令出现顺序去重）：
- ABSOLUTE_CD（阻断）：cd/pushd 目标为绝对路径（含引号包裹）——无论目标在
  仓内外一律改写 cwd 语义，脱离任务 env。
- PROJECT_PATH（阻断）：非引号字面量 token 中的绝对路径（含 $HOME/${HOME}/~
  展开）落在 project_dir 内且非依赖目录（venv/.venv/node_modules/site-packages
  按路径段豁免）——覆盖可执行文件、source/. 目标、普通文件参数与 PYTHONPATH
  等赋值。引号内路径字面量放行（grep 模式文本等不得误杀）。
- GLOBAL_STATE（仅告警，blocking=false）：落在 HOME 下 .config/.local/state/
  .local/share 的可变全局面——跨任务共享态，隔离卫生问题但不至于假红。

明确放行：相对 cd、project 外部绝对工具/跨仓引用、project 内依赖目录路径。
shlex 无法解析但语法过关的构造（复杂 heredoc 等）静默返回空（无法安全静态
判定）；未闭合 quote/heredoc 等真语法错误由 F123 预检先拦（不再 fail-open）。
"""
import os
import re
import shlex
import subprocess

# 依赖目录按路径段豁免：env 内 venv/node_modules 是隔离副本，非主仓工作区
DEP_SEGMENTS = frozenset({"venv", ".venv", "node_modules", "site-packages"})

# HOME 下可变全局状态目录（相对 home）：隔离卫生告警面，不阻断
HOME_STATE_DIRS = (".config", os.path.join(".local", "state"),
                   os.path.join(".local", "share"))

_HINTS = {
    "ABSOLUTE_CD": ("绝对 cd/pushd 改写 cwd 语义、脱离任务 env（ag-df192c 假红）"
                    "——改用相对路径 cd"),
    "PROJECT_PATH": ("绝对路径落在主仓内，verify 将读改主检出而非任务 env 副本"
                     "——改用相对路径或依赖目录"),
    "GLOBAL_STATE": ("HOME 下全局状态目录被 verify 改写，属跨任务共享可变态"
                     "——建议落到 project_dir 内"),
    # Req: F123 登记命令语法预检（/bin/sh -n 无执行，同生产 shell 方言）
    "SHELL_SYNTAX": ("登记命令 shell 语法无效——用 /bin/sh -n -c '<命令>'"
                     " 复现修复后再登记"),
    "SHELL_SYNTAX_PRECHECK": ("语法预检器不可用（/bin/sh 缺失/超时/异常）"
                              "——fail-closed 拒登记，恢复预检器后重试"),
}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DOLLAR_HOME_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")
_PUNCT = frozenset("();<>|&")


def _tokens(cmd):
    """shlex 词法（posix=False 保留引号；punctuation_chars 分离 &&/;/| 等操作符）。"""
    lex = shlex.shlex(cmd, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _segments(tokens):
    """按 shell 操作符切简单命令段，返回 token 列表序列。"""
    seg, out = [], []
    for tok in tokens:
        if tok and all(c in _PUNCT for c in tok):
            if seg:
                out.append(seg)
                seg = []
        else:
            seg.append(tok)
    if seg:
        out.append(seg)
    return out


def _is_quoted_literal(tok):
    return len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]


def _unquote(tok):
    return tok[1:-1] if _is_quoted_literal(tok) else tok


def _expand_home(text, home):
    text = _DOLLAR_HOME_RE.sub(home, text)
    return home + text[1:] if text.startswith("~") else text


def _inside(path, base):
    """path==base 或位于 base 之下时返回相对段，否则 None（两侧均已归一）。"""
    if path == base:
        return ""
    if path.startswith(base + os.sep):
        return path[len(base) + 1:]
    return None


def _classify(abs_path, project, home):
    """绝对路径归类：主仓内（非依赖段）＝PROJECT_PATH；HOME 全局面＝GLOBAL_STATE。"""
    rel = _inside(abs_path, project)
    if rel is not None:
        if DEP_SEGMENTS.isdisjoint(rel.split(os.sep)):
            return "PROJECT_PATH"
        return None  # 依赖目录：env 内自带副本，非主仓工作区
    if home:
        for d in HOME_STATE_DIRS:
            if _inside(abs_path, os.path.join(home, d)) is not None:
                return "GLOBAL_STATE"
    return None


def _safe_fragment(cmd, limit=80):
    """F123 语法 finding 的安全截断：控制字符剥离＋空白折叠保单行＋限长。"""
    frag = re.sub(r"[\x00-\x1f\x7f]", "", cmd)
    frag = " ".join(frag.split())
    return frag[:limit] + "…" if len(frag) > limit else frag


def check_shell_syntax(cmd):
    """Req: F123 登记前无执行语法预检：/bin/sh -n -c 只解析不执行，与生产
    runner（Popen shell=True）同方言。返回 None＝通过；语法坏＝SHELL_SYNTAX
    blocking finding；预检器自身 OSError/超时＝SHELL_SYNTAX_PRECHECK
    blocking finding（fail-closed，不得静默放行）。"""
    try:
        proc = subprocess.run(["/bin/sh", "-n", "-c", cmd],
                              capture_output=True, timeout=1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # ValueError：cmd 含 NUL 时 fork_exec 拒参——预检器无法运行，同归
        # fail-closed 面（评审修复：不得裸崩出登记面变 500）
        return {"code": "SHELL_SYNTAX_PRECHECK",
                "fragment": _safe_fragment(cmd),
                "hint": _HINTS["SHELL_SYNTAX_PRECHECK"], "blocking": True}
    if proc.returncode != 0:
        return {"code": "SHELL_SYNTAX", "fragment": _safe_fragment(cmd),
                "hint": _HINTS["SHELL_SYNTAX"], "blocking": True}
    return None


def find_verify_hygiene_issues(cmd, project_dir, home=None):
    """静态检查单条 verify 命令，返回 finding dict 列表（code/fragment/hint/blocking）。

    home 缺省取进程 HOME；project_dir 以 realpath 归一。纯文本分析：不执行
    命令、不展开命令替换、不读取被引用文件。
    """
    project = os.path.realpath(project_dir)
    if home is None:
        home = os.environ.get("HOME") or None
    else:
        home = os.path.realpath(home)
    hit = check_shell_syntax(cmd)
    if hit is not None:  # Req: F123：语法坏即阻断，词法面不再可靠不再分析
        return [hit]
    try:
        segments = _segments(_tokens(cmd))
    except ValueError:
        return []  # 引号不闭合等：无法安全静态判定，静默放行
    findings, seen = [], set()

    def add(code, fragment):
        key = (code, fragment)
        if key not in seen:  # 同 token 同码不重复
            seen.add(key)
            findings.append({"code": code, "fragment": fragment,
                             "hint": _HINTS[code],
                             "blocking": code != "GLOBAL_STATE"})

    for seg in segments:
        absolute_cd_tok = None
        if seg[0] in ("cd", "pushd"):
            for tok in seg[1:]:
                if tok.startswith("-") and tok != "-":
                    continue
                if os.path.isabs(_unquote(tok)):
                    add("ABSOLUTE_CD", tok)
                    absolute_cd_tok = tok
                break  # 只判首个操作数；非字面量绝对目标留给通用分析（cd $HOME/proj）
        for tok in seg:
            if tok is absolute_cd_tok or _is_quoted_literal(tok):
                continue  # 已按 ABSOLUTE_CD 判定；引号字面量放行
            content = _expand_home(_unquote(_ASSIGN_RE.sub("", tok, count=1)), home or "")
            for piece in content.split(":"):
                if piece.startswith("/") and home:
                    code = _classify(os.path.normpath(piece), project, home)
                    if code:
                        add(code, tok)
    return findings
