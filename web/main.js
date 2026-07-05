// Main thread: owns the Web MIDI port + UI (CodeMirror editor, console, live
// sensor monitor), and bridges MIDI <-> the Pyodide worker through two
// SharedArrayBuffers.

const RING = 65536;
const HDR = 16;
const PORT_FILTERS = ["STEAM BOX", "MidiDako", "AkaDako"];

const STARTER_CODE = `# AkaDako のプログラム
# 「Connect」→「Run ▶」（止めるときは「Stop ◼」）
# 「Sample」ボタンで、つないだボードに合わせた例を表示できます

from akadako import AkaDako
import time

# AkaDako に接続する
board = AkaDako.connect()
print("接続しました")

# ずっとくりかえす
while True:
    # ここにプログラムを書く（例: print(board.fetch_temperature())）

    time.sleep(1)   # 1秒まつ
`;

// --- shared buffers ---------------------------------------------------------
const inSAB = new SharedArrayBuffer(HDR + RING);
const irqSAB = new SharedArrayBuffer(1);
const ctrl = new Int32Array(inSAB, 0, HDR / 4);
const ring = new Uint8Array(inSAB, HDR, RING);
const irq = new Uint8Array(irqSAB);

const SHARE_HDR = 16, SHARE_CAP = 32768;           // 共有通信 (share server)
const shareSAB = new SharedArrayBuffer(SHARE_HDR + SHARE_CAP);
const shareCtrl = new Int32Array(shareSAB, 0, 4);  // [0]=connected [1]=seq [2]=len
const shareData = new Uint8Array(shareSAB, SHARE_HDR, SHARE_CAP);

const BP_MAX = 4096;                                // ブレークポイント行ビットマップの上限
const dbgSAB = new SharedArrayBuffer(16 + BP_MAX); // デバッガ制御 + bpビットマップ
const dbgCtrl = new Int32Array(dbgSAB, 0, 4);      // [0]=command (1=step,2=continue,3=stop)
const bpMap = new Uint8Array(dbgSAB, 16, BP_MAX);  // bpMap[行番号]=1 ならブレークポイント（実行中もライブ反映）

// --- DOM --------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const consoleEl = $("console");
const statusEl = $("status");
const runBtn = $("run");
const stopBtn = $("stop");
const connectBtn = $("connect");
const clearBtn = $("clear");
const watchlist = $("watchlist");

