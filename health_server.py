"""Lightweight stdlib HTTP health endpoint for PaaS hosting (Hugging Face Spaces, etc.)."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)


def build_health_response(
    path: str,
    status_provider: Callable[[], Dict[str, Any]],
) -> Tuple[int, bytes]:
    """Build (http_status, json_bytes) for health check routes."""
    if path not in ("/", "/health"):
        return 404, b""
    payload: Dict[str, Any] = {
        "status": "ok",
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    try:
        payload.update(status_provider())
    except Exception as exc:
        payload["detail"] = str(exc)
    return 200, json.dumps(payload).encode("utf-8")


def start_health_server(
    port: int,
    status_provider: Callable[[], Dict[str, Any]],
    host: str = "0.0.0.0",
) -> ThreadingHTTPServer:
    """Start a background HTTP server responding 200 OK on / and /health."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, body = build_health_response(self.path, status_provider)
            self.send_response(code)
            if body:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-http")
    thread.start()
    logger.info("Health HTTP server listening on http://%s:%d/health", host, server.server_address[1])
    return server
