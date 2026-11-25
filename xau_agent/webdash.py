"""Serve the dashboard over the Tailscale network only, so it can be opened from a phone anywhere.

Serves exactly dashboard.html and status_*.js from the project folder — nothing else (no .env, journal, logs).
Binds to this PC's Tailscale IPv4 address (100.x.y.z), so it is unreachable from the LAN or the internet; the
firewall rule from scripts/runtime_setup.ps1 also limits the port to Tailscale addresses.

    python -m xau_agent.webdash              # http://<tailscale-ip>:8765/
    DASH_BIND=100.81.86.78 DASH_PORT=8765    # optional overrides
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = re.compile(r"^/(dashboard\.html|status_[a-z0-9_]+\.js)$")
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def tailscale_ip() -> str:
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()
            if out:
                return out[0]
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise SystemExit("Tailscale IP not found: is Tailscale running? (or set DASH_BIND)")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/dashboard.html"
        if not ALLOWED.match(path) or ipaddress.ip_address(self.client_address[0]) not in TAILNET:
            self.send_error(404)
            return
        f = ROOT / path.lstrip("/")
        if not f.is_file():
            self.send_error(404)
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if path.endswith(".html")
                         else "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    host = os.environ.get("DASH_BIND") or tailscale_ip()
    if ipaddress.ip_address(host) not in TAILNET:
        raise SystemExit(f"refusing to bind {host}: not a Tailscale address")
    port = int(os.environ.get("DASH_PORT", "8765"))
    print(f"dashboard on http://{host}:{port}/ (Tailscale only)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
