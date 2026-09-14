# -*- coding: utf-8 -*-
"""零依赖 HTTP 服务（标准库 http.server + 真实 SQLite）。

- 写请求一律包在显式事务里；付款/冲正由 services 自行 BEGIN IMMEDIATE。
- 所有接口返回 JSON；业务错误带 error/message，状态码语义化。
- Cookie 会话仅用于识别当前操作人员（本地演示，无密码）。
"""
import json
import os
import re
import secrets
from contextlib import contextmanager
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import services as svc
from .db import connect, init_db

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")
SESSION_COOKIE = "PULP_SESSION"
SESSIONS = {}  # token -> user dict（进程内；重启需重新登录，业务数据不受影响）

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


@contextmanager
def transaction(conn):
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "PulpSettlement/1.0"

    # ------------------------------------------------------------ 基础收发
    def log_message(self, fmt, *args):
        pass  # 测试输出保持干净；需要时设 PULP_QUIET=0

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise svc.ServiceError("请求体必须是合法 JSON", "invalid_json")
        if not isinstance(data, dict):
            raise svc.ServiceError("请求体必须是 JSON 对象", "invalid_json")
        return data

    def _actor(self, conn):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(SESSION_COOKIE)
        if not morsel:
            return None
        return SESSIONS.get(morsel.value)

    def _serve_file(self, filename):
        if filename not in ("index.html", "app.js", "styles.css"):
            self.send_error(404)
            return
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        ext = os.path.splitext(filename)[1]
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        conn = connect()
        try:
            actor = self._actor(conn)
            m = re.fullmatch(r"/api/receipts/(\d+)", path)
            ms = re.fullmatch(r"/api/settlements/(\d+)", path)
            routes = {
                "/": lambda: self._serve_file("index.html"),
                "/static/app.js": lambda: self._serve_file("app.js"),
                "/static/styles.css": lambda: self._serve_file("styles.css"),
                "/api/me": lambda: self._ok(actor or {}),
                # 用户花名册用于登录下拉，公开可读（写操作仍各自鉴权）
                "/api/users": lambda: self._ok(svc.list_users(conn)),
                "/api/suppliers": lambda: self._ok(svc.list_suppliers(conn)),
                "/api/contracts": lambda: self._ok(svc.list_contracts(conn)),
                "/api/tiers": lambda: self._ok(svc.list_tiers(conn)),
                "/api/prepayments": lambda: self._ok(svc.list_prepayments(conn)),
                "/api/receipts": lambda: self._ok(svc.list_receipts(conn)),
                "/api/settlements": lambda: self._ok(svc.list_settlements(conn)),
                "/api/summary": lambda: self._ok(self._summary(conn)),
            }
            if m:
                rid = int(m.group(1))
                return self._ok(svc.get_receipt(conn, rid) or {})
            if ms:
                sid = int(ms.group(1))
                d = svc.get_settlement(conn, sid)
                if d is None:
                    return self._send_json(404, {"error": "settlement_not_found",
                                                 "message": "结算单不存在"})
                return self._ok(d)
            handler = routes.get(path)
            if handler:
                return handler()
            self._send_json(404, {"error": "not_found", "message": "接口不存在"})
        except svc.ServiceError as e:
            self._send_json(e.http, {"error": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": "server_error", "message": str(e)})
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        conn = connect()
        try:
            actor = self._actor(conn)
            data = self._read_json()

            if path == "/api/login":
                user = svc.authenticate(conn, str(data.get("username", "")).strip())
                token = secrets.token_hex(16)
                SESSIONS[token] = user
                return self._send_json(200, user,
                                       [("Set-Cookie",
                                         f"{SESSION_COOKIE}={token}; Path=/; "
                                         "HttpOnly; SameSite=Lax")])
            if path == "/api/logout":
                cookie_header = self.headers.get("Cookie")
                if cookie_header:
                    jar = cookies.SimpleCookie()
                    jar.load(cookie_header)
                    morsel = jar.get(SESSION_COOKIE)
                    if morsel:
                        SESSIONS.pop(morsel.value, None)
                return self._send_json(200, {"ok": True},
                                       [("Set-Cookie",
                                         f"{SESSION_COOKIE}=; Path=/; Max-Age=0")])

            if not actor:
                raise svc.ServiceError("请先登录", "unauthorized", 401)

            # 简单写操作：统一事务包裹
            simple = {
                "/api/suppliers": lambda: svc.create_supplier(conn, data, actor),
                "/api/contracts": lambda: svc.create_contract(conn, data, actor),
                "/api/prepayments": lambda: svc.create_prepayment(conn, data, actor),
                "/api/receipts": lambda: svc.create_receipt(conn, data, actor),
            }
            if path in simple:
                with transaction(conn):
                    result = simple[path]()
                return self._ok(result, 201)

            m = re.fullmatch(r"/api/receipts/(\d+)/settle", path)
            if m:
                with transaction(conn):
                    result = svc.create_settlement(conn, int(m.group(1)), actor)
                return self._ok(result, 201)

            m = re.fullmatch(r"/api/settlements/(\d+)/review", path)
            if m:
                with transaction(conn):
                    result = svc.add_review(conn, int(m.group(1)), actor,
                                            data.get("note", ""))
                return self._ok(result)

            m = re.fullmatch(r"/api/settlements/(\d+)/pay", path)
            if m:
                # 付款自行管理 IMMEDIATE 事务（整单成功或整单回滚）
                result = svc.pay_settlement(conn, int(m.group(1)), actor)
                return self._ok(result)

            m = re.fullmatch(r"/api/settlements/(\d+)/reverse", path)
            if m:
                result = svc.reverse_payment(conn, int(m.group(1)), actor,
                                             data.get("reason", ""))
                return self._ok(result)

            self._send_json(404, {"error": "not_found", "message": "接口不存在"})
        except svc.ServiceError as e:
            self._send_json(e.http, {"error": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": "server_error", "message": str(e)})
        finally:
            conn.close()

    # ------------------------------------------------------------ 响应辅助
    def _ok(self, payload, status=200):
        self._send_json(status, payload)

    def _require(self, actor, fn):
        if not actor:
            raise svc.ServiceError("请先登录", "unauthorized", 401)
        return fn()

    def _summary(self, conn):
        c = conn.cursor()
        def scalar(sql, args=()):
            return c.execute(sql, args).fetchone()[0]
        return {
            "receipt_count": scalar("SELECT COUNT(*) FROM receipts"),
            "settled_count": scalar(
                "SELECT COUNT(*) FROM settlements WHERE status='paid'"),
            "pending_review": scalar(
                "SELECT COUNT(*) FROM settlements WHERE status='pending_review'"),
            "payable_cents": scalar(
                "SELECT COALESCE(SUM(amount_cents),0) FROM settlements "
                "WHERE status IN ('pending_review','approved')"),
            "paid_cents": scalar(
                "SELECT COALESCE(SUM(amount_cents),0) FROM settlements "
                "WHERE status='paid'"),
            "prepay_remaining_cents": scalar(
                "SELECT COALESCE(SUM(remaining_cents),0) FROM prepayments"),
        }


def main():
    init_db()
    host = os.environ.get("PULP_HOST", "127.0.0.1")
    port = int(os.environ.get("PULP_PORT", "8050"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"纸浆原料验收结算系统: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
