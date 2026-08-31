#!/usr/bin/env python3
"""Serve the generated Codex Screener app on a loopback-only HTTP server."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serve mutable dashboard assets without stale browser caching."""

    def __init__(self, *args: object, directory: str, **kwargs: object) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        path = self.path.partition("?")[0]
        if path in {"/", "/index.html", "/assets/app.css", "/assets/app.js", "/data/latest.json", "/data/live-status.json"}:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
        super().end_headers()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("dashboard-app"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    def handler(*handler_args: object, **handler_kwargs: object) -> DashboardHandler:
        return DashboardHandler(*handler_args, directory=str(root), **handler_kwargs)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {root} at http://{args.host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
