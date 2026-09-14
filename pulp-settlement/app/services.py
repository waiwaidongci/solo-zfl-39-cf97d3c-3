# -*- coding: utf-8 -*-
"""核心业务规则与事务：

净重 = 毛重 - 皮重；
扣量 = 净重 ×（含水档扣点 + 杂质档扣点）/100；
结算重量 = 净重 - 扣量；金额 = 结算重量 × 合同分段价（分/千克）。
付款：BEGIN IMMEDIATE 整事务，预付款按到账先后 FIFO 抵扣，
      余额不足整单回滚；金额超阈值需两名不同人员复核。
冲正：付款只能冲正（红冲），不可改写、不可删除。
"""
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from .db import connect

# 超过该金额（含，单位：分）必须两名不同人员复核
REVIEW_THRESHOLD_CENTS = 5_000_000  # 50,000.00 元

KG = Decimal("0.001")          # 重量保留 3 位小数（千克）
CENT = Decimal("1")            # 金额整数分
PCT_Q = Decimal("0.01")        # 百分比保留 2 位小数
GRADES = ("A", "B", "C")


class ServiceError(Exception):
    """业务校验失败：message 面向用户，code 供前端/测试识别，http 为状态码。"""

    def __init__(self, message, code="bad_request", http=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http = http


# ---------------------------------------------------------------- 工具函数

def now_str():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def parse_date(s, field="日期"):
    if not isinstance(s, str):
        raise ServiceError(f"{field}格式应为 YYYY-MM-DD", "invalid_date")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ServiceError(f"{field}格式应为 YYYY-MM-DD", "invalid_date")


def parse_dt(s, field="时间"):
    if not isinstance(s, str):
        raise ServiceError(f"{field}格式应为 YYYY-MM-DD HH:MM:SS", "invalid_datetime")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise ServiceError(f"{field}格式应为 YYYY-MM-DD HH:MM:SS", "invalid_datetime")


def parse_kg(raw, field, positive=True):
    try:
        v = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        raise ServiceError(f"{field}必须是数字", "invalid_number")
    if not v.is_finite():
        raise ServiceError(f"{field}必须是数字", "invalid_number")
    if v.as_tuple().exponent < -3:
        raise ServiceError(f"{field}最多保留 3 位小数", "invalid_number")
    if positive and v <= 0:
        raise ServiceError(f"{field}必须大于 0", "invalid_number")
    return v.quantize(KG, rounding=ROUND_HALF_UP)


def parse_pct(raw, field):
    try:
        v = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        raise ServiceError(f"{field}必须是数字", "invalid_number")
    if not v.is_finite() or v < 0 or v > 100:
        raise ServiceError(f"{field}必须在 0~100 之间", "invalid_range")
    if v.as_tuple().exponent < -2:
        raise ServiceError(f"{field}最多保留 2 位小数", "invalid_number")
    return v.quantize(PCT_Q, rounding=ROUND_HALF_UP)


def yuan_to_cents(raw, field="金额"):
    """页面录入元（最多两位小数），转整数分。"""
    try:
        v = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        raise ServiceError(f"{field}必须是数字", "invalid_number")
    if not v.is_finite() or v <= 0:
        raise ServiceError(f"{field}必须大于 0", "invalid_number")
    if v.as_tuple().exponent < -2:
        raise ServiceError(f"{field}最多保留 2 位小数", "invalid_number")
    return int((v * 100).quantize(CENT, rounding=ROUND_HALF_UP))


def cents_to_yuan(c):
    sign = "-" if c < 0 else ""
    c = abs(int(c))
    return f"{sign}{c // 100}.{c % 100:02d}"


def require_actor(actor):
    if not actor:
        raise ServiceError("请先登录", "unauthorized", 401)
    return actor


def audit(conn, actor_id, action, entity, entity_id, detail=""):
    conn.execute(
        "INSERT INTO audit_log(at,actor_id,action,entity,entity_id,detail) "
        "VALUES (?,?,?,?,?,?)",
        (now_str(), actor_id, action, entity, str(entity_id) if entity_id is not None else None,
         detail))


def get_supplier(conn, supplier_id):
    row = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not row:
        raise ServiceError("供应商不存在", "supplier_not_found", 404)
    return row


def next_code(conn, prefix, table):
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
    return f"{prefix}{date.today().strftime('%Y%m%d')}-{n:03d}"


# ---------------------------------------------------------------- 纯计算

def pick_tier(conn, measure, value):
    """区间 (上一档上限, 本档上限]，边界值就低档。

    比较必须 CAST 成数值：min/max 以规范小数字符串存储，
    直接按文本比较会让 '12.00' > '12' 错误成立。
    """
    row = conn.execute(
        "SELECT * FROM deduction_tiers WHERE measure=? "
        "AND CAST(? AS REAL) > CAST(min_value AS REAL) "
        "AND CAST(? AS REAL) <= CAST(max_value AS REAL) "
        "ORDER BY CAST(max_value AS REAL) LIMIT 1",
        (measure, str(value), str(value))).fetchone()
    if row is None:  # value == 0 落在最低档（不扣）
        row = conn.execute(
            "SELECT * FROM deduction_tiers WHERE measure=? "
            "ORDER BY CAST(max_value AS REAL) LIMIT 1", (measure,)).fetchone()
    return row


def compute_weights(conn, gross, tare, water_pct, impurity_pct):
    net = gross - tare
    if net <= 0:
        raise ServiceError("净重必须大于 0（毛重必须大于皮重）", "net_not_positive")
    wt = pick_tier(conn, "water", water_pct)
    im = pick_tier(conn, "impurity", impurity_pct)
    wpct = Decimal(wt["deduction_pct"])
    ipct = Decimal(im["deduction_pct"])
    deducted = (net * (wpct + ipct) / Decimal(100)).quantize(KG, rounding=ROUND_HALF_UP)
    settled = (net - deducted).quantize(KG, rounding=ROUND_HALF_UP)
    return {
        "net": net,
        "water_tier": wt,
        "impurity_tier": im,
        "water_deduct_pct": wpct,
        "impurity_deduct_pct": ipct,
        "deducted": deducted,
        "settled": settled,
    }


def effective_contract(conn, supplier_id, grade, weigh_date_iso):
    """取过磅日当天（含）之前已生效的最新一段；未生效的未来价绝不参与。"""
    return conn.execute(
        "SELECT * FROM contracts WHERE supplier_id=? AND grade=? AND effective_date<=? "
        "ORDER BY effective_date DESC, id DESC LIMIT 1",
        (supplier_id, grade, weigh_date_iso)).fetchone()


# ---------------------------------------------------------------- 主数据

def authenticate(conn, username):
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        raise ServiceError("用户不存在", "invalid_user", 404)
    return dict(row)


def list_users(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id,username,display_name,role FROM users ORDER BY id")]


def create_supplier(conn, data, actor):
    require_actor(actor)
    code = str(data.get("code", "")).strip()
    name = str(data.get("name", "")).strip()
    if not code or not name:
        raise ServiceError("供应商编码与名称必填", "missing_field")
    try:
        cur = conn.execute("INSERT INTO suppliers(code,name) VALUES (?,?)", (code, name))
    except Exception:
        raise ServiceError("供应商编码已存在", "supplier_code_exists")
    audit(conn, actor["id"], "create", "supplier", cur.lastrowid, name)
    return dict(conn.execute("SELECT * FROM suppliers WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def list_suppliers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT s.*, COALESCE(SUM(p.remaining_cents),0) AS remaining_cents "
        "FROM suppliers s LEFT JOIN prepayments p ON p.supplier_id=s.id "
        "GROUP BY s.id ORDER BY s.id")]


def create_contract(conn, data, actor):
    require_actor(actor)
    try:
        supplier_id = int(data.get("supplier_id"))
    except (TypeError, ValueError):
        raise ServiceError("请选择供应商", "missing_field")
    grade = str(data.get("grade", "")).strip()
    if grade not in GRADES:
        raise ServiceError("等级必须为 A/B/C", "invalid_grade")
    price = yuan_to_cents(data.get("price_yuan"), "合同单价")
    eff = parse_date(str(data.get("effective_date", "")), "生效日期")
    get_supplier(conn, supplier_id)
    note = str(data.get("note", "")).strip()
    try:
        cur = conn.execute(
            "INSERT INTO contracts(supplier_id,grade,price_cents_per_kg,"
            "effective_date,note) VALUES (?,?,?,?,?)",
            (supplier_id, grade, price, eff.isoformat(), note))
    except Exception:
        raise ServiceError("同一供应商、同一等级在该生效日期已有合同段",
                           "contract_exists")
    audit(conn, actor["id"], "create", "contract", cur.lastrowid,
          f"{supplier_id}/{grade}/{eff}/{price}")
    return dict(conn.execute("SELECT * FROM contracts WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def list_contracts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT c.*, s.name AS supplier_name FROM contracts c "
        "JOIN suppliers s ON s.id=c.supplier_id ORDER BY s.id, c.grade, c.effective_date")]


def list_tiers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM deduction_tiers ORDER BY measure, CAST(max_value AS REAL)")]


def create_prepayment(conn, data, actor):
    actor = require_actor(actor)
    if actor["role"] not in ("finance", "manager"):
        raise ServiceError("只有财务/主管可以登记预付款", "forbidden", 403)
    try:
        supplier_id = int(data.get("supplier_id"))
    except (TypeError, ValueError):
        raise ServiceError("请选择供应商", "missing_field")
    amount = yuan_to_cents(data.get("amount_yuan"), "预付款金额")
    received = parse_dt(str(data.get("received_at", "")).strip(), "到账时间")
    get_supplier(conn, supplier_id)
    code = str(data.get("code") or "").strip() or next_code(conn, "P", "prepayments")
    note = str(data.get("note", "")).strip()
    try:
        cur = conn.execute(
            "INSERT INTO prepayments(code,supplier_id,amount_cents,remaining_cents,"
            "received_at,created_by,note) VALUES (?,?,?,?,?,?,?)",
            (code, supplier_id, amount, amount,
             received.isoformat(sep=" ", timespec="seconds"), actor["id"], note))
    except Exception:
        raise ServiceError("预付款单号已存在", "prepayment_code_exists")
    audit(conn, actor["id"], "create", "prepayment", cur.lastrowid,
          f"{code}/{amount}")
    return dict(conn.execute("SELECT * FROM prepayments WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def list_prepayments(conn):
    return [dict(r) for r in conn.execute(
        "SELECT p.*, s.name AS supplier_name FROM prepayments p "
        "JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.received_at, p.id")]


# ---------------------------------------------------------------- 验收入库

def create_receipt(conn, data, actor):
    actor = require_actor(actor)
    try:
        supplier_id = int(data.get("supplier_id"))
    except (TypeError, ValueError):
        raise ServiceError("请选择供应商", "missing_field")
    get_supplier(conn, supplier_id)

    wd = parse_date(str(data.get("weigh_date", "")), "过磅日期")
    if wd > date.today():
        raise ServiceError("过磅日期不能晚于今天", "future_date")

    grade = str(data.get("grade", "")).strip()
    if grade not in GRADES:
        raise ServiceError("等级必须为 A/B/C", "invalid_grade")

    gross = parse_kg(data.get("gross_kg"), "毛重")
    tare = parse_kg(data.get("tare_kg"), "皮重")
    if tare >= gross:
        raise ServiceError("皮重必须小于毛重", "tare_ge_gross")
    water = parse_pct(data.get("water_pct"), "含水")
    impurity = parse_pct(data.get("impurity_pct"), "杂质")

    calc = compute_weights(conn, gross, tare, water, impurity)

    code = str(data.get("code") or "").strip() or next_code(conn, "RC", "receipts")
    try:
        cur = conn.execute(
            "INSERT INTO receipts(code,supplier_id,weigh_date,gross_kg,tare_kg,net_kg,"
            "water_pct,impurity_pct,grade,deducted_kg,settled_kg,status,created_by,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, supplier_id, wd.isoformat(), str(gross), str(tare), str(calc["net"]),
             str(water), str(impurity), grade, str(calc["deducted"]),
             str(calc["settled"]), "weighed", actor["id"], now_str()))
    except Exception:
        raise ServiceError("车号/单号已存在", "receipt_code_exists")
    audit(conn, actor["id"], "create", "receipt", cur.lastrowid,
          f"{code} 净重{calc['net']} 结算重{calc['settled']}")
    return get_receipt(conn, cur.lastrowid)


def get_receipt(conn, rid):
    row = conn.execute(
        "SELECT r.*, s.name AS supplier_name, u.display_name AS creator_name "
        "FROM receipts r JOIN suppliers s ON s.id=r.supplier_id "
        "JOIN users u ON u.id=r.created_by WHERE r.id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_receipts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT r.*, s.name AS supplier_name, u.display_name AS creator_name, "
        "st.code AS settlement_code, st.status AS settlement_status "
        "FROM receipts r JOIN suppliers s ON s.id=r.supplier_id "
        "JOIN users u ON u.id=r.created_by "
        "LEFT JOIN settlements st ON st.receipt_id=r.id ORDER BY r.id DESC")]


# ---------------------------------------------------------------- 结算计价

def create_settlement(conn, receipt_id, actor):
    actor = require_actor(actor)
    receipt = get_receipt(conn, receipt_id)
    if not receipt:
        raise ServiceError("验收单不存在", "receipt_not_found", 404)
    if receipt["status"] != "weighed":
        raise ServiceError("该验收单已生成结算，不能重复计价", "already_settled", 409)

    contract = effective_contract(conn, receipt["supplier_id"], receipt["grade"],
                                  receipt["weigh_date"])
    if contract is None:
        future = conn.execute(
            "SELECT MIN(effective_date) FROM contracts WHERE supplier_id=? AND grade=?",
            (receipt["supplier_id"], receipt["grade"])).fetchone()[0]
        hint = f"；仅有将于 {future} 生效的合同，未到生效日" if future else ""
        raise ServiceError(
            f"供应商该等级在过磅日 {receipt['weigh_date']} 没有已生效合同价{hint}，"
            "未来价格不能提前使用", "no_effective_contract", 422)

    settled_kg = Decimal(receipt["settled_kg"])
    price = contract["price_cents_per_kg"]
    amount = int((settled_kg * Decimal(price)).quantize(CENT, rounding=ROUND_HALF_UP))

    need_review = amount > REVIEW_THRESHOLD_CENTS
    detail = {
        "gross_kg": receipt["gross_kg"],
        "tare_kg": receipt["tare_kg"],
        "net_kg": receipt["net_kg"],
        "water_pct": receipt["water_pct"],
        "impurity_pct": receipt["impurity_pct"],
        "grade": receipt["grade"],
        "settled_kg": str(settled_kg),
        "contract_id": contract["id"],
        "contract_effective_date": contract["effective_date"],
        "price_cents_per_kg": price,
        "amount_cents": amount,
        "review_threshold_cents": REVIEW_THRESHOLD_CENTS,
        "need_review": need_review,
    }
    code = next_code(conn, "JS", "settlements")
    cur = conn.execute(
        "INSERT INTO settlements(code,receipt_id,supplier_id,contract_id,"
        "price_cents_per_kg,settled_kg,amount_cents,status,detail_json,created_by,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (code, receipt["id"], receipt["supplier_id"], contract["id"], price,
         str(settled_kg), amount,
         "pending_review" if need_review else "approved",
         json.dumps(detail, ensure_ascii=False), actor["id"], now_str()))
    conn.execute("UPDATE receipts SET status='settled' WHERE id=?", (receipt["id"],))
    if not need_review:
        audit(conn, actor["id"], "auto_approve", "settlement", cur.lastrowid,
              f"金额未超阈值，免复核 {amount}")
    audit(conn, actor["id"], "create", "settlement", cur.lastrowid,
          f"{code} {amount} {'待复核' if need_review else '免复核'}")
    return get_settlement(conn, cur.lastrowid)


def _reviews(conn, sid):
    return [dict(r) for r in conn.execute(
        "SELECT rv.*, u.display_name AS reviewer_name FROM reviews rv "
        "JOIN users u ON u.id=rv.reviewer_id WHERE rv.settlement_id=? ORDER BY rv.seq",
        (sid,))]


def get_settlement(conn, sid):
    row = conn.execute(
        "SELECT st.*, s.name AS supplier_name, r.code AS receipt_code, "
        "u.display_name AS creator_name FROM settlements st "
        "JOIN suppliers s ON s.id=st.supplier_id "
        "JOIN receipts r ON r.id=st.receipt_id "
        "JOIN users u ON u.id=st.created_by WHERE st.id=?", (sid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json"))
    d["reviews"] = _reviews(conn, sid)
    pay = conn.execute("SELECT * FROM payments WHERE settlement_id=?", (sid,)).fetchone()
    d["payment"] = dict(pay) if pay else None
    if pay:
        d["payment"]["allocations"] = [dict(a) for a in conn.execute(
            "SELECT a.*, p.code AS prepayment_code FROM allocations a "
            "JOIN prepayments p ON p.id=a.prepayment_id "
            "WHERE a.payment_id=? ORDER BY a.id", (pay["id"],))]
    return d


def list_settlements(conn):
    rows = conn.execute("SELECT id FROM settlements ORDER BY id DESC").fetchall()
    return [get_settlement(conn, r["id"]) for r in rows]


def add_review(conn, settlement_id, actor, note=""):
    actor = require_actor(actor)
    st = conn.execute("SELECT * FROM settlements WHERE id=?", (settlement_id,)).fetchone()
    if not st:
        raise ServiceError("结算单不存在", "settlement_not_found", 404)
    if st["status"] != "pending_review":
        raise ServiceError("该结算单不在待复核状态", "not_pending_review", 409)
    if actor["id"] == st["created_by"]:
        raise ServiceError("制单人不能复核自己的单据", "reviewer_is_creator", 403)
    existing = conn.execute(
        "SELECT seq, reviewer_id FROM reviews WHERE settlement_id=? ORDER BY seq",
        (settlement_id,)).fetchall()
    if any(r["reviewer_id"] == actor["id"] for r in existing):
        raise ServiceError("同一人不能重复复核", "duplicate_reviewer", 409)
    if len(existing) >= 2:
        raise ServiceError("该单已完成两名复核", "review_full", 409)
    seq = len(existing) + 1
    conn.execute(
        "INSERT INTO reviews(settlement_id,seq,reviewer_id,note,created_at) "
        "VALUES (?,?,?,?,?)",
        (settlement_id, seq, actor["id"], str(note or ""), now_str()))
    audit(conn, actor["id"], "review", "settlement", settlement_id, f"第{seq}审")
    if seq == 2:
        conn.execute("UPDATE settlements SET status='approved' WHERE id=?",
                     (settlement_id,))
        audit(conn, actor["id"], "approve", "settlement", settlement_id, "双人复核通过")
    return get_settlement(conn, settlement_id)


# ---------------------------------------------------------------- 付款 / 冲正

def _supplier_remaining(conn, supplier_id):
    return conn.execute(
        "SELECT COALESCE(SUM(remaining_cents),0) FROM prepayments "
        "WHERE supplier_id=? AND remaining_cents>0", (supplier_id,)).fetchone()[0]


def pay_settlement(conn, settlement_id, actor):
    """预付款 FIFO 抵扣付款。整事务提交；余额不足或并发落败时整体回滚。"""
    actor = require_actor(actor)
    if actor["role"] not in ("finance", "manager"):
        raise ServiceError("只有财务/主管可以执行付款", "forbidden", 403)

    conn.execute("BEGIN IMMEDIATE")
    try:
        st = conn.execute(
            "SELECT * FROM settlements WHERE id=?", (settlement_id,)).fetchone()
        if not st:
            raise ServiceError("结算单不存在", "settlement_not_found", 404)
        if st["status"] == "paid":
            raise ServiceError("该单已付款，不能重复支付（如需更正请走冲正）",
                               "already_paid", 409)
        if st["status"] == "reversed":
            raise ServiceError("该单已冲正，不能再付款", "reversed", 409)
        if st["status"] != "approved":
            raise ServiceError("复核未完成，不能付款", "not_approved", 409)

        amount = st["amount_cents"]
        total = _supplier_remaining(conn, st["supplier_id"])
        if total < amount:
            raise ServiceError(
                f"预付款余额不足：需 {cents_to_yuan(amount)} 元，"
                f"可用 {cents_to_yuan(total)} 元。整单失败，未扣任何预付款",
                "insufficient_prepayment", 422)

        code = next_code(conn, "PAY", "payments")
        cur = conn.execute(
            "INSERT INTO payments(code,settlement_id,amount_cents,status,paid_by,paid_at)"
            " VALUES (?,?,?,?,?,?)",
            (code, st["id"], amount, "paid", actor["id"], now_str()))
        payment_id = cur.lastrowid

        left = amount
        rows = conn.execute(
            "SELECT id, remaining_cents FROM prepayments "
            "WHERE supplier_id=? AND remaining_cents>0 ORDER BY received_at, id",
            (st["supplier_id"],)).fetchall()
        allocations = []
        for r in rows:
            if left <= 0:
                break
            take = min(r["remaining_cents"], left)
            conn.execute("UPDATE prepayments SET remaining_cents=remaining_cents-? "
                         "WHERE id=? AND remaining_cents>=?",
                         (take, r["id"], take))
            conn.execute(
                "INSERT INTO allocations(payment_id,prepayment_id,amount_cents) "
                "VALUES (?,?,?)", (payment_id, r["id"], take))
            allocations.append((r["id"], take))
            left -= take
        if left != 0:
            # 兜底：理论上前面余额检查已覆盖，绝不允许留下半笔账
            raise ServiceError("预付款抵扣异常，整单回滚", "alloc_failed", 500)

        conn.execute("UPDATE settlements SET status='paid' WHERE id=?", (st["id"],))
        audit(conn, actor["id"], "pay", "settlement", st["id"],
              f"{code} 全额抵扣 {amount}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_settlement(conn, settlement_id)


def reverse_payment(conn, settlement_id, actor, reason):
    actor = require_actor(actor)
    if actor["role"] not in ("finance", "manager"):
        raise ServiceError("只有财务/主管可以冲正", "forbidden", 403)
    reason = str(reason or "").strip()
    if not reason:
        raise ServiceError("冲正必须填写原因", "reason_required")

    conn.execute("BEGIN IMMEDIATE")
    try:
        st = conn.execute(
            "SELECT * FROM settlements WHERE id=?", (settlement_id,)).fetchone()
        if not st:
            raise ServiceError("结算单不存在", "settlement_not_found", 404)
        if st["status"] != "paid":
            raise ServiceError("只有已付款单据可以冲正", "not_paid", 409)
        pay = conn.execute(
            "SELECT * FROM payments WHERE settlement_id=? AND status='paid'",
            (settlement_id,)).fetchone()
        if not pay:
            raise ServiceError("没有可冲正的付款记录", "payment_not_found", 404)

        allocs = conn.execute(
            "SELECT prepayment_id, amount_cents FROM allocations WHERE payment_id=?",
            (pay["id"],)).fetchall()
        restored = 0
        for a in allocs:
            conn.execute(
                "UPDATE prepayments SET remaining_cents=remaining_cents+? WHERE id=?",
                (a["amount_cents"], a["prepayment_id"]))
            restored += a["amount_cents"]
        if restored != pay["amount_cents"]:
            raise ServiceError("冲正金额不平，整笔回滚", "reverse_unbalanced", 500)

        conn.execute(
            "UPDATE payments SET status='reversed', reversed_by=?, reversed_at=?, "
            "reverse_reason=? WHERE id=?",
            (actor["id"], now_str(), reason, pay["id"]))
        conn.execute("UPDATE settlements SET status='reversed' WHERE id=?",
                     (settlement_id,))
        audit(conn, actor["id"], "reverse", "payment", pay["id"],
              f"原因：{reason}；恢复预付 {restored}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_settlement(conn, settlement_id)
