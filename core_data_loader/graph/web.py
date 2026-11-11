"""
The local query page: ready-made questions (free, always on) plus optional
plain-English questions answered by Claude once an API key is set.

    filovirus serve            # http://localhost:8765

LOCAL ONLY. The server binds to 127.0.0.1 and answers only requests whose
Host is localhost, and its POST endpoints accept only same-origin JSON.
Browsers won't send a cross-site JSON POST without a CORS preflight, which
this server never approves, so another website open in the same browser
can't spend the API key or read results. (Checking Host stops a DNS-rebinding
page from pretending to be localhost.)
"""

import json
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic
from neo4j.exceptions import Neo4jError

from core_data_loader.graph import api_key, review
from core_data_loader.graph.ask import GUIDE, MODEL, Graph, ask
from core_data_loader.graph.templates import (
    OPTION_QUERIES,
    TEMPLATES,
    TEMPLATES_BY_ID,
    VIRUSES,
    resolve_params,
)

PAGE = Path(__file__).with_name("query_page.html")


def _cell(value):
    if value is None:
        return None
    if hasattr(value, "iso_format"):              # neo4j Date / DateTime
        return value.iso_format()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(str(_cell(v)) for v in value if v is not None)
    if isinstance(value, float):
        return round(value, 2)
    return value


class QueryApp:
    def __init__(self, graph: Graph):
        self.graph = graph
        self.system = GUIDE + graph.schema()
        self.options = {kind: graph.run(q) for kind, q in OPTION_QUERIES.items()}
        self.options["virus"] = [{"value": k, "label": label} for k, label, _ in VIRUSES]
        self.client = None
        self.key_source = None
        key, source = api_key.current_key()
        if key:
            self.client, problem = api_key.check_key(key, MODEL)
            if self.client:
                self.key_source = source
            else:
                print(f"  ! {source} API key not used: {problem}")

    # --- endpoints ------------------------------------------------------------

    def status(self, _body=None):
        return {"ai": self.client is not None, "key_source": self.key_source}

    def templates(self, _body=None):
        return {
            "templates": [{k: t[k] for k in ("id", "group", "question", "params")} for t in TEMPLATES],
            "options": self.options,
        }

    def run_template(self, body):
        template = TEMPLATES_BY_ID.get(body.get("id"))
        if template is None:
            return {"error": "Unknown question."}
        try:
            params = resolve_params(template, body.get("params") or {}, self.options)
        except ValueError as e:
            return {"error": str(e)}
        rows = self.graph.run(template["cypher"], params)
        columns = list(rows[0].keys()) if rows else []
        return {
            "columns": columns,
            "rows": [[_cell(r[c]) for c in columns] for r in rows],
            "cypher": template["cypher"],
            "params": params,
        }

    def ask(self, body):
        if self.client is None:
            return {"error": "Add an Anthropic API key first, in the 'Ask in your own words' section."}
        question = (body.get("question") or "").strip()
        if not question:
            return {"error": "Type a question first."}
        return ask(question, self.graph, self.client, self.system)

    def set_key(self, body):
        client, problem = api_key.check_key(body.get("key", ""), MODEL)
        if problem:
            return {"error": problem}
        self.client = client
        if body.get("remember"):
            api_key.save_key(body["key"])
            self.key_source = "saved"
        else:
            self.key_source = "this session"
        return self.status()

    # --- link review (graph/review.py) --------------------------------------------

    def review_next(self, body):
        tier = body.get("tier")
        link = review.next_link(float(tier) if tier not in (None, "") else None)
        return link or {"done": True}

    def review_record(self, body):
        try:
            review.record(body.get("link_key", ""), body.get("decision", ""), float(body.get("confidence", 0)),
                          (body.get("note") or "").strip() or None)
        except ValueError as e:
            return {"error": str(e)}
        return {"stats": review.stats()}

    def review_stats(self, _body=None):
        return {"stats": review.stats()}

    def forget_key(self, _body=None):
        api_key.forget_key()
        key, source = api_key.current_key()        # the environment variable may still be set
        self.client = api_key.check_key(key, MODEL)[0] if key else None
        self.key_source = source if self.client else None
        return self.status()


def make_handler(app: QueryApp, port: int):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    get_routes = {"/api/status": app.status, "/api/templates": app.templates}
    post_routes = {
        "/api/template": app.run_template, "/api/ask": app.ask,
        "/api/key": app.set_key, "/api/key/forget": app.forget_key,
        "/api/review/next": app.review_next, "/api/review": app.review_record,
        "/api/review/stats": app.review_stats,
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "Open this page at http://localhost:%d" % port})
                return False
            return True

        def _call(self, handler, body=None):
            try:
                self._json(200, handler(body or {}))
            except Neo4jError as e:
                self._json(500, {"error": f"Graph query failed: {e.message}. Is Neo4j running?"})
            except anthropic.APIError as e:
                self._json(502, {"error": f"Claude API error: {e}"})

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path in get_routes:
                self._call(get_routes[self.path])
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self._json(403, {"error": "Cross-site request refused."})
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": "Expected JSON."})
                return
            handler = post_routes.get(self.path)
            if handler is None:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "Bad JSON."})
                return
            self._call(handler, body)

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(port: int = 8765):
    graph = Graph()
    try:
        app = QueryApp(graph)
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
        ai = f"on (key from {app.key_source})" if app.client else "off until you add a key on the page"
        print(f"Query page: http://localhost:{port}   (plain-English AI questions: {ai})", flush=True)
        print("Ctrl-C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        graph.close()


if __name__ == "__main__":
    serve()
