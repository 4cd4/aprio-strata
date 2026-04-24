# Aprio Strata

> _Documents, layered into strata._

Accounting firms drown in documents — tax returns, engagement letters, financial statements, due-diligence packs — arriving with no uniform naming convention, from different clients, through different channels, in whatever filename the sender's assistant happened to choose that day. Aprio Strata is a **local-first** tool that reads the first three pages of every document, proposes a consistent `snake_case` filename and the right client folder, and records byte-identical duplicates as metadata ("echoes") on the canonical file instead of quarantining them.

The whole pipeline — layout detection, OCR, table extraction, LLM naming, classification — runs entirely on your machine. No cloud upload. No third-party vendor. No telemetry. The only outbound traffic is MinerU loading its cached model weights once, and Ollama talking to itself on `localhost`. **Your client documents never leave the laptop.**

This is the start of something bigger. Strata is the ingestion and taxonomy layer for an eventual document-intelligence stack — search-by-content, version families, cross-client pattern detection, integration with existing document-management systems. The sorter is useful standalone today.

<br>

## What makes it distinctive

- **Echoes, not a duplicates folder.** Upload a byte-identical copy of something already in the library → Strata records it as an *echo* on the canonical (original filename, upload time, source) rather than stashing a second copy in a quarantine folder. Hover any document with echoes to see its history.
- **Live-typing filename suggestions.** The LLM's tokens stream directly into the filename input as they're generated, with a soft sapphire caret. A 2-second call becomes a tiny performance.
- **Consistency-aware classification.** Every call to the model includes the existing client list — so you don't end up with `Dexafit` / `Dexafit Inc` / `DexaFit LLC` scattered across the sidebar.
- **Warm-paper Liquid Glass interface.** Palette and typography drawn from Aprio's own triage prototype (Fraunces serif, Instrument Sans, JetBrains Mono on a `#F4EFE6` paper with `#2D5F3E` accent), all vendored locally. No CDN fonts.
- **Three-page budget.** MinerU only reads pages 0–2 of each file. Identification rarely needs more; each file processes in ~8–12 s end-to-end.
- **Nothing leaves your machine.** Loopback-only (`127.0.0.1`). Airplane-mode it after first run and the whole flow still works.

## Screenshots

> _Drop one in after your first run — the empty state is a clean place to start._

## Architecture at a glance

```
Browser
  │  POST /sorter/process  (multipart upload)
  ▼
FastAPI (uvicorn)
  │  SHA-256 each file → check manifest for echoes
  │  spawn `mineru` subprocess (pipeline backend, 3 pages)
  │  POST localhost:11434/api/generate  (gemma4:e4b, streaming)
  │      — filename tokens stream back live-typed into the UI
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

Everything on `127.0.0.1`. Code lives in [`app.py`](app.py) (FastAPI glue, MinerU playground at `/mineru`) and [`sorter.py`](sorter.py) (the Strata app at `/`).

<br>

## Run your own instance

**Each person who clones this gets their own isolated library.** Your `sorted/` folder and `sorted/.manifest.json` are gitignored, never synced, and live only on the machine that created them. There is no shared server and no way for two people's libraries to collide. If two people want to run Strata, each clones the repo, each runs their own `uvicorn` locally.

### Prerequisites

- **macOS 14+** (tested on Apple Silicon; Linux also works)
- **Python 3.12** (3.10+ is fine)
- **[Ollama](https://ollama.com/download)** — then pull the model:
  ```bash
  ollama pull gemma4:e4b    # ~6 GB, one-time
  ```
- **[uv](https://github.com/astral-sh/uv)** (optional — faster installs)

### Install & run

```bash
git clone https://github.com/4cd4/aprio-strata.git
cd aprio-strata

# option A — uv (recommended, fast)
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -U "mineru[core]" fastapi uvicorn httpx python-multipart

# option B — stdlib pip
python3.12 -m venv .venv
source .venv/bin/activate
pip install "mineru[core]" fastapi uvicorn httpx python-multipart

# run it
uvicorn app:app --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765/** and drop in a file.

The first MinerU run downloads ~1.2 GB of layout / OCR / table / formula model weights into `~/.cache/huggingface/`. One-time.

### Keyboard shortcuts

| | |
|---|---|
| `/` | Focus search |
| `D` | Open deposit drawer |
| `⌘↵` | Commit (in drawer) |
| `Esc` | Close drawer / preview / shortcut overlay |
| `?` | Toggle this help |

<br>

## RAM during a batch

| Component | Peak | Notes |
|---|---|---|
| MinerU subprocess | 2–3 GB | spawned per file, exits when done |
| Ollama + `gemma4:e4b` | 6–9 GB | 5-min keepalive, then unloads |
| uvicorn + app | ~100 MB | |
| **Total peak** | **~10 GB** | comfortable on a 16 GB Mac, trivial on 32+ GB |

<br>

## Privacy

Everything lives on `127.0.0.1` or `localhost:11434`. Fonts are vendored (no Google Fonts CDN), icons are inline SVG, there is no analytics code. Files land in `sorted/<name>.ext`; provenance (original name, SHA-256, echoes, client) lives in `sorted/.manifest.json`. Both are gitignored.

Airplane-mode the Mac after first run and the full pipeline still works.

<br>

## Design credits

- **Palette and typography** ported from the [Aprio triage prototype](https://github.com/4cd4/valued-ai).
- **MinerU** — [opendatalab/MinerU](https://github.com/opendatalab/MinerU).
- **Gemma** — [Google](https://deepmind.google/models/gemma/).
- **Fraunces / Instrument Sans / JetBrains Mono** — [Google Fonts](https://fonts.google.com/), vendored locally.

## License

MIT
