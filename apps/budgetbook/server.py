"""BudgetBook HTTP 后端：仅绑定 127.0.0.1。"""

import argparse
import csv
import io
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from solution import Book

HERE = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024
DATA = HERE / "book.json"


class Handler(BaseHTTPRequestHandler):
    server_version = "BudgetBook/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _book(self):
        return Book(DATA)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                html = (HERE / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
            elif route == "/api/entries":
                query = self._query()
                rows = self._book().entries(
                    month=(query.get("month") or [None])[0],
                    category=(query.get("category") or [None])[0],
                )
                self._json(200, rows)
            elif route == "/api/summary":
                query = self._query()
                self._json(
                    200, self._book().summary(month=(query.get("month") or [None])[0])
                )
            elif route == "/api/export":
                book = self._book()
                buffer = io.StringIO()
                writer = csv.DictWriter(
                    buffer, fieldnames=["id", "amount", "category", "note", "date"]
                )
                writer.writeheader()
                for item in book.entries():
                    writer.writerow(item)
                body = buffer.getvalue().encode("utf-8")
                self._send(
                    200,
                    body,
                    "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="budgetbook.csv"'},
                )
            else:
                self._error(404, "未找到该接口")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception:  # noqa: BLE001 - HTTP boundary returns a generic 500.
            self._error(500, "服务器内部错误")

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/entries":
            self._error(404, "未找到该接口")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "请求体长度无效")
            return
        if length > MAX_BODY:
            self._error(400, "请求体过大")
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "请求体必须是 JSON 对象")
            return
        try:
            entry = self._book().add(
                payload.get("amount"),
                payload.get("category"),
                payload.get("note") or "",
                payload.get("date") or None,
            )
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except Exception:  # noqa: BLE001 - HTTP boundary returns a generic 500.
            self._error(500, "服务器内部错误")
            return
        self._json(201, entry)

    def do_DELETE(self):
        route = urlparse(self.path).path
        prefix = "/api/entries/"
        if not route.startswith(prefix):
            self._error(404, "未找到该接口")
            return
        raw_id = unquote(route[len(prefix) :]).strip("/")
        try:
            entry_id = int(raw_id)
        except ValueError:
            self._error(400, "记录 ID 无效")
            return
        try:
            deleted = self._book().delete(entry_id)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except Exception:  # noqa: BLE001 - HTTP boundary returns a generic 500.
            self._error(500, "服务器内部错误")
            return
        self._json(200, {"deleted": deleted})


def main(argv=None):
    global DATA
    parser = argparse.ArgumentParser(description="BudgetBook 本地记账服务")
    parser.add_argument("--data", default=str(HERE / "book.json"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    DATA = Path(args.data)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "port": port}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