function log(text, cls) {
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text;
  consoleEl.appendChild(span);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function setStatus(text, ok) {
  statusEl.textContent = text;
  statusEl.className = ok ? "ok" : "warn";
}

// Surface silent failures: route uncaught errors to both consoles.
window.addEventListener("error", (e) => {
  console.error("[akadako] error:", e.error || e.message);
  log("JSエラー: " + (e.message || e.error) + "\n", "err");
});
window.addEventListener("unhandledrejection", (e) => {
  console.error("[akadako] unhandled rejection:", e.reason);
  log("Promiseエラー: " + e.reason + "\n", "err");
});

// --- editor (CodeMirror 5) --------------------------------------------------
const cm = CodeMirror.fromTextArea($("editor"), {
  value: STARTER_CODE,
  mode: "python",
  lineNumbers: true,
  gutters: ["CodeMirror-linenumbers", "breakpoints"],
  indentUnit: 4,
  tabSize: 4,
  matchBrackets: true,
  autoCloseBrackets: true,
  extraKeys: {
    "Ctrl-Enter": runCode,
    "Cmd-Enter": runCode,
    "Ctrl-Space": (cmi) => cmi.showHint({ hint: pyHint, completeSingle: false }),
    "Tab": (cmi) => cmi.replaceSelection("    "),
    "Ctrl-/": toggleComment,
    "Cmd-/": toggleComment,
  },
});
cm.setValue(STARTER_CODE);

// Toggle "# " comments on the selected lines (or the current line). If every
// non-blank target line is already commented, uncomment them; otherwise comment
// them all at the shallowest indentation so the block stays aligned.
function toggleComment(cmi) {
  cmi.operation(() => {
    for (const sel of cmi.listSelections()) {
      const from = sel.from(), to = sel.to();
      let endLine = to.line;
      // A selection ending at column 0 doesn't really include that last line.
      if (endLine > from.line && to.ch === 0) endLine--;

      const lines = [];
      for (let n = from.line; n <= endLine; n++) lines.push({ n, text: cmi.getLine(n) });
      const target = lines.filter((l) => l.text.trim() !== "");
      if (!target.length) continue;   // nothing but blank lines

      if (target.every((l) => /^\s*#/.test(l.text))) {
        for (const l of target) {
          const stripped = l.text.replace(/^(\s*)#\s?/, "$1");
          cmi.replaceRange(stripped, { line: l.n, ch: 0 }, { line: l.n, ch: l.text.length });
        }
      } else {
        const indent = Math.min(...target.map((l) => l.text.match(/^\s*/)[0].length));
        for (const l of target) cmi.replaceRange("# ", { line: l.n, ch: indent });
      }
    }
  });
}

// Track whether the user has hand-edited the editor. We only auto-replace the
// starter with a board-tailored version while it is still untouched.
let editorPristine = true;
let draftTimer = null;
cm.on("change", (_cmi, change) => {
  if (change.origin !== "setValue") {
    editorPristine = false;
    // 手で編集したらドラフトを自動保存（連打を抑えるため少し遅延）
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 400);
  }
});
// うっかり閉じた直前にも最新状態を確実に書き出す（localStorage は同期）
window.addEventListener("beforeunload", saveDraft);

// Breakpoints: click the gutter to toggle a red dot on a line.
const breakpoints = new Set();   // 1-based line numbers
cm.on("gutterClick", (cmi, n) => {
  const info = cmi.lineInfo(n);
  const line = n + 1;
  if (info.gutterMarkers && info.gutterMarkers.breakpoints) {
    cmi.setGutterMarker(n, "breakpoints", null);
    breakpoints.delete(line);
    if (line < BP_MAX) Atomics.store(bpMap, line, 0);   // 実行中でも即反映
  } else {
    const dot = document.createElement("div");
    dot.className = "cm-bp";
    dot.textContent = "●";
    cmi.setGutterMarker(n, "breakpoints", dot);
    breakpoints.add(line);
    if (line < BP_MAX) Atomics.store(bpMap, line, 1);   // 実行中でも即反映
  }
});

// Board-tailored starter generation. Order = priority; one line per label.
const CODEGEN = [
  ["fetch_temperature", "温度", "board.fetch_temperature()", "℃"],
  ["fetch_humidity", "湿度", "board.fetch_humidity()", "％"],
  ["fetch_pressure", "気圧", "board.fetch_pressure()", "hPa"],
  ["fetch_brightness", "明るさ", "board.fetch_brightness()", ""],
  ["analog_brightness", "明るさ", "board.analog_brightness()", ""],
  ["fetch_optical_distance", "距離", "board.fetch_optical_distance()", "cm"],
  ["fetch_acceleration_x", "加速度X", "board.fetch_acceleration_x()", ""],
  ["motion_sensor", "人感センサー", "board.motion_sensor()", ""],
  ["analog_a1", "アナログA1", "board.analog_a1()", ""],
  ["digital_a1", "デジタルA1", "board.digital_a1()", ""],
];

function generateStarter(available) {
  const set = new Set(available);
  const lines = [];
  const usedLabels = new Set();
  for (const [m, label, expr, unit] of CODEGEN) {
    if (!set.has(m) || usedLabels.has(label)) continue;
    usedLabels.add(label);
    const u = unit ? `, "${unit}"` : "";
    lines.push(`    print("${label}:", ${expr}${u})`);
    if (lines.length >= 5) break;   // 初学者向けに行数を抑える
  }
  const body = lines.length ? lines.join("\n") : '    print("バージョン:", board.version)';
  return `# このボードに搭載されているセンサーに合わせて自動生成しました
# 「Run ▶」で動かし、「Stop ◼」で止めます
from akadako import AkaDako
import time

board = AkaDako.connect()
print("接続しました")

while True:
${body}
    time.sleep(1)
`;
}

// Python keywords + common builtins for general-purpose completion.
const PY_KEYWORDS = [
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "class", "continue", "def", "del", "elif", "else", "except", "finally",
  "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
  "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
];
const PY_BUILTINS = [
  "print", "input", "range", "len", "int", "float", "str", "bool", "list",
  "dict", "tuple", "set", "abs", "min", "max", "sum", "round", "sorted",
  "reversed", "enumerate", "zip", "map", "filter", "type", "isinstance",
  "open", "format", "any", "all", "chr", "ord",
];

// Collect identifiers already present in the document (for word completion).
function collectWords(cmi) {
  const words = new Set();
  const re = /[A-Za-z_]\w+/g;
  let m;
  const text = cmi.getValue();
  while ((m = re.exec(text))) words.add(m[0]);
  return words;
}

// Completion: board./AkaDako. -> API catalogue; otherwise Python keywords,
// builtins, and words already typed in the document.
function pyHint(cmi) {
  const cur = cmi.getCursor();
  const line = cmi.getLine(cur.line).slice(0, cur.ch);

  // member completion (board.xxx / AkaDako.xxx)
  const mem = line.match(/([A-Za-z_][\w.]*)\.(\w*)$/);
  if (mem) {
    const obj = mem[1], prefix = mem[2];
    let pool = null;
    if (obj === "AkaDako") pool = window.AKADAKO_STATICS;
    else if (obj === "board" || obj.endsWith("board")) pool = window.AKADAKO_BOARD_API;
    if (pool) {
      const list = pool
        .filter((c) => c.name.toLowerCase().startsWith(prefix.toLowerCase()))
        .map((c) => ({
          text: c.insert,
          displayText: c.sig,
          render(el) {
            el.innerHTML =
              '<span style="color:#6f42c1">' + c.sig + "</span>" +
              '<span style="color:#888; margin-left:.6em">' + c.doc + "</span>";
          },
        }));
      return list.length
        ? { list, from: CodeMirror.Pos(cur.line, cur.ch - prefix.length), to: cur }
        : null;
    }
    // unknown object before the dot -> fall through to general word completion
  }

  // general completion on the current word
  const wm = line.match(/[A-Za-z_]\w*$/);
  const word = wm ? wm[0] : "";
  const lower = word.toLowerCase();
  const seen = new Set();
  const list = [];
  const add = (text, color) => {
    if (text === word || seen.has(text)) return;
    if (word && !text.toLowerCase().startsWith(lower)) return;
    seen.add(text);
    list.push(color
      ? { text, render(el) { el.innerHTML = '<span style="color:' + color + '">' + text + "</span>"; } }
      : { text });
  };
  PY_KEYWORDS.forEach((k) => add(k, "#a626a4"));
  PY_BUILTINS.forEach((b) => add(b, "#b26a00"));
  collectWords(cmi).forEach((w) => add(w));
  if (!list.length) return null;
  return {
    list: list.slice(0, 60),
    from: CodeMirror.Pos(cur.line, cur.ch - word.length),
    to: cur,
  };
}

// Pop completion automatically while typing (not inside strings/comments).
cm.on("inputRead", (cmi, change) => {
  const t = change.text[0];
  if (!t) return;
  const cur = cmi.getCursor();
  const tok = cmi.getTokenAt(cur);
  if (tok.type === "string" || tok.type === "comment") return;
  const line = cmi.getLine(cur.line).slice(0, cur.ch);
  if (t === ".") {
    if (/([A-Za-z_][\w.]*)\.$/.test(line)) cmi.showHint({ hint: pyHint, completeSingle: false });
    return;
  }
  if (/[A-Za-z_]/.test(t)) {
    const memberCtx = /([A-Za-z_][\w.]*)\.(\w*)$/.test(line);
    const wm = line.match(/[A-Za-z_]\w*$/);
    if (memberCtx || (wm && wm[0].length >= 2)) {
      cmi.showHint({ hint: pyHint, completeSingle: false });
    }
  }
});

// --- live monitor panel -----------------------------------------------------
const watches = new Map(); // name -> {values, valEl, ctx, canvas}

function clearWatches() {
  watches.clear();
  watchlist.innerHTML = "";
}

function fmtVal(v) {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "boolean") return v ? "True" : "False";
  return String(v);
}

function ensureWatch(name) {
  let w = watches.get(name);
  if (w) return w;
  const row = document.createElement("div");
  row.className = "watch";
  const nameEl = document.createElement("span");
  nameEl.className = "name";
  nameEl.textContent = (window.AKADAKO_LABELS && window.AKADAKO_LABELS[name]) || name;
  nameEl.title = name;   // 元のメソッド名はツールチップで確認できる
  const valEl = document.createElement("span");
  valEl.className = "val";
  const canvas = document.createElement("canvas");
  canvas.width = 280;
  canvas.height = 26;
  row.append(nameEl, valEl, canvas);
  watchlist.append(row);
  w = { values: [], valEl, canvas, ctx: canvas.getContext("2d") };
  watches.set(name, w);
  return w;
}

function drawSpark(w) {
  const { ctx, canvas, values } = w;
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  if (values.length < 2) return;
  let min = Math.min(...values), max = Math.max(...values);
  if (max === min) { max += 1; min -= 1; }
  ctx.strokeStyle = "#1565c0";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (i / (values.length - 1)) * (W - 2) + 1;
    const y = H - 2 - ((v - min) / (max - min)) * (H - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}

function onDbg(name, value) {
  const w = ensureWatch(name);
  w.valEl.textContent = fmtVal(value);
  let num = null;
  if (typeof value === "number") num = value;
  else if (typeof value === "boolean") num = value ? 1 : 0;
  if (num !== null) {
    w.values.push(num);
    if (w.values.length > 80) w.values.shift();
    drawSpark(w);
  }
}

// --- MIDI -------------------------------------------------------------------
let midiOut = null;

function feedIncoming(bytes) {
  const w = Atomics.load(ctrl, 0);
  for (let i = 0; i < bytes.length; i++) ring[(w + i) % RING] = bytes[i];
  Atomics.store(ctrl, 0, w + bytes.length);
  Atomics.notify(ctrl, 0);
}

const matchPort = (name) => PORT_FILTERS.some((f) => (name || "").includes(f));

async function connectMidi() {
  console.log("[akadako] Connect clicked; requestMIDIAccess =", typeof navigator.requestMIDIAccess);
  log("接続中…（Web MIDI の使用許可を要求しています）\n", "muted");
  if (!navigator.requestMIDIAccess) {
    setStatus("このブラウザは Web MIDI 非対応です（Chrome / Edge を使ってください）", false);
    log("Web MIDI が利用できません（navigator.requestMIDIAccess が未定義）\n", "err");
    return;
  }
  let access;
  try {
    access = await navigator.requestMIDIAccess({ sysex: true });
  } catch (err) {
    setStatus("MIDI の使用許可が拒否されました: " + err, false);
    log("MIDI アクセス要求が拒否されました: " + err + "\n", "err");
    return;
  }
  const ins = [...access.inputs.values()].map((i) => i.name);
  const outs = [...access.outputs.values()].map((o) => o.name);
  console.log("[akadako] MIDI inputs:", ins, "outputs:", outs);
  log(`MIDI 入力=[${ins}] 出力=[${outs}]\n`, "muted");

  let input = null, output = null;
  for (const i of access.inputs.values()) if (matchPort(i.name)) { input = i; break; }
  for (const o of access.outputs.values()) if (matchPort(o.name)) { output = o; break; }
  if (!input || !output) {
    setStatus(`AkaDako が見つかりません（入力=${ins.length}, 出力=${outs.length}）— コンソール参照`, false);
    return;
  }
  input.onmidimessage = (e) => feedIncoming(e.data);
  midiOut = output;
  setStatus("接続済み", true);
  log(`接続しました: 入力="${input.name}" 出力="${output.name}"\n`, "muted");
}

// --- share server (共有通信): WebSocket + fetch live here on the main thread --
let shareWS = null;
let shareWanted = false;
let shareUrl = "";
let shareGroupId = "";
const shareValues = {};
const shareEnc = new TextEncoder();

function shareUpdate() {
  const bytes = shareEnc.encode(JSON.stringify(shareValues));
  const n = Math.min(bytes.length, SHARE_CAP);
  shareData.set(bytes.subarray(0, n));
  Atomics.store(shareCtrl, 2, n);
  Atomics.add(shareCtrl, 1, 1);          // bump seq so the worker re-parses
}

function openShare(url, groupId, announce) {
  shareUrl = url;
  if (groupId !== undefined) shareGroupId = groupId;
  shareWanted = true;
  try { if (shareWS) shareWS.close(); } catch {}
  let ws;
  try { ws = new WebSocket(url); }
  catch (e) { log("通信：接続できません (" + e + ")\n", "err"); return; }
  shareWS = ws;
  ws.onopen = () => {
    Atomics.store(shareCtrl, 0, 1);
    if (announce) log("通信：" + shareGroupId + "に接続\n", "muted");   // 再接続では出さない
  };
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m && m.key !== undefined) {
        shareValues[m.key] = m.value;
        shareUpdate();
        log("通信：" + m.key + "=" + m.value + "を受信\n", "muted");
      }
    } catch {}
  };
  ws.onclose = () => {
    Atomics.store(shareCtrl, 0, 0);
    if (shareWanted) setTimeout(() => { if (shareWanted) openShare(shareUrl, shareGroupId, false); }, 1000);
  };
  ws.onerror = () => { Atomics.store(shareCtrl, 0, 0); };
}

