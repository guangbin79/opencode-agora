"""Req: F180 — wiki_write 追加面（mode=append）＋CAS（expected_hash）＋wiki_read hash。

两栈同构：v2（transport/httpd 单线程串行＝架构级互斥）＋v3（serverv3
ThreadingHTTPServer，append/CAS 临界区走 compose §4 全局写锁内读-拼-写）。
A2 并发锚只测 v3（v2 无竞态面）；跨进程互斥由 flock 单实例闸兜底（main.py，
同锁两栈，不入单测）。世系：F179（ag-c99eb2）同需求取消单，本条为其重立。

A1 append 单行→整页＝原文＋该行（原文无尾换行先补 \n）
A2 两线程并发 append 各 N 行→终态行数＝原＋2N、零丢失
A3 expected_hash 匹配成功；过期 hash 拒（409 conflict）且页面零改动、返回实况哈希
A4 wiki_read 返回 hash＝页文本 sha256
A5 不带新参＝旧行为（整页覆写）逐字节一致
A6 append 不存在页＝创建该页
"""
import hashlib
import threading

from tests.test_transport_httpd import HttpdTest, call as v2call
from tests.test_v3_server import _Base as V3Base


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestV2WikiAppendCas(HttpdTest):  # Req: F180（v2 栈）
    def _w(self, path, **body):
        return v2call(self.port, "POST", "/wiki_write",
                      {"path": path, **body}, self.root)

    def _r(self, path):
        return v2call(self.port, "POST", "/wiki_read", {"path": path}, self.root)

    def test_a1_a4_append_line_and_hash(self):  # A1＋A4
        self._w("spec/a1.md", text="line-0")  # 原文无尾换行
        st, r = self._w("spec/a1.md", text="line-1", mode="append")
        self.assertEqual((st, r), (200, {"ok": True}))
        st, r = self._r("spec/a1.md")
        self.assertEqual(st, 200)
        self.assertEqual(r["text"], "line-0\nline-1")  # 先补 \n 再拼
        self.assertEqual(r["hash"], _sha(r["text"]))
        self._w("spec/a1.md", text="line-2\n", mode="append")  # 已有尾换行不双补
        _, r = self._r("spec/a1.md")
        self.assertEqual(r["text"], "line-0\nline-1\nline-2\n")
        self._w("spec/a1b.md", text="x")  # 无尾换行页（round2 ocr low 锚）
        st, r = self._w("spec/a1b.md", text="", mode="append")  # 空文本＝零追加
        self.assertEqual((st, r), (200, {"ok": True}))
        _, r = self._r("spec/a1b.md")
        self.assertEqual(r["text"], "x")  # 零追加零改动（不补 \n 不重写）

    def test_a3_cas_match_and_stale_reject(self):  # A3
        self._w("spec/a3.md", text="v1\n")
        _, r = self._r("spec/a3.md")
        h = r["hash"]
        st, r = self._w("spec/a3.md", text="v2\n", expected_hash=h)  # 匹配成功
        self.assertEqual((st, r), (200, {"ok": True}))
        _, r = self._r("spec/a3.md")
        self.assertEqual(r["text"], "v2\n")
        stale = h  # v2 轮已推进，旧 hash 过期
        st, r = self._w("spec/a3.md", text="v3\n", expected_hash=stale)
        self.assertEqual(st, 409)
        self.assertIn(_sha("v2\n"), r["error"])  # 拒因携带实况哈希
        _, r = self._r("spec/a3.md")
        self.assertEqual(r["text"], "v2\n")  # 页面零改动

    def test_a5_a6_overwrite_regression_and_create(self):  # A5＋A6
        self._w("spec/a5.md", text="a\nb\n")
        st, r = self._w("spec/a5.md", text="c")  # 不带新参＝整页覆写
        self.assertEqual((st, r), (200, {"ok": True}))
        _, r = self._r("spec/a5.md")
        self.assertEqual(r["text"], "c")  # 逐字节一致：无尾换行不补
        self._w("spec/a6.md", text="first\n", mode="append")  # 不存在页＝创建
        _, r = self._r("spec/a6.md")
        self.assertEqual(r["text"], "first\n")

    def test_bad_mode_and_hash_type_400(self):
        st, r = self._w("spec/bad.md", text="x", mode="bogus")
        self.assertEqual(st, 400)
        self.assertIn("mode", r["error"])
        st, _ = self._w("spec/bad.md", text="x", expected_hash=123)
        self.assertEqual(st, 400)
        st, _ = self._w("tasks/bad.md", text="x", mode="append")
        self.assertEqual(st, 403)  # 服务独占前缀守卫对 append 同样生效


