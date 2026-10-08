"""Authenticated loopback GET API using the same policy as MCP."""
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import whatsapp


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), expected.encode("ascii")):
            self.respond(401, {"error": "Authentication required"}); return
        url = urlsplit(self.path)
        routes = {
            "/api/chats": (whatsapp.list_chats, {"query", "limit", "page"}),
            "/api/messages": (whatsapp.list_messages, {"chat_jid", "query", "after", "before", "limit", "page"}),
            "/api/context": (whatsapp.get_message_context, {"message_id", "before", "after"}),
        }
        if url.path not in routes:
            self.respond(404, {"error": "Unknown read endpoint"}); return
        function, allowed = routes[url.path]
        try:
            parameters = parse_qs(url.query, keep_blank_values=True, max_num_fields=20)
            if set(parameters) - allowed or any(len(v) != 1 for v in parameters.values()):
                raise ValueError("Unsupported parameters")
            parameters = {k: int(v[0]) if k in {"limit", "page", "before", "after"} else v[0] for k, v in parameters.items()}
            if url.path == "/api/messages":
                parameters["include_context"] = False
            self.respond(200, {"data": function(**parameters)})
        except (ValueError, TypeError):
            self.respond(400, {"error": "Invalid query"})
        except Exception:
            self.respond(503, {"error": "Verified context unavailable"})

    def deny_write(self):
        self.respond(405, {"error": "Read-only API"})

    do_POST = do_PUT = do_PATCH = do_DELETE = deny_write


def make_server(token, port=8080):
    if len(token) < 32 or not token.isascii():
        raise ValueError("ARC_API_TOKEN must contain at least 32 ASCII characters")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token
    return server


if __name__ == "__main__":
    server = make_server(os.environ.get("ARC_API_TOKEN", ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