// --- worker -----------------------------------------------------------------
const worker = new Worker("/web/worker.js");
let running = false;
let workerReady = false;
let probing = false;

worker.onmessage = (e) => {
  const m = e.data;
  switch (m.type) {
    case "ready":
      workerReady = true;
      runBtn.disabled = false;
      debugBtn.disabled = false;
      setStatus("Pyodide 準備完了 —「Connect」でボードに接続してから「Run ▶」", false);
      break;
    case "stdout": log(m.text); break;
    case "stderr": log(m.text, "err"); break;
    case "error-line": markErrorLine(m.line); break;
    case "dbg-pause": onDebugPause(m.line, m.vars); break;
    case "dbg": onDbg(m.name, m.value); break;
    case "midi-out": if (midiOut) midiOut.send(m.data); break;
    case "share-connect": openShare(m.url, m.groupId, true); break;
    case "share-send":
      try { const b = JSON.parse(m.body); log("通信：" + b.key + "=" + b.value + "を送信\n", "muted"); } catch {}
      fetch(m.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: m.body })
        .catch(() => {});
      break;
    case "share-close":
      shareWanted = false;
      try { if (shareWS) shareWS.close(); } catch {}
      Atomics.store(shareCtrl, 0, 0);
      break;
    case "probe-result": {
      probing = false;
      runBtn.disabled = false;
      const avail = m.available || [];
      cm.setValue(generateStarter(avail));
      editorPristine = true;   // 生成直後は未編集あつかい
      currentName = "";
      log("センサー検出: " + (avail.join(", ") || "なし") + " → サンプルコードを表示しました\n", "muted");
      setStatus("接続済み", true);
      break;
    }
    case "done":
      running = false;
      runBtn.disabled = false;
      debugBtn.disabled = false;
      stopBtn.disabled = true;
      endDebug();
      break;
  }
};