class TestV3WikiAppendCas(V3Base):  # Req: F180（v3 栈：legacy＋/v3 双面）
    def _w(self, path, body, face="wiki_write"):
        return self.call(face, {"path": path, **body}, self.root)

    def _r(self, path, face="wiki_read"):
        return self.call(face, {"path": path}, self.root)

    def test_a1_a4_append_line_and_hash_both_faces(self):  # A1＋A4
        self._w("spec/a1.md", {"text": "line-0"})
        for face in ("wiki_write", "v3/wiki_write"):  # legacy 适配面＋v3 正名面
            st, r = self._w("spec/a1.md", {"text": f"{face}\n",
                                           "mode": "append"}, face)
            self.assertEqual((st, r), (200, {"ok": True}), face)
        st, r = self._r("spec/a1.md")
        self.assertEqual(st, 200)
        self.assertEqual(r["text"], "line-0\nwiki_write\nv3/wiki_write\n")
        self.assertEqual(r["hash"], _sha(r["text"]))  # A4：hash＝页文本 sha256
        st, r = self._r("spec/a1.md", "v3/wiki_read")
        self.assertEqual((st, r["hash"]), (200, _sha(r["text"])))
        self._w("spec/a1b.md", {"text": "x"})  # 无尾换行页（round2 ocr low 锚）
        st, r = self._w("spec/a1b.md", {"text": "", "mode": "append"})
        self.assertEqual((st, r), (200, {"ok": True}))  # 空文本＝零追加
        _, r = self._r("spec/a1b.md")
        self.assertEqual(r["text"], "x")  # 零追加零改动（不补 \n 不重写）

    def test_a2_concurrent_append_zero_loss(self):  # A2：v3 真并发（写锁内 RMW）
        self._w("spec/a2.md", {"text": "base\n"})
        n, errors = 25, []

        def hammer(tag):
            try:
                for i in range(n):
                    s, b = self._w("spec/a2.md", {"text": f"{tag}-{i}\n",
                                                  "mode": "append"})
                    if (s, b) != (200, {"ok": True}):
                        errors.append((s, b))
            except Exception as e:  # 线程内异常不冒泡——收集后统一断言
                errors.append(repr(e))

        ts = [threading.Thread(target=hammer, args=(t,)) for t in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        _, r = self._r("spec/a2.md")
        lines = r["text"].splitlines()
        self.assertEqual(len(lines), 1 + 2 * n)  # 零丢失
        self.assertEqual({ln for ln in lines if ln != "base"},
                         {f"{t}-{i}" for t in ("a", "b") for i in range(n)})

    def test_a3_cas_match_and_stale_reject(self):  # A3：legacy 平面＋v3 结构化
        self._w("spec/a3.md", {"text": "v1\n"})
        _, r = self._r("spec/a3.md")
        h = r["hash"]
        st, r = self._w("spec/a3.md", {"text": "v2\n", "expected_hash": h})
        self.assertEqual((st, r), (200, {"ok": True}))
        st, r = self._w("spec/a3.md", {"text": "v3\n", "expected_hash": h})
        self.assertEqual(st, 409)
        actual = _sha("v2\n")
        self.assertIn(actual, r["error"])  # legacy 面：拒因携带实况哈希
        # F106 实战形态：读→携实况 hash 追加（append×CAS 组合路径）
        st, r = self._w("spec/a3.md", {"text": "v2b\n", "mode": "append",
                                       "expected_hash": actual})
        self.assertEqual((st, r), (200, {"ok": True}))
        _, r = self._r("spec/a3.md")
        self.assertEqual(r["text"], "v2\nv2b\n")
        st, r = self._w("spec/a3.md", {"text": "v4\n", "expected_hash": "stale"},
                        face="v3/wiki_write")
        self.assertEqual(st, 409)
        self.assertEqual(r["error"]["code"], "conflict")  # 结构化 409 语义
        self.assertEqual(r["error"]["details"]["hash"], _sha("v2\nv2b\n"))
        self.assertTrue(r["error"]["retryable"])
        _, r = self._r("spec/a3.md")
        self.assertEqual(r["text"], "v2\nv2b\n")  # 页面零改动

    def test_a5_a6_overwrite_regression_and_create(self):  # A5＋A6
        self._w("spec/a5.md", {"text": "a\nb\n"})
        st, r = self._w("spec/a5.md", {"text": "c"})
        self.assertEqual((st, r), (200, {"ok": True}))
        _, r = self._r("spec/a5.md")
        self.assertEqual(r["text"], "c")
        self._w("spec/a6.md", {"text": "first\n", "mode": "append"})
        _, r = self._r("spec/a6.md")
        self.assertEqual(r["text"], "first\n")

    def test_bad_params_400(self):
        st, r = self._w("spec/bad.md", {"text": "x", "mode": "bogus"},
                        face="v3/wiki_write")
        self.assertEqual((st, r["error"]["code"]), (400, "invalid_argument"))
        st, _ = self._w("spec/bad.md", {"text": "x", "expected_hash": None},
                        face="v3/wiki_write")
        self.assertEqual(st, 400)
        st, _ = self._w("tasks/bad.md", {"text": "x", "mode": "append"})
        self.assertEqual(st, 403)  # 独占守卫（resolve 后落点判）对 append 生效

    def test_all_write_paths_take_storage_lock(self):  # Req: F180（round1 终闸）
        """OCR/B 册同拍 finding：缺省覆写不得绕过 §4 写锁——锁内 append 的
        读-写窗会吞掉锁外盲覆写（丢更新，正是 F180 封堵类）。确定性锚＝
        计数锁代理断言任意 mode/参数组合的写调用都取过存储写锁。"""

        class _Count:
            def __init__(self, inner):
                self.inner, self.acquires = inner, 0

            def __enter__(self):
                self.acquires += 1
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        orig = self.stack.write_lock
        counting = _Count(orig)
        self.stack.write_lock = counting  # SerialRepo 持原锁引用，仅换动态读点
        try:
            for body in ({"text": "x"},  # 缺省覆写（round1 finding 的主锚）
                         {"text": "y", "mode": "append"}):
                s, b = self._w("spec/lock.md", body)
                self.assertEqual((s, b), (200, {"ok": True}), body)
            _, r = self._r("spec/lock.md")
            s, b = self._w("spec/lock.md", {"text": "z",
                                            "expected_hash": r["hash"]})
            self.assertEqual((s, b), (200, {"ok": True}))  # CAS 匹配成功
        finally:
            self.stack.write_lock = orig
        self.assertGreaterEqual(counting.acquires, 3)
