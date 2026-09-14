# -*- coding: utf-8 -*-
"""数据库连接、建表与种子数据。

设计要点：
- 金额一律以「分」存为整数，杜绝浮点误差；对外展示再换算成元。
- 重量一律以「千克」存为规范小数字符串（DECIMAL(12,3)），计算用 Decimal。
- 合同价格按「分/千克」存为整数；生效日期分段，计价时只取 weigh_date
  当天之前（含当天）已生效、且 grade 匹配的那一段。
- 预付款按 received_at 先后排序 FIFO 抵扣。
"""
import os
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

DB_ENV = "PULP_DB_PATH"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "pulp.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('clerk','finance','manager'))
);

CREATE TABLE IF NOT EXISTS suppliers (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL
);

-- 供应商合同分段价（按生效日期分段，未来价格未来才能用）
CREATE TABLE IF NOT EXISTS contracts (
    id              INTEGER PRIMARY KEY,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    grade           TEXT NOT NULL CHECK (grade IN ('A','B','C')),
    price_cents_per_kg INTEGER NOT NULL CHECK (price_cents_per_kg > 0),
    effective_date  TEXT NOT NULL,           -- ISO YYYY-MM-DD
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE (supplier_id, grade, effective_date)
);

-- 按档扣量规则：measure=water(含水%) / impurity(杂质%)，
-- max_value 为该档闭区间上限（最大档为 100）；deduction_pct 为扣净重百分比
CREATE TABLE IF NOT EXISTS deduction_tiers (
    id             INTEGER PRIMARY KEY,
    measure        TEXT NOT NULL CHECK (measure IN ('water','impurity')),
    min_value      TEXT NOT NULL,
    max_value      TEXT NOT NULL,
    deduction_pct  TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS receipts (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,          -- 车号/单号
    supplier_id  INTEGER NOT NULL REFERENCES suppliers(id),
    weigh_date   TEXT NOT NULL,                 -- 过磅/验收日期
    gross_kg     TEXT NOT NULL,                 -- 毛重
    tare_kg      TEXT NOT NULL,                 -- 皮重
    net_kg       TEXT NOT NULL,                 -- 净重 = 毛重 - 皮重
    water_pct    TEXT NOT NULL,                 -- 含水 %
    impurity_pct TEXT NOT NULL,                 -- 杂质 %
    grade        TEXT NOT NULL CHECK (grade IN ('A','B','C')),
    deducted_kg  TEXT NOT NULL,                 -- 扣量合计
    settled_kg   TEXT NOT NULL,                 -- 结算重量 = 净重 - 扣量
    status       TEXT NOT NULL DEFAULT 'weighed'
                 CHECK (status IN ('weighed','settled','reversed')),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    receipt_id     INTEGER NOT NULL UNIQUE REFERENCES receipts(id),
    supplier_id    INTEGER NOT NULL REFERENCES suppliers(id),
    contract_id    INTEGER NOT NULL REFERENCES contracts(id),
    price_cents_per_kg INTEGER NOT NULL,
    settled_kg     TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending_review'
                   CHECK (status IN ('pending_review','approved',
                                     'paid','reversed')),
    detail_json    TEXT NOT NULL,
    created_by     INTEGER NOT NULL REFERENCES users(id),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,
    settlement_id INTEGER NOT NULL REFERENCES settlements(id),
    seq           INTEGER NOT NULL,             -- 1=初审 2=复审
    reviewer_id   INTEGER NOT NULL REFERENCES users(id),
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (settlement_id, seq),
    UNIQUE (settlement_id, reviewer_id)         -- 两名不同人员
);

CREATE TABLE IF NOT EXISTS prepayments (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    supplier_id  INTEGER NOT NULL REFERENCES suppliers(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    remaining_cents INTEGER NOT NULL,
    received_at  TEXT NOT NULL,                 -- ISO，含时分秒，决定抵扣先后
    created_by   INTEGER NOT NULL REFERENCES users(id),
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS allocations (
    id             INTEGER PRIMARY KEY,
    payment_id     INTEGER NOT NULL,
    prepayment_id  INTEGER NOT NULL REFERENCES prepayments(id),
    amount_cents   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    settlement_id INTEGER NOT NULL UNIQUE REFERENCES settlements(id),
    amount_cents  INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'paid'
                  CHECK (status IN ('paid','reversed')),
    paid_by       INTEGER NOT NULL REFERENCES users(id),
    paid_at       TEXT NOT NULL,
    reversed_by   INTEGER REFERENCES users(id),
    reversed_at   TEXT,
    reverse_reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    detail     TEXT NOT NULL DEFAULT ''
);
"""

# 扣量档（默认规则，可在管理页查看）
WATER_TIERS = [
    ("0",    "12",   "0",   "含水≤12% 不扣"),
    ("12",   "15",   "2",   "含水12%~15% 扣2%"),
    ("15",   "20",   "5",   "含水15%~20% 扣5%"),
    ("20",   "100",  "10",  "含水>20% 扣10%"),
]
IMPURITY_TIERS = [
    ("0",    "2",    "0",   "杂质≤2% 不扣"),
    ("2",    "5",    "1",   "杂质2%~5% 扣1%"),
    ("5",    "8",    "3",   "杂质5%~8% 扣3%"),
    ("8",    "100",  "6",   "杂质>8% 扣6%"),
]


def connect(db_path=None):
    path = db_path or os.environ.get(DB_ENV, DEFAULT_DB)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA)
        _seed(conn)
    finally:
        if own:
            conn.close()


def _seed(conn):
    """幂等写入演示用基础数据：用户、供应商、扣量档、分段合同、预付款。

    合同日期相对今天生成，保证「当前价 / 未来价」演示任何时候都成立。
    """
    today = date.today()
    d = lambda delta: (today + timedelta(days=delta)).isoformat()

    users = [
        ("u1", "张三", "clerk"),
        ("u2", "李四", "finance"),
        ("u3", "王五", "manager"),
        ("u4", "赵六", "clerk"),
    ]
    for username, name, role in users:
        conn.execute(
            "INSERT OR IGNORE INTO users(username, display_name, role) VALUES (?,?,?)",
            (username, name, role))

    suppliers = [("S001", "青山竹木原料社"), ("S002", "白水秸秆回收站")]
    for code, name in suppliers:
        conn.execute("INSERT OR IGNORE INTO suppliers(code, name) VALUES (?,?)",
                     (code, name))

    for measure, tiers in (("water", WATER_TIERS), ("impurity", IMPURITY_TIERS)):
        for mn, mx, pct, note in tiers:
            exists = conn.execute(
                "SELECT 1 FROM deduction_tiers WHERE measure=? AND min_value=?",
                (measure, mn)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO deduction_tiers(measure,min_value,max_value,"
                    "deduction_pct,note) VALUES (?,?,?,?,?)",
                    (measure, mn, mx, pct, note))

    # S001 合同：A级。过去价 5.00 元/kg；当前价 5.60；未来价 6.20（不可提前使用）
    s001 = conn.execute("SELECT id FROM suppliers WHERE code='S001'").fetchone()[0]
    contracts_s001 = [
        ("A", 500, d(-60), "春季协议价"),
        ("A", 560, d(-10), "现行价（含水季节补贴）"),
        ("A", 620, d(15),  "下季度拟调价（未生效）"),
        ("B", 430, d(-60), "B级统货价"),
        ("C", 320, d(-60), "C级等外料价"),
    ]
    for grade, price, eff, note in contracts_s001:
        conn.execute(
            "INSERT OR IGNORE INTO contracts(supplier_id,grade,price_cents_per_kg,"
            "effective_date,note) VALUES (?,?,?,?,?)",
            (s001, grade, price, eff, note))

    s002 = conn.execute("SELECT id FROM suppliers WHERE code='S002'").fetchone()[0]
    contracts_s002 = [
        ("A", 480, d(-30), "现行A级价"),
        ("B", 390, d(-30), "现行B级价"),
        ("C", 280, d(-30), "现行C级价"),
    ]
    for grade, price, eff, note in contracts_s002:
        conn.execute(
            "INSERT OR IGNORE INTO contracts(supplier_id,grade,price_cents_per_kg,"
            "effective_date,note) VALUES (?,?,?,?,?)",
            (s002, grade, price, eff, note))

    # 预付款：S001 两笔（验证按到账先后 FIFO），S002 一笔小额（验证余额不足整单失败）
    u2 = conn.execute("SELECT id FROM users WHERE username='u2'").fetchone()[0]
    pre = [
        ("P001", s001, 4_000_000, d(-20) + " 09:15:00", "首笔预付 4 万元"),
        ("P002", s001, 6_000_000, d(-3)  + " 14:40:00", "追加预付 6 万元"),
        ("P003", s002,   500_000, d(-2)  + " 10:00:00", "小额预付 5 千元"),
    ]
    for code, sid, amt, at, note in pre:
        conn.execute(
            "INSERT OR IGNORE INTO prepayments(code,supplier_id,amount_cents,"
            "remaining_cents,received_at,created_by,note) VALUES (?,?,?,?,?,?,?)",
            (code, sid, amt, amt, at, u2, note))