// On iPad-non-Scrub we redirect to Scrub (see index.html); don't download
// Pyodide (~13MB) behind the overlay.
if (!window.__scrubRedirect) {
  worker.postMessage({ type: "boot", inSAB, irqSAB, shareSAB, dbgSAB });
  // Cache the runtime so subsequent loads are instant / offline (GIGA wifi).
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

// --- controls ---------------------------------------------------------------
let errLine = null;
function clearErrorLine() {
  if (errLine != null) { cm.removeLineClass(errLine, "background", "cm-errorline"); errLine = null; }
}
function markErrorLine(n) {
  clearErrorLine();
  const ln = n - 1;
  if (ln < 0 || ln >= cm.lineCount()) return;
  cm.addLineClass(ln, "background", "cm-errorline");
  errLine = ln;
  cm.scrollIntoView({ line: ln, ch: 0 }, 80);
}

// --- ステップ実行 / ブレークポイント -----------------------------------------
let debugging = false;
let dbgLine = null;
const debugBtn = $("debug");
const debugbar = $("debugbar");
const dbgStatus = $("dbg-status");
const dbgVars = $("dbg-vars");

function clearDebugLine() {
  if (dbgLine != null) { cm.removeLineClass(dbgLine, "background", "cm-debugline"); dbgLine = null; }
}
function markDebugLine(n) {
  clearDebugLine();
  const ln = n - 1;
  if (ln < 0 || ln >= cm.lineCount()) return;
  cm.addLineClass(ln, "background", "cm-debugline");
  dbgLine = ln;
  cm.scrollIntoView({ line: ln, ch: 0 }, 80);
}
function setDbgButtons(enabled) {
  $("dbg-step").disabled = !enabled;
  $("dbg-continue").disabled = !enabled;
}
function onDebugPause(line, varsJson) {
  markDebugLine(line);
  dbgStatus.textContent = "⏸ " + line + " 行目で停止";
  let parts = [];
  try { parts = JSON.parse(varsJson).map((kv) => kv[0] + "=" + kv[1]); } catch (e) {}
  dbgVars.textContent = parts.length ? parts.join("   ") : "(変数なし)";
  debugbar.hidden = false;
  setDbgButtons(true);
}
function sendDbg(cmd) {
  setDbgButtons(false);
  clearDebugLine();
  dbgStatus.textContent = "実行中…";
  Atomics.store(dbgCtrl, 0, cmd);
  Atomics.notify(dbgCtrl, 0);
}
function endDebug() {
  debugging = false;
  debugbar.hidden = true;
  clearDebugLine();
}
function debugRun() {
  if (running) return;
  running = true;
  debugging = true;
  runBtn.disabled = true;
  debugBtn.disabled = true;
  stopBtn.disabled = false;
  irq[0] = 0;
  clearWatches();
  clearErrorLine();
  log("\n🐞 デバッグ実行（行番号の左をクリックでブレークポイント設定）\n", "muted");
  worker.postMessage({ type: "run-debug", code: cm.getValue(), step: true });
}
debugBtn.addEventListener("click", debugRun);
$("dbg-step").addEventListener("click", () => sendDbg(1));
$("dbg-continue").addEventListener("click", () => sendDbg(2));
$("dbg-stop").addEventListener("click", () => { irq[0] = 2; sendDbg(3); });

function runCode() {
  if (running) return;
  running = true;
  runBtn.disabled = true;
  debugBtn.disabled = true;
  stopBtn.disabled = false;
  irq[0] = 0;
  clearWatches();
  clearErrorLine();
  log("\n▶ 実行開始\n", "muted");
  worker.postMessage({ type: "run", code: cm.getValue() });
}

function stopCode() {
  if (debugging) { irq[0] = 2; sendDbg(3); return; }  // wake a paused debugger too
  irq[0] = 2;                  // SIGINT -> KeyboardInterrupt in Pyodide
  Atomics.notify(ctrl, 0);     // wake the pump so it returns to Python
}

runBtn.addEventListener("click", runCode);
stopBtn.addEventListener("click", stopCode);
connectBtn.addEventListener("click", connectMidi);
clearBtn.addEventListener("click", () => (consoleEl.textContent = ""));

// --- プログラムの保存 / 読み込み (localStorage) ----------------------------
const STORE_KEY = "akadako_programs";
let currentName = "";

const modal = $("modal");
const modalTitle = $("modal-title");
const modalBody = $("modal-body");
const modalActions = $("modal-actions");

function loadStore() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); }
  catch { return {}; }
}
function saveStore(obj) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(obj)); return true; }
  catch (e) { setStatus("保存できませんでした: " + e, false); return false; }
}
function defaultName() {
  const s = loadStore();
  let i = 1;
  while (s["プログラム" + i]) i++;
  return "プログラム" + i;
}

