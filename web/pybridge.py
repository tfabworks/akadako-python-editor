"""Pyodide-side bridge that lets the unmodified `akadako` library drive a real
AkaDako board over the browser Web MIDI API.

The library was written for desktop CPython + python-rtmidi, where:

  * MIDI input arrives on a separate (rtmidi callback) OS thread, and
  * request/reply reads block the calling thread on `threading.Event.wait()`.

A browser worker has no such background thread and cannot block without
starving the event loop. We bridge the gap with three pieces, all installed by
`install()` *before* `akadako` is used:

  1. WebMidiTransport  - replaces MidiTransport; out-bytes go to the main thread
     (which owns the MIDI port), in-bytes are pulled from a SharedArrayBuffer
     ring that the main thread fills.
  2. a "pumping" time.sleep - while sleeping we drain the in-ring and feed the
     bytes to Firmata, so streamed analog/digital values keep the caches fresh.
  3. a "pumping" Event - the same drain loop also resolves the request/reply
     waiters Firmata blocks on (I2C reads, board version/uid, water temp).

The drain blocks efficiently via `Atomics.wait` (exposed from JS as
`midiPump`), and returns at least every ~100 ms so Pyodide can honour the
interrupt buffer (Stop button -> KeyboardInterrupt).
"""

import sys
import time
import types

from js import midiPump, midiSend  # JS helpers installed by worker.js
from pyodide.ffi import to_js


def _send(data):
    """Hand a list of MIDI bytes to the main thread as a real JS array."""
    midiSend(to_js(list(data)))

# The transport currently feeding Firmata (set in WebMidiTransport.find_and_open).
_active = None


# ---------------------------------------------------------------------------
# Incoming-byte pump  (device -> Python)
# ---------------------------------------------------------------------------

def _pump(timeout_ms):
    """Pull any bytes the main thread has queued and feed them to Firmata.

    Blocks up to `timeout_ms` (via Atomics.wait in JS) when nothing is ready.
    Returns the number of bytes drained.
    """
    chunk = midiPump(int(timeout_ms))          # JS Uint8Array (possibly empty)
    if chunk is None:
        return 0
    data = bytes(chunk.to_py())
    if data and _active is not None and _active._callback is not None:
        _active._callback(data)
    return len(data)


# ---------------------------------------------------------------------------
# 1. Transport
# ---------------------------------------------------------------------------

class WebMidiTransport:
    """Drop-in replacement for akadako.transport.MidiTransport.

    The actual MIDI port is opened on the main thread before any Python runs;
    here we only need the same public surface the library calls:
    find_and_open / set_data_callback / write / close. The byte-level Firmata
    conversions in write() are copied verbatim from MidiTransport so board
    behaviour is identical.
    """

    def __init__(self):
        self._callback = None

    @classmethod
    def find_and_open(cls, name_filter=None):
        global _active
        t = cls()
        _active = t
        return t

    def set_data_callback(self, callback):
        self._callback = callback

    def write(self, data):
        data = list(data)
        if not data:
            return
        if data[0] == 0xF9:                       # report version - reserved
            return
        if data[0] == 0xF4:                       # set PinMode -> 0xA0
            _send([0xA0, data[1], data[2]])
            return
        if data[0] == 0xF0 and len(data) > 1 and data[1] == 0x79:
            return                                # query firmware - board freezes
        if len(data) == 3 and data[0] == 0xF0 and data[2] == 0xF7:
            data.insert(2, 0x00)                  # one-byte SysEx - pad
            _send(data)
            return
        _send(data)

    def close(self):
        global _active
        if _active is self:
            _active = None


# ---------------------------------------------------------------------------
# 2. Pumping sleep
# ---------------------------------------------------------------------------

