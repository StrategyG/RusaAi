"""Tiny deterministic API probe used only by the real Compose rollback drill."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os


READY_STATUS = int(os.getenv("READY_STATUS", "200"))
RELEASE_ID = os.getenv("RELEASE_ID", "ci")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health/live":
            status = 200
            payload = {"status": "ok", "release": RELEASE_ID}
        elif self.path == "/health/ready":
            status = READY_STATUS
            payload = {"status": "ready" if status == 200 else "not_ready", "release": RELEASE_ID}
        else:
            status = 404
            payload = {"detail": "not found"}

        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        return


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
