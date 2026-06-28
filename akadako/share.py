"""
Communication "share server" client.

Mirrors the share-server feature of akadako.js: a WebSocket subscription for
receiving labelled values and an HTTP POST endpoint for publishing them, scoped
by a group id. Full-width digits/letters in ids and labels are normalised to
half-width, exactly like akadako.js.

The subscription runs on a background (daemon) thread; received values are
stored and read back synchronously via ``shared_data()``. Publishing uses a
plain blocking HTTP POST.

Requires the optional ``websocket-client`` package (install with the ``share``
extra: ``pip install akadako[share]``).
"""

import json
import threading
import time
import urllib.parse
import urllib.request

_SUB_URL = "wss://ws.akadako.com/sub/"
_PUB_URL = "https://ws.akadako.com/pub/"


def normalize_id(s):
    """Convert full-width alphanumerics to half-width and strip whitespace."""
    s = str(s).strip()
    out = []
    for ch in s:
        code = ord(ch)
        # U+FF10-FF19 digits, U+FF21-FF3A upper, U+FF41-FF5A lower
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


class ShareServer:
    def __init__(self, board_is_connected=None):
        self._group_id = None
        self._ws = None
        self._recv_thread = None
        self._shared_data = {}
        self._sending = False
        self._send_interval = 1.0
        self._connect_timeout = 1.0
        self._closing = False
        self._connected = False
        self._board_is_connected = board_is_connected or (lambda: True)
        self._backoff_base = 0.1
        self._backoff_cap = 10.0

    @property
    def group_id(self):
        return self._group_id or ""

    def is_connected(self):
        return self._connected

    def connect(self, group_id):
        if not isinstance(group_id, str) or group_id.strip() == "":
            raise ValueError(group_id)
        self._group_id = normalize_id(group_id)
        self._closing = False
        self._open()

    def _open(self):
        try:
            import websocket  # websocket-client
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the 'websocket-client' package is required for share-server "
                "support; install with: pip install akadako[share]"
            ) from exc

        url = _SUB_URL + urllib.parse.quote(self._group_id, safe="")
        try:
            ws = websocket.create_connection(url, timeout=self._connect_timeout)
        except Exception as exc:
            raise RuntimeError("failed to connect to the share server") from exc
        # block on recv() until a message arrives (no read timeout)
        ws.settimeout(None)
        self._ws = ws
        self._connected = True
        self._recv_thread = threading.Thread(target=self._receive_loop, args=(ws,), daemon=True)
        self._recv_thread.start()

    def _receive_loop(self, ws):
        try:
            while not self._closing:
                try:
                    message = ws.recv()
                except Exception:
                    break
                if not message:
                    break  # connection closed
                try:
                    received = json.loads(message)
                    self._shared_data[received["key"]] = {
                        "content": received["value"],
                    }
                except (ValueError, KeyError):
                    continue
        finally:
            self._connected = False
            if not self._closing:
                self._reconnect()

    def _reconnect(self):
        attempt = 0
        while not self._closing and self._group_id and self._board_is_connected():
            jitter = min(self._backoff_cap, self._backoff_base * (2 ** attempt))
            attempt += 1
            time.sleep(jitter)
            if self._closing or not self._group_id or not self._board_is_connected():
                return
            try:
                self._open()  # starts a fresh receive thread on success
                return
            except Exception:
                continue

    def send(self, label, data):
        if not isinstance(label, str) or label.strip() == "":
            raise ValueError(label)
        if not self.is_connected():
            raise RuntimeError("share server is disconnected")
        if self._sending:
            raise RuntimeError("share server is busy")
        self._sending = True
        url = _PUB_URL + urllib.parse.quote(self._group_id, safe="")
        body = json.dumps({
            "groupId": urllib.parse.quote(self._group_id, safe=""),
            "key": normalize_id(str(label)),
            "value": str(data),
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"share server returned {resp.status}")
        finally:
            time.sleep(self._send_interval)
            self._sending = False

    def shared_data(self, label):
        if not self.is_connected():
            return ""
        key = normalize_id(str(label))
        entry = self._shared_data.get(key)
        return entry["content"] if entry else ""

    def close(self):
        self._closing = True
        self._connected = False
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._ws = None
