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
    try:
        rel = py.relative_to(ROOT / "agorad")
    except ValueError:  # 包外顶层脚本（./agora 无扩展名入口——F140 显式入册，勿按扩展名漏盘）
        return []
    return ["agorad", *rel.with_suffix("").parts[:-1]]


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

    def test_contracts_stdlib_only(self):  # Req: F130——v3 Contract Catalog 仅 stdlib
        assert_only("agorad/contracts.py", may_import=[])

    def test_v3_domain_stdlib_only(self):  # Req: F131——domain 层仅 stdlib
        assert_only("agorad/domain.py")

    def test_v3_storage_layer(self):  # Req: F133——storage 仅 stdlib＋domain
        assert_only("agorad/storage.py", may_import=["agorad.domain"])

    def test_v3_messaging_layer(self):  # Req: F134——messaging 仅 stdlib＋domain（OC/仓库/导出经 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/messaging.py", may_import=["agorad.domain"])

    def test_v3_tasksvc_layer(self):  # Req: F135——task 服务仅 stdlib＋domain＋contracts（仓库/gitx/oc/Notifier 全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/tasksvc.py",
                    may_import=["agorad.domain", "agorad.contracts"])

    def test_v3_timersvc_layer(self):  # Req: F145——timer 服务仅 stdlib＋domain＋contracts（仓库/Notifier 全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/timersvc.py",
                    may_import=["agorad.domain", "agorad.contracts"])

    def test_v3_gateverify_layer(self):  # Req: F136——verify/review 门执行体仅 stdlib＋domain＋contracts（仓库/gitx/runner/锚/产物/Notifier/streak 全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/gateverify.py",
                    may_import=["agorad.domain", "agorad.contracts"])

    def test_v3_gatedelivery_layer(self):  # Req: F137——交付执行体仅 stdlib＋domain＋contracts＋gateverify 基类（gitx 交付面/交付锁/名册全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/gatedelivery.py",
                    may_import=["agorad.domain", "agorad.contracts",
                                "agorad.gateverify"])

    def test_v3_authsvc_layer(self):  # Req: F138——auth 服务仅 stdlib＋domain＋contracts（仓库/oc 探测全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/authsvc.py",
                    may_import=["agorad.domain", "agorad.contracts"])

    def test_v3_healthsvc_layer(self):  # Req: F138——health 服务仅 stdlib＋domain（仓库/oc/gitx/Notifier/recover 钩全 Protocol 注入，不 import access/storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/healthsvc.py", may_import=["agorad.domain"])

    def test_v3_migrator_layer(self):  # Req: F139——迁移器仅 stdlib＋domain＋storage（SQL 仅经 storage、源只读；不 import access——v2 磁盘形状解析自含；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/migrator.py",
                    may_import=["agorad.domain", "agorad.storage"])

    def test_v3_cutover_layer(self):  # Req: F140——切换/导出/备份服务面仅 stdlib＋domain＋storage＋migrator（不直接写 SQL、不 import access——backup/checkpoint/体检/全量扫描全走 storage；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/cutover.py",
                    may_import=["agorad.domain", "agorad.storage",
                                "agorad.migrator"])

    def test_v3_adapter_layer(self):  # Req: F141——v2 适配层仅 stdlib＋domain＋contracts（纯数据面翻译：请求/响应/错误/时间向，不 import storage/access；盘面/活性/探测经参数注入归 T13a 装配；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/adapter.py",
                    may_import=["agorad.domain", "agorad.contracts"])

    def test_v3_compose_layer(self):  # Req: F142——v3 装配层（§1 main 唯一装配的 v3 对应件）：services＋storage＋access＋cutover 导出器全可入；oc/gitx/runner/anchors 经构造注入（Protocol 面，本模块不构造真件）；F155 后端选择归装配——gitstore 同 storage 家族可入；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖
        assert_only("agorad/compose.py",
                    may_import=["agorad.domain", "agorad.contracts",
                                "agorad.storage", "agorad.access",
                                "agorad.cutover", "agorad.tasksvc",
                                "agorad.messaging", "agorad.authsvc",
                                "agorad.healthsvc", "agorad.gateverify",
                                "agorad.gatedelivery", "agorad.timersvc",
                                "agorad.gitstore"])

    def test_v3_serverv3_layer(self):  # Req: F142——v3 传输层仅 contracts/adapter/domain＋application 服务（不 import storage/access——存储经 V3Stack 注入；board/doctor/消息形状经 adapter 翻译）；SQL 圈禁由 test_sql_confined_to_storage 自动覆盖
        assert_only("agorad/serverv3.py",
                    may_import=["agorad.domain", "agorad.contracts",
                                "agorad.adapter", "agorad.tasksvc",
                                "agorad.messaging", "agorad.authsvc",
                                "agorad.healthsvc", "agorad.gateverify",
                                "agorad.gatedelivery"])

    def test_entry_script_layer(self):  # Req: F140——无扩展名入口 ./agora 显式入册（勿按扩展名漏盘）：仅 stdlib＋agorad.cutover（延迟导入，HTTP 谓词零 agorad 依赖）
        assert_only("agora", may_import=["agorad.cutover"])

    def test_v3_gitstore_layer(self):  # Req: F147——GitStore 仅 stdlib＋domain＋storage（错误基类复用 storage；SQL/sqlite 字样圈禁由 test_sql_confined_to_storage 自动覆盖）
        assert_only("agorad/gitstore.py",
                    may_import=["agorad.domain", "agorad.storage"])

    def test_git_subprocess_confined_to_gitstore(self):  # Req: F147——journal git 子进程圈禁 gitstore：journal.git 字样全 agorad 唯一落点；v3 应用层（含 storage/服务/装配/传输）零 git 子进程自起（业务 git 归 access/gitx 注入与 v2 main.py/access，不在本守卫面）
        v3_modules = {
            "contracts.py", "domain.py", "storage.py", "messaging.py",
            "tasksvc.py", "timersvc.py", "gateverify.py", "gatedelivery.py",
            "authsvc.py", "healthsvc.py", "migrator.py", "cutover.py",
            "adapter.py", "compose.py", "serverv3.py"}
        for py in sorted((ROOT / "agorad").rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            rel = str(py.relative_to(ROOT))
            if py.name == "gitstore.py":
                self.assertIn('"git", ', text)  # 唯一 git 起点在案
                continue
            self.assertNotIn(
                "journal.git", text,
                f"{rel}: journal.git 只许出现在 agorad/gitstore.py（设计 §4）")
            if py.name in v3_modules:
                for marker in ('"git"', "'git'"):
                    self.assertNotIn(
                        marker, text,
                        f"{rel}: v3 应用层禁自起 git 子进程（归 gitstore/access 注入）")

    def test_sql_confined_to_storage(self):  # Req: F133——SQL 不得出 storage（设计 §1）；F140：./agora 无扩展名入口同面入册
        for py in sorted((ROOT / "agorad").rglob("*.py")) + [ROOT / "agora"]:
            if py.name == "storage.py":
                continue
            text = py.read_text(encoding="utf-8")
            for marker in ("sqlite3", "CREATE TABLE", "INSERT INTO", "SELECT ",
                           "PRAGMA ", "EXPLAIN QUERY PLAN"):
                self.assertNotIn(
                    marker, text,
                    f"{py.name}: SQL/sqlite3 只许出现在 agorad/storage.py（设计 §1）")


if __name__ == "__main__":
    unittest.main()
