"""
The research dashboard, served locally with its graph explorer reading the
whole graph live from Neo4j (graph/explore.py): no size limit and nothing left
out.

    filovirus serve            # http://localhost:8765/explore

LOCAL ONLY. The server binds to 127.0.0.1 and answers only requests whose
Host is localhost, and its POST endpoints accept only same-origin JSON.
Browsers won't send a cross-site JSON POST without a CORS preflight, which
this server never approves, so another website open in the same browser
can't read the graph through it. (Checking Host stops a DNS-rebinding page
from pretending to be localhost.)
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from neo4j.exceptions import Neo4jError

from core_data_loader.graph.ask import Graph
from core_data_loader.graph.explore import LiveExplorer


def make_handler(explorer: LiveExplorer, port: int):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    post_routes = {"/api/explore/search": explorer.search, "/api/explore/subgraph": explorer.subgraph}

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
                self._json(403, {"error": "Open this page at http://localhost:%d/explore" % port})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/explore")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/explore":
                try:
                    self._send(200, explorer.page(), "text/html; charset=utf-8")
                except Neo4jError as e:
                    self._json(500, {"error": f"Graph query failed: {e.message}. Is Neo4j running?"})
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
            try:
                self._json(200, handler(body))
            except Neo4jError as e:
                self._json(500, {"error": f"Graph query failed: {e.message}. Is Neo4j running?"})

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(port: int = 8765):
    graph = Graph()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(LiveExplorer(graph), port))
        print(f"Dashboard, full live graph: http://localhost:{port}/explore", flush=True)
        print("Ctrl-C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        graph.close()


if __name__ == "__main__":
    serve()