// --- ドラフト自動保存（うっかり閉じても続きから再開できるように）--------------
// 名前付き保存(akadako_programs)とは別に、編集中の状態を1つだけ保持する。
const DRAFT_KEY = "akadako_draft";
function saveDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      code: cm.getValue(),
      name: currentName,
      edited: !editorPristine,   // 自動生成や初期表示のままなら復元を促さない
      ts: Date.now(),
    }));
  } catch {}
}
function loadDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); }
  catch { return null; }
}
function clearDraft() { try { localStorage.removeItem(DRAFT_KEY); } catch {} }

function showModal(title, contentNode, actions) {
  modalTitle.textContent = title;
  modalBody.innerHTML = "";
  modalBody.append(contentNode);
  modalActions.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.textContent = a.label;
    if (a.primary) b.classList.add("primary");
    b.addEventListener("click", a.onClick);
    modalActions.append(b);
  }
  modal.hidden = false;
}
function closeModal() { modal.hidden = true; }
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

function openSaveDialog() {
  const wrap = document.createElement("div");
  const label = document.createElement("div");
  label.textContent = "プログラムの名前:";
  label.style.marginBottom = ".4rem";
  const input = document.createElement("input");
  input.value = currentName || defaultName();
  wrap.append(label, input);

  const doSave = () => {
    const n = input.value.trim();
    if (!n) { input.focus(); return; }
    const s = loadStore();
    s[n] = cm.getValue();
    if (!saveStore(s)) return;
    currentName = n;
    editorPristine = false;   // 保存したコードは自動生成で上書きしない
    saveDraft();              // ドラフトにも名前を反映しておく
    closeModal();
    setStatus("保存しました: " + n, true);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSave(); }
  });
  showModal("プログラムを保存", wrap, [
    { label: "保存", primary: true, onClick: doSave },
    { label: "キャンセル", onClick: closeModal },
  ]);
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

