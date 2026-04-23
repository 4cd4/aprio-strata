"""Minimal streaming UI for MinerU document parsing.

Run:  source .venv/bin/activate && uvicorn app:app --reload --port 8765
Open: http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
INPUTS = BASE / "inputs"
OUTPUTS = BASE / "outputs"
RESULTS = BASE / "results"
INPUTS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

SORTED_DIR = BASE / "sorted"
SORTED_DIR.mkdir(exist_ok=True)
FONTS_DIR = BASE / "fonts"
FONTS_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")
app.mount("/sorted", StaticFiles(directory=SORTED_DIR), name="sorted")
app.mount("/fonts", StaticFiles(directory=FONTS_DIR), name="fonts")

from sorter import router as sorter_router  # noqa: E402
app.include_router(sorter_router)


def _find_outputs(out_dir: Path) -> tuple[Path | None, Path | None]:
    md = next(iter(sorted(out_dir.rglob("*.md"))), None)
    cl = next(
        iter(sorted(out_dir.rglob("*_content_list*.json"))),
        None,
    )
    return md, cl


@app.post("/parse")
async def parse(file: UploadFile, backend: str = Form("pipeline")):
    job_id = uuid.uuid4().hex[:8]
    safe_name = Path(file.filename or "upload.pdf").name
    job_in = INPUTS / job_id
    job_in.mkdir(parents=True, exist_ok=True)
    in_path = job_in / safe_name
    with in_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    job_out = OUTPUTS / job_id
    job_out.mkdir(parents=True, exist_ok=True)

    async def stream():
        def emit(obj: dict) -> bytes:
            return (json.dumps(obj) + "\n").encode()

        yield emit({"type": "log", "msg": f"→ Saved {safe_name} ({in_path.stat().st_size:,} bytes)"})
        yield emit({"type": "log", "msg": f"→ Launching mineru (backend={backend}) ..."})
        yield emit({"type": "log", "msg": "(first run on a backend downloads ~1–3 GB of weights to ~/.cache/)"})

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "TERM": "dumb",
            "COLUMNS": "120",
        }
        proc = await asyncio.create_subprocess_exec(
            "mineru",
            "-p", str(in_path),
            "-o", str(job_out),
            "-b", backend,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        assert proc.stdout is not None
        pending = b""
        try:
            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break
                pending += chunk
                # split on either \n or \r so tqdm progress lines stream live
                while True:
                    nl = pending.find(b"\n")
                    cr = pending.find(b"\r")
                    cuts = [i for i in (nl, cr) if i >= 0]
                    if not cuts:
                        break
                    idx = min(cuts)
                    line, pending = pending[:idx], pending[idx + 1 :]
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        yield emit({"type": "log", "msg": text})
            if pending.strip():
                yield emit({"type": "log", "msg": pending.decode("utf-8", errors="replace").strip()})
            rc = await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        md_path, cl_path = _find_outputs(job_out)
        markdown = md_path.read_text() if md_path else ""
        content_list = []
        if cl_path:
            try:
                content_list = json.loads(cl_path.read_text())
            except Exception as e:
                yield emit({"type": "log", "msg": f"(could not parse content_list.json: {e})"})

        image_base = ""
        if md_path:
            image_base = f"/outputs/{md_path.relative_to(OUTPUTS).parent.as_posix()}/"

        saved_to: str | None = None
        if rc == 0 and md_path:
            stem = Path(safe_name).stem
            dest = RESULTS / f"{backend}_{stem}"
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            shutil.copy2(md_path, dest / f"{stem}.md")
            if cl_path:
                shutil.copy2(cl_path, dest / f"{stem}_content_list.json")
            src_images = md_path.parent / "images"
            if src_images.is_dir():
                shutil.copytree(src_images, dest / "images")
            saved_to = str(dest.relative_to(BASE))
            yield emit({"type": "log", "msg": f"→ Saved results to {saved_to}/"})

        yield emit({
            "type": "done",
            "exit_code": rc,
            "markdown": markdown,
            "content_list": content_list,
            "image_base": image_base,
            "md_path": str(md_path.relative_to(BASE)) if md_path else None,
            "saved_to": saved_to,
        })

    return StreamingResponse(stream(), media_type="application/x-ndjson")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>MinerU playground</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1f232c;
    --border: #2a2f3a;
    --fg: #e7ecf3;
    --muted: #8a93a6;
    --accent: #7aa2ff;
    --ok: #6bd68a;
    --warn: #f2c14e;
    --err: #ff6e7a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg);
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 12px; }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: .3px; }
  header .sub { color: var(--muted); font-size: 12px; }
  main { display: grid; grid-template-columns: 380px 1fr; gap: 0; height: calc(100vh - 49px); }
  aside { border-right: 1px solid var(--border); padding: 16px; overflow: auto; background: var(--panel); }
  section.right { display: flex; flex-direction: column; min-width: 0; }
  .drop {
    border: 2px dashed var(--border); border-radius: 10px; padding: 28px 16px;
    text-align: center; color: var(--muted); cursor: pointer; transition: all .15s ease;
    background: var(--panel-2);
  }
  .drop.hover { border-color: var(--accent); color: var(--fg); background: #1c2230; }
  .drop strong { color: var(--fg); display: block; margin-bottom: 4px; }
  .drop small { display: block; margin-top: 6px; font-size: 11px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 14px 0 6px; }
  .row label { font-size: 12px; color: var(--muted); }
  select, button {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font: inherit;
  }
  button { cursor: pointer; }
  button.primary { background: var(--accent); color: #0b1020; border-color: var(--accent); font-weight: 600; }
  button.primary:disabled { opacity: .5; cursor: not-allowed; }
  .status { font-size: 12px; color: var(--muted); margin-top: 10px; }
  .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); margin-right: 6px; vertical-align: middle; }
  .status.running .dot { background: var(--warn); animation: pulse 1s ease-in-out infinite; }
  .status.done .dot { background: var(--ok); }
  .status.err .dot { background: var(--err); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--border); background: var(--panel); padding: 0 16px; }
  .tabs button { border: none; background: transparent; color: var(--muted); padding: 10px 14px; border-radius: 0; border-bottom: 2px solid transparent; }
  .tabs button.active { color: var(--fg); border-bottom-color: var(--accent); }
  .pane { flex: 1; overflow: auto; padding: 16px 20px; min-height: 0; }
  .pane.hidden { display: none; }
  pre.log { margin: 0; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
    white-space: pre-wrap; word-break: break-word; color: #c9d1e1; }
  pre.log .line { padding: 1px 0; }
  pre.log .line.meta { color: var(--accent); }
  pre.json { margin: 0; font: 12px/1.5 ui-monospace, Menlo, monospace; white-space: pre-wrap; word-break: break-word; color: #c9d1e1; }
  .md { max-width: 900px; }
  .md img { max-width: 100%; border-radius: 4px; border: 1px solid var(--border); }
  .md table { border-collapse: collapse; margin: 10px 0; }
  .md th, .md td { border: 1px solid var(--border); padding: 4px 8px; }
  .md th { background: var(--panel); }
  .md h1, .md h2, .md h3 { border-bottom: 1px solid var(--border); padding-bottom: 4px; }
  .md code { background: var(--panel); padding: 1px 5px; border-radius: 3px; }
  .empty { color: var(--muted); font-style: italic; }
  .file-chip { font-size: 12px; color: var(--fg); margin-top: 10px; padding: 6px 10px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; word-break: break-all; }
</style>
</head>
<body>
<header>
  <h1>MinerU playground</h1>
  <span class="sub">drop a PDF/DOCX/PPTX/XLSX/image, watch it parse, inspect the result</span>
</header>
<main>
  <aside>
    <div id="drop" class="drop">
      <strong>Drop a file here</strong>
      <span>or click to choose</span>
      <small>PDF, DOCX, PPTX, XLSX, PNG, JPG</small>
    </div>
    <input id="file" type="file" style="display:none"
      accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff" />
    <div id="chip" class="file-chip" style="display:none"></div>

    <div class="row">
      <label for="backend">Backend</label>
      <select id="backend">
        <option value="pipeline" selected>pipeline (fast, MPS-friendly)</option>
        <option value="hybrid-auto-engine">hybrid-auto-engine (VLM, higher accuracy)</option>
        <option value="vlm-auto-engine">vlm-auto-engine (pure VLM)</option>
      </select>
    </div>

    <div class="row">
      <button id="go" class="primary" disabled>Parse</button>
      <button id="clear">Clear</button>
    </div>
    <div id="status" class="status"><span class="dot"></span><span id="status-text">idle</span></div>
  </aside>

  <section class="right">
    <div class="tabs">
      <button data-tab="log" class="active">Log</button>
      <button data-tab="md">Markdown</button>
      <button data-tab="json">Content JSON</button>
    </div>
    <div class="pane" id="pane-log"><pre class="log" id="log"><span class="empty">Waiting for a file…</span></pre></div>
    <div class="pane hidden" id="pane-md"><div class="md" id="md"><span class="empty">No output yet.</span></div></div>
    <div class="pane hidden" id="pane-json"><pre class="json" id="json"><span class="empty">No output yet.</span></pre></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file"), chip = $("chip");
const go = $("go"), clearBtn = $("clear"), backend = $("backend");
const statusEl = $("status"), statusText = $("status-text");
const logEl = $("log"), mdEl = $("md"), jsonEl = $("json");
let currentFile = null;

drop.addEventListener("click", () => fileInput.click());
["dragenter","dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("hover"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", ev => { if (ev.dataTransfer.files[0]) setFile(ev.dataTransfer.files[0]); });
fileInput.addEventListener("change", ev => { if (ev.target.files[0]) setFile(ev.target.files[0]); });

function setFile(f) {
  currentFile = f;
  chip.style.display = "block";
  chip.textContent = `${f.name}  ·  ${(f.size/1024).toFixed(1)} KB`;
  go.disabled = false;
}

document.querySelectorAll(".tabs button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    ["log","md","json"].forEach(t => $("pane-"+t).classList.toggle("hidden", t !== b.dataset.tab));
  });
});

clearBtn.addEventListener("click", () => {
  logEl.innerHTML = '<span class="empty">Waiting for a file…</span>';
  mdEl.innerHTML = '<span class="empty">No output yet.</span>';
  jsonEl.innerHTML = '<span class="empty">No output yet.</span>';
  setStatus("idle", "idle");
});

function setStatus(cls, text) {
  statusEl.className = "status " + (cls || "");
  statusText.textContent = text;
}

function appendLog(msg, meta=false) {
  if (logEl.querySelector(".empty")) logEl.innerHTML = "";
  const line = document.createElement("span");
  line.className = "line" + (meta ? " meta" : "");
  line.textContent = msg + "\n";
  logEl.appendChild(line);
  const pane = $("pane-log");
  pane.scrollTop = pane.scrollHeight;
}

go.addEventListener("click", async () => {
  if (!currentFile) return;
  go.disabled = true;
  setStatus("running", "parsing…");
  logEl.innerHTML = "";
  mdEl.innerHTML = '<span class="empty">Parsing…</span>';
  jsonEl.innerHTML = '<span class="empty">Parsing…</span>';
  document.querySelector('.tabs button[data-tab="log"]').click();

  const fd = new FormData();
  fd.append("file", currentFile);
  fd.append("backend", backend.value);

  appendLog(`POST /parse  (backend=${backend.value})`, true);

  let resp;
  try {
    resp = await fetch("/parse", { method: "POST", body: fd });
  } catch (e) {
    setStatus("err", "network error");
    appendLog("network error: " + e, true);
    go.disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    setStatus("err", "http " + resp.status);
    appendLog("server returned " + resp.status, true);
    go.disabled = false;
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const raw = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!raw) continue;
      let ev;
      try { ev = JSON.parse(raw); } catch { appendLog(raw); continue; }
      handleEvent(ev);
    }
  }
  go.disabled = false;
});

function handleEvent(ev) {
  if (ev.type === "log") {
    appendLog(ev.msg);
  } else if (ev.type === "done") {
    const ok = ev.exit_code === 0;
    setStatus(ok ? "done" : "err", ok ? `done (exit ${ev.exit_code})` : `failed (exit ${ev.exit_code})`);
    appendLog(`— finished (exit ${ev.exit_code}) —`, true);

    if (ev.markdown) {
      let md = ev.markdown;
      if (ev.image_base) md = md.replace(/(!\[[^\]]*\]\()(?!https?:)([^)]+)\)/g, `$1${ev.image_base}$2)`);
      mdEl.innerHTML = marked.parse(md);
    } else {
      mdEl.innerHTML = '<span class="empty">No markdown produced.</span>';
    }
    jsonEl.textContent = JSON.stringify(ev.content_list || [], null, 2);
  }
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
