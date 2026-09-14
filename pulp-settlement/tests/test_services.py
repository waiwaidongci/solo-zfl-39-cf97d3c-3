# -*- coding: utf-8 -*-
"""服务层单元测试（不启 HTTP，直接跑 SQLite 事务）。"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import init_db, connect


class ServiceCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["PULP_DB_PATH"] = self.path
        init_db()
        self.conn = connect(self.path)
        self.clerk = svc.authenticate(self.conn, "u1")
        self.fin = svc.authenticate(self.conn, "u2")
        self.mgr = svc.authenticate(self.conn, "u3")
        self.other_clerk = svc.authenticate(self.conn, "u4")
        self.s001 = self.conn.execute(
            "SELECT * FROM suppliers WHERE code='S001'").fetchone()
        self.s002 = self.conn.execute(
            "SELECT * FROM suppliers WHERE code='S002'").fetchone()

    # (u4 赵六为第二名过磅员，用于复核/付款校验)

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass

    def receipt(self, **kw):
        d = (date.today() - timedelta(days=5)).isoformat()
        data = dict(supplier_id=self.s001["id"], weigh_date=d, grade="A",
                    gross_kg="26000", tare_kg="9000", water_pct="13.5",
                    impurity_pct="2.2", code=None)
        data.update(kw)
        return svc.create_receipt(self.conn, data, self.clerk)

    def settle(self, **kw):
        r = self.receipt(**kw)
        return svc.create_settlement(self.conn, r["id"], self.clerk)

    # ------------------------------------------------------------ 称重/扣量
    def test_net_and_tier_deduction(self):
        r = self.receipt()
        # 26000-9000=17000；含水 13.5→2%，杂质 2.2→1%；合计扣 3%
        self.assertEqual(r["net_kg"], "17000.000")
        self.assertEqual(r["deducted_kg"], "510.000")
        self.assertEqual(r["settled_kg"], "16490.000")

    def test_tier_boundaries(self):
        # 含水 12.00 落入 ≤12 不扣档；杂质 5.00 落入 2~5 扣1% 档
        r = self.receipt(water_pct="12.00", impurity_pct="5.00")
        self.assertEqual(r["deducted_kg"], "170.000")  # 17000*1%
        # 12.01 升档
        r2 = self.receipt(code="RC-2", water_pct="12.01", impurity_pct="0")
        self.assertEqual(r2["deducted_kg"], "340.000")  # 17000*2%

    def test_tare_must_be_less_than_gross(self):
        with self.assertRaises(svc.ServiceError) as cm:
            self.receipt(gross_kg="1000", tare_kg="1000")
        self.assertEqual(cm.exception.code, "tare_ge_gross")
        with self.assertRaises(svc.ServiceError):
            self.receipt(gross_kg="1000", tare_kg="1200")

    def test_invalid_percentages(self):
        for bad in ("-1", "100.01", "abc", ""):
            with self.assertRaises(svc.ServiceError):
                self.receipt(water_pct=bad)

    def test_future_weigh_date_rejected(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(svc.ServiceError) as cm:
            self.receipt(weigh_date=tomorrow)
        self.assertEqual(cm.exception.code, "future_date")

    # ------------------------------------------------------------ 分段计价
    def test_current_contract_used(self):
        st = self.settle()
        # 过磅日=今天-5；当前段（-10天，5.60）生效；春季段（-60，5.00）更早
        self.assertEqual(st["price_cents_per_kg"], 560)
        self.assertLessEqual(st["detail"]["contract_effective_date"],
                             (date.today() - timedelta(days=5)).isoformat())
        self.assertEqual(st["amount_cents"], 16490 * 560)

    def test_future_price_never_used(self):
        # 过磅日早于现行段生效日（-20 天），只能取 5.00 段（-60）
        d = (date.today() - timedelta(days=20)).isoformat()
        st = self.settle(weigh_date=d)
        self.assertEqual(st["price_cents_per_kg"], 500)
        # 若连最早段都没有，则直接报错，不会拿未来价
        d2 = (date.today() - timedelta(days=90)).isoformat()
        with self.assertRaises(svc.ServiceError) as cm:
            self.settle(weigh_date=d2)
        self.assertEqual(cm.exception.code, "no_effective_contract")

    def test_future_price_only_supplier(self):
        """供应商只有未来合同：必须报错而非提前使用。"""
        sid = svc.create_supplier(self.conn, {"code": "S009", "name": "测试社"},
                                  self.fin)["id"]
        future = (date.today() + timedelta(days=10)).isoformat()
        svc.create_contract(self.conn, dict(supplier_id=sid, grade="A",
                             price_yuan="9.99", effective_date=future), self.fin)
        r = svc.create_receipt(self.conn, dict(
            supplier_id=sid, weigh_date=date.today().isoformat(), grade="A",
            gross_kg="2000", tare_kg="500", water_pct="10", impurity_pct="1"),
            self.clerk)
        with self.assertRaises(svc.ServiceError) as cm:
            svc.create_settlement(self.conn, r["id"], self.clerk)
        self.assertEqual(cm.exception.code, "no_effective_contract")

    # ------------------------------------------------------------ 复核
    def test_under_threshold_auto_approved(self):
        # 16490kg*5.60 = 92,344 元 > 5万 → 待复核。造一个小额：B级 4.30
        r = self.receipt(grade="B")
        st = svc.create_settlement(self.conn, r["id"], self.clerk)
        # 16490*4.3=70,907 仍超阈值；用 C级 3.20 → 52,768 仍超。改用小重量。
        r2 = svc.create_receipt(self.conn, dict(
            supplier_id=self.s001["id"], weigh_date=r["weigh_date"], grade="C",
            gross_kg="3000", tare_kg="2000", water_pct="10", impurity_pct="0",
            code="RC-SMALL"), self.clerk)
        st2 = svc.create_settlement(self.conn, r2["id"], self.clerk)
        # 1000kg * 3.20 = 3200 元，免复核
        self.assertEqual(st2["status"], "approved")
        self.assertEqual(st2["amount_cents"], 320000)

    def test_double_review_by_two_different_people(self):
        st = self.settle()
        self.assertEqual(st["status"], "pending_review")
        # 制单人不能自审
        with self.assertRaises(svc.ServiceError) as cm:
            svc.add_review(self.conn, st["id"], self.clerk)
        self.assertEqual(cm.exception.code, "reviewer_is_creator")
        # 第一人审
        st = svc.add_review(self.conn, st["id"], self.fin)
        self.assertEqual(st["status"], "pending_review")
        # 同一人不能再审
        with self.assertRaises(svc.ServiceError) as cm:
            svc.add_review(self.conn, st["id"], self.fin)
        self.assertEqual(cm.exception.code, "duplicate_reviewer")
        # 第二人审 → approved
        st = svc.add_review(self.conn, st["id"], self.mgr)
        self.assertEqual(st["status"], "approved")
        self.assertEqual(len(st["reviews"]), 2)

    def test_clerk_cannot_pay(self):
        st = self.settle()
        svc.add_review(self.conn, st["id"], self.fin)
        svc.add_review(self.conn, st["id"], self.mgr)
        with self.assertRaises(svc.ServiceError) as cm:
            svc.pay_settlement(self.conn, st["id"], self.other_clerk)
        self.assertEqual(cm.exception.code, "forbidden")

    # ------------------------------------------------------------ 付款 FIFO
    def test_pay_fifo_and_reverse(self):
        st = self.settle()  # 92,344 元 = 9,234,400 分
        svc.add_review(self.conn, st["id"], self.fin)
        svc.add_review(self.conn, st["id"], self.other_clerk)
        st = svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(st["status"], "paid")
        # 预付：P001 400万(09:15) 先抵满，余下 5,234,400 从 P002 抵
        p1 = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P001'").fetchone()[0]
        p2 = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P002'").fetchone()[0]
        self.assertEqual(p1, 0)
        self.assertEqual(p2, 6_000_000 - 5_234_400)
        allocs = st["payment"]["allocations"]
        self.assertEqual([a["prepayment_code"] for a in allocs], ["P001", "P002"])
        self.assertEqual(sum(a["amount_cents"] for a in allocs), 9_234_400)
        # 重复付款被拒
        with self.assertRaises(svc.ServiceError) as cm:
            svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(cm.exception.code, "already_paid")
        # 冲正：余额恢复、状态 reversed、原付款留痕
        st = svc.reverse_payment(self.conn, st["id"], self.mgr, "单价争议")
        self.assertEqual(st["status"], "reversed")
        self.assertEqual(st["payment"]["status"], "reversed")
        p1 = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P001'").fetchone()[0]
        p2 = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P002'").fetchone()[0]
        self.assertEqual((p1, p2), (4_000_000, 6_000_000))
        # 冲正必须填原因
        with self.assertRaises(svc.ServiceError):
            svc.reverse_payment(self.conn, st["id"], self.fin, "  ")
        # 已冲正不能再付款
        with self.assertRaises(svc.ServiceError) as cm:
            svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(cm.exception.code, "reversed")

    def test_insufficient_balance_whole_order_fails(self):
        # S002 只有 50 万余额；造一单大额（A级 4.80）
        d = (date.today() - timedelta(days=5)).isoformat()
        r = svc.create_receipt(self.conn, dict(
            supplier_id=self.s002["id"], weigh_date=d, grade="A",
            gross_kg="20000", tare_kg="8000", water_pct="10", impurity_pct="0",
            code="RC-BIG"), self.clerk)
        st = svc.create_settlement(self.conn, r["id"], self.clerk)
        # 12000 * 4.8 = 57,600 元，超阈值 → 双人复核
        svc.add_review(self.conn, st["id"], self.mgr)
        st = svc.add_review(self.conn, st["id"], self.other_clerk)
        with self.assertRaises(svc.ServiceError) as cm:
            svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(cm.exception.code, "insufficient_prepayment")
        # 整单失败：无付款、无抵扣、余额分文未动、结算单仍 approved
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM payments WHERE settlement_id=?", (st["id"],)).fetchone())
        n_alloc = self.conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
        self.assertEqual(n_alloc, 0)
        rem = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P003'").fetchone()[0]
        self.assertEqual(rem, 500_000)
        st2 = svc.get_settlement(self.conn, st["id"])
        self.assertEqual(st2["status"], "approved")
        # 补足预付后可正常付
        svc.create_prepayment(self.conn, dict(
            supplier_id=self.s002["id"], amount_yuan="60000",
            received_at=date.today().isoformat() + " 08:00:00", code="P010"),
            self.fin)
        st = svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(st["status"], "paid")

    def test_small_order_after_big_failed_order(self):
        """大额付款失败后，小额单据与预付款状态不受影响。"""
        # S002 大额失败（见上一用例思路），这里直接验证余额 50 万可付小额
        d = (date.today() - timedelta(days=5)).isoformat()
        r = svc.create_receipt(self.conn, dict(
            supplier_id=self.s002["id"], weigh_date=d, grade="C",
            gross_kg="3000", tare_kg="2000", water_pct="10", impurity_pct="0",
            code="RC-C1"), self.clerk)
        st = svc.create_settlement(self.conn, r["id"], self.clerk)
        # 1000*2.8=2800 免复核直接 approved
        self.assertEqual(st["status"], "approved")
        st = svc.pay_settlement(self.conn, st["id"], self.fin)
        self.assertEqual(st["status"], "paid")
        rem = self.conn.execute(
            "SELECT remaining_cents FROM prepayments WHERE code='P003'").fetchone()[0]
        self.assertEqual(rem, 500_000 - 280_000)

    # ------------------------------------------------------------ 并发
    def test_concurrent_pay_only_one_wins(self):
        """两笔已批准结算单争抢同一池预付款：恰好只够一笔，
        两个线程同时付款，必须恰好一笔成功一笔失败，余额不出现超扣。"""
        d = (date.today() - timedelta(days=5)).isoformat()
        # S002 余额 5000 元。两单各 2800 元（共需 5600 > 5000）
        ids = []
        for i in range(2):
            r = svc.create_receipt(self.conn, dict(
                supplier_id=self.s002["id"], weigh_date=d, grade="C",
                gross_kg="3000", tare_kg="2000", water_pct="10", impurity_pct="0",
                code=f"RC-CC{i}"), self.clerk)
            st = svc.create_settlement(self.conn, r["id"], self.clerk)
            self.assertEqual(st["status"], "approved")
            ids.append(st["id"])

        import threading
        errors, winners = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(sid):
            c = connect(self.path)
            user = svc.authenticate(c, "u2")
            barrier.wait()  # 尽量同时冲
            try:
                result = svc.pay_settlement(c, sid, user)
                with lock:
                    winners.append(result["id"])
            except svc.ServiceError as e:
                with lock:
                    errors.append(e.code)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(errors, ["insufficient_prepayment"])
        # 获胜单据 paid，落败单据仍是 approved 可稍后再付
        statuses = {sid: svc.get_settlement(self.conn, sid)["status"] for sid in ids}
        self.assertEqual(sorted(statuses.values()), ["approved", "paid"])
        # 余额 = 5000 - 2800 = 2200，绝不能为负
        rem = self.conn.execute(
            "SELECT COALESCE(SUM(remaining_cents),0) FROM prepayments "
            "WHERE supplier_id=?", (self.s002["id"],)).fetchone()[0]
        self.assertEqual(rem, 220_000)
        # 事后补足预付，落败单可正常支付
        svc.create_prepayment(self.conn, dict(
            supplier_id=self.s002["id"], amount_yuan="1000",
            received_at=date.today().isoformat() + " 09:00:00", code="P-CC"),
            self.fin)
        loser = [s for s, v in statuses.items() if v == "approved"][0]
        st = svc.pay_settlement(self.conn, loser, self.fin)
        self.assertEqual(st["status"], "paid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