function openOpenDialog() {
  const names = Object.keys(loadStore()).sort();
  const list = document.createElement("div");
  if (!names.length) {
    list.textContent = "保存されたプログラムはありません。";
    showModal("開く", list, [{ label: "閉じる", onClick: closeModal }]);
    return;
  }
  for (const name of names) {
    const row = document.createElement("div");
    row.className = "prog-row";
    const nm = document.createElement("span");
    nm.className = "pname";
    nm.textContent = name;
    const openB = document.createElement("button");
    openB.textContent = "開く";
    openB.addEventListener("click", () => {
      const code = loadStore()[name];
      if (code === undefined) return;
      cm.setValue(code);
      currentName = name;
      editorPristine = false;   // 読み込んだコードは自動生成で上書きしない
      closeModal();
      setStatus("読み込みました: " + name, true);
    });
    const delB = document.createElement("button");
    delB.textContent = "削除";
    delB.addEventListener("click", () => confirmDelete(name));
    row.append(nm, openB, delB);
    list.append(row);
  }
  showModal("開く", list, [{ label: "閉じる", onClick: closeModal }]);
}

function confirmDelete(name) {
  const msg = document.createElement("div");
  msg.textContent = "「" + name + "」を削除しますか？";
  showModal("削除の確認", msg, [
    { label: "削除する", primary: true, onClick: () => {
        const s = loadStore();
        delete s[name];
        saveStore(s);
        openOpenDialog();   // 一覧を開き直す
      } },
    { label: "やめる", onClick: openOpenDialog },
  ]);
}

$("save").addEventListener("click", openSaveDialog);
$("open").addEventListener("click", openOpenDialog);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeModal();
});

// --- バイブコーディング（生成AIでPythonプログラムを作る） ---------------------
// xcx-g2s の「生成AI」ブロックと同じエンドポイントを使う。
// 認証は 699.jp のアクセスコード Cookie（credentials: include で送信）。
// py.699.jp と xcratch.699.jp は同一サイトなのでサードパーティ Cookie の制約を受けない。
const GENERATIVE_AI_URL = "https://xcratch.699.jp/agai/ai";

let lastVibePrompt = "";

// 生成AIに渡す指示文。ユーザーの要望を「1つの完結したPythonプログラム」に仕立てさせる。
// currentCode を渡すと「今のコードを修正」、null なら「ゼロから作成」のプロンプトになる。
function buildVibePrompt(userRequest, currentCode) {
  const api = (window.AKADAKO_BOARD_API || [])
    .map((a) => "  board." + a.sig + " — " + a.doc)
    .join("\n");
  const head = currentCode
    ? [
        "あなたは AkaDako Python Editor のためのコード生成アシスタントです。",
        "下記の「現在のPythonコード」を、ユーザーの要望に沿って修正した「1つの完結したPythonプログラム」を返してください。",
        "既存の処理や構造はできるだけ活かし、要望に関係する部分だけを変更してください。",
      ]
    : [
        "あなたは AkaDako Python Editor のためのコード生成アシスタントです。",
        "ユーザーの要望に合う「1つの完結したPythonプログラム」を新しく作成してください。",
      ];
  const lines = [
    ...head,
    "",
    "# 制約",
    "- ブラウザ内の Pyodide で実行される。使えるのは Python 標準ライブラリと akadako モジュールのみ。",
    "- AkaDako（センサーやLED等）を使う場合は `from akadako import AkaDako` を書き、",
    "  `board = AkaDako.connect()` で接続してから board のメソッドを使う。",
    "- くりかえし読むときは while True と time.sleep() を使う（sleep なしの busy loop にしない）。",
    "- monitor(\"名前\", 値) を呼ぶと、右のモニターパネルに値をリアルタイム表示できる。",
    "- コメントは日本語で、初学者にも分かりやすい簡潔なコードにする。",
    "- 出力は完成したPythonコードのみ。説明文やマークダウンのコードフェンス(```)は付けない。",
    "",
    "# 使える主なAPI（AkaDako.connect() が返す board のメソッド）",
    api,
    "AkaDako.Color.RED などの色定数、AkaDako.ColorLed.OnBoard などの接続先定数も使える。",
  ];
  if (currentCode) {
    lines.push("", "# 現在のPythonコード", "```python", currentCode, "```");
  }
  lines.push("", "# ユーザーの要望", userRequest);
  return lines.join("\n");
}

// 応答テキストからコード本体を取り出す（マークダウンのコードフェンスがあれば剥がす）。
function extractCode(text) {
  if (!text) return "";
  const fence = text.match(/```(?:python|py)?\s*([\s\S]*?)```/i);
  return (fence ? fence[1] : text).trim();
}

async function callGenerativeAI(promptText) {
  // エンドポイントは board.version を要求する。Python 版はボードがワーカー側に
  // あって手軽に取れないため、既定値を送る。
  const res = await fetch(GENERATIVE_AI_URL, {
    mode: "cors",
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content: [{ text: promptText }],
      board: { version: "2.0.0" },
      locale: "ja",
    }),
  });
  return res.json(); // { content: string|null, error?: string|object }
}

