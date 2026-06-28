// Worker: runs Pyodide + the unmodified akadako library, bridged to the main
// thread's Web MIDI port via two SharedArrayBuffers.
//
//   inSAB  (device -> Python):  ring buffer the main thread fills with incoming
//                               MIDI bytes; we drain it in midiPump() and block
//                               on Atomics.wait when it's empty.
//   irqSAB (Stop button):       Pyodide interrupt buffer (writes SIGINT here).
//
// Outgoing bytes (Python -> device) are posted to the main thread, which owns
// the MIDIOutput.

importScripts("/web/vendor/pyodide/pyodide.js");   // self-hosted (see fetch-vendor.sh)

const RING = 65536;          // incoming ring capacity (bytes)
const HDR = 16;              // header bytes before the data region

let pyodide = null;
let ctrl = null;             // Int32Array view over inSAB header ([0] = writeCount)
let ring = null;             // Uint8Array view over inSAB data region
let readCount = 0;           // worker-local read cursor
let shareCtrl = null;        // Int32Array over shareSAB: [0]=connected [1]=seq [2]=len
let shareData = null;        // Uint8Array over shareSAB data region (UTF-8 JSON)

// Files of the vendored akadako package + this PoC's bridge, written into the
// Pyodide virtual FS so `import akadako` works offline.
const PY_FILES = [
  ["/akadako/__init__.py",          "akadako/__init__.py"],
  ["/akadako/board.py",             "akadako/board.py"],
  ["/akadako/firmata.py",           "akadako/firmata.py"],
  ["/akadako/transport.py",         "akadako/transport.py"],
  ["/akadako/constants.py",         "akadako/constants.py"],
  ["/akadako/errors.py",            "akadako/errors.py"],
  ["/akadako/share.py",             "akadako/share.py"],
  ["/akadako/sensors/__init__.py",  "akadako/sensors/__init__.py"],
  ["/akadako/sensors/adxl345.py",   "akadako/sensors/adxl345.py"],
  ["/akadako/sensors/bme280.py",    "akadako/sensors/bme280.py"],
  ["/akadako/sensors/kxtj3.py",     "akadako/sensors/kxtj3.py"],
  ["/akadako/sensors/ltr303.py",    "akadako/sensors/ltr303.py"],
  ["/akadako/sensors/vl53l0x.py",   "akadako/sensors/vl53l0x.py"],
];

function post(type, payload) {
  self.postMessage({ type, ...payload });
}

// --- JS helpers exposed to Python via the `js` module -----------------------

// Drain whatever the main thread has queued; block up to timeoutMs when empty.
self.midiPump = function (timeoutMs) {
  let w = Atomics.load(ctrl, 0);
  if (w === readCount) {
    Atomics.wait(ctrl, 0, readCount, timeoutMs);   // sleeps unless notified
    w = Atomics.load(ctrl, 0);
  }
  if (w === readCount) return new Uint8Array(0);
  const n = w - readCount;
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = ring[(readCount + i) % RING];
  readCount = w;
  return out;
};

// Send outgoing MIDI bytes to the main thread (which owns the output port).
self.midiSend = function (bytes) {
  // bytes is a JS Array (converted from Python via to_js).
  post("midi-out", { data: Array.from(bytes) });
};

// Push a named value to the live monitor panel (sensor reads + monitor()).
self.dbgValue = function (name, value) {
  post("dbg", { name, value });
};

// Share server: the WebSocket/fetch live on the main thread; received values
// come back through shareSAB so Python can read them synchronously.
self.shareConnect = (url, groupId) => post("share-connect", { url, groupId });
self.shareSend = (url, body) => post("share-send", { url, body });
self.shareClose = () => post("share-close", {});
self.shareConnected = () => (shareCtrl ? Atomics.load(shareCtrl, 0) : 0);
self.shareSeq = () => (shareCtrl ? Atomics.load(shareCtrl, 1) : 0);
self.shareReadValues = () => {
  if (!shareCtrl) return "";
  const n = Atomics.load(shareCtrl, 2);
  return n ? new TextDecoder().decode(shareData.subarray(0, n)) : "";
};

// --- bootstrap --------------------------------------------------------------

async function boot(inSAB, irqSAB, shareSAB) {
  ctrl = new Int32Array(inSAB, 0, HDR / 4);
  ring = new Uint8Array(inSAB, HDR, RING);
  shareCtrl = new Int32Array(shareSAB, 0, 4);
  shareData = new Uint8Array(shareSAB, 16, shareSAB.byteLength - 16);

  pyodide = await loadPyodide({ indexURL: "/web/vendor/pyodide/" });
  pyodide.setInterruptBuffer(new Uint8Array(irqSAB));
  pyodide.setStdout({ batched: (s) => post("stdout", { text: s + "\n" }) });
  pyodide.setStderr({ batched: (s) => post("stderr", { text: s + "\n" }) });

  pyodide.FS.mkdirTree("/lib/akadako/sensors");
  for (const [url, fsRel] of PY_FILES) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`fetch ${url}: ${res.status}`);
    pyodide.FS.writeFile("/lib/" + fsRel, await res.text());
  }
  const bridge = await (await fetch("/web/pybridge.py")).text();
  pyodide.FS.writeFile("/lib/pybridge.py", bridge);

  pyodide.runPython(`
import sys
sys.path.insert(0, "/lib")
import pybridge
pybridge.install()
`);

  post("ready", {});
}

// --- run / interrupt-reset --------------------------------------------------

async function run(code) {
  try {
    await pyodide.runPythonAsync(code);
    post("done", {});
  } catch (err) {
    const msg = String((err && err.message) || err);
    // Stop button raises KeyboardInterrupt -- show a calm message, not a traceback.
    const interrupted = (err && err.type === "KeyboardInterrupt") || msg.includes("KeyboardInterrupt");
    if (interrupted) {
      post("stdout", { text: "■ 停止しました\n" });
    } else {
      post("stderr", { text: msg + "\n" });
      // Pull the user's line number from the traceback (user code runs as "<exec>").
      const hits = [...msg.matchAll(/File "<exec>", line (\d+)/g)];
      if (hits.length) post("error-line", { line: parseInt(hits[hits.length - 1][1], 10) });
    }
    post("done", {});
  }
}

self.onmessage = async (e) => {
  const msg = e.data;
  try {
    if (msg.type === "boot") {
      await boot(msg.inSAB, msg.irqSAB, msg.shareSAB);
    } else if (msg.type === "run") {
      await run(msg.code);
    } else if (msg.type === "probe") {
      try {
        const json = await pyodide.runPythonAsync("import pybridge; pybridge.probe_sensors()");
        post("probe-result", { available: JSON.parse(json) });
      } catch (err) {
        post("probe-result", { available: [], error: String(err.message || err) });
      }
    }
  } catch (err) {
    post("stderr", { text: "worker: " + String(err.message || err) + "\n" });
    post("done", {});
  }
};
