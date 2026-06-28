#!/usr/bin/env python3
"""Dev server for the AkaDako browser Python editor PoC.

SharedArrayBuffer (required for Atomics-based synchronous MIDI reads and the
Pyodide interrupt buffer) is only available in a *cross-origin isolated* page,
which requires these two response headers:

    Cross-Origin-Opener-Policy:   same-origin
    Cross-Origin-Embedder-Policy: require-corp

Python's stock http.server does not send them, so this thin wrapper adds them
(plus no-cache, so edits show up on reload).

Usage:
    python3 serve.py            # http://localhost:8770
    python3 serve.py 9000       # custom port
"""

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",     # required for WebAssembly streaming compile
    }

    def end_headers(self):
        # crossOriginIsolated (-> SharedArrayBuffer + Atomics) requires these.
        # require-corp works across Chrome/Edge AND Safari/iPad (credentialless
        # is not supported on older Safari), and all our assets are same-origin
        # (see fetch-vendor.sh), so require-corp is the portable choice.
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    handler = partial(Handler, directory=".")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://localhost:{port}/web/"
    print(f"AkaDako Python (browser PoC) serving at {url}")
    print("Open it in Chrome/Edge (Web MIDI required). Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
