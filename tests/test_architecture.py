"""分层守卫（F2）：AST 扫描 agorad 全部 import——方向＝架构铁律。

access 仅 stdlib；core 仅 stdlib（依赖全构造注入，禁 import 兄弟模块）；
transport 加 agorad.core；main.py 汇装配（三本地包皆可）。相对导入按所在
包解析，allowed_local 按前缀匹配；动态 import（importlib/__import__）不在
静态扫描范围。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_STDLIB = sys.stdlib_module_names


def _pkg_parts(py):
    """文件所在包的模块段：agorad/core/gate.py 与 …/core/__init__.py 均 ['agorad','core']。"""
    rel = py.relative_to(ROOT / "agorad").with_suffix("").parts
    return ["agorad", *rel[:-1]]


def _import_names(node, pkg):
    """一条 import 语句涉及的绝对模块名（import 取各别名的顶层名）。"""
    if isinstance(node, ast.Import):
        return [a.name.split(".")[0] for a in node.names]
    if node.level == 0:  # 绝对 from-import：模块名即所导入名
        return [node.module]
    parts = pkg[: len(pkg) - (node.level - 1)]  # level=1 同包、2 父包…
    return [".".join([*parts, *node.module.split(".")]) if node.module
            else ".".join(parts)]


def _violations(py, allowed):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    pkg = _pkg_parts(py)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _import_names(node, pkg):
            if name.split(".")[0] in _STDLIB:
                continue
            if any(name == p or name.startswith(p + ".") for p in allowed):
                continue
            bad.append(f"{py.relative_to(ROOT)}:{node.lineno} imports {name}")
    return bad


def _assert_clean(files, allowed):
    bad = [v for f in files for v in _violations(f, allowed)]
    assert not bad, "分层违规（仅许 stdlib＋白名单）:\n" + "\n".join(bad)


def assert_pkg_imports(pkg_dir, allowed_local=()):
    _assert_clean(sorted((ROOT / pkg_dir).rglob("*.py")), tuple(allowed_local))


def assert_only(file, may_import=()):
    _assert_clean([ROOT / file], tuple(may_import))


class TestLayering(unittest.TestCase):
    def test_import_direction(self):  # Req: F2
        assert_pkg_imports("agorad/access", allowed_local=[])          # 仅 stdlib
        assert_pkg_imports("agorad/core", allowed_local=[])            # 仅 stdlib（依赖注入）
        assert_pkg_imports("agorad/transport", allowed_local=["agorad.core"])
        assert_only("agorad/main.py",
                    may_import=["agorad.access", "agorad.core", "agorad.transport"])


if __name__ == "__main__":
    unittest.main()