// prefill を渡すと入力欄に復元する（エラー後の「入力に戻る」でのみ使用）。
// 通常のボタンからの起動は空欄で開き、前回のプロンプトは表示しない。
function openVibeDialog(prefill) {
  const wrap = document.createElement("div");

  // モード切り替え（今のコードを修正 / ゼロから作る）。
  // 初期値は編集状態から自動選択: 未編集(スターター/生成直後)ならゼロから、手を加えていれば修正。
  let mode = editorPristine ? "new" : "modify";
  const seg = document.createElement("div");
  seg.className = "seg";
  const btnModify = document.createElement("button");
  btnModify.textContent = "今のコードを修正";
  const btnNew = document.createElement("button");
  btnNew.textContent = "ゼロから作る";
  seg.append(btnModify, btnNew);

  const label = document.createElement("div");
  label.style.marginBottom = ".4rem";
  const ta = document.createElement("textarea");
  ta.rows = 5;
  ta.value = typeof prefill === "string" ? prefill : "";   // 通常は空欄で開く
  const hint = document.createElement("div");
  hint.style.cssText = "font-size:.85rem;color:var(--muted);margin-top:.5rem;line-height:1.6;";

  function renderMode() {
    btnModify.classList.toggle("active", mode === "modify");
    btnNew.classList.toggle("active", mode === "new");
    if (mode === "modify") {
      label.textContent = "今のコードをどう直したいか、日本語で説明してください:";
      ta.placeholder = "例: 温度が30度をこえたら警告を表示して";
      hint.textContent = "エディタの現在のコードを送り、要望に沿って修正します。コードが大きいと送信できないことがあります。Ctrl/⌘+Enter でも実行できます。";
    } else {
      label.textContent = "作りたいものを日本語で説明してください:";
      ta.placeholder = "例: 温度と湿度を1秒ごとに表示して、暑いときは警告を出す";
      hint.textContent = "ゼロから新しいPythonプログラムを作ります（今の内容は置き換わります）。Ctrl/⌘+Enter でも実行できます。";
    }
  }
  btnModify.addEventListener("click", () => { mode = "modify"; renderMode(); ta.focus(); });
  btnNew.addEventListener("click", () => { mode = "new"; renderMode(); ta.focus(); });
  renderMode();

  wrap.append(seg, label, ta, hint);

  const doGen = () => {
    const p = ta.value.trim();
    if (!p) { ta.focus(); return; }
    lastVibePrompt = p;
    runVibeGeneration(p, mode);
  };
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doGen(); }
  });
  showModal("バイブコーディング ✨", wrap, [
    { label: "生成 ✨", primary: true, onClick: doGen },
    { label: "キャンセル", onClick: closeModal },
  ]);
  setTimeout(() => ta.focus(), 0);
}

async function runVibeGeneration(promptText, mode) {
  const currentCode = mode === "modify" ? cm.getValue() : null;
  const node = document.createElement("div");
  node.style.cssText = "display:flex;align-items:center;gap:.7rem;";
  const spin = document.createElement("span");
  spin.className = "vibe-spinner";
  const txt = document.createElement("span");
  txt.textContent = "生成中です…（10〜30秒ほどかかります）";
  node.append(spin, txt);
  showModal("バイブコーディング ✨", node, []); // 生成中はボタンなし

  try {
    const data = await callGenerativeAI(buildVibePrompt(promptText, currentCode));
    if (data && typeof data.content === "string" && data.content.trim() !== "") {
      const code = extractCode(data.content);
      closeModal();
      applyVibeResult(code, mode);
      return;
    }
    // エラー処理（xcx-g2s の生成AIブロックと同じ分岐）
    let errHtml = null, errText = null;
    if (data && data.error) {
      if (typeof data.error === "string") {
        errText = data.error;
      } else if (typeof data.error === "object") {
        if (data.error.type === "text/html") errHtml = data.error.content;
        else if (typeof data.error.content === "string") errText = data.error.content;
      }
    }
    if (!errHtml && !errText) {
      errText = "空の応答が返りました。要望を具体的にして、もう一度お試しください。";
    }
    showVibeError(errHtml, errText);
  } catch (e) {
    showVibeError(null, "生成AIに接続できませんでした: " + ((e && e.message) || e));
  }
}

function showVibeError(html, text) {
  const node = document.createElement("div");
  node.className = "vibe-errbox";
  if (html) node.innerHTML = html;          // 699.jp のエラー（アクセスコード案内リンク等）
  else node.textContent = text || "生成に失敗しました。";
  showModal("バイブコーディング ✨", node, [
    // エラー時だけは、打ち直さずに済むよう直前の入力を復元する
    { label: "入力に戻る", primary: true, onClick: () => openVibeDialog(lastVibePrompt) },
    { label: "閉じる", onClick: closeModal },
  ]);
}

// 生成結果をエディタへ。
// - modify: 現在コードを直した版なので確認なしで反映（ユーザーが修正を明示）。
// - new: 未保存の編集があるときだけ置き換え確認する（サンプルと同じ作法）。
function applyVibeResult(code, mode) {
  const put = () => {
    cm.setValue(code);
    editorPristine = true;   // 生成直後は未編集あつかい
    currentName = "";
    setStatus("バイブコーディングでコードを生成しました —「Run ▶」で実行できます", true);
    log("バイブコーディングでコードを生成しました。\n", "muted");
  };
  if (mode === "new" && !editorPristine) {
    const msg = document.createElement("div");
    msg.textContent = "生成したコードで今の内容を置き換えます。よろしいですか？（保存していない変更は消えます）";
    showModal("確認", msg, [
      { label: "置き換える", primary: true, onClick: () => { closeModal(); put(); } },
      { label: "やめる", onClick: closeModal },
    ]);
    return;
  }
  put();
}

$("vibe").addEventListener("click", () => openVibeDialog());   // 空欄で開く（前回のプロンプトは表示しない）

// --- 右パネルのタブ（センサー / リファレンス）。リファレンスは静的データ ------
const refview = $("refview");
const tabSensor = $("tab-sensor");
const tabRef = $("tab-ref");
let refBuilt = false;

