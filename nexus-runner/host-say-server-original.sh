#!/usr/bin/env python3
# ─── Clippy Host Say Server ─────────────────────────────────────────────
# Run this on your Mac (host). It listens for HTTP POSTs with text and
# speaks them using macOS "say", so Clippy's responses play on your speakers
# instead of in the VM (which has no real audio).
#
# Usage:
#   ./host-say-server.sh              # listen on 0.0.0.0:3938
#   PORT=3939 ./host-say-server.sh    # custom port
#
# The VM bridge can send TTS here by setting CLIPPY_HOST_SAY_URL
# (see README). Test: curl -X POST -d "Hello from Clippy" http://localhost:3938/say
# ─────────────────────────────────────────────────────────────────────────

import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(__import__("os").environ.get("PORT", "3938"))
SAY_VOICE = __import__("os").environ.get("SAY_VOICE", "Daniel")
SAY_RATE = __import__("os").environ.get("SAY_RATE", "175")


class SayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/say":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace").strip()[:50000]
        self.send_response(200)
        self.end_headers()
        if body:
            subprocess.Popen(
                ["say", "-v", SAY_VOICE, "-r", SAY_RATE, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}", file=sys.stderr)


if __name__ == "__main__":
    print(f"Say server listening on 0.0.0.0:{PORT} (voice={SAY_VOICE} rate={SAY_RATE})", file=sys.stderr)
    HTTPServer(("0.0.0.0", PORT), SayHandler).serve_forever()