def _pumping_sleep(seconds):
    """time.sleep replacement that drains incoming MIDI while waiting.

    Without this, streamed analog/digital values would never reach the caches
    during a plain sleep (a very common shape: read -> sleep -> repeat).
    """
    deadline = time.monotonic() + (seconds or 0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _pump(min(remaining * 1000, 100))         # cap so interrupts stay responsive


# ---------------------------------------------------------------------------
# 3. Pumping Event  (resolves Firmata request/reply waiters)
# ---------------------------------------------------------------------------

class _PumpingEvent:
    """threading.Event look-alike whose wait() drives the incoming pump.

    Firmata's _Waiter.event.set() is called from inside _on_data, which we
    invoke from _pump -- so a reply that arrives mid-wait flips _flag and the
    loop returns True.
    """

    __slots__ = ("_flag",)

    def __init__(self):
        self._flag = False

    def set(self):
        self._flag = True

    def clear(self):
        self._flag = False

    def is_set(self):
        return self._flag

    def wait(self, timeout=None):
        if timeout is None:
            timeout = 3600
        deadline = time.monotonic() + timeout
        while not self._flag:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _pump(min(remaining * 1000, 100))
        return self._flag


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def _jsval(value):
    """Reduce a Python value to something the JS side can display directly."""
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    return str(value)


def _install_debug():
    """Wire a live "sensor monitor": every sensor read (and any explicit
    monitor() call) is pushed to the main thread for display.

    Read methods are wrapped on the AkaDako class so all instances created by
    user code are instrumented automatically. Only leaf reads are wrapped
    (fetch_* / analog_* / digital_* / motion_sensor); the bare analog()/digital()
    dispatchers they call internally are left alone to avoid double-reporting.
    """
    try:
        from js import dbgValue
    except Exception:
        return  # no debug channel installed by the host; skip silently

    import builtins
    import akadako.board as board_mod

    def report(name, value):
        try:
            dbgValue(str(name), _jsval(value))
        except Exception:
            pass

    cls = board_mod.AkaDako
    prefixes = ("fetch_", "analog_", "digital_")
    extra = {"motion_sensor"}

    def wrap(name, fn):
        def wrapper(self, *args, **kwargs):
            result = fn(self, *args, **kwargs)
            report(name, result)
            return result
        wrapper.__name__ = name
        return wrapper

    for name in list(vars(cls)):
        if name.startswith("_"):
            continue
        if name.startswith(prefixes) or name in extra:
            fn = vars(cls)[name]
            if callable(fn):
                setattr(cls, name, wrap(name, fn))

    def monitor(name, value):
        """Expose a value in the live monitor panel; returns it unchanged."""
        report(name, value)
        return value

    builtins.monitor = monitor


_SHARE_SUB = "wss://ws.akadako.com/sub/"
_SHARE_PUB = "https://ws.akadako.com/pub/"


def _normalize_id(s):
    """Full-width alphanumerics -> half-width, stripped (matches share.py)."""
    s = str(s).strip()
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


class WebShareServer:
    """Browser replacement for akadako.share.ShareServer.

    Same public surface board.py relies on (connect / send / shared_data /
    is_connected / _sending), but the WebSocket + HTTP POST live on the main
    thread (see worker.js / main.js); received values cross back via a
    SharedArrayBuffer so shared_data() can read them synchronously even while
    the worker is blocked in a pumping time.sleep.
    """

    def __init__(self, board_is_connected=None):
        self._group_id = None
        self._sending = False
        self._send_interval = 1.0
        self._cache = {}
        self._cache_seq = -1

    def is_connected(self):
        from js import shareConnected
        return bool(shareConnected())

    def connect(self, group_id):
        if not isinstance(group_id, str) or group_id.strip() == "":
            raise ValueError(group_id)
        from js import shareConnect
        from urllib.parse import quote
        self._group_id = _normalize_id(group_id)
        shareConnect(_SHARE_SUB + quote(self._group_id, safe=""), self._group_id)
        # give the socket a moment to open so is_connected() is meaningful
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.is_connected():
            _pump(100)

    def send(self, label, data):
        if not isinstance(label, str) or label.strip() == "":
            raise ValueError(label)
        if not self.is_connected():
            raise RuntimeError("share server is disconnected")
        if self._sending:
            raise RuntimeError("share server is busy")
        self._sending = True
        from js import shareSend
        from urllib.parse import quote
        import json
        url = _SHARE_PUB + quote(self._group_id, safe="")
        body = json.dumps({
            "groupId": quote(self._group_id, safe=""),
            "key": _normalize_id(str(label)),
            "value": str(data),
        })
        try:
            shareSend(url, body)
        finally:
            time.sleep(self._send_interval)   # pumping sleep (rate limit)
            self._sending = False

    def shared_data(self, label):
        if not self.is_connected():
            return ""
        from js import shareSeq, shareReadValues
        seq = shareSeq()
        if seq != self._cache_seq:
            import json
            raw = shareReadValues()
            try:
                self._cache = json.loads(raw) if raw else {}
            except Exception:
                self._cache = {}
            self._cache_seq = seq
        return self._cache.get(_normalize_id(str(label)), "")

    def close(self):
        try:
            from js import shareClose
            shareClose()
        except Exception:
            pass


def probe_sensors():
    """Connect, try each sensor once, and return JSON of the ones that respond.

    Used to tailor the starter program to the board actually plugged in.
    Missing I2C sensors time out (~2s each), so we group the BME280 readings
    (temperature implies humidity/pressure) to keep detection short.
    """
    import json
    from akadako import AkaDako

    board = AkaDako.connect()

    def ok(call):
        try:
            call()
            return True
        except Exception:
            return False

    available = []
    # BME280 (環境センサー): 温度が読めれば湿度・気圧も同じセンサー
    if ok(board.fetch_temperature):
        available += ["fetch_temperature", "fetch_humidity", "fetch_pressure"]
    for name in (
        "fetch_brightness", "analog_brightness", "fetch_optical_distance",
        "fetch_acceleration_x", "motion_sensor", "analog_a1", "digital_a1",
    ):
        if ok(getattr(board, name)):
            available.append(name)

    try:
        board.disconnect()
    except Exception:
        pass
    return json.dumps(available)


def _dbg_collect_vars(frame):
    """Snapshot the simple local variables at a pause point (for display)."""
    out = []
    for k, v in list(frame.f_locals.items()):
        if k.startswith("__"):
            continue
        try:
            if isinstance(v, bool) or isinstance(v, (int, float, str)):
                out.append([k, repr(v)])
            elif isinstance(v, (list, tuple, dict)):
                s = repr(v)
                out.append([k, s[:60] + ("…" if len(s) > 60 else "")])
            # objects / functions / modules are skipped to keep it readable
        except Exception:
            pass
    return out


def run_debug(code, breakpoints, step):
    """Run user code under sys.settrace for step / breakpoint debugging.

    Pauses on every line (step mode) or at breakpoint lines, sends the current
    line + locals to the UI (dbgPause), then blocks on dbgWait until the UI
    sends a command: 1=step, 2=continue, 3=stop.
    """
    import sys
    import json
    from js import dbgPause, dbgWait

    bps = set(int(b) for b in (breakpoints or []))
    state = {"mode": "step" if step else "run"}

    def _pause(lineno, frame):
        dbgPause(lineno, json.dumps(_dbg_collect_vars(frame), ensure_ascii=False))
        cmd = int(dbgWait())
        if cmd == 3:
            raise KeyboardInterrupt()
        state["mode"] = "step" if cmd == 1 else "run"

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != "<exec>":
            return None          # don't trace into library code (fast)
        if event == "line":
            ln = frame.f_lineno
            if state["mode"] == "step" or ln in bps:
                _pause(ln, frame)
        return tracer

    compiled = compile(code, "<exec>", "exec")
    sys.settrace(tracer)
    try:
        exec(compiled, {"__name__": "__main__"})
    finally:
        sys.settrace(None)


def install():
    """Patch akadako + stdlib so the library runs in the browser worker."""
    # A stub so `import rtmidi` (lazy, inside the original find_and_open) never
    # explodes even if some code path reaches it.
    if "rtmidi" not in sys.modules:
        sys.modules["rtmidi"] = types.ModuleType("rtmidi")

    time.sleep = _pumping_sleep

    import akadako.board as board_mod
    import akadako.firmata as firmata_mod

    # board.connect() calls MidiTransport.find_and_open -> swap the name it uses.
    board_mod.MidiTransport = WebMidiTransport

    # Browser share server (WebSocket/fetch bridge instead of websocket-client).
    board_mod.ShareServer = WebShareServer

    # Firmata blocks on threading.Event(); give it our pumping variant while
    # keeping the real Lock (harmless and correct in a single thread).
    import threading as _real_threading
    firmata_mod.threading = types.SimpleNamespace(
        Event=_PumpingEvent,
        Lock=_real_threading.Lock,
    )

    _install_debug()
