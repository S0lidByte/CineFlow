"""Disposable deterministic provider/HTTP Range fixture, never real credentials."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

PAYLOAD = bytes(range(256)) * 8192

_STATE_LOCK = threading.Lock()
_RUNTIME_STATE = {
    "alldebrid_mode": os.getenv("E2E_ALLDEBRID_MODE", "premium"),
}


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path == "/health":
            self.reply(200, {"ready": True})
        elif clean_path == "/control/state":
            with _STATE_LOCK:
                state_copy = dict(_RUNTIME_STATE)
            self.reply(200, {"ok": True, "state": state_copy})
        elif clean_path == "/v4/user":
            with _STATE_LOCK:
                mode = _RUNTIME_STATE["alldebrid_mode"]
            user = {"username": "fixture", "isPremium": mode == "premium"}
            self.reply(
                200,
                (
                    {"status": "error"}
                    if mode == "error"
                    else {"status": "success", "data": {"user": user}}
                ),
            )
        elif clean_path in ("/rest/1.0/user", "/user"):
            # Real-Debrid probe endpoint
            self.reply(
                200,
                {
                    "id": 12345,
                    "username": "rd_fixture_user",
                    "email": "fixture@cineflow.local",
                },
            )
        elif clean_path in ("/account", "/identity"):
            # Plex probe endpoint
            self.reply(
                200,
                {
                    "user": {
                        "title": "plex_fixture_user",
                        "email": "plex@cineflow.local",
                    }
                },
            )
        elif clean_path == "/healthchecks/ping":
            # Zilean ping endpoint fallback if routed to fixture
            self.reply(200, {"ping": "pong"})
        elif clean_path == "/api/v1/system/status":
            # Prowlarr status endpoint fallback if routed to fixture
            self.reply(200, {"version": "1.0.0", "appName": "Prowlarr"})
        elif clean_path == "/media":
            self.send_media()
        else:
            self.reply(404, {"error": "fixture route unavailable", "path": clean_path})

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        if clean_path == "/control/alldebrid":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                parsed_raw: object = (
                    json.loads(raw_body.decode("utf-8")) if raw_body else {}
                )
            except Exception:
                self.reply(400, {"ok": False, "error": "Invalid JSON body"})
                return

            mode: str | None = None
            if isinstance(parsed_raw, dict):
                val = cast(dict[str, object], parsed_raw).get("mode")
                if isinstance(val, str):
                    mode = val
            if mode not in ("premium", "free", "error"):
                self.reply(
                    400,
                    {
                        "ok": False,
                        "error": "Invalid mode. Allowed: premium, free, error",
                    },
                )
                return

            with _STATE_LOCK:
                _RUNTIME_STATE["alldebrid_mode"] = mode
                current_mode = _RUNTIME_STATE["alldebrid_mode"]
            self.reply(200, {"ok": True, "mode": current_mode})
        else:
            self.reply(404, {"error": "fixture route unavailable", "path": clean_path})

    def send_media(self):
        requested = self.headers.get("Range")
        if requested:
            try:
                start_text, _, end_text = requested.removeprefix("bytes=").partition(
                    "-"
                )
                start = int(start_text)
                end = min(
                    int(end_text) if end_text else len(PAYLOAD) - 1, len(PAYLOAD) - 1
                )
            except ValueError:
                self.send_error(400)
                return
            if start >= len(PAYLOAD) or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(PAYLOAD)}")
                self.end_headers()
                return
            body = PAYLOAD[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self, status: int, payload: dict[str, object]):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object):
        # Do not log request headers or credentials.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), Fixture).serve_forever()
