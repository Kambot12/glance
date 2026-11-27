#!/usr/bin/env python3
"""Serve the overlay detector and collect what it observed."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8900
RESULTS = ROOT / "results.json"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        RESULTS.write_text(json.dumps(data, indent=2))
        print(f"\n=== results received {data.get('when')} ===")
        for r in data.get("rows", []):
            print(f"  [{r['state']:4}] {r['name']:32} {r['obs']}")
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    print(f"detector on http://127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
