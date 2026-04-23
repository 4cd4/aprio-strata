# Aprio Strata

A local-first document sorter for accountants (and anyone drowning in client PDFs). Drop files, a 3-page MinerU pass extracts the title block, Gemma 4 E4B suggests a `snake_case` name and assigns it to the right client, you review, commit. Nothing leaves your machine.

Bundled with a second tool — a MinerU playground at `/` that streams per-page parsing progress so you can see how different backends handle a PDF.

Built around three local components:

- **[MinerU](https://github.com/opendatalab/MinerU) 3.x** (`pipeline` backend) — extracts structured markdown from the first 3 pages of each doc
- **[Ollama](https://ollama.com/)** running **`gemma4:e4b`** — proposes a snake_case filename and an existing-or-new client folder, given the extracted markdown + the current client list
- **FastAPI + vanilla JS** — streams each file's progress as NDJSON; no CDN, no external fonts, no telemetry

## What's distinctive about it

- **Echoes instead of a duplicates folder.** When you upload a byte-identical copy of something already in your library, Strata records it as an *echo* on the canonical file — a little gold badge with the original filename and timestamp. No quarantine folder to wade through. Explanation and rationale is the clearest way I've found to handle "your partner re-sent you the same 10-K again."
- **Live-typing filename suggestions.** Gemma's tokens stream directly into the filename input as they're generated, with a sapphire caret. A 2-second LLM call becomes a tiny performance.
- **Consistency-aware client extraction.** Every call to Gemma gets the existing client list injected — so you don't end up with `Dexafit` / `Dexafit Inc` / `DexaFit LLC` scattered across the sidebar.
- **Warm-paper Liquid Glass UI.** Palette + typography lifted from the Aprio triage pipeline (Fraunces serif, Instrument Sans, JetBrains Mono, forest green #2D5F3E on #F4EFE6 paper). Aurora drift in the background, hairline strata etched across the viewport, frosted-cream glass panels.
- **Three-page budget.** MinerU only reads pages 0–2. Identification rarely needs more; each file processes in ~3–5 s.
- **Everything vendored locally.** The 20 font files (700 KB) live in `fonts/`. No CDN, no `fonts.googleapis.com`, no outbound requests of any kind beyond `127.0.0.1` (FastAPI) and `localhost:11434` (Ollama).

## Screenshots

> _Add your own after running_

## Architecture

```
Browser
  │  POST /sorter/process  (multipart upload)
  ▼
FastAPI (uvicorn)
  │  SHA-256 each file → check against manifest for echoes
  │  spawn `mineru` subprocess (pipeline backend, 3 pages)
  │  POST localhost:11434/api/generate  (gemma4:e4b, streaming)
  │      — filename tokens stream back to the browser live-typed
  │  second Gemma call with known-clients context → assigns client
  │  emits NDJSON events (row_start, row_step, row_token, row_done)
  ▼
Browser review drawer
  │  edit name + client per-row, optional skip
  │  Commit → POST /sorter/apply
  ▼
FastAPI
  │  re-hash each temp file (authoritative SHA-256)
  │  accept → shutil.copy2 → sorted/<name>.ext
  │  duplicate → append echo entry to canonical (no file copy)
  │  write sorted/.manifest.json
```

See [`sorter.py`](sorter.py) for the sorter routes and UI, [`app.py`](app.py) for the MinerU playground at `/`.

## Setup

Tested on macOS 26 / Apple Silicon. Should work on Linux with minor tweaks.

### Prereqs

1. **Python 3.12** (3.10+ should work).
2. **[Ollama](https://ollama.com/download)** running locally:
   ```bash
   ollama pull gemma4:e4b      # ~6 GB
   ```
3. **[uv](https://github.com/astral-sh/uv)** (optional but recommended for fast installs).

### Install

```bash
git clone https://github.com/4cd4/aprio-strata.git
cd aprio-strata

uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -U "mineru[core]" fastapi uvicorn httpx python-multipart

# OR if you don't use uv:
# python3.12 -m venv .venv && source .venv/bin/activate
# pip install "mineru[core]" fastapi uvicorn httpx python-multipart
```

The first MinerU invocation downloads ~1.2 GB of layout / OCR / table / formula model weights into `~/.cache/huggingface/`. One-time.

### Run

```bash
uvicorn app:app --host 127.0.0.1 --port 8765
```

Open:
- **http://127.0.0.1:8765/sorter** — Aprio Strata (the main app)
- **http://127.0.0.1:8765/** — the MinerU playground (drop one PDF, watch it parse)

## How the pipeline uses RAM

Peak during a batch:

| Component | RAM | Notes |
|---|---|---|
| MinerU subprocess | 2–3 GB | spawned per file, exits when done |
| Ollama + `gemma4:e4b` | 6–9 GB | 5-min keepalive, then unloads |
| uvicorn + everything else | ~100 MB | |
| **Peak** | **~10 GB** | comfortable on a 16 GB Mac, trivial on 32+ GB |

## Privacy

Everything runs on `127.0.0.1` or `localhost:11434`. The only outbound requests at runtime are Ollama talking to itself and MinerU loading already-cached weights from `~/.cache/`. Even the fonts are vendored.

Files you sort live in `sorted/<name>.ext`, with provenance (original filename, SHA-256, echoes) in `sorted/.manifest.json`. That folder is gitignored — private to your machine.

## Design credits

- **Palette + typography** borrowed from the [Aprio triage pipeline](https://github.com/4cd4/valued-ai) prototype.
- **MinerU** — [opendatalab/MinerU](https://github.com/opendatalab/MinerU).
- **Gemma 3/4** — [Google](https://deepmind.google/models/gemma/).
- **Fraunces / Instrument Sans / JetBrains Mono** — [Google Fonts](https://fonts.google.com/), vendored locally.

## License

MIT
