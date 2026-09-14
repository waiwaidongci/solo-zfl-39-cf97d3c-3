# -*- coding: utf-8 -*-
"""真实 HTTP 端到端验证（标准库，无第三方依赖）：

启动真实服务子进程 → 走 HTTP 完成
录入 → 试算校验 → 计价（分段/未来价拦截）→ 双人复核 → FIFO 抵扣付款
→ 并发付款（恰好一成一败）→ 冲正 → 杀进程重启 → 状态恢复并继续业务。

运行：python3 -m unittest tests.e2e_http -v
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.cookiejar import CookieJar
from urllib.request import Request, build_opener, HTTPCookieProcessor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = int(os.environ.get("PULP_TEST_PORT", "18080"))
BASE = f"http://127.0.0.1:{PORT}"


class ApiError(Exception):
    def __init__(self, status, payload):
        self.status = status
        self.code = payload.get("error")
        self.message = payload.get("message", "")
        super().__init__(f"HTTP {status} {self.code}: {self.message}")


class Client:
    """每个用户一个 Client：独立 cookie 罐，模拟不同操作员。"""
    def __init__(self):
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, raw=None):
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(BASE + path, data=data, headers=headers, method=method)
        try:
            resp = self.opener.open(req, timeout=15)
            return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib_err() as e:
            payload = {}
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                pass
            raise ApiError(e.code, payload)

    def login(self, username):
        return self.call("POST", "/api/login", {"username": username})[1]

    def ok(self, method, path, body=None):
        status, data = self.call(method, path, body)
        self.assert_ok(status, data)
        return data

    def fail(self, method, path, body=None, raw=None):
        try:
            self.call(method, path, body, raw=raw)
        except ApiError as e:
            return e
        raise AssertionError(f"{method} {path} 应当失败却成功了")

    @staticmethod
    def assert_ok(status, data):
        if status >= 400:
            raise AssertionError(f"请求失败 {status}: {data}")


def urllib_err():
    from urllib.error import HTTPError
    return HTTPError


def wait_port(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("服务未在规定时间内启动")


class E2ECase(unittest.TestCase):
    proc = None
    db_path = None

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.db_path)  # 让 init_db 全新创建
        env = dict(os.environ, PULP_DB_PATH=cls.db_path,
                   PULP_PORT=str(PORT), PULP_HOST="127.0.0.1")
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "run.py")],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            wait_port("127.0.0.1", PORT)
        except Exception:
            out = cls.proc.stdout.read() if cls.proc.stdout else ""
            raise RuntimeError(f"服务启动失败:\n{out}")

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.send_signal(signal.SIGINT)
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db_path + ext)
            except OSError:
                pass

    def setUp(self):
        self.anon = Client()
        self.u1 = Client(); self.u1.login("u1")  # 张三 过磅员（制单）
        self.u2 = Client(); self.u2.login("u2")  # 李四 财务
        self.u3 = Client(); self.u3.login("u3")  # 王五 主管
        self.u4 = Client(); self.u4.login("u4")  # 赵六 过磅员（第二复核人）
        self.suppliers = self.u1.call("GET", "/api/suppliers")[1]
        self.s001 = next(s for s in self.suppliers if s["code"] == "S001")
        self.s002 = next(s for s in self.suppliers if s["code"] == "S002")
        self.d = lambda delta: (date.today() + timedelta(days=delta)).isoformat()

    # ---------------------------------------------------------- 0. 网页可访问
    def test_00_page_and_static_served(self):
        for path, marker in (("/", "纸浆原料验收入库与结算"),
                             ("/static/app.js", "decimalOk"),
                             ("/static/styles.css", "--accent")):
            body = self.u1.opener.open(BASE + path, timeout=5).read().decode("utf-8")
            self.assertIn(marker, body, f"{path} 内容异常")

    # ---------------------------------------------------------- 1. 接口校验
    def test_01_api_validation(self):
        # 未登录写操作被拒
        e = self.anon.fail("POST", "/api/receipts", {"supplier_id": 1})
        self.assertEqual(e.status, 401)

        # 非法 JSON
        e = self.u1.fail("POST", "/api/receipts", raw=b"{not json")
        self.assertEqual(e.status, 400)
        self.assertEqual(e.code, "invalid_json")

        base = dict(supplier_id=self.s001["id"], weigh_date=self.d(-5), grade="A",
                    gross_kg="26000", tare_kg="9000",
                    water_pct="13.5", impurity_pct="2.2")
        bad_cases = [
            (dict(base, tare_kg="26000"), "tare_ge_gross"),
            (dict(base, tare_kg="30000"), "tare_ge_gross"),
            (dict(base, water_pct="120"), "invalid_range"),
            (dict(base, impurity_pct="-0.1"), "invalid_range"),
            (dict(base, weigh_date=self.d(1)), "future_date"),
            (dict(base, grade="X"), "invalid_grade"),
            (dict(base, gross_kg="abc"), "invalid_number"),
            (dict(base, supplier_id=99999), "supplier_not_found"),
            ({**base, "tare_kg": None}, "invalid_number"),
        ]
        for body, expect_code in bad_cases:
            e = self.u1.fail("POST", "/api/receipts", body)
            self.assertEqual(e.status, 400 if e.code != "supplier_not_found" else 404,
                             (body, e.status, e.code))
            self.assertEqual(e.code, expect_code, (body, e.code, e.message))

        # 过磅员不能登记预付款
        e = self.u1.fail("POST", "/api/prepayments", {
            "supplier_id": self.s001["id"], "amount_yuan": "100",
            "received_at": self.d(0) + " 10:00:00"})
        self.assertEqual(e.code, "forbidden")

    # ---------------------------------------------------------- 2. 主链路
    settled_big_id = None
    paid_small_id = None
    conc_ids = None

    def test_02_full_chain_receive_price_review_pay_reverse(self):
        # 录入：26000-9000=17000；含水13.5(扣2%)+杂质2.2(扣1%)=3% → 扣510，结算重16490
        r = self.u1.ok("POST", "/api/receipts", {
            "supplier_id": self.s001["id"], "weigh_date": self.d(-5), "grade": "A",
            "gross_kg": "26000", "tare_kg": "9000",
            "water_pct": "13.5", "impurity_pct": "2.2", "code": "RC-E2E-BIG"})
        self.assertEqual((r["net_kg"], r["deducted_kg"], r["settled_kg"]),
                         ("17000.000", "510.000", "16490.000"))

        # 计价：过磅日 -5，现行段 -10 的 5.60 生效；+15 的未来段 6.20 不得使用
        st = self.u1.ok("POST", f"/api/receipts/{r['id']}/settle")
        self.assertEqual(st["price_cents_per_kg"], 560)
        self.assertEqual(st["amount_cents"], 16490 * 560)  # 92,344.00 元
        self.assertEqual(st["status"], "pending_review")    # 超 5 万须双人复核
        E2ECase.settled_big_id = st["id"]

        # 制单人自审被拒
        e = self.u1.fail("POST", f"/api/settlements/{st['id']}/review", {"note": "自审"})
        self.assertEqual(e.code, "reviewer_is_creator")

        # 财务第一审；同人二审被拒
        st = self.u2.ok("POST", f"/api/settlements/{st['id']}/review", {"note": "第1审"})
        self.assertEqual(st["status"], "pending_review")
        e = self.u2.fail("POST", f"/api/settlements/{st['id']}/review", {})
        self.assertEqual(e.code, "duplicate_reviewer")

        # 主管第二审 → approved
        st = self.u3.ok("POST", f"/api/settlements/{st['id']}/review", {"note": "第2审"})
        self.assertEqual(st["status"], "approved")
        self.assertEqual(len(st["reviews"]), 2)
        reviewers = {rv["reviewer_id"] for rv in st["reviews"]}
        self.assertEqual(len(reviewers), 2)  # 两名不同人员

        # 未复核完成不能付（构造一单 pending 验证）
        r2 = self.u1.ok("POST", "/api/receipts", {
            "supplier_id": self.s001["id"], "weigh_date": self.d(-5), "grade": "A",
            "gross_kg": "25000", "tare_kg": "8000",
            "water_pct": "13", "impurity_pct": "3", "code": "RC-E2E-WAIT"})
        st_wait = self.u1.ok("POST", f"/api/receipts/{r2['id']}/settle")
        e = self.u2.fail("POST", f"/api/settlements/{st_wait['id']}/pay")
        self.assertEqual(e.code, "not_approved")

        # 过磅员不能付款
        e = self.u4.fail("POST", f"/api/settlements/{st['id']}/pay")
        self.assertEqual(e.code, "forbidden")

        # 付款成功：FIFO 先抵 P001(4万) 再抵 P002；92,344 = 40,000 + 52,344
        st = self.u2.ok("POST", f"/api/settlements/{st['id']}/pay")
        self.assertEqual(st["status"], "paid")
        codes = [a["prepayment_code"] for a in st["payment"]["allocations"]]
        self.assertEqual(codes, ["P001", "P002"])
        pre = {p["code"]: p for p in self.u2.call("GET", "/api/prepayments")[1]}
        self.assertEqual(pre["P001"]["remaining_cents"], 0)
        self.assertEqual(pre["P002"]["remaining_cents"], 6_000_000 - 5_234_400)

        # 重复付款被拒
        e = self.u2.fail("POST", f"/api/settlements/{st['id']}/pay")
        self.assertEqual(e.code, "already_paid")

        # 冲正校验：无原因 / 无权角色
        e = self.u2.fail("POST", f"/api/settlements/{st['id']}/reverse", {"reason": "  "})
        self.assertEqual(e.code, "reason_required")
        e = self.u1.fail("POST", f"/api/settlements/{st['id']}/reverse",
                         {"reason": "x"})
        self.assertEqual(e.code, "forbidden")
        # 冲正成功：原付款记录保留（status=reversed），余额恢复
        st = self.u2.ok("POST", f"/api/settlements/{st['id']}/reverse",
                        {"reason": "结算单价争议，红冲重来"})
        self.assertEqual(st["status"], "reversed")
        self.assertEqual(st["payment"]["status"], "reversed")
        self.assertTrue(st["payment"]["reverse_reason"])
        pre = {p["code"]: p for p in self.u2.call("GET", "/api/prepayments")[1]}
        self.assertEqual((pre["P001"]["remaining_cents"],
                          pre["P002"]["remaining_cents"]),
                         (4_000_000, 6_000_000))
        # 已冲正不能再付
        e = self.u2.fail("POST", f"/api/settlements/{st['id']}/pay")
        self.assertEqual(e.code, "reversed")
        # 非已付款单不能冲正
        e = self.u2.fail("POST", f"/api/settlements/{st_wait['id']}/reverse",
                         {"reason": "x"})
        self.assertEqual(e.code, "not_paid")

    def test_03_under_threshold_skips_review_and_pays(self):
        """未超阈值免复核；为重启后留下一笔已付款。"""
        # 净重 1000kg、不扣量；B级 4.30 → 4,300 元，免复核
        r = self.u1.ok("POST", "/api/receipts", {
            "supplier_id": self.s001["id"], "weigh_date": self.d(-5), "grade": "B",
            "gross_kg": "3000", "tare_kg": "2000",
            "water_pct": "10", "impurity_pct": "1", "code": "RC-E2E-SMALL"})
        self.assertEqual(r["settled_kg"], "1000.000")
        st = self.u1.ok("POST", f"/api/receipts/{r['id']}/settle")
        self.assertEqual(st["status"], "approved")
        self.assertEqual(st["amount_cents"], 430_000)
        st = self.u2.ok("POST", f"/api/settlements/{st['id']}/pay")
        self.assertEqual(st["status"], "paid")
        # P001 先抵 4,300
        pre = {p["code"]: p for p in self.u2.call("GET", "/api/prepayments")[1]}
        self.assertEqual(pre["P001"]["remaining_cents"], 4_000_000 - 430_000)
        E2ECase.paid_small_id = st["id"]

    def test_04_future_price_guarded_over_http(self):
        # -20 过磅：现行段(-10)未生效，只能用 -60 的 5.00
        r = self.u1.ok("POST", "/api/receipts", {
            "supplier_id": self.s001["id"], "weigh_date": self.d(-20), "grade": "A",
            "gross_kg": "12000", "tare_kg": "2000",
            "water_pct": "10", "impurity_pct": "1", "code": "RC-E2E-OLD"})
        st = self.u1.ok("POST", f"/api/receipts/{r['id']}/settle")
        self.assertEqual(st["price_cents_per_kg"], 500)

        # 合同列表中未来段必须明确可见但未生效
        contracts = self.u1.call("GET", "/api/contracts")[1]
        future = [c for c in contracts
                  if c["supplier_id"] == self.s001["id"] and c["grade"] == "A"
                  and c["effective_date"] > self.d(0)]
        self.assertTrue(future and all(c["price_cents_per_kg"] == 620 for c in future))

        # 新供应商只有未来价 → 计价 422，绝不提前使用
        sup = self.u2.ok("POST", "/api/suppliers", {"code": "S009", "name": "未来价测试社"})
        self.u2.ok("POST", "/api/contracts", {
            "supplier_id": sup["id"], "grade": "A", "price_yuan": "9.99",
            "effective_date": self.d(10), "note": "未生效价"})
        r2 = self.u1.ok("POST", "/api/receipts", {
            "supplier_id": sup["id"], "weigh_date": self.d(0), "grade": "A",
            "gross_kg": "2000", "tare_kg": "500",
            "water_pct": "10", "impurity_pct": "1", "code": "RC-E2E-FUT"})
        e = self.u1.fail("POST", f"/api/receipts/{r2['id']}/settle")
        self.assertEqual(e.status, 422)
        self.assertEqual(e.code, "no_effective_contract")

    # ---------------------------------------------------------- 3. 并发付款
    def test_05_concurrent_pay_exactly_one_wins(self):
        """S002 预付余额 5,000 元；两单各 2,800 元同时付款：
        必须恰好一成一败，余额不超扣、无半笔账。"""
        ids = []
        for i in range(2):
            r = self.u1.ok("POST", "/api/receipts", {
                "supplier_id": self.s002["id"], "weigh_date": self.d(-5), "grade": "C",
                "gross_kg": "3000", "tare_kg": "2000",
                "water_pct": "10", "impurity_pct": "0", "code": f"RC-E2E-CC{i}"})
            st = self.u1.ok("POST", f"/api/receipts/{r['id']}/settle")
            self.assertEqual(st["amount_cents"], 280_000)
            self.assertEqual(st["status"], "approved")  # 小额免复核
            ids.append(st["id"])
        E2ECase.conc_ids = ids

        results = []
        barrier = threading.Barrier(2)

        def worker(sid, out):
            c = Client(); c.login("u2")
            barrier.wait()
            try:
                st = c.ok("POST", f"/api/settlements/{sid}/pay")
                out.append(("ok", st["id"], st["status"]))
            except ApiError as e:
                out.append(("fail", sid, e.code))

        t1, t2 = [], []
        threads = [threading.Thread(target=worker, args=(ids[0], t1)),
                   threading.Thread(target=worker, args=(ids[1], t2))]
        for t in threads: t.start()
        for t in threads: t.join()
        results = t1 + t2
        wins = [r for r in results if r[0] == "ok"]
        fails = [r for r in results if r[0] == "fail"]
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(len(fails), 1, results)
        self.assertEqual(fails[0][2], "insufficient_prepayment")

        statuses = {sid: self.u2.call("GET", f"/api/settlements/{sid}")[1]["status"]
                    for sid in ids}
        self.assertEqual(sorted(statuses.values()), ["approved", "paid"])
        pre = {p["code"]: p for p in self.u2.call("GET", "/api/prepayments")[1]}
        self.assertEqual(pre["P003"]["remaining_cents"], 500_000 - 280_000)  # 2,200
        # 没有半笔：付款数=1，抵扣行合计=2800
        paid_id = wins[0][1]
        st = self.u2.call("GET", f"/api/settlements/{paid_id}")[1]
        self.assertEqual(sum(a["amount_cents"] for a in st["payment"]["allocations"]),
                         280_000)
        E2ECase.loser_id = [sid for sid, v in statuses.items() if v == "approved"][0]

    # ---------------------------------------------------------- 4. 重启恢复
    def test_06_restart_recovery_then_continue(self):
        self.assertIsNotNone(E2ECase.loser_id)
        # 杀掉服务进程（模拟宕机/重启）
        self.__class__.proc.send_signal(signal.SIGINT)
        self.__class__.proc.wait(timeout=5)
        if self.__class__.proc.stdout:
            self.__class__.proc.stdout.close()

        env = dict(os.environ, PULP_DB_PATH=self.db_path,
                   PULP_PORT=str(PORT), PULP_HOST="127.0.0.1")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "run.py")],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.__class__.proc = proc
        wait_port("127.0.0.1", PORT)

        # 内存会话失效，需要重新登录；业务数据完整
        old = self.u2
        with self.assertRaises(ApiError) as cm:
            old.call("POST", f"/api/settlements/{E2ECase.loser_id}/pay")
        self.assertEqual(cm.exception.status, 401)
        fresh = Client(); fresh.login("u2")

        # 关键状态全部持久化
        big = fresh.call("GET", f"/api/settlements/{E2ECase.settled_big_id}")[1]
        self.assertEqual(big["status"], "reversed")
        self.assertEqual(big["payment"]["status"], "reversed")
        small = fresh.call("GET", f"/api/settlements/{E2ECase.paid_small_id}")[1]
        self.assertEqual(small["status"], "paid")
        pre = {p["code"]: p for p in fresh.call("GET", "/api/prepayments")[1]}
        # P001=40000-4300=35700（小单）；P002=60000（大单红冲恢复）；P003=2200
        self.assertEqual((pre["P001"]["remaining_cents"],
                          pre["P002"]["remaining_cents"],
                          pre["P003"]["remaining_cents"]),
                         (3_570_000, 6_000_000, 220_000))

        # 重启后继续业务：补登记一笔预付 1,000 元，把落败单付清
        # FIFO：P003 剩 2,200 先抵，P010 再抵 600
        fresh.ok("POST", "/api/prepayments", {
            "supplier_id": self.s002["id"], "amount_yuan": "1000.00",
            "received_at": self.d(0) + " 09:30:00", "code": "P010",
            "note": "重启后补足"})
        st = fresh.ok("POST", f"/api/settlements/{E2ECase.loser_id}/pay")
        self.assertEqual(st["status"], "paid")
        codes = [a["prepayment_code"] for a in st["payment"]["allocations"]]
        self.assertEqual(codes, ["P003", "P010"])
        pre = {p["code"]: p for p in fresh.call("GET", "/api/prepayments")[1]}
        self.assertEqual((pre["P003"]["remaining_cents"],
                          pre["P010"]["remaining_cents"]), (0, 40_000))

        # 并发中已付的那一单重启后仍不可重复支付
        winner = [sid for sid in E2ECase.conc_ids if sid != E2ECase.loser_id][0]
        e = fresh.fail("POST", f"/api/settlements/{winner}/pay")
        self.assertEqual(e.code, "already_paid")

        # 汇总数据自洽：所有付款/抵扣总额守恒
        summary = fresh.call("GET", "/api/summary")[1]
        self.assertGreaterEqual(summary["receipt_count"], 7)
        self.assertEqual(summary["pending_review"], 1)  # RC-E2E-WAIT 一直没复核
        settlements = fresh.call("GET", "/api/settlements")[1]
        paid_total = sum(s["amount_cents"] for s in settlements
                         if s["status"] == "paid")
        self.assertEqual(summary["paid_cents"], paid_total)


if __name__ == "__main__":
    unittest.main(verbosity=2)