function refItem(sig, doc) {
  const d = document.createElement("div");
  d.className = "ritem";
  const s = document.createElement("code");
  s.className = "rsig";
  s.textContent = sig;
  const p = document.createElement("span");
  p.className = "rdoc";
  p.textContent = doc;
  d.append(s, p);
  return d;
}

function buildReference() {
  const api = window.AKADAKO_BOARD_API || [];
  const statics = window.AKADAKO_STATICS || [];
  const findS = (n) => statics.find((a) => a.name === n);
  const findB = (n) => api.find((a) => a.name === n);

  const frag = document.createDocumentFragment();
  const note = document.createElement("div");
  note.className = "rnote";
  note.textContent = "board. / AkaDako. のあとに入力すると候補が出ます。";
  frag.append(note);

  function section(title, rows) {
    const valid = rows.filter(Boolean);
    if (!valid.length) return;
    const h = document.createElement("h4");
    h.textContent = title;
    frag.append(h);
    for (const [sig, doc] of valid) frag.append(refItem(sig, doc));
  }

  const connect = findS("connect");
  const disc = findB("disconnect");
  const isc = findB("is_connected");
  section("接続", [
    connect && ["AkaDako." + connect.sig, connect.doc + "（最初に必ず実行）"],
    disc && ["board." + disc.sig, disc.doc],
    isc && ["board." + isc.sig, isc.doc],
  ]);

  const used = new Set(["disconnect", "is_connected"]);
  const groups = [
    ["センサー（取得）", (a) => a.name.startsWith("fetch_") &&
      !["fetch_version", "fetch_uid", "fetch_i2c_read"].includes(a.name)],
    ["入力", (a) => a.name.startsWith("analog_") || a.name.startsWith("digital_") || a.name === "motion_sensor"],
    ["通信（共有）", (a) => a.name.startsWith("run_share_") || a.name === "shared_data" || a.name === "is_share_server_connected"],
    ["出力・動作", (a) => a.name.startsWith("run_")],
    ["その他", () => true],
  ];
  for (const [title, pred] of groups) {
    const items = api.filter((a) => !used.has(a.name) && pred(a));
    items.forEach((a) => used.add(a.name));
    section(title, items.map((a) => ["board." + a.sig, a.doc]));
  }

  // 定数（connect 以外の static）
  section("定数 (AkaDako.◯◯)",
    statics.filter((a) => a.name !== "connect").map((a) => ["AkaDako." + a.sig, a.doc]));

  refview.innerHTML = "";
  refview.append(frag);
}

function showTab(which) {
  const ref = which === "ref";
  if (ref && !refBuilt) { buildReference(); refBuilt = true; }
  watchlist.hidden = ref;
  refview.hidden = !ref;
  tabSensor.classList.toggle("active", !ref);
  tabRef.classList.toggle("active", ref);
}
tabSensor.addEventListener("click", () => showTab("sensor"));
tabRef.addEventListener("click", () => showTab("ref"));

// --- 「サンプル」: 接続中のボードを調べて、合ったサンプルコードに差し替える ----
function startProbe() {
  probing = true;
  runBtn.disabled = true;
  setStatus("センサーを調べています…（数秒かかります）", false);
  worker.postMessage({ type: "probe" });   // 結果は probe-result で受ける
}

function onSampleClick() {
  if (!midiOut) {
    setStatus("先に「Connect」でボードに接続してください", false);
    log("「Sample」はボードに接続してから使えます。\n", "err");
    return;
  }
  if (!workerReady) { setStatus("Pyodide の準備中です…少し待ってください", false); return; }
  if (probing || running) return;
  if (!editorPristine) {
    const msg = document.createElement("div");
    msg.textContent = "今のコードをサンプルに置き換えます。よろしいですか？（保存していない変更は消えます）";
    showModal("確認", msg, [
      { label: "置き換える", primary: true, onClick: () => { closeModal(); startProbe(); } },
      { label: "やめる", onClick: closeModal },
    ]);
    return;
  }
  startProbe();
}
$("sample").addEventListener("click", onSampleClick);

// 再アクセス時：前回編集していたコードが残っていれば「続きから / 新規」を選ばせる。
(function maybeRestoreDraft() {
  const d = loadDraft();
  if (!d || !d.edited || !d.code || !d.code.trim()) return;   // 編集実績がある時だけ
  const msg = document.createElement("div");
  msg.textContent =
    "前回編集していたコード" + (d.name ? "「" + d.name + "」" : "") +
    "が残っています。続きから編集しますか？";
  showModal("おかえりなさい", msg, [
    {
      label: "続きから", primary: true, onClick: () => {
        cm.setValue(d.code);
        currentName = d.name || "";
        editorPristine = false;   // 復元したコードは自動生成で上書きしない
        closeModal();
        setStatus("前回のコードを復元しました", true);
      },
    },
    {
      label: "新規で始める", onClick: () => {
        clearDraft();
        editorPristine = true;
        closeModal();
      },
    },
  ]);
})();

if (!crossOriginIsolated) {
  setStatus("クロスオリジン分離が無効です — serve.py 経由で開いてください（SharedArrayBuffer に必要）", false);
}

console.log("[akadako] main.js loaded; crossOriginIsolated =", crossOriginIsolated);
log("準備完了。crossOriginIsolated=" + crossOriginIsolated + "\n", "muted");
