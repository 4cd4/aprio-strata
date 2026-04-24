"""File sorter: MinerU (first 3 pages) + Gemma 4 E4B → snake_case rename.

Mounted as /sorter from app.py.

Flow:
1. User drops files in UI → they upload to `inputs/_sorter/<job>/`.
2. For each file: hash it, run mineru pipeline on pages 0-2, read markdown,
   POST to Ollama with Gemma 4 E4B for a snake_case name suggestion.
3. Duplicates (by SHA-256) are flagged against `sorted/.manifest.json`.
4. User reviews, edits, hits Apply — approved copies go to `sorted/`,
   duplicates to `sorted/duplicates/`, manifest updated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

BASE = Path(__file__).parent
SORTER_TMP = BASE / "inputs" / "_sorter"
SORTED = BASE / "sorted"
DUPES = SORTED / "duplicates"
MANIFEST_PATH = SORTED / ".manifest.json"
SORTER_TMP.mkdir(parents=True, exist_ok=True)
SORTED.mkdir(parents=True, exist_ok=True)
DUPES.mkdir(parents=True, exist_ok=True)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
MAX_MARKDOWN_CHARS = 1800

router = APIRouter(prefix="/sorter")


# ── manifest ──────────────────────────────────────────────────────────
def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except Exception:
            pass
    return {"files": []}


def _save_manifest(m: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(m, indent=2))


def _find_dup(manifest: dict, file_hash: str) -> dict | None:
    for entry in manifest["files"]:
        if entry.get("hash") == file_hash:
            return entry
    return None


# ── filename helpers ──────────────────────────────────────────────────
_SANITIZE_RE = re.compile(r"[^a-z0-9_\-]+")


def sanitize_snake(raw: str, fallback: str) -> str:
    s = (raw or "").strip()
    # strip markdown, quotes, leading hints Gemma sometimes emits
    s = re.sub(r"^(?:filename|file\s*name|name)\s*[:=]\s*", "", s, flags=re.I)
    s = s.strip("`*_\"' \n\t.")
    s = s.lower().replace(" ", "_").replace("-", "_")
    s = _SANITIZE_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) > 80:
        s = s[:80].rstrip("_")
    if not s:
        s = Path(fallback).stem.lower()
        s = _SANITIZE_RE.sub("_", s).strip("_") or "untitled"
    return s


def resolve_collision(target_dir: Path, stem: str, ext: str) -> Path:
    """If `<stem>.<ext>` exists, return `<stem>_2.<ext>`, `<stem>_3.<ext>`, ..."""
    candidate = target_dir / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = target_dir / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


# ── mineru + ollama ───────────────────────────────────────────────────
async def run_mineru_first_pages(in_path: Path, out_dir: Path, pages: int = 3) -> Path | None:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TERM": "dumb", "COLUMNS": "100"}
    proc = await asyncio.create_subprocess_exec(
        "mineru",
        "-p", str(in_path),
        "-o", str(out_dir),
        "-b", "pipeline",
        "-s", "0",
        "-e", str(pages - 1),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    await proc.wait()
    md = next(iter(sorted(out_dir.rglob("*.md"))), None)
    return md


NAME_PROMPT = """You are a filename generator. Read the document excerpt and output EXACTLY ONE descriptive filename in snake_case.

STRICT OUTPUT RULES:
- lowercase letters, digits, underscores only
- no extension, no quotes, no markdown, no explanation
- 3 to 7 words total
- identify: the company/person, the document type, and the year if visible

GOOD EXAMPLES:
- tesla_10k_annual_report_2023
- 1life_healthcare_10k_2020
- john_smith_employment_agreement_2024
- q3_2024_financial_statements
- apple_proxy_statement_2022
- dexafit_balance_sheet_2025

DOCUMENT EXCERPT:
---
{md}
---

Output ONLY the filename (no other text):"""


CLIENT_PROMPT = """Identify the CLIENT this document is about or for — typically the company, person,
or organization named as the subject. Think: whose file does this belong in?

{known_clients_hint}

STRICT OUTPUT RULES:
- just the client name, 1 to 4 words, Title Case
- no explanation, no quotes, no punctuation trailing
- prefer a name from the existing list above if the document clearly refers to that client
- if the document is about an individual person, use their full name
- if you genuinely cannot tell, output: Unassigned

GOOD EXAMPLES:
- Tesla Inc
- John Smith
- Dexafit
- 1Life Healthcare
- Acme Partners LLC

Document excerpt:
---
{md}
---

Client:"""


_CLIENT_FALLBACK = "Unassigned"
_CLIENT_SANITIZE = re.compile(r"[^A-Za-z0-9\-\s&\.,']")

# Default buckets seeded for every new client. Each client can add/remove on top of these.
DEFAULT_BUCKETS = ["Finance", "Tax", "Legal"]
_BUCKET_FALLBACK = "Unfiled"
_BUCKET_SANITIZE = re.compile(r"[^A-Za-z0-9\-\s&]")


def sanitize_bucket(raw: str) -> str:
    if not raw:
        return _BUCKET_FALLBACK
    s = raw.strip().strip("`*_\"'.")
    s = s.splitlines()[0] if s.splitlines() else s
    s = re.sub(r"^(?:bucket|category|label|type)\s*[:=]\s*", "", s, flags=re.I)
    s = _BUCKET_SANITIZE.sub("", s).strip()
    if not s:
        return _BUCKET_FALLBACK
    words = [w for w in re.split(r"\s+", s) if w]
    words = [w[:1].upper() + w[1:].lower() for w in words]
    out = " ".join(words[:3])[:30]
    return out or _BUCKET_FALLBACK


BUCKET_PROMPT = """Classify this accounting document into ONE of these buckets for the client "{client}":

{bucket_list}

STRICT OUTPUT RULES:
- one bucket name only, matching the exact spelling from the list above
- if none of the existing buckets fit and the document clearly belongs to a new category, you may output a short new bucket name (1-2 words, Title Case)
- otherwise, output Unfiled
- no explanation, no quotes, no markdown

Document excerpt:
---
{md}
---

Bucket:"""


def client_buckets(manifest: dict, client: str) -> list[str]:
    """Return the bucket list for a given client (defaults if new)."""
    taxonomies = manifest.get("client_taxonomies", {})
    return taxonomies.get(client, list(DEFAULT_BUCKETS))


def ensure_client_taxonomy(manifest: dict, client: str) -> list[str]:
    """Seed the default taxonomy for a client if they don't have one yet."""
    manifest.setdefault("client_taxonomies", {})
    if client not in manifest["client_taxonomies"]:
        manifest["client_taxonomies"][client] = list(DEFAULT_BUCKETS)
    return manifest["client_taxonomies"][client]


# ── Training data log (append-only JSONL) ─────────────────────────────
TRAINING_LOG = SORTED / ".bucket_changes.jsonl"


def log_training_event(event: dict) -> None:
    """Append a bucket-related event to the training log."""
    event = {**event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with TRAINING_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")


def sanitize_client(raw: str) -> str:
    if not raw:
        return _CLIENT_FALLBACK
    s = raw.strip().strip("`*_\"'.")
    s = s.splitlines()[0] if s.splitlines() else s
    s = re.sub(r"^(?:client|company|person|name)\s*[:=]\s*", "", s, flags=re.I)
    s = _CLIENT_SANITIZE.sub("", s).strip()
    if not s:
        return _CLIENT_FALLBACK
    words = [w for w in re.split(r"\s+", s) if w]
    # preserve common all-caps tokens; otherwise Title Case
    def cap(w):
        if re.fullmatch(r"(?:LLC|LLP|INC|LTD|PLC|CO|NV|SA|AG|AB|GMBH|NDA|LP|USA|UK|EU)\.?", w.upper()):
            return w.upper().rstrip(".")
        if re.match(r"^\d", w):  # "1life" stays lowercase digits
            return w.lower()
        return w[:1].upper() + w[1:].lower()
    out = " ".join(cap(w) for w in words[:5])[:50]
    return out or _CLIENT_FALLBACK


# SEC/legal boilerplate patterns that bury the useful identifying info.
# We remove paragraphs that start with these so Gemma sees the title block
# and company name, not 2000 chars of "Indicate by check mark whether..."
_BOILERPLATE_PREFIXES = (
    "indicate by check mark",
    "securities registered pursuant to",
    "if an emerging growth company",
    "check the appropriate box",
    "large accelerated filer",
    "smaller reporting company",
    "the aggregate market value",
    "portions of the registrant",
)


def _prep_for_llm(md: str, limit: int = MAX_MARKDOWN_CHARS) -> str:
    """Strip tables, images, and SEC-style boilerplate paragraphs before truncation."""
    lines: list[str] = []
    for ln in md.splitlines():
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        low = s.lower()
        if low.startswith("![") or low.startswith("|") or s.startswith("<!"):
            continue  # markdown image / table row / html comment
        if any(low.startswith(p) for p in _BOILERPLATE_PREFIXES):
            continue
        lines.append(s)
    cleaned = "\n".join(lines).strip()
    # cap at `limit` chars; if clipped, try to end at a paragraph break
    if len(cleaned) > limit:
        cut = cleaned[:limit]
        last_break = cut.rfind("\n\n")
        if last_break > limit // 2:
            cut = cut[:last_break]
        cleaned = cut
    return cleaned or "(empty document)"


async def stream_ollama(prompt: str, *, max_tokens: int = 60, temp: float = 0.3):
    """Async generator yielding token chunks from Ollama as they arrive."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": temp, "num_predict": max_tokens, "top_p": 0.9},
    }
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                tok = obj.get("response") or ""
                if tok:
                    yield tok
                if obj.get("done"):
                    return


async def suggest_name_and_bucket(md_text: str, fallback: str, emit_token, client: str, manifest: dict):
    """Stream filename tokens, then infer ONE bucket from the given client's taxonomy.
    Client is fixed at the batch level (the user picks it in the drawer).
    Returns (suggested_name, bucket)."""
    prompt = NAME_PROMPT.format(md=_prep_for_llm(md_text))
    raw = ""
    try:
        async for tok in stream_ollama(prompt, max_tokens=60, temp=0.3):
            raw += tok
            await emit_token(tok)
    except Exception:
        return sanitize_snake(f"error_{Path(fallback).stem}", fallback), _BUCKET_FALLBACK

    first_line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    suggested = sanitize_snake(first_line, fallback)

    try:
        buckets = client_buckets(manifest, client) if client != _CLIENT_FALLBACK else list(DEFAULT_BUCKETS)
        # Always include Unfiled as the "unsure" fallback — Gemma is explicitly
        # told to pick it when it can't confidently classify.
        if "Unfiled" not in buckets:
            buckets = buckets + ["Unfiled"]
        bucket_list = "\n".join(f"- {b}" for b in buckets)
        b_prompt = BUCKET_PROMPT.format(
            md=_prep_for_llm(md_text, limit=1400),
            client=client or "this client",
            bucket_list=bucket_list,
        )
        raw_b = ""
        async for tok in stream_ollama(b_prompt, max_tokens=10, temp=0.15):
            raw_b += tok
        bucket = sanitize_bucket(raw_b)
    except Exception:
        bucket = _BUCKET_FALLBACK

    return suggested, bucket


# ── routes ────────────────────────────────────────────────────────────
@router.post("/process")
async def process(files: list[UploadFile], batch_client: str = Form("")):
    """Stream NDJSON: per-file `row_start`, `row_step`, `row_done` events."""
    uploads: list[dict] = []
    for uf in files:
        data = await uf.read()
        file_hash = hashlib.sha256(data).hexdigest()
        safe_name = Path(uf.filename or "upload").name
        file_id = uuid.uuid4().hex[:10]
        job_dir = SORTER_TMP / file_id
        job_dir.mkdir(parents=True, exist_ok=True)
        in_path = job_dir / safe_name
        in_path.write_bytes(data)
        uploads.append({
            "id": file_id,
            "name": safe_name,
            "size": len(data),
            "hash": file_hash,
            "path": str(in_path),
        })

    async def stream():
        def emit(o: dict) -> bytes:
            return (json.dumps(o) + "\n").encode()

        manifest = _load_manifest()

        # in-batch dedup
        seen_hashes_in_batch: dict[str, str] = {}

        for u in uploads:
            yield emit({
                "type": "row_start",
                "id": u["id"],
                "name": u["name"],
                "size": u["size"],
                "hash": u["hash"][:12],
            })

            # same file already sorted previously?
            prev = _find_dup(manifest, u["hash"])
            if prev is not None:
                yield emit({
                    "type": "row_done",
                    "id": u["id"],
                    "status": "duplicate",
                    "dup_reason": "already_in_sorted",
                    "dup_of": prev.get("final_name") or prev.get("original"),
                    "suggested": None,
                    "temp_path": u["path"],
                })
                continue

            # same file appearing twice in THIS batch?
            if u["hash"] in seen_hashes_in_batch:
                yield emit({
                    "type": "row_done",
                    "id": u["id"],
                    "status": "duplicate",
                    "dup_reason": "duplicate_in_batch",
                    "dup_of": seen_hashes_in_batch[u["hash"]],
                    "suggested": None,
                    "temp_path": u["path"],
                })
                continue
            seen_hashes_in_batch[u["hash"]] = u["name"]

            # mineru pass
            yield emit({"type": "row_step", "id": u["id"], "step": "extracting"})
            out_dir = Path(u["path"]).parent / "out"
            out_dir.mkdir(exist_ok=True)
            md_path = await run_mineru_first_pages(Path(u["path"]), out_dir, pages=3)
            md_text = md_path.read_text(encoding="utf-8", errors="replace") if md_path else ""
            if not md_text.strip():
                yield emit({
                    "type": "row_done",
                    "id": u["id"],
                    "status": "error",
                    "error": "mineru produced no markdown (is this a PDF/image/DOCX?)",
                    "suggested": sanitize_snake("", u["name"]),
                    "preview": "",
                    "temp_path": u["path"],
                })
                continue

            # ollama pass — stream tokens as they arrive
            yield emit({"type": "row_step", "id": u["id"], "step": "naming"})

            # The inner function can't `yield` directly — we collect messages
            # into a queue and drain after suggest_name_and_category runs.
            token_events: list[bytes] = []

            async def emit_token(tok: str, _uid=u["id"]):
                token_events.append(emit({"type": "row_token", "id": _uid, "tok": tok}))

            # We want live streaming back to the browser, so interleave by
            # running suggest as a task and draining the queue while it works.
            queue: asyncio.Queue[bytes | None] = asyncio.Queue()

            async def emit_token_q(tok: str, _uid=u["id"]):
                await queue.put(emit({"type": "row_token", "id": _uid, "tok": tok}))

            # The batch client is the single authoritative client for every
            # file in this process call. We still let Gemma predict a per-file
            # bucket against that client's taxonomy.
            bc = sanitize_client(batch_client) if batch_client else _CLIENT_FALLBACK

            async def worker():
                try:
                    result = await suggest_name_and_bucket(md_text, u["name"], emit_token_q, bc, manifest)
                    await queue.put(b"__RESULT__:" + json.dumps(result).encode())
                except Exception:
                    await queue.put(b"__RESULT__:" + json.dumps([sanitize_snake("", u["name"]), _BUCKET_FALLBACK]).encode())
                finally:
                    await queue.put(None)

            task = asyncio.create_task(worker())
            suggested: str = ""
            bucket: str = _BUCKET_FALLBACK
            while True:
                item = await queue.get()
                if item is None:
                    break
                if item.startswith(b"__RESULT__:"):
                    suggested, bucket = json.loads(item[len(b"__RESULT__:"):])
                    continue
                yield item
            await task

            yield emit({
                "type": "row_done",
                "id": u["id"],
                "status": "ready",
                "suggested": suggested,
                "client": bc,
                "bucket": bucket,
                "preview": md_text[:800],
                "temp_path": u["path"],
            })

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.post("/apply")
async def apply(payload: dict):
    """payload = {decisions: [{id, temp_path, original, status, final_name, hash, dup_of?}]}"""
    manifest = _load_manifest()
    results = []
    for d in payload.get("decisions", []):
        src = Path(d["temp_path"])
        original = d.get("original") or src.name
        if not src.exists():
            results.append({"id": d["id"], "ok": False, "error": "source missing"})
            continue
        # Re-hash the source from disk. The client only has a truncated prefix
        # (for display); trusting that would let duplicates slip through on
        # the next upload since the manifest would store only 12 chars.
        full_hash = hashlib.sha256(src.read_bytes()).hexdigest()
        ext = Path(original).suffix or ".bin"
        status = d.get("status", "skip")

        if status == "accept":
            stem = sanitize_snake(d.get("final_name") or "", original)
            dest = resolve_collision(SORTED, stem, ext)
            shutil.copy2(src, dest)
            client_name = sanitize_client(d.get("client") or "")
            bucket_name = sanitize_bucket(d.get("bucket") or "")
            manifest["files"].append({
                "hash": full_hash,
                "original": original,
                "final_name": dest.name,
                "client": client_name,
                "bucket": bucket_name,
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "echoes": [],
            })
            # register the client in the top-level list for empty-client support
            manifest.setdefault("clients", [])
            if client_name != _CLIENT_FALLBACK and client_name not in manifest["clients"]:
                manifest["clients"].append(client_name)
            # seed the client's default taxonomy if this is their first file
            if client_name != _CLIENT_FALLBACK:
                bucket_list = ensure_client_taxonomy(manifest, client_name)
                # if Gemma invented a new bucket, add it to the client's taxonomy
                if bucket_name != _BUCKET_FALLBACK and bucket_name not in bucket_list:
                    bucket_list.append(bucket_name)
            # log the initial classification as training data
            log_training_event({
                "event": "initial",
                "file": dest.name,
                "hash": full_hash,
                "client": client_name,
                "from_bucket": None,
                "to_bucket": bucket_name,
                "gemma_predicted_bucket": d.get("gemma_bucket") or bucket_name,
                "markdown_preview": (d.get("preview") or "")[:200],
            })
            results.append({"id": d["id"], "ok": True, "final_path": str(dest.relative_to(BASE))})

        elif status == "duplicate":
            # Pitch A ("Echoes"): duplicates don't get copied to disk. They become
            # a metadata entry on the canonical file, recording that the same bytes
            # showed up again (and what name the user had for it this time).
            canonical = next((e for e in manifest["files"] if e.get("hash") == full_hash), None)
            if canonical is None:
                # Shouldn't happen if /process flagged it as duplicate, but be safe.
                results.append({"id": d["id"], "ok": False, "error": "no canonical match for echo"})
            else:
                canonical.setdefault("echoes", []).append({
                    "original": original,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": "deposit",
                })
                results.append({
                    "id": d["id"],
                    "ok": True,
                    "echoed_into": canonical["final_name"],
                    "note": f"echo of {canonical['final_name']}",
                })
            # clean up the temp file either way
            try: src.unlink()
            except Exception: pass
        else:  # skip
            results.append({"id": d["id"], "ok": True, "skipped": True})

    _save_manifest(manifest)
    return JSONResponse({"results": results, "sorted_count": len(manifest["files"])})


@router.get("/manifest")
async def get_manifest():
    return JSONResponse(_load_manifest())


@router.get("/files")
async def list_files():
    """Enriched manifest: adds size + disk-verified existence per entry."""
    manifest = _load_manifest()
    out = []
    for e in manifest["files"]:
        p = SORTED / e["final_name"]
        exists = p.is_file()
        echoes = e.get("echoes", []) or []
        out.append({
            **e,
            "client": e.get("client") or _CLIENT_FALLBACK,
            "bucket": e.get("bucket") or _BUCKET_FALLBACK,
            "size": p.stat().st_size if exists else None,
            "exists": exists,
            "url": f"/sorted/{e['final_name']}" if exists else None,
            "echoes": echoes,
            "echo_count": len(echoes),
            "is_duplicate": False,
        })
    # Pull clients from the registered list AND any that have a taxonomy
    # seeded but weren't registered yet (e.g. user created buckets for a
    # client via the drawer before committing any files).
    all_clients = set(manifest.get("clients", [])) | set(manifest.get("client_taxonomies", {}).keys())
    clients = sorted(all_clients)
    taxonomies = {c: client_buckets(manifest, c) for c in clients}
    return JSONResponse({"files": out, "clients": clients, "client_taxonomies": taxonomies})


# ── bucket endpoints ──────────────────────────────────────────────────
@router.post("/bucket/move")
async def bucket_move(payload: dict):
    """Reassign a file to a different bucket. payload = {final_name, bucket}"""
    final_name = payload.get("final_name")
    target = sanitize_bucket(payload.get("bucket") or "")
    if not final_name:
        return JSONResponse({"ok": False, "error": "final_name required"}, status_code=400)
    manifest = _load_manifest()
    entry = next((e for e in manifest["files"] if e["final_name"] == final_name), None)
    if entry is None:
        return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
    from_bucket = entry.get("bucket") or _BUCKET_FALLBACK
    entry["bucket"] = target
    # add to client's taxonomy if it's a new bucket
    client_name = entry.get("client") or _CLIENT_FALLBACK
    if client_name != _CLIENT_FALLBACK:
        blist = ensure_client_taxonomy(manifest, client_name)
        if target != _BUCKET_FALLBACK and target not in blist:
            blist.append(target)
    _save_manifest(manifest)
    log_training_event({
        "event": "reassign",
        "file": final_name,
        "hash": entry.get("hash"),
        "client": client_name,
        "from_bucket": from_bucket,
        "to_bucket": target,
        "gemma_predicted_bucket": entry.get("gemma_predicted_bucket"),
    })
    return JSONResponse({"ok": True, "from_bucket": from_bucket, "to_bucket": target})


@router.post("/bucket/add")
async def bucket_add(payload: dict):
    """Add a new bucket to a client's taxonomy. payload = {client, bucket}
    Also registers the client if it didn't exist — so adding a bucket for a
    brand-new client in the deposit drawer creates the client too."""
    client_name = sanitize_client(payload.get("client") or "")
    bucket = sanitize_bucket(payload.get("bucket") or "")
    if not client_name or client_name == _CLIENT_FALLBACK:
        return JSONResponse({"ok": False, "error": "client required"}, status_code=400)
    if not bucket or bucket == _BUCKET_FALLBACK:
        return JSONResponse({"ok": False, "error": "bucket required"}, status_code=400)
    manifest = _load_manifest()
    manifest.setdefault("clients", [])
    if client_name not in manifest["clients"]:
        manifest["clients"].append(client_name)
    blist = ensure_client_taxonomy(manifest, client_name)
    changed = False
    if bucket not in blist:
        blist.append(bucket)
        changed = True
    _save_manifest(manifest)
    return JSONResponse({"ok": True, "bucket": bucket, "taxonomy": blist, "created_client": changed})


@router.post("/bucket/remove")
async def bucket_remove(payload: dict):
    """Remove an empty bucket from a client's taxonomy."""
    client_name = sanitize_client(payload.get("client") or "")
    bucket = sanitize_bucket(payload.get("bucket") or "")
    manifest = _load_manifest()
    blist = ensure_client_taxonomy(manifest, client_name)
    # refuse to remove if any file still uses this bucket
    in_use = any(e.get("client") == client_name and e.get("bucket") == bucket for e in manifest["files"])
    if in_use:
        return JSONResponse({"ok": False, "error": "bucket has files — reassign them first"}, status_code=400)
    if bucket in blist:
        blist.remove(bucket)
        _save_manifest(manifest)
    return JSONResponse({"ok": True, "taxonomy": blist})


@router.get("/training/export")
async def training_export():
    """Return the bucket-change training log as CSV."""
    from fastapi.responses import Response
    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["at", "event", "file", "hash", "client", "from_bucket", "to_bucket", "gemma_predicted_bucket", "markdown_preview"])
    if TRAINING_LOG.exists():
        for line in TRAINING_LOG.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            writer.writerow([
                e.get("at", ""), e.get("event", ""), e.get("file", ""), e.get("hash", ""),
                e.get("client", ""), e.get("from_bucket") or "", e.get("to_bucket") or "",
                e.get("gemma_predicted_bucket") or "", (e.get("markdown_preview") or "").replace("\n", " ")[:500],
            ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bucket_changes.csv"},
    )


@router.post("/client/create")
async def create_client(payload: dict):
    name = sanitize_client(payload.get("name") or "")
    if not name or name == _CLIENT_FALLBACK:
        return JSONResponse({"ok": False, "error": "invalid client name"}, status_code=400)
    manifest = _load_manifest()
    manifest.setdefault("clients", [])
    if name not in manifest["clients"]:
        manifest["clients"].append(name)
        _save_manifest(manifest)
    return JSONResponse({"ok": True, "name": name})


@router.post("/client/delete")
async def delete_client(payload: dict):
    """Remove an empty client from the list. Files remain; they get their client reset to Unassigned."""
    name = sanitize_client(payload.get("name") or "")
    manifest = _load_manifest()
    manifest.setdefault("clients", [])
    if name in manifest["clients"]:
        manifest["clients"].remove(name)
    # reassign any files pointing at that client to Unassigned
    for e in manifest["files"]:
        if e.get("client") == name:
            e["client"] = _CLIENT_FALLBACK
    _save_manifest(manifest)
    return JSONResponse({"ok": True})


@router.post("/move")
async def move_file(payload: dict):
    """Reassign a file's client. payload = {final_name, client}"""
    final_name = payload.get("final_name")
    client_name = sanitize_client(payload.get("client") or "")
    if not final_name:
        return JSONResponse({"ok": False, "error": "final_name required"}, status_code=400)
    manifest = _load_manifest()
    hit = False
    for e in manifest["files"]:
        if e["final_name"] == final_name:
            e["client"] = client_name
            hit = True
            break
    if not hit:
        return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
    manifest.setdefault("clients", [])
    if client_name != _CLIENT_FALLBACK and client_name not in manifest["clients"]:
        manifest["clients"].append(client_name)
    _save_manifest(manifest)
    return JSONResponse({"ok": True, "client": client_name})


@router.post("/rename")
async def rename_file(payload: dict):
    """Rename an already-sorted file. payload = {final_name, new_name}"""
    final_name = payload.get("final_name")
    new_stem = sanitize_snake(payload.get("new_name") or "", final_name or "")
    if not final_name or not new_stem:
        return JSONResponse({"ok": False, "error": "bad input"}, status_code=400)
    src = SORTED / final_name
    if not src.is_file():
        return JSONResponse({"ok": False, "error": "source missing"}, status_code=404)
    ext = src.suffix
    dest = resolve_collision(SORTED, new_stem, ext)
    src.rename(dest)
    manifest = _load_manifest()
    for e in manifest["files"]:
        if e["final_name"] == final_name:
            e["final_name"] = dest.name
            break
    _save_manifest(manifest)
    return JSONResponse({"ok": True, "final_name": dest.name, "url": f"/sorted/{dest.name}"})


# ── UI ────────────────────────────────────────────────────────────────
_SORTER_HTML_OLD_DISABLED = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>File sorter · MinerU + Gemma</title>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #141820;
    --panel-2: #1b2029;
    --panel-3: #232935;
    --border: #262d3a;
    --border-2: #333a48;
    --fg: #edf0f6;
    --muted: #8691a6;
    --subtle: #5d6578;
    --accent: #8ab4ff;
    --accent-2: #b394ff;
    --ok: #6bd68a;
    --warn: #f2c14e;
    --err: #ff7a85;
    --dup: #c78aff;
    --shadow: 0 10px 40px -12px rgba(0,0,0,0.5);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; }
  body { background: radial-gradient(1200px 600px at 50% -100px, rgba(138,180,255,0.08), transparent), var(--bg); }
  .nav { display: flex; align-items: center; gap: 20px; padding: 14px 28px; border-bottom: 1px solid var(--border); }
  .nav a { color: var(--muted); text-decoration: none; font-size: 13px; padding: 6px 10px; border-radius: 6px; }
  .nav a:hover { color: var(--fg); background: var(--panel); }
  .nav a.active { color: var(--fg); background: var(--panel-2); }
  .brand { font-weight: 600; letter-spacing: .5px; }
  .brand span { color: var(--accent); }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 28px; }
  h1 { font-size: 24px; font-weight: 700; margin: 0 0 4px; letter-spacing: -.02em; }
  .subtitle { color: var(--muted); font-size: 14px; margin: 0 0 24px; }
  .subtitle kbd { background: var(--panel-2); border: 1px solid var(--border); padding: 1px 6px; border-radius: 4px; font-size: 12px; }

  .drop {
    position: relative; border: 2px dashed var(--border-2); border-radius: 14px;
    padding: 48px 24px; text-align: center; cursor: pointer;
    background: linear-gradient(180deg, var(--panel), var(--panel-2));
    transition: all .18s ease; margin-bottom: 20px;
  }
  .drop:hover, .drop.hover {
    border-color: var(--accent); background: linear-gradient(180deg, #172136, #13182b);
    transform: translateY(-1px);
  }
  .drop .icon {
    width: 44px; height: 44px; border-radius: 12px; margin: 0 auto 12px;
    background: linear-gradient(135deg, rgba(138,180,255,0.2), rgba(179,148,255,0.2));
    display: grid; place-items: center; font-size: 22px; color: var(--accent);
  }
  .drop strong { display: block; font-size: 16px; margin-bottom: 2px; }
  .drop .hint { color: var(--muted); font-size: 13px; }
  .drop.compact { padding: 14px; display: flex; align-items: center; gap: 12px; text-align: left; }
  .drop.compact .icon { width: 32px; height: 32px; font-size: 16px; margin: 0; }
  .drop.compact strong { font-size: 13px; }
  .drop.compact .hint { font-size: 12px; }

  .toolbar { display: flex; gap: 10px; align-items: center; margin: 16px 0 12px; flex-wrap: wrap; }
  .toolbar .spacer { flex: 1; }
  .summary { color: var(--muted); font-size: 13px; }
  .summary b { color: var(--fg); }

  button, .btn {
    display: inline-flex; align-items: center; gap: 6px; font: inherit;
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; cursor: pointer; transition: all .12s ease;
  }
  button:hover:not(:disabled) { background: var(--panel-3); border-color: var(--border-2); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary { background: linear-gradient(180deg, #8ab4ff, #5e8bff); color: #0b1020; border-color: #5e8bff; font-weight: 600; box-shadow: 0 2px 8px rgba(94,139,255,0.25); }
  button.primary:hover:not(:disabled) { filter: brightness(1.1); }
  button.ghost { background: transparent; border-color: transparent; color: var(--muted); padding: 4px 8px; }
  button.ghost:hover { background: var(--panel-2); color: var(--fg); }

  .grid {
    display: grid;
    grid-template-columns: 28px minmax(200px, 1.2fr) 120px minmax(220px, 1.5fr) 160px 80px;
    gap: 0;
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    background: var(--panel); box-shadow: var(--shadow);
  }
  .grid .head, .grid .cell {
    padding: 10px 14px; border-bottom: 1px solid var(--border); min-width: 0;
    display: flex; align-items: center; gap: 6px;
  }
  .grid .head {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--subtle); background: var(--panel-2); border-bottom-color: var(--border-2);
  }
  .grid .row { display: contents; }
  .grid .row:hover .cell { background: rgba(138,180,255,0.03); }
  .grid .row.accepted .cell { background: rgba(107,214,138,0.05); }
  .grid .row.skipped .cell { opacity: 0.5; }
  .grid .row:last-child .cell { border-bottom: none; }

  .truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-weight: 500; padding: 3px 8px; border-radius: 999px;
    background: var(--panel-3); color: var(--muted); border: 1px solid var(--border-2);
  }
  .badge .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .badge.queued { color: var(--muted); }
  .badge.extracting, .badge.naming { color: var(--warn); border-color: rgba(242,193,78,0.3); background: rgba(242,193,78,0.08); }
  .badge.extracting .dot, .badge.naming .dot { animation: pulse 1s ease-in-out infinite; }
  .badge.ready { color: var(--accent); border-color: rgba(138,180,255,0.3); background: rgba(138,180,255,0.08); }
  .badge.duplicate { color: var(--dup); border-color: rgba(199,138,255,0.3); background: rgba(199,138,255,0.08); }
  .badge.error { color: var(--err); border-color: rgba(255,122,133,0.3); background: rgba(255,122,133,0.08); }
  .badge.accepted { color: var(--ok); border-color: rgba(107,214,138,0.3); background: rgba(107,214,138,0.08); }

  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

  input[type="text"].name-input {
    flex: 1; background: var(--panel-2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px;
    font: 13px ui-monospace, "SF Mono", Menlo, monospace; min-width: 0;
  }
  input[type="text"].name-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(138,180,255,0.15); }

  .actions { display: flex; gap: 4px; justify-content: flex-end; }
  .act-btn {
    padding: 4px 8px; font-size: 11px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--muted); cursor: pointer;
  }
  .act-btn:hover { color: var(--fg); border-color: var(--border-2); }
  .act-btn.preview-open { color: var(--accent); border-color: var(--accent); }

  .preview {
    grid-column: 1 / -1;
    padding: 16px 24px; background: var(--panel-2); border-top: 1px dashed var(--border-2);
    border-bottom: 1px solid var(--border); font: 12px/1.55 ui-monospace, Menlo, monospace;
    color: var(--muted); max-height: 260px; overflow: auto; white-space: pre-wrap;
  }
  .preview .label { font: 11px -apple-system; color: var(--subtle); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; display: block; }
  .hash { font: 11px ui-monospace, Menlo, monospace; color: var(--subtle); }
  .dup-note { font-size: 12px; color: var(--dup); }

  .empty { padding: 40px; text-align: center; color: var(--muted); }
  .empty h2 { font-weight: 500; color: var(--fg); margin: 0 0 6px; font-size: 15px; }

  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--panel-2); border: 1px solid var(--border-2); color: var(--fg);
    padding: 10px 16px; border-radius: 10px; box-shadow: var(--shadow); font-size: 13px;
    opacity: 0; transition: opacity .2s ease; pointer-events: none; z-index: 100; }
  .toast.show { opacity: 1; }

  details.preview-wrap summary { list-style: none; cursor: pointer; }
  details.preview-wrap summary::-webkit-details-marker { display: none; }
</style>
</head>
<body>

<div class="nav">
  <span class="brand">major<span>/</span>miner</span>
  <a href="/">Playground</a>
  <a href="/sorter" class="active">File sorter</a>
</div>

<div class="wrap">
  <h1>Sort a pile of documents</h1>
  <p class="subtitle">
    Drop PDFs, DOCX, or images. MinerU reads the first 3 pages, Gemma 4 E4B suggests a <kbd>snake_case</kbd> name,
    you approve, they land in <kbd>sorted/</kbd>. Duplicates are quarantined in <kbd>sorted/duplicates/</kbd>.
  </p>

  <div id="drop" class="drop">
    <div class="icon">⇲</div>
    <strong>Drop files here</strong>
    <div class="hint">or click to pick — PDFs, DOCX, PPTX, XLSX, images</div>
    <input id="file" type="file" multiple style="display:none"
      accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.webp,.tif,.tiff" />
  </div>

  <div class="toolbar" id="toolbar" style="display:none">
    <span class="summary" id="summary"></span>
    <span class="spacer"></span>
    <button id="process">Process all</button>
    <button id="clear" class="ghost">Clear</button>
    <button id="apply" class="primary" disabled>Apply accepted →</button>
  </div>

  <div id="table-wrap" style="display:none">
    <div class="grid" id="grid">
      <div class="head">☑</div>
      <div class="head">Original</div>
      <div class="head">Status</div>
      <div class="head">Suggested name</div>
      <div class="head">Preview</div>
      <div class="head" style="justify-content:flex-end">Skip</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file");
const toolbar = $("toolbar"), summaryEl = $("summary");
const tableWrap = $("table-wrap"), grid = $("grid");
const processBtn = $("process"), applyBtn = $("apply"), clearBtn = $("clear");
const toast = $("toast");

const rows = new Map(); // id -> row state

function showToast(msg, ms=2500) {
  toast.textContent = msg;
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), ms);
}

function formatSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  return (n/1024/1024).toFixed(2) + " MB";
}

function updateUI() {
  const n = rows.size;
  toolbar.style.display = n ? "flex" : "none";
  tableWrap.style.display = n ? "block" : "none";
  drop.classList.toggle("compact", n > 0);
  if (n > 0) {
    drop.querySelector("strong").textContent = "+ Add more files";
    drop.querySelector(".hint").textContent = "drop to append";
  } else {
    drop.querySelector("strong").textContent = "Drop files here";
    drop.querySelector(".hint").textContent = "or click to pick — PDFs, DOCX, PPTX, XLSX, images";
  }

  const ready = [...rows.values()].filter(r => r.status === "ready" && r.accepted !== false).length;
  const dup = [...rows.values()].filter(r => r.status === "duplicate" && r.accepted !== false).length;
  const total = rows.size;
  const processed = [...rows.values()].filter(r => ["ready","duplicate","error"].includes(r.status)).length;
  summaryEl.innerHTML = `<b>${total}</b> file${total !== 1 ? "s" : ""}  ·  processed <b>${processed}</b>  ·  accepting <b>${ready}</b> + <b>${dup}</b> dup${dup !== 1 ? "s" : ""}`;
  applyBtn.disabled = (ready + dup) === 0;
  processBtn.disabled = [...rows.values()].every(r => r.status !== "queued");
}

function addFiles(fileList) {
  for (const f of fileList) {
    const id = "loc-" + Math.random().toString(36).slice(2, 10);
    rows.set(id, { id, file: f, name: f.name, size: f.size, status: "queued", accepted: true });
    renderRow(id);
  }
  updateUI();
}

function renderRow(id) {
  const r = rows.get(id);
  let el = document.getElementById("row-" + id);
  if (!el) {
    el = document.createElement("div");
    el.className = "row";
    el.id = "row-" + id;
    el.innerHTML = `
      <div class="cell"><input type="checkbox" checked data-role="accept"/></div>
      <div class="cell"><div style="min-width:0"><div class="truncate" style="font-weight:500"></div>
        <div class="hash"></div></div></div>
      <div class="cell"><span class="badge queued" data-role="badge"><span class="dot"></span>queued</span></div>
      <div class="cell"><input type="text" class="name-input" data-role="name" placeholder="—" disabled/></div>
      <div class="cell"><button class="act-btn" data-role="preview" disabled>preview</button></div>
      <div class="cell actions"><button class="act-btn" data-role="skip" title="Skip this file">✕</button></div>
      <div class="preview" data-role="preview-pane" style="display:none"></div>
    `;
    grid.appendChild(el);

    el.querySelector('[data-role="accept"]').addEventListener("change", (e) => {
      r.accepted = e.target.checked;
      el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
      el.classList.toggle("skipped", !r.accepted);
      updateUI();
    });
    el.querySelector('[data-role="name"]').addEventListener("input", (e) => {
      r.final_name = e.target.value;
    });
    el.querySelector('[data-role="preview"]').addEventListener("click", () => togglePreview(id));
    el.querySelector('[data-role="skip"]').addEventListener("click", () => {
      rows.delete(id);
      el.remove();
      updateUI();
    });
  }

  el.querySelector(".truncate").textContent = r.name;
  el.querySelector(".hash").innerHTML = r.size != null ? (formatSize(r.size) + (r.hash ? " · " + r.hash : "")) : "";

  const badge = el.querySelector('[data-role="badge"]');
  badge.className = "badge " + r.status;
  const labelMap = { queued: "queued", extracting: "extracting", naming: "naming", ready: "ready", duplicate: "duplicate", error: "error", accepted: "accepted" };
  badge.innerHTML = '<span class="dot"></span>' + labelMap[r.status] + (r.status === "duplicate" && r.dup_of ? " · " + r.dup_of : "") + (r.status === "error" ? " · " + (r.error || "") : "");

  const nameInput = el.querySelector('[data-role="name"]');
  nameInput.value = r.final_name ?? (r.suggested || "");
  nameInput.disabled = !(r.status === "ready" || r.status === "duplicate" || r.status === "error");

  const prevBtn = el.querySelector('[data-role="preview"]');
  prevBtn.disabled = !r.preview;

  el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
  el.classList.toggle("skipped", !r.accepted);
}

function togglePreview(id) {
  const el = document.getElementById("row-" + id);
  const pane = el.querySelector('[data-role="preview-pane"]');
  const btn = el.querySelector('[data-role="preview"]');
  if (pane.style.display === "none") {
    const r = rows.get(id);
    pane.innerHTML = '<span class="label">First-3-pages markdown</span>' + (r.preview || "(empty)");
    pane.style.display = "block";
    btn.classList.add("preview-open");
  } else {
    pane.style.display = "none";
    btn.classList.remove("preview-open");
  }
}

// drop-zone
drop.addEventListener("click", () => fileInput.click());
["dragenter","dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("hover"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", ev => { addFiles(ev.dataTransfer.files); });
fileInput.addEventListener("change", ev => { addFiles(ev.target.files); fileInput.value = ""; });

clearBtn.addEventListener("click", () => {
  if (!confirm("Clear all files from the queue?")) return;
  rows.clear();
  grid.querySelectorAll(".row").forEach(e => e.remove());
  updateUI();
});

// process — upload queued files and stream results
processBtn.addEventListener("click", async () => {
  const queued = [...rows.values()].filter(r => r.status === "queued");
  if (!queued.length) return;
  processBtn.disabled = true;

  const fd = new FormData();
  const localIdsByIndex = [];
  for (const r of queued) {
    fd.append("files", r.file, r.name);
    localIdsByIndex.push(r.id);
    r.status = "extracting"; renderRow(r.id);
  }
  updateUI();

  let resp;
  try {
    resp = await fetch("/sorter/process", { method: "POST", body: fd });
  } catch (e) { showToast("network error"); processBtn.disabled = false; return; }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let serverIdx = -1;
  const idMap = {}; // server_id -> local_id
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
      try { ev = JSON.parse(raw); } catch { continue; }

      if (ev.type === "row_start") {
        serverIdx++;
        const localId = localIdsByIndex[serverIdx];
        idMap[ev.id] = localId;
        const r = rows.get(localId);
        r.server_id = ev.id;
        r.hash_full = ev.hash; // short hash display
        r.hash = ev.hash;
        r.status = "extracting";
        renderRow(localId);
      } else if (ev.type === "row_step") {
        const localId = idMap[ev.id];
        const r = rows.get(localId);
        r.status = ev.step;
        renderRow(localId);
      } else if (ev.type === "row_done") {
        const localId = idMap[ev.id];
        const r = rows.get(localId);
        r.status = ev.status;
        r.suggested = ev.suggested;
        r.final_name = ev.suggested;
        r.preview = ev.preview;
        r.dup_of = ev.dup_of;
        r.error = ev.error;
        r.temp_path = ev.temp_path;
        // store full hash from server response if present (kept short for display)
        if (ev.hash) r.hash = ev.hash;
        renderRow(localId);
      }
      updateUI();
    }
  }
  updateUI();
});

// apply — POST decisions, copy files into sorted/
applyBtn.addEventListener("click", async () => {
  const decisions = [];
  for (const r of rows.values()) {
    if (!r.temp_path) continue;
    if (!r.accepted) { decisions.push({ id: r.server_id, temp_path: r.temp_path, original: r.name, hash: r.hash, status: "skip" }); continue; }
    if (r.status === "duplicate") {
      decisions.push({ id: r.server_id, temp_path: r.temp_path, original: r.name, hash: r.hash, status: "duplicate", dup_of: r.dup_of });
    } else if (r.status === "ready" || r.status === "error") {
      decisions.push({ id: r.server_id, temp_path: r.temp_path, original: r.name, hash: r.hash, status: "accept", final_name: r.final_name || r.suggested });
    }
  }
  if (!decisions.filter(d => d.status !== "skip").length) return;

  applyBtn.disabled = true;
  const resp = await fetch("/sorter/apply", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions }),
  });
  const data = await resp.json();
  let ok = 0, dupes = 0, errs = 0;
  for (const res of data.results) {
    if (res.ok && res.note) dupes++;
    else if (res.ok && !res.skipped) ok++;
    else if (!res.ok) errs++;
    if (res.ok && res.final_path) {
      // mark row as accepted
      for (const r of rows.values()) {
        if (r.server_id === res.id) { r.status = "accepted"; r.final_path = res.final_path; renderRow(r.id); break; }
      }
    }
  }
  showToast(`Sorted ${ok} · duplicates ${dupes} · errors ${errs}  →  sorted/  (total ${data.sorted_count})`);
  updateUI();
  applyBtn.disabled = false;
});

updateUI();
</script>
</body>
</html>
"""


_SORTER_HTML_V2_DISABLED = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Sorter · MinerU + Gemma</title>
<style>
  :root {
    --bg: #0a0c12;
    --bg-2: #0d1019;
    --panel: #12161f;
    --panel-2: #191e2a;
    --panel-3: #212837;
    --panel-hover: #1e2431;
    --border: #222938;
    --border-2: #2e3648;
    --border-3: #3b445a;
    --fg: #eef1f7;
    --fg-2: #c9cfdc;
    --muted: #8591a7;
    --subtle: #5b6478;
    --accent: #8ab4ff;
    --accent-2: #b394ff;
    --accent-glow: rgba(138,180,255,0.15);
    --ok: #6bd68a;
    --warn: #f2c14e;
    --err: #ff7a85;
    --dup: #c78aff;
    --shadow-lg: 0 20px 60px -20px rgba(0,0,0,0.6), 0 4px 16px -4px rgba(0,0,0,0.4);
    --shadow-md: 0 6px 20px -6px rgba(0,0,0,0.4);
    --radius: 12px;
    --radius-sm: 8px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
  body {
    background:
      radial-gradient(900px 500px at 15% -50px, rgba(138,180,255,0.06), transparent 60%),
      radial-gradient(900px 500px at 85% -50px, rgba(179,148,255,0.05), transparent 60%),
      var(--bg);
    min-height: 100vh;
  }
  a { color: inherit; text-decoration: none; }
  button { font: inherit; }

  /* ─── top nav ───────────────────────────────────────── */
  .topbar {
    display: flex; align-items: center; gap: 24px;
    padding: 12px 28px; border-bottom: 1px solid var(--border);
    background: rgba(10,12,18,0.8); backdrop-filter: blur(12px);
    position: sticky; top: 0; z-index: 50;
  }
  .brand {
    font-weight: 650; letter-spacing: -.01em; font-size: 15px;
    display: inline-flex; align-items: center; gap: 8px;
  }
  .brand .logo {
    width: 22px; height: 22px; border-radius: 6px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: grid; place-items: center; color: #0a0c12; font-weight: 800; font-size: 12px;
  }
  .brand span.muted { color: var(--muted); font-weight: 400; }
  .tabs { display: flex; gap: 2px; }
  .tab {
    padding: 7px 14px; border-radius: 7px; color: var(--muted);
    cursor: pointer; font-size: 13px; font-weight: 500;
    transition: all .12s ease; border: 1px solid transparent;
  }
  .tab:hover { color: var(--fg-2); background: var(--panel); }
  .tab.active { color: var(--fg); background: var(--panel-2); border-color: var(--border); }
  .tab .count {
    display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
    background: var(--panel-3); color: var(--muted); font-size: 11px; font-weight: 600;
  }
  .tab.active .count { background: var(--accent-glow); color: var(--accent); }
  .nav-spacer { flex: 1; }
  .nav-ext { color: var(--muted); font-size: 12px; padding: 4px 10px; border-radius: 6px; }
  .nav-ext:hover { color: var(--fg); background: var(--panel); }

  /* ─── page wrap ─────────────────────────────────────── */
  .page { max-width: 1240px; margin: 0 auto; padding: 32px 28px 80px; }
  .view { display: none; }
  .view.active { display: block; animation: fadeIn .2s ease-out; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

  h1 { font-size: 26px; font-weight: 700; margin: 0 0 6px; letter-spacing: -.02em; }
  .lede { color: var(--muted); font-size: 14px; max-width: 680px; margin: 0 0 28px; }
  .lede kbd {
    background: var(--panel-2); border: 1px solid var(--border);
    padding: 1.5px 6px; border-radius: 4px; font-size: 12px; color: var(--fg-2);
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }

  /* ─── drop zone ─────────────────────────────────────── */
  .drop {
    position: relative; border: 2px dashed var(--border-2); border-radius: 16px;
    padding: 56px 24px; text-align: center; cursor: pointer;
    background:
      radial-gradient(400px 200px at 50% -20px, rgba(138,180,255,0.08), transparent 70%),
      linear-gradient(180deg, var(--panel), var(--panel-2));
    transition: all .2s cubic-bezier(.2,.8,.2,1); margin-bottom: 20px;
  }
  .drop:hover, .drop.hover {
    border-color: var(--accent); transform: translateY(-2px);
    box-shadow: var(--shadow-md);
    background:
      radial-gradient(400px 240px at 50% -20px, rgba(138,180,255,0.14), transparent 70%),
      linear-gradient(180deg, #172136, #141a2b);
  }
  .drop .icon {
    width: 56px; height: 56px; border-radius: 14px; margin: 0 auto 16px;
    background: linear-gradient(135deg, rgba(138,180,255,0.25), rgba(179,148,255,0.25));
    border: 1px solid rgba(138,180,255,0.4);
    display: grid; place-items: center; font-size: 26px; color: var(--accent);
    transition: transform .2s ease;
  }
  .drop:hover .icon, .drop.hover .icon { transform: scale(1.05); }
  .drop strong { display: block; font-size: 17px; margin-bottom: 4px; letter-spacing: -.01em; }
  .drop .hint { color: var(--muted); font-size: 13px; }
  .drop .or { display: inline-flex; align-items: center; gap: 10px; margin-top: 14px; color: var(--subtle); font-size: 12px; }
  .drop .or:before, .drop .or:after { content: ""; width: 40px; height: 1px; background: var(--border-2); }
  .drop .pick-folder {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 14px; border-radius: 8px; background: var(--panel-3); color: var(--fg-2);
    border: 1px solid var(--border-2); font-size: 12px; margin-top: 12px;
    cursor: pointer; transition: all .12s ease;
  }
  .drop .pick-folder:hover { background: var(--panel-hover); color: var(--fg); border-color: var(--border-3); }

  .drop.compact {
    padding: 16px 20px; display: flex; align-items: center; gap: 14px; text-align: left;
  }
  .drop.compact .icon { width: 36px; height: 36px; font-size: 18px; margin: 0; border-radius: 10px; }
  .drop.compact strong { font-size: 14px; }
  .drop.compact .hint { font-size: 12px; }
  .drop.compact .or, .drop.compact .pick-folder { display: none; }

  /* ─── toolbar ───────────────────────────────────────── */
  .toolbar {
    display: flex; gap: 10px; align-items: center; margin: 18px 0 14px; flex-wrap: wrap;
    padding: 14px 16px; background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow-md);
  }
  .toolbar .spacer { flex: 1; }
  .summary { color: var(--muted); font-size: 13px; display: flex; gap: 14px; align-items: center; }
  .summary b { color: var(--fg); font-weight: 600; }
  .summary .sep { color: var(--border-3); }
  .progress-bar {
    height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; width: 140px;
  }
  .progress-bar .fill {
    height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2));
    width: 0%; transition: width .3s ease;
  }

  /* ─── buttons ───────────────────────────────────────── */
  .btn {
    display: inline-flex; align-items: center; gap: 6px; font: inherit;
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: 8px 14px; cursor: pointer; transition: all .12s ease;
    font-size: 13px; font-weight: 500;
  }
  .btn:hover:not(:disabled) { background: var(--panel-hover); border-color: var(--border-2); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn.primary {
    background: linear-gradient(180deg, var(--accent), #6597ff);
    color: #0a0c12; border-color: #6597ff; font-weight: 650;
    box-shadow: 0 2px 10px rgba(94,139,255,0.3), inset 0 1px 0 rgba(255,255,255,0.2);
  }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.08); transform: translateY(-1px); box-shadow: 0 4px 14px rgba(94,139,255,0.4); }
  .btn.ghost { background: transparent; border-color: transparent; color: var(--muted); }
  .btn.ghost:hover { background: var(--panel-2); color: var(--fg); }
  .btn.danger { color: var(--err); }
  .btn.danger:hover:not(:disabled) { background: rgba(255,122,133,0.08); border-color: rgba(255,122,133,0.3); }

  /* ─── review table ──────────────────────────────────── */
  .table {
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
    overflow: hidden; box-shadow: var(--shadow-md);
  }
  .table .head, .table .row {
    display: grid;
    grid-template-columns: 36px minmax(200px,1.3fr) 130px minmax(220px,1.5fr) 150px 60px;
    gap: 0; align-items: center;
  }
  .table .head {
    padding: 12px 16px; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--subtle); background: var(--panel-2); border-bottom: 1px solid var(--border-2);
    font-weight: 600;
  }
  .table .row {
    padding: 12px 16px; border-bottom: 1px solid var(--border); transition: background .12s ease;
    position: relative;
  }
  .table .row:last-child { border-bottom: none; }
  .table .row:hover { background: rgba(138,180,255,0.02); }
  .table .row.accepted { background: rgba(107,214,138,0.04); }
  .table .row.skipped { opacity: 0.45; }
  .table .row.expanded { background: var(--panel-2); }

  .cell { min-width: 0; display: flex; align-items: center; gap: 8px; }
  .cell.actions { justify-content: flex-end; }

  .filename { min-width: 0; }
  .filename .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--fg); }
  .filename .meta { font-size: 11px; color: var(--subtle); margin-top: 2px; font-family: ui-monospace, Menlo, monospace; }

  /* ─── badges ────────────────────────────────────────── */
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-weight: 500; padding: 3px 9px; border-radius: 999px;
    background: var(--panel-3); color: var(--muted); border: 1px solid var(--border-2);
    white-space: nowrap;
  }
  .badge .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .badge.queued { color: var(--muted); }
  .badge.extracting, .badge.naming, .badge.hashing {
    color: var(--warn); border-color: rgba(242,193,78,0.3); background: rgba(242,193,78,0.08);
  }
  .badge.extracting .dot, .badge.naming .dot, .badge.hashing .dot {
    animation: pulse 1s ease-in-out infinite;
  }
  .badge.ready { color: var(--accent); border-color: rgba(138,180,255,0.3); background: rgba(138,180,255,0.08); }
  .badge.duplicate { color: var(--dup); border-color: rgba(199,138,255,0.3); background: rgba(199,138,255,0.08); }
  .badge.error { color: var(--err); border-color: rgba(255,122,133,0.3); background: rgba(255,122,133,0.08); }
  .badge.accepted { color: var(--ok); border-color: rgba(107,214,138,0.3); background: rgba(107,214,138,0.08); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

  /* ─── name input ────────────────────────────────────── */
  .name-input {
    flex: 1; background: var(--panel-2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px;
    font: 13px ui-monospace, "SF Mono", Menlo, monospace; min-width: 0;
    transition: all .12s ease;
  }
  .name-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); background: var(--panel-3); }
  .name-input:disabled { opacity: 0.5; }

  /* ─── row actions ───────────────────────────────────── */
  .act-btn {
    padding: 4px 9px; font-size: 11px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--muted); cursor: pointer; transition: all .12s ease;
  }
  .act-btn:hover { color: var(--fg); border-color: var(--border-2); background: var(--panel-2); }
  .act-btn.active { color: var(--accent); border-color: var(--accent); background: var(--accent-glow); }
  .act-btn.icon { padding: 4px 7px; }

  /* ─── inline preview ────────────────────────────────── */
  .preview-pane {
    padding: 20px 24px; background: var(--panel-2); border-top: 1px dashed var(--border-2);
    border-bottom: 1px solid var(--border);
    font: 12px/1.6 ui-monospace, "SF Mono", Menlo, monospace; color: var(--muted);
    max-height: 320px; overflow: auto; white-space: pre-wrap;
  }
  .preview-pane .label {
    display: block; margin-bottom: 10px; font: 11px -apple-system; color: var(--subtle);
    text-transform: uppercase; letter-spacing: .08em; font-weight: 600;
  }

  /* ─── sorted grid ───────────────────────────────────── */
  .stats {
    display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 22px;
  }
  .stat {
    flex: 1; min-width: 160px; padding: 16px 18px; background: var(--panel);
    border: 1px solid var(--border); border-radius: var(--radius);
  }
  .stat .label {
    font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--subtle); font-weight: 600;
  }
  .stat .value { font-size: 24px; font-weight: 700; margin-top: 4px; letter-spacing: -.02em; }
  .stat .value.accent { color: var(--accent); }
  .stat .value.dup { color: var(--dup); }
  .stat .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

  .search-bar {
    display: flex; gap: 10px; margin-bottom: 18px; align-items: center;
  }
  .search {
    flex: 1; position: relative;
  }
  .search input {
    width: 100%; background: var(--panel); color: var(--fg);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 10px 14px 10px 38px; font: inherit; font-size: 13px;
    transition: all .12s ease;
  }
  .search input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
  .search .ic {
    position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
    color: var(--subtle); font-size: 14px; pointer-events: none;
  }

  .cards {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 14px;
  }
  .card {
    position: relative;
    padding: 16px; background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); cursor: pointer; transition: all .15s ease;
    text-decoration: none; color: inherit; display: flex; flex-direction: column;
    min-height: 110px;
  }
  .card:hover {
    transform: translateY(-2px); border-color: var(--accent);
    box-shadow: 0 10px 30px -10px rgba(138,180,255,0.25);
    background: var(--panel-2);
  }
  .card.is-dup { border-left: 3px solid var(--dup); }
  .card.is-dup:hover { border-color: var(--dup); box-shadow: 0 10px 30px -10px rgba(199,138,255,0.25); }
  .card .fname {
    font: 13px ui-monospace, "SF Mono", Menlo, monospace;
    font-weight: 600; color: var(--fg); word-break: break-all; line-height: 1.4;
  }
  .card .cmeta {
    margin-top: auto; padding-top: 10px; display: flex; gap: 10px; align-items: center;
    font-size: 11px; color: var(--muted);
  }
  .card .cmeta .size { font-family: ui-monospace, Menlo, monospace; color: var(--subtle); }
  .card .cmeta .ago { color: var(--muted); }
  .card .dupTag {
    position: absolute; top: 10px; right: 10px;
    font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
    padding: 2px 7px; border-radius: 4px; background: rgba(199,138,255,0.15); color: var(--dup); font-weight: 600;
  }

  /* ─── custom hover tooltip for sorted cards ─────────── */
  .tip {
    position: fixed; pointer-events: none;
    background: var(--panel-2); border: 1px solid var(--border-2);
    border-radius: var(--radius-sm); padding: 14px 16px;
    box-shadow: var(--shadow-lg); min-width: 280px; max-width: 380px;
    z-index: 200; opacity: 0; transform: translateY(4px); transition: opacity .12s ease, transform .12s ease;
  }
  .tip.show { opacity: 1; transform: none; }
  .tip .tip-label {
    font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--subtle); font-weight: 700; margin-bottom: 4px;
  }
  .tip .tip-original {
    font: 13px ui-monospace, Menlo, monospace; color: var(--fg); margin-bottom: 14px; word-break: break-all; font-weight: 500;
  }
  .tip .tip-row { display: flex; justify-content: space-between; font-size: 12px; gap: 12px; margin-top: 6px; }
  .tip .tip-row .k { color: var(--subtle); }
  .tip .tip-row .v { color: var(--fg-2); font-family: ui-monospace, Menlo, monospace; text-align: right; }
  .tip .tip-sep { height: 1px; background: var(--border); margin: 10px 0; }

  /* ─── empty state ───────────────────────────────────── */
  .empty {
    padding: 60px 30px; text-align: center; background: var(--panel);
    border: 1px dashed var(--border-2); border-radius: var(--radius); color: var(--muted);
  }
  .empty .ico {
    width: 56px; height: 56px; border-radius: 14px; margin: 0 auto 16px;
    background: var(--panel-2); border: 1px solid var(--border-2); display: grid; place-items: center;
    font-size: 26px; color: var(--subtle);
  }
  .empty h2 { color: var(--fg); margin: 0 0 6px; font-size: 16px; font-weight: 600; }
  .empty p { margin: 0; font-size: 13px; }
  .empty .btn { margin-top: 16px; }

  /* ─── toast ─────────────────────────────────────────── */
  .toast {
    position: fixed; bottom: 24px; left: 50%; transform: translate(-50%, 12px);
    background: var(--panel-2); border: 1px solid var(--border-2); color: var(--fg);
    padding: 11px 18px; border-radius: 10px; box-shadow: var(--shadow-lg); font-size: 13px;
    opacity: 0; transition: all .2s cubic-bezier(.2,.8,.2,1); pointer-events: none; z-index: 200;
    display: inline-flex; align-items: center; gap: 10px;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .toast .ok { color: var(--ok); }
  .toast .err { color: var(--err); }

  /* ─── row preview expansion ─────────────────────────── */
  .preview-wrap { display: none; }
  .row.expanded + .preview-wrap { display: block; }

  @media (max-width: 760px) {
    .table .head, .table .row { grid-template-columns: 36px 1fr 110px; grid-template-rows: auto auto; }
    .table .head > div:nth-child(4), .table .head > div:nth-child(5), .table .head > div:nth-child(6) { display: none; }
    .cell.name-cell { grid-column: 2 / 4; grid-row: 2; }
    .cell.preview-cell, .cell.actions { grid-column: 3; grid-row: 1; justify-content: flex-end; }
  }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand"><span class="logo">M</span>major<span class="muted">/miner</span></div>
  <div class="tabs">
    <div class="tab active" data-view="upload">Upload <span class="count" id="count-upload">0</span></div>
    <div class="tab" data-view="sorted">Sorted files <span class="count" id="count-sorted">0</span></div>
  </div>
  <div class="nav-spacer"></div>
  <a class="nav-ext" href="/">MinerU playground ↗</a>
</div>

<div class="page">

  <!-- ============ UPLOAD VIEW ============ -->
  <div class="view active" id="view-upload">
    <h1>Sort a pile of documents</h1>
    <p class="lede">
      Drop files, AI reads the first 3 pages, Gemma names them in <kbd>snake_case</kbd>,
      you review and approve, they land in <kbd>sorted/</kbd>. Duplicates are quarantined.
      Everything runs on this Mac — nothing leaves your machine.
    </p>

    <div id="drop" class="drop">
      <div class="icon">⇲</div>
      <strong>Drop files or folders here</strong>
      <div class="hint">PDFs, DOCX, PPTX, XLSX, images — as many as you like</div>
      <input id="file" type="file" multiple style="display:none"
        accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.webp,.tif,.tiff" />
      <input id="folder" type="file" webkitdirectory directory multiple style="display:none" />
      <div class="or">or</div>
      <label for="folder" class="pick-folder" onclick="event.stopPropagation()">📁 Pick a folder</label>
    </div>

    <div class="toolbar" id="toolbar" style="display:none">
      <span class="summary" id="summary"></span>
      <div class="progress-bar" id="progress" style="display:none"><div class="fill" id="progress-fill"></div></div>
      <span class="spacer"></span>
      <button class="btn" id="process">Process all</button>
      <button class="btn ghost danger" id="clear">Clear</button>
      <button class="btn primary" id="apply" disabled>Apply accepted →</button>
    </div>

    <div id="table-wrap" style="display:none">
      <div class="table" id="table">
        <div class="head">
          <div>☑</div>
          <div>File</div>
          <div>Status</div>
          <div>Suggested name</div>
          <div>Preview</div>
          <div style="justify-content:flex-end; display:flex"></div>
        </div>
      </div>
    </div>

    <div class="empty" id="empty-upload" style="display:none">
      <div class="ico">📄</div>
      <h2>No files yet</h2>
      <p>Drop files above to get started</p>
    </div>
  </div>

  <!-- ============ SORTED VIEW ============ -->
  <div class="view" id="view-sorted">
    <h1>Your sorted files</h1>
    <p class="lede">
      Hover any file to see its original name and metadata. Click to open. Duplicates are marked in purple.
    </p>

    <div class="stats" id="stats"></div>

    <div class="search-bar">
      <div class="search">
        <span class="ic">🔍</span>
        <input id="search" placeholder="Search by new or original name…" />
      </div>
      <button class="btn ghost" id="refresh">↻ Refresh</button>
    </div>

    <div class="cards" id="cards"></div>
    <div class="empty" id="empty-sorted" style="display:none">
      <div class="ico">📦</div>
      <h2>Nothing sorted yet</h2>
      <p>Files you accept on the Upload tab appear here.</p>
      <button class="btn" onclick="switchView('upload')">Upload files →</button>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>
<div class="tip" id="tip" style="display:none"></div>

<script>
// ============================================================
// STATE
// ============================================================
const $ = id => document.getElementById(id);
const rows = new Map();
let sortedFiles = [];
let currentView = "upload";

// ============================================================
// TABS
// ============================================================
function switchView(name) {
  currentView = name;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "sorted") loadSorted();
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => switchView(t.dataset.view)));

// ============================================================
// TOAST
// ============================================================
const toast = $("toast");
function showToast(html, kind="", ms=2800) {
  toast.innerHTML = html;
  toast.classList.remove("ok","err"); if (kind) toast.classList.add(kind);
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), ms);
}

// ============================================================
// UPLOAD VIEW
// ============================================================
const drop = $("drop"), fileInput = $("file"), folderInput = $("folder");
const toolbar = $("toolbar"), summaryEl = $("summary");
const tableWrap = $("table-wrap"), table = $("table");
const processBtn = $("process"), applyBtn = $("apply"), clearBtn = $("clear");
const progressBar = $("progress"), progressFill = $("progress-fill");
const emptyUpload = $("empty-upload");

function fmtSize(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(2) + " MB";
  return (n/1024/1024/1024).toFixed(2) + " GB";
}
function fmtAgo(iso) {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms/1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s/60) + " min ago";
  if (s < 86400) return Math.floor(s/3600) + " h ago";
  if (s < 86400*7) return Math.floor(s/86400) + " d ago";
  return new Date(iso).toLocaleDateString();
}

function updateToolbar() {
  const n = rows.size;
  $("count-upload").textContent = n;
  toolbar.style.display = n ? "flex" : "none";
  tableWrap.style.display = n ? "block" : "none";
  emptyUpload.style.display = "none";
  drop.classList.toggle("compact", n > 0);

  const compactStrong = drop.querySelector("strong");
  const compactHint = drop.querySelector(".hint");
  if (n > 0) {
    compactStrong.textContent = "+ Add more files";
    compactHint.textContent = "drop to append";
  } else {
    compactStrong.textContent = "Drop files or folders here";
    compactHint.textContent = "PDFs, DOCX, PPTX, XLSX, images — as many as you like";
  }

  const arr = [...rows.values()];
  const processed = arr.filter(r => ["ready","duplicate","error","accepted"].includes(r.status)).length;
  const ready = arr.filter(r => r.status === "ready" && r.accepted !== false).length;
  const dup = arr.filter(r => r.status === "duplicate" && r.accepted !== false).length;
  summaryEl.innerHTML =
    `<span><b>${n}</b> file${n !== 1 ? "s" : ""}</span>` +
    `<span class="sep">·</span>` +
    `<span>processed <b>${processed}</b></span>` +
    `<span class="sep">·</span>` +
    `<span>accepting <b>${ready + dup}</b></span>`;
  applyBtn.disabled = (ready + dup) === 0;
  processBtn.disabled = !arr.some(r => r.status === "queued");
  if (processed > 0 && processed < n) {
    progressBar.style.display = "block";
    progressFill.style.width = (processed / n * 100) + "%";
  } else if (processed === n && n > 0) {
    progressBar.style.display = "block";
    progressFill.style.width = "100%";
  } else {
    progressBar.style.display = "none";
  }
}

function addFiles(fileList) {
  const added = [];
  for (const f of fileList) {
    if (!f.name || f.name.startsWith(".")) continue;
    const id = "loc-" + Math.random().toString(36).slice(2, 10);
    const row = { id, file: f, name: f.name, size: f.size, status: "queued", accepted: true };
    rows.set(id, row);
    added.push(id);
    renderRow(id);
  }
  updateToolbar();
  if (added.length) showToast(`<b>${added.length}</b> file${added.length !== 1 ? "s" : ""} queued`, "ok");
}

// recursive folder drop via FileSystemEntry API
async function readAllEntries(dataTransferItems) {
  const files = [];
  async function walk(entry) {
    if (entry.isFile) {
      const f = await new Promise((res, rej) => entry.file(res, rej));
      files.push(f);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      while (true) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e);
      }
    }
  }
  const tasks = [];
  for (const it of dataTransferItems) {
    const entry = it.webkitGetAsEntry?.();
    if (entry) tasks.push(walk(entry));
    else if (it.getAsFile) { const f = it.getAsFile(); if (f) files.push(f); }
  }
  await Promise.all(tasks);
  return files;
}

drop.addEventListener("click", (e) => {
  if (e.target.closest(".pick-folder")) return;
  fileInput.click();
});
["dragenter","dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("hover"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", async (ev) => {
  ev.preventDefault();
  drop.classList.remove("hover");
  if (ev.dataTransfer.items?.length && ev.dataTransfer.items[0].webkitGetAsEntry) {
    const files = await readAllEntries(ev.dataTransfer.items);
    addFiles(files);
  } else {
    addFiles(ev.dataTransfer.files);
  }
});
fileInput.addEventListener("change", ev => { addFiles(ev.target.files); fileInput.value = ""; });
folderInput.addEventListener("change", ev => { addFiles(ev.target.files); folderInput.value = ""; });

function renderRow(id) {
  const r = rows.get(id);
  let el = document.getElementById("row-" + id);
  if (!el) {
    el = document.createElement("div");
    el.className = "row"; el.id = "row-" + id;
    el.innerHTML = `
      <div class="cell"><input type="checkbox" checked data-role="accept"/></div>
      <div class="cell filename">
        <div style="min-width:0; flex:1">
          <div class="name" data-role="origname"></div>
          <div class="meta" data-role="filemeta"></div>
        </div>
      </div>
      <div class="cell"><span class="badge queued" data-role="badge"><span class="dot"></span>queued</span></div>
      <div class="cell name-cell"><input type="text" class="name-input" data-role="name" placeholder="—" disabled/></div>
      <div class="cell preview-cell"><button class="act-btn" data-role="preview" disabled>preview</button></div>
      <div class="cell actions"><button class="act-btn icon" data-role="skip" title="Remove">✕</button></div>`;
    const previewWrap = document.createElement("div");
    previewWrap.className = "preview-wrap";
    previewWrap.innerHTML = `<div class="preview-pane"><span class="label">First-3-pages markdown</span><div data-role="preview-content"></div></div>`;
    table.appendChild(el);
    table.appendChild(previewWrap);

    el.querySelector('[data-role="accept"]').addEventListener("change", e => {
      r.accepted = e.target.checked;
      el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
      el.classList.toggle("skipped", !r.accepted);
      updateToolbar();
    });
    el.querySelector('[data-role="name"]').addEventListener("input", e => r.final_name = e.target.value);
    el.querySelector('[data-role="preview"]').addEventListener("click", () => togglePreview(id));
    el.querySelector('[data-role="skip"]').addEventListener("click", () => {
      rows.delete(id); el.remove(); previewWrap.remove(); updateToolbar();
    });
  }

  el.querySelector('[data-role="origname"]').textContent = r.name;
  const metaParts = [];
  if (r.size != null) metaParts.push(fmtSize(r.size));
  if (r.hash) metaParts.push(r.hash);
  el.querySelector('[data-role="filemeta"]').textContent = metaParts.join("  ·  ");

  const badge = el.querySelector('[data-role="badge"]');
  badge.className = "badge " + r.status;
  const label = {queued:"queued", extracting:"reading pages", naming:"naming", ready:"ready", duplicate:"duplicate", error:"error", accepted:"applied"}[r.status] || r.status;
  let extra = "";
  if (r.status === "duplicate" && r.dup_of) extra = " · " + r.dup_of.slice(0, 24);
  if (r.status === "error" && r.error) extra = " · " + r.error;
  badge.innerHTML = '<span class="dot"></span>' + label + extra;

  const nameInput = el.querySelector('[data-role="name"]');
  nameInput.value = r.final_name ?? (r.suggested || "");
  nameInput.disabled = !(r.status === "ready" || r.status === "duplicate" || r.status === "error");

  const previewBtn = el.querySelector('[data-role="preview"]');
  previewBtn.disabled = !r.preview;

  el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
  el.classList.toggle("skipped", !r.accepted);
}

function togglePreview(id) {
  const el = document.getElementById("row-" + id);
  const wrap = el.nextElementSibling;
  const btn = el.querySelector('[data-role="preview"]');
  const r = rows.get(id);
  if (el.classList.toggle("expanded")) {
    wrap.querySelector('[data-role="preview-content"]').textContent = r.preview || "(empty)";
    btn.classList.add("active");
  } else {
    btn.classList.remove("active");
  }
}

clearBtn.addEventListener("click", () => {
  if (!rows.size) return;
  if (!confirm(`Clear ${rows.size} file${rows.size !== 1 ? "s" : ""} from the queue?`)) return;
  rows.clear();
  [...table.querySelectorAll(".row, .preview-wrap")].forEach(e => e.remove());
  updateToolbar();
});

processBtn.addEventListener("click", async () => {
  const queued = [...rows.values()].filter(r => r.status === "queued");
  if (!queued.length) return;
  processBtn.disabled = true;
  const fd = new FormData();
  const localIdsByIndex = [];
  for (const r of queued) {
    fd.append("files", r.file, r.name);
    localIdsByIndex.push(r.id);
    r.status = "hashing"; renderRow(r.id);
  }
  updateToolbar();

  let resp;
  try {
    resp = await fetch("/sorter/process", { method: "POST", body: fd });
  } catch (e) { showToast("network error", "err"); processBtn.disabled = false; return; }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", serverIdx = -1;
  const idMap = {};
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
      try { ev = JSON.parse(raw); } catch { continue; }
      if (ev.type === "row_start") {
        serverIdx++;
        const local = localIdsByIndex[serverIdx];
        idMap[ev.id] = local;
        const r = rows.get(local);
        r.server_id = ev.id; r.hash = ev.hash; r.status = "extracting";
        renderRow(local);
      } else if (ev.type === "row_step") {
        const r = rows.get(idMap[ev.id]); r.status = ev.step; renderRow(idMap[ev.id]);
      } else if (ev.type === "row_done") {
        const r = rows.get(idMap[ev.id]);
        Object.assign(r, {
          status: ev.status, suggested: ev.suggested, final_name: ev.suggested,
          preview: ev.preview, dup_of: ev.dup_of, error: ev.error, temp_path: ev.temp_path,
        });
        renderRow(idMap[ev.id]);
      }
      updateToolbar();
    }
  }
  updateToolbar();
});

applyBtn.addEventListener("click", async () => {
  const decisions = [];
  for (const r of rows.values()) {
    if (!r.temp_path) continue;
    if (!r.accepted) { decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"skip"}); continue; }
    if (r.status === "duplicate") decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"duplicate", dup_of:r.dup_of});
    else if (r.status === "ready" || r.status === "error") decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"accept", final_name:r.final_name || r.suggested});
  }
  if (!decisions.filter(d => d.status !== "skip").length) return;
  applyBtn.disabled = true;
  const resp = await fetch("/sorter/apply", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decisions}),
  });
  const data = await resp.json();
  let ok = 0, dupes = 0;
  for (const res of data.results) {
    if (res.ok && res.note) dupes++;
    else if (res.ok && !res.skipped) ok++;
    if (res.ok && res.final_path) {
      for (const r of rows.values()) if (r.server_id === res.id) { r.status = "accepted"; r.final_path = res.final_path; renderRow(r.id); break; }
    }
  }
  showToast(`<span class="ok">✓</span> Sorted <b>${ok}</b> · duplicates <b>${dupes}</b> · total in library <b>${data.sorted_count}</b>`, "ok", 4200);
  loadSorted();
  updateToolbar();
  applyBtn.disabled = false;
});

// ============================================================
// SORTED VIEW
// ============================================================
const cardsEl = $("cards"), statsEl = $("stats"), searchInput = $("search"), emptySorted = $("empty-sorted");
$("refresh").addEventListener("click", loadSorted);
searchInput.addEventListener("input", renderCards);

async function loadSorted() {
  try {
    const r = await fetch("/sorter/files");
    const d = await r.json();
    sortedFiles = d.files || [];
  } catch (e) { sortedFiles = []; }
  renderStats();
  renderCards();
}

function renderStats() {
  const total = sortedFiles.length;
  const dups = sortedFiles.filter(f => f.is_duplicate).length;
  const bytes = sortedFiles.reduce((a, f) => a + (f.size || 0), 0);
  $("count-sorted").textContent = total;
  statsEl.innerHTML = `
    <div class="stat"><div class="label">Total sorted</div><div class="value accent">${total - dups}</div><div class="sub">in sorted/</div></div>
    <div class="stat"><div class="label">Duplicates</div><div class="value dup">${dups}</div><div class="sub">in sorted/duplicates/</div></div>
    <div class="stat"><div class="label">Total size</div><div class="value">${fmtSize(bytes)}</div><div class="sub">on disk</div></div>
  `;
}

function renderCards() {
  const q = (searchInput.value || "").toLowerCase().trim();
  const matches = sortedFiles.filter(f => !q || (f.final_name || "").toLowerCase().includes(q) || (f.original || "").toLowerCase().includes(q));
  cardsEl.innerHTML = "";
  if (!matches.length) {
    cardsEl.style.display = "none";
    emptySorted.style.display = sortedFiles.length ? "none" : "block";
    if (sortedFiles.length && !matches.length) {
      cardsEl.style.display = "grid";
      cardsEl.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="ico">🔍</div><h2>No matches</h2><p>Nothing matches "${q}".</p></div>`;
    }
    return;
  }
  cardsEl.style.display = "grid";
  emptySorted.style.display = "none";
  for (const f of matches) {
    const a = document.createElement("a");
    a.className = "card" + (f.is_duplicate ? " is-dup" : "");
    a.href = f.url || "#";
    a.target = "_blank";
    a.rel = "noopener";
    a.innerHTML = `
      ${f.is_duplicate ? '<span class="dupTag">dup</span>' : ''}
      <div class="fname">${escapeHTML(f.final_name)}</div>
      <div class="cmeta">
        <span class="size">${fmtSize(f.size)}</span>
        <span class="ago">${fmtAgo(f.applied_at)}</span>
      </div>
    `;
    a.addEventListener("mouseenter", e => showTip(e, f));
    a.addEventListener("mousemove", e => positionTip(e));
    a.addEventListener("mouseleave", hideTip);
    cardsEl.appendChild(a);
  }
}

function escapeHTML(s) { return (s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

// ============================================================
// HOVER TOOLTIP
// ============================================================
const tip = $("tip");
function showTip(e, f) {
  const applied = f.applied_at ? new Date(f.applied_at).toLocaleString() : "—";
  tip.innerHTML = `
    <div class="tip-label">Original filename</div>
    <div class="tip-original">${escapeHTML(f.original || "(unknown)")}</div>
    <div class="tip-sep"></div>
    <div class="tip-row"><span class="k">Renamed to</span><span class="v">${escapeHTML(f.final_name)}</span></div>
    <div class="tip-row"><span class="k">Sorted</span><span class="v">${fmtAgo(f.applied_at)} · ${applied}</span></div>
    <div class="tip-row"><span class="k">Size</span><span class="v">${fmtSize(f.size)}</span></div>
    ${f.hash ? `<div class="tip-row"><span class="k">SHA-256</span><span class="v">${f.hash.slice(0,16)}…</span></div>` : ""}
    ${f.is_duplicate ? `<div class="tip-row"><span class="k">Status</span><span class="v" style="color:var(--dup)">duplicate (quarantined)</span></div>` : ""}
  `;
  tip.style.display = "block";
  positionTip(e);
  requestAnimationFrame(() => tip.classList.add("show"));
}
function positionTip(e) {
  const pad = 12;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > window.innerWidth - 10) x = e.clientX - w - pad;
  if (y + h > window.innerHeight - 10) y = e.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() {
  tip.classList.remove("show");
  setTimeout(() => { if (!tip.classList.contains("show")) tip.style.display = "none"; }, 150);
}

// ============================================================
// INIT
// ============================================================
updateToolbar();
loadSorted();
</script>
</body>
</html>
"""


SORTER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Aprio Strata</title>
<style>
/* ═══════════════════════════════════════════════════════════════════
   APRIO STRATA — Aprio palette (warm paper + forest green), paper-glass
   ═══════════════════════════════════════════════════════════════════ */

/* Local fonts — vendored from Google Fonts into /fonts/ (no CDN) */
__FONT_FACES__

:root {
  /* APRIO palette — warm paper backgrounds */
  --paper: #F4EFE6;
  --paper-soft: #FBFAF5;
  --paper-raised: #FFFFFF;
  --paper-shade: #EBE4D7;
  --paper-deep: #DCD4C3;

  /* Ink — text levels */
  --ink: #1C1917;
  --ink-muted: #57534E;
  --ink-faint: #9F9890;
  --ink-whisper: #C4BDB1;

  /* Rules */
  --rule: #D6D3D1;
  --rule-soft: #E7E3DC;
  --rule-fine: #EAE5DB;

  /* Mineral accents (straight from Aprio triage) */
  --accent: #2D5F3E;            /* forest green */
  --accent-dim: #4A7C59;
  --accent-deep: #1F4A2E;
  --accent-wash: rgba(45, 95, 62, 0.06);
  --accent-soft: rgba(45, 95, 62, 0.14);

  --gold: #B8833E;
  --gold-dim: #C9A267;
  --gold-wash: rgba(184, 131, 62, 0.08);

  --danger: #9B3F2F;
  --danger-wash: rgba(155, 63, 47, 0.05);

  /* Glass tiers — subtle paper overlays with frosted edges */
  --glass-1: rgba(255, 253, 248, 0.62);
  --glass-2: rgba(255, 253, 248, 0.78);
  --glass-3: rgba(255, 253, 248, 0.90);

  /* Type families — Aprio stack: Fraunces / Instrument Sans / JetBrains Mono */
  --font-display: "Fraunces", "Iowan Old Style", "Palatino", Georgia, serif;
  --font-body: "Instrument Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;

  /* Shadows — editorial soft, not drop-shadowy */
  --shadow-lg: 0 24px 56px -18px rgba(28, 25, 23, 0.18), 0 4px 14px -4px rgba(28, 25, 23, 0.08);
  --shadow-md: 0 8px 24px -8px rgba(28, 25, 23, 0.12), 0 2px 6px -2px rgba(28, 25, 23, 0.05);
  --shadow-sm: 0 1px 3px rgba(28, 25, 23, 0.04), 0 1px 2px rgba(28, 25, 23, 0.06);
}

* { box-sizing: border-box; }
html, body {
  margin: 0; height: 100%; overflow: hidden;
  background: var(--paper); color: var(--ink);
  font: 14px/1.55 var(--font-body);
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
  font-feature-settings: "ss01", "cv01", "kern";
}
a { color: inherit; text-decoration: none; }
button { font: inherit; color: inherit; background: none; border: none; padding: 0; cursor: pointer; }
input { font: inherit; color: inherit; }

/* ─── ambient paper backdrop ──────────────────────────────────────── */
.backdrop {
  position: fixed; inset: 0; z-index: -2;
  background:
    radial-gradient(at 15% 20%, rgba(184, 131, 62, 0.09) 0%, transparent 52%),
    radial-gradient(at 85% 10%, rgba(45, 95, 62, 0.07) 0%, transparent 55%),
    radial-gradient(at 75% 90%, rgba(184, 131, 62, 0.08) 0%, transparent 58%),
    radial-gradient(at 15% 85%, rgba(45, 95, 62, 0.05) 0%, transparent 55%),
    linear-gradient(180deg, #F8F3E8 0%, var(--paper) 50%, #EEE7D6 100%);
}
.aurora {
  position: fixed; inset: -15%; z-index: -1; pointer-events: none;
  background:
    radial-gradient(ellipse 55% 40% at 50% 0%, rgba(45, 95, 62, 0.09) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 80% 70%, rgba(184, 131, 62, 0.10) 0%, transparent 55%),
    radial-gradient(ellipse 40% 30% at 20% 90%, rgba(45, 95, 62, 0.07) 0%, transparent 55%);
  filter: blur(70px);
  animation: drift 32s ease-in-out infinite alternate;
  opacity: 0.85;
}
@keyframes drift {
  0%   { transform: translate(0, 0) rotate(0deg) scale(1); }
  50%  { transform: translate(-3%, 2%) rotate(3deg) scale(1.04); }
  100% { transform: translate(2%, -2%) rotate(-2deg) scale(0.98); }
}
.strata {
  position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background-image:
    linear-gradient(0deg, transparent calc(17% - 1px), rgba(28, 25, 23, 0.04) 17%, transparent calc(17% + 1px)),
    linear-gradient(0deg, transparent calc(38% - 1px), rgba(28, 25, 23, 0.03) 38%, transparent calc(38% + 1px)),
    linear-gradient(0deg, transparent calc(62% - 1px), rgba(28, 25, 23, 0.04) 62%, transparent calc(62% + 1px)),
    linear-gradient(0deg, transparent calc(81% - 1px), rgba(28, 25, 23, 0.025) 81%, transparent calc(81% + 1px));
}

/* ─── frosted-paper glass primitive ───────────────────────────────── */
.glass {
  background: linear-gradient(135deg, var(--glass-1) 0%, rgba(251, 250, 245, 0.5) 100%);
  backdrop-filter: blur(24px) saturate(140%);
  -webkit-backdrop-filter: blur(24px) saturate(140%);
  border: 1px solid rgba(28, 25, 23, 0.08);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.8),
    inset 0 -1px 0 rgba(28, 25, 23, 0.04),
    var(--shadow-lg);
  border-radius: 22px;
  position: relative;
  isolation: isolate;
}
.glass::before {
  content: ""; position: absolute; inset: 0;
  border-radius: inherit;
  background-image: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='0.5'/></svg>");
  opacity: 0.025; mix-blend-mode: multiply;
  pointer-events: none; z-index: 0;
}
.glass > * { position: relative; z-index: 1; }
.glass--elevated {
  background: linear-gradient(135deg, var(--glass-3) 0%, rgba(255, 253, 248, 0.75) 100%);
  backdrop-filter: blur(36px) saturate(160%);
  -webkit-backdrop-filter: blur(36px) saturate(160%);
}
.glass--inner {
  background: rgba(251, 250, 245, 0.55);
  border: 1px solid rgba(28, 25, 23, 0.08);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border-radius: 14px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
}

/* ═══════════════════════════════════════════════════════════════════
   LAYOUT
   ═══════════════════════════════════════════════════════════════════ */
.shell {
  position: fixed; inset: 14px;
  display: grid; grid-template-columns: 272px 1fr;
  overflow: hidden;
  animation: rise 600ms cubic-bezier(.2,.9,.2,1) both;
}
@keyframes rise {
  from { opacity: 0; transform: translateY(8px) scale(0.995); }
  to   { opacity: 1; transform: none; }
}

/* ─── SIDEBAR ─────────────────────────────────────────────────────── */
.sidebar {
  display: flex; flex-direction: column; gap: 8px;
  padding: 20px 16px;
  border-right: 1px solid rgba(28, 25, 23, 0.08);
  overflow: hidden; min-height: 0;
}
.brand {
  padding: 2px 10px 18px;
}
.brand .mark {
  display: flex; align-items: center; gap: 12px;
}
.brand .strata-mark {
  color: var(--accent);
  flex-shrink: 0;
  margin-top: 1px;
}
.brand .wordmark {
  display: flex; flex-direction: column;
  line-height: 1.05;
}
.brand .wordmark .w1 {
  font-family: var(--font-display);
  font-weight: 500;
  font-size: 19px;
  letter-spacing: -0.015em;
  color: var(--ink);
}
.brand .wordmark .w2 {
  font-family: var(--font-display);
  font-weight: 400;
  font-style: italic;
  font-size: 19px;
  letter-spacing: -0.005em;
  color: var(--ink-muted);
}

.side-section {
  margin-top: 12px; opacity: 0; animation: fade-slide 500ms cubic-bezier(.2,.9,.2,1) forwards;
}
.side-section:nth-child(3) { animation-delay: 80ms; }
.side-section:nth-child(4) { animation-delay: 160ms; }
@keyframes fade-slide {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: none; }
}
.side-label {
  font-family: var(--font-mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--ink-faint); padding: 0 10px 8px;
  font-weight: 500;
}
.side-label-row {
  display: flex; align-items: center; justify-content: space-between;
  padding-right: 6px;
}
.side-label-row .side-label { padding: 0 10px 8px; flex: 1; }
.side-add {
  width: 22px; height: 22px; border-radius: 6px;
  display: grid; place-items: center;
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.1);
  color: var(--ink-muted); font-size: 13px; line-height: 1; font-weight: 600;
  transition: all 140ms ease; margin-bottom: 6px;
}
.side-add:hover { color: var(--accent-deep); background: var(--accent-wash); border-color: var(--accent-soft); }
.cat .del {
  opacity: 0; width: 18px; height: 18px; border-radius: 4px; line-height: 1;
  color: var(--ink-faint); font-size: 11px; transition: all 140ms ease;
  display: grid; place-items: center; margin-left: 4px;
}
.cat:hover .del { opacity: 1; }
.cat .del:hover { color: var(--danger); background: var(--danger-wash); }

.cat-list { display: flex; flex-direction: column; gap: 1px; overflow: auto; min-height: 0; }
.cat {
  position: relative;
  display: grid; grid-template-columns: 22px 1fr auto;
  align-items: center; gap: 10px;
  padding: 9px 12px; border-radius: 10px;
  color: var(--ink-muted); font-size: 13px;
  cursor: pointer; transition: all 160ms ease;
  border: 1px solid transparent;
}
.cat:hover { background: rgba(28, 25, 23, 0.03); color: var(--ink); }
.cat.active {
  background: var(--accent-wash);
  color: var(--accent-deep);
  border-color: var(--accent-soft);
  font-weight: 500;
}
.cat.active::before {
  content: ""; position: absolute; left: -1px; top: 10%; bottom: 10%; width: 2px;
  background: var(--accent);
  border-radius: 2px;
}
.cat .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: currentColor; opacity: 0.4;
  justify-self: center;
}
.cat.active .dot { opacity: 1; background: var(--accent); }
.cat.is-quarantine.active .dot { background: var(--danger); }
.cat.is-quarantine.active { background: var(--danger-wash); border-color: rgba(155, 63, 47, 0.18); color: var(--danger); }
.cat.is-quarantine.active::before { background: var(--danger); }
.cat .count {
  font-family: var(--font-mono); font-size: 11px;
  color: var(--ink-faint); padding: 2px 7px; border-radius: 999px;
  background: rgba(28, 25, 23, 0.04); font-weight: 500;
}
.cat.active .count { color: var(--accent-deep); background: var(--accent-soft); }
.cat.is-quarantine.active .count { color: var(--danger); background: rgba(155, 63, 47, 0.12); }

.cat.dragover { border-color: var(--accent); background: var(--accent-soft); color: var(--accent-deep); }

.side-bottom { margin-top: auto; display: flex; flex-direction: column; gap: 10px; padding-top: 10px; }
.btn-deposit {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  padding: 11px 14px; border-radius: 10px;
  background: var(--accent);
  color: var(--paper-soft); font-family: var(--font-body); font-weight: 600; font-size: 13px;
  border: 1px solid var(--accent-deep);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.12), 0 4px 14px -4px rgba(45, 95, 62, 0.4);
  transition: all 180ms cubic-bezier(.2,.9,.2,1);
  letter-spacing: 0.005em;
}
.btn-deposit:hover { transform: translateY(-1px); background: var(--accent-deep); box-shadow: 0 6px 18px -4px rgba(45, 95, 62, 0.5); }
.btn-deposit:active { transform: translateY(0); }
.btn-deposit .plus {
  width: 18px; height: 18px; border-radius: 5px;
  background: rgba(255,255,255,0.18); display: grid; place-items: center;
  font-size: 13px; font-weight: 700; font-family: var(--font-body);
}

/* ─── CONTENT PANE ────────────────────────────────────────────────── */
.content {
  display: flex; flex-direction: column; overflow: hidden;
  border-radius: 0 22px 22px 0;
}
.toolbar {
  display: flex; align-items: center; gap: 12px;
  padding: 16px 24px;
  border-bottom: 1px solid rgba(28, 25, 23, 0.07);
  min-height: 60px;
}
.breadcrumb {
  display: flex; align-items: baseline; gap: 12px; min-width: 0;
}
.breadcrumb .ctx {
  font-family: var(--font-display); font-weight: 300;
  font-size: 24px; letter-spacing: -0.015em; color: var(--ink);
}
.breadcrumb .meta {
  font-size: 11.5px; color: var(--ink-faint);
  font-family: var(--font-mono); letter-spacing: 0.02em;
}
.toolbar-spacer { flex: 1; }
.search {
  display: flex; align-items: center; gap: 8px; min-width: 200px;
  padding: 7px 12px; border-radius: 10px;
  background: var(--paper-soft);
  border: 1px solid rgba(28, 25, 23, 0.1);
  transition: all 160ms ease;
}
.search:focus-within { border-color: var(--accent); background: var(--paper-raised); box-shadow: 0 0 0 3px var(--accent-wash); }
.search svg { opacity: 0.5; flex-shrink: 0; color: var(--ink-muted); }
.search input { background: transparent; border: none; outline: none; width: 100%; font-size: 13px; color: var(--ink); }
.search input::placeholder { color: var(--ink-faint); }

/* ─── content body ────────────────────────────────────────────────── */
.body { flex: 1; overflow: auto; padding: 24px 28px 40px; min-height: 0; }
.body::-webkit-scrollbar { width: 8px; }
.body::-webkit-scrollbar-thumb { background: rgba(28, 25, 23, 0.1); border-radius: 4px; }
.body::-webkit-scrollbar-track { background: transparent; }

.grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

/* ─── FILE CARD ───────────────────────────────────────────────────── */
.card {
  position: relative;
  padding: 14px 16px; border-radius: 12px;
  background: var(--paper-soft);
  border: 1px solid rgba(28, 25, 23, 0.08);
  display: flex; flex-direction: column; gap: 10px;
  min-height: 108px; cursor: pointer;
  transition: all 220ms cubic-bezier(.2,.9,.2,1);
  animation: card-in 400ms cubic-bezier(.2,.9,.2,1) both;
  box-shadow: var(--shadow-sm);
}
@keyframes card-in {
  from { opacity: 0; transform: translateY(6px) scale(0.97); }
  to { opacity: 1; transform: none; }
}
.card:hover {
  background: var(--paper-raised);
  border-color: var(--accent-soft);
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
}
.card.is-dup { border-left: 2px solid var(--danger); }
.card.is-dup:hover { border-left-color: var(--danger); box-shadow: var(--shadow-md), 0 0 0 1px rgba(155, 63, 47, 0.1); }
.card.dragging { opacity: 0.4; transform: rotate(-2deg) scale(1.02); }
.card.selected {
  border-color: var(--accent);
  box-shadow: var(--shadow-md), 0 0 0 2px var(--accent-wash);
}
.card .fname {
  font-family: var(--font-mono); font-size: 12.5px; font-weight: 500;
  color: var(--ink); line-height: 1.45; word-break: break-all;
  letter-spacing: 0.01em;
}
.card .meta {
  margin-top: auto; display: flex; align-items: center; gap: 10px;
  font-size: 11px; color: var(--ink-faint);
  font-family: var(--font-mono); letter-spacing: 0.02em;
}
.card .meta .sep { opacity: 0.5; }
.card .chip {
  position: absolute; top: 10px; right: 10px;
  font-family: var(--font-body); font-size: 10px; font-weight: 500;
  padding: 2.5px 8px; border-radius: 999px;
  background: var(--danger-wash); color: var(--danger);
  border: 1px solid rgba(155, 63, 47, 0.2);
  text-transform: lowercase; letter-spacing: 0.02em;
}
.card .echo-chip {
  position: absolute; top: 10px; right: 10px;
  display: inline-flex; align-items: center; gap: 4px;
  font-family: var(--font-mono); font-size: 10px; font-weight: 500;
  padding: 2.5px 7px; border-radius: 999px;
  background: var(--gold-wash); color: var(--gold);
  border: 1px solid rgba(184, 131, 62, 0.22);
  letter-spacing: 0.02em;
}
.card .echo-chip .ripple {
  width: 8px; height: 8px; display: inline-block; position: relative;
}
.card .echo-chip .ripple::before,
.card .echo-chip .ripple::after {
  content: ""; position: absolute; inset: 0;
  border: 1px solid currentColor; border-radius: 50%;
  opacity: 0.7;
}
.card .echo-chip .ripple::after {
  inset: 2px; opacity: 1;
}

/* Flash gold ring on a card that just received a new echo from an apply batch */
.card.just-echoed {
  animation: echo-flash 2.4s cubic-bezier(.2,.8,.2,1) 1;
}
@keyframes echo-flash {
  0%   { box-shadow: 0 0 0 0 rgba(184, 131, 62, 0.55), var(--shadow-sm); }
  15%  { box-shadow: 0 0 0 6px rgba(184, 131, 62, 0.35), 0 10px 30px -10px rgba(184, 131, 62, 0.35); }
  55%  { box-shadow: 0 0 0 3px rgba(184, 131, 62, 0.18), var(--shadow-md); }
  100% { box-shadow: 0 0 0 0 rgba(184, 131, 62, 0), var(--shadow-sm); }
}

/* ─── PREVIEW MODE ────────────────────────────────────────────────── */
.preview-wrap { display: grid; grid-template-columns: 1fr 300px; gap: 14px; height: 100%; }
.preview-frame {
  border-radius: 14px; overflow: hidden; background: var(--paper-shade);
  border: 1px solid rgba(28, 25, 23, 0.08); position: relative;
  box-shadow: var(--shadow-sm);
}
.preview-frame iframe { width: 100%; height: 100%; border: none; display: block; background: #fff; }
.preview-meta { padding: 18px; overflow: auto; }
.preview-meta h3 {
  margin: 0 0 14px; font-family: var(--font-display); font-weight: 400;
  font-size: 17px; letter-spacing: -0.01em; color: var(--ink);
}
.meta-row {
  display: flex; flex-direction: column; gap: 3px;
  padding: 10px 0; border-bottom: 1px dashed rgba(28, 25, 23, 0.08);
}
.meta-row:last-child { border-bottom: none; }
.meta-row .k { font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--ink-faint); font-family: var(--font-mono); font-weight: 500; }
.meta-row .v { font-size: 13px; color: var(--ink); word-break: break-word; font-family: var(--font-mono); line-height: 1.5; }
.meta-row .v.body { font-family: var(--font-body); }

.meta-section-head {
  margin: 18px 0 8px; font-family: var(--font-mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.12em; color: var(--ink-faint); font-weight: 600;
}
.meta-section-head em {
  font-style: normal; color: var(--gold); font-weight: 700;
}
.preview-echoes { display: flex; flex-direction: column; gap: 6px; }
.preview-echo {
  display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
  padding: 6px 10px; border-radius: 8px;
  background: var(--gold-wash); border: 1px solid rgba(184, 131, 62, 0.2);
}
.preview-echo-name {
  font-family: var(--font-mono); font-size: 11.5px; color: var(--ink);
  min-width: 0; word-break: break-all;
}
.preview-echo-when {
  font-family: var(--font-mono); font-size: 10.5px; color: var(--gold);
  font-style: italic; white-space: nowrap;
}

.preview-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
.pa-btn {
  display: flex; align-items: center; gap: 8px; justify-content: flex-start;
  padding: 9px 12px; border-radius: 10px;
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.1);
  color: var(--ink-muted); font-size: 13px; transition: all 140ms ease;
  font-family: var(--font-body);
}
.pa-btn:hover { background: var(--paper-raised); color: var(--ink); border-color: var(--accent); }
.pa-btn.danger:hover { color: var(--danger); border-color: var(--danger); }

/* ─── EMPTY STATES ────────────────────────────────────────────────── */
.empty {
  padding: 72px 24px; text-align: center; color: var(--ink-faint);
  display: flex; flex-direction: column; align-items: center; gap: 10px;
}
.empty .big {
  font-family: var(--font-display); font-style: italic;
  font-size: 24px; color: var(--ink-muted); letter-spacing: -0.01em;
  font-weight: 300;
}
.empty .small { font-size: 13.5px; max-width: 360px; color: var(--ink-faint); }
.empty .ghost-btn {
  margin-top: 10px; padding: 9px 16px; border-radius: 10px;
  background: var(--accent-wash); color: var(--accent);
  border: 1px solid var(--accent-soft); font-size: 13px; font-weight: 600;
  font-family: var(--font-body);
}
.empty .ghost-btn:hover { background: var(--accent-soft); color: var(--accent-deep); }
.local-note {
  margin-top: 28px; display: inline-flex; align-items: center; gap: 7px;
  font-family: var(--font-display); font-style: italic; font-size: 12.5px;
  color: var(--ink-faint); padding: 6px 12px; border-radius: 999px;
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.06);
}
.local-note svg { color: var(--accent); opacity: 0.7; }
.side-local {
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 2px;
  font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.02em;
  color: var(--ink-faint); opacity: 0.85;
}
.side-local svg { color: var(--accent); opacity: 0.7; flex-shrink: 0; }

/* ═══════════════════════════════════════════════════════════════════
   UPLOAD DRAWER
   ═══════════════════════════════════════════════════════════════════ */
.drawer-scrim {
  position: fixed; inset: 0; background: rgba(28, 25, 23, 0.18);
  backdrop-filter: blur(4px); z-index: 40;
  opacity: 0; pointer-events: none; transition: opacity 200ms ease;
}
.drawer-scrim.open { opacity: 1; pointer-events: auto; }

.drawer {
  position: fixed; top: 14px; bottom: 14px; right: 14px; width: min(560px, 55vw);
  z-index: 50; display: flex; flex-direction: column;
  transform: translateX(120%); transition: transform 360ms cubic-bezier(.2,.9,.2,1);
  border-radius: 22px; overflow: hidden;
}
.drawer.open { transform: translateX(0); }
.drawer-head { padding: 22px 24px 14px; border-bottom: 1px solid rgba(28, 25, 23, 0.08); }
.drawer-head .t {
  font-family: var(--font-display); font-weight: 300;
  font-size: 24px; letter-spacing: -0.015em; color: var(--ink);
}
.drawer-head .s {
  font-size: 13.5px; color: var(--ink-muted); margin-top: 2px;
  font-family: var(--font-display); font-style: italic;
  font-weight: 400;
}
.drawer-head .close {
  position: absolute; top: 18px; right: 18px;
  width: 30px; height: 30px; border-radius: 8px;
  display: grid; place-items: center; color: var(--ink-muted);
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.1);
}
.drawer-head .close:hover { color: var(--ink); background: var(--paper-raised); }

.drawer-body { flex: 1; overflow: auto; padding: 20px 24px; min-height: 0; }

.drop {
  position: relative; border: 2px dashed rgba(28, 25, 23, 0.18);
  border-radius: 14px; padding: 32px 20px; text-align: center;
  cursor: pointer; transition: all 220ms ease;
  background: var(--paper-soft);
}
.drop:hover, .drop.hover {
  border-color: var(--accent); background: var(--accent-wash);
}
.drop.compact { padding: 14px 18px; text-align: left; display: flex; align-items: center; gap: 14px; }
.drop .icon {
  width: 44px; height: 44px; border-radius: 12px;
  margin: 0 auto 10px;
  background: linear-gradient(135deg, var(--accent-wash), var(--gold-wash));
  border: 1px solid var(--accent-soft);
  display: grid; place-items: center; color: var(--accent);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.5);
}
.drop.compact .icon { margin: 0; width: 36px; height: 36px; }
.drop strong { display: block; font-size: 14px; font-weight: 600; color: var(--ink); }
.drop .hint { font-size: 12px; color: var(--ink-faint); margin-top: 2px; }
.drop .or { display: inline-flex; gap: 8px; align-items: center; margin-top: 10px; font-size: 11px; color: var(--ink-faint); font-style: italic; font-family: var(--font-display); }
.drop .or:before, .drop .or:after { content: ""; display: block; width: 24px; height: 1px; background: rgba(28, 25, 23, 0.12); }
.pick-folder {
  margin-top: 8px; display: inline-flex; gap: 6px; align-items: center;
  padding: 7px 12px; border-radius: 8px; font-size: 12px; font-weight: 500;
  background: var(--paper-raised); color: var(--ink-muted);
  border: 1px solid rgba(28, 25, 23, 0.1); cursor: pointer;
}
.pick-folder:hover { color: var(--accent-deep); background: var(--accent-wash); border-color: var(--accent-soft); }

/* queue */
.queue { margin-top: 14px; display: flex; flex-direction: column; gap: 10px; }
.qrow {
  display: grid; grid-template-columns: auto 1fr; gap: 12px; align-items: center;
  padding: 14px; border-radius: 12px;
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.08);
  animation: card-in 300ms ease both;
}
.qrow.accepted { border-color: var(--accent-soft); background: var(--accent-wash); }
.qrow.skipped { opacity: 0.4; }
.qrow .check {
  width: 18px; height: 18px; border-radius: 5px;
  border: 1.5px solid rgba(28, 25, 23, 0.2); display: grid; place-items: center;
  cursor: pointer; transition: all 140ms; background: var(--paper-raised);
}
.qrow .check.on { background: var(--accent); border-color: var(--accent); }
.qrow .check.on::after { content: "✓"; color: var(--paper-soft); font-size: 11px; font-weight: 700; font-family: var(--font-body); }
.qrow .main { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.qrow .top { display: flex; align-items: center; gap: 10px; min-width: 0; }
.qrow .ofn {
  font-family: var(--font-mono); font-size: 12px; font-weight: 500;
  color: var(--ink); min-width: 0; flex: 1;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.qrow .stat {
  font-family: var(--font-mono); font-size: 10px; font-weight: 500;
  padding: 2px 8px; border-radius: 999px; letter-spacing: 0.04em;
  background: rgba(28, 25, 23, 0.05); color: var(--ink-muted);
  border: 1px solid rgba(28, 25, 23, 0.08); white-space: nowrap;
}
.qrow .stat.extracting, .qrow .stat.naming, .qrow .stat.hashing {
  color: var(--gold); background: var(--gold-wash);
  border-color: rgba(184, 131, 62, 0.25);
}
.qrow .stat.extracting::before, .qrow .stat.naming::before {
  content: "●"; margin-right: 5px; animation: pulse 1.2s ease-in-out infinite;
}
.qrow .stat.ready { color: var(--accent); background: var(--accent-wash); border-color: var(--accent-soft); }
.qrow .stat.duplicate { color: var(--danger); background: var(--danger-wash); border-color: rgba(155, 63, 47, 0.2); }
.qrow .stat.error { color: var(--danger); background: var(--danger-wash); border-color: rgba(155, 63, 47, 0.2); }
.qrow .stat.accepted { color: var(--accent); background: var(--accent-wash); border-color: var(--accent-soft); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

.qrow .bottom { display: grid; grid-template-columns: 1fr 110px; gap: 10px; align-items: center; }
.name-input {
  background: var(--paper-raised);
  border: 1px solid rgba(28, 25, 23, 0.1); border-radius: 8px;
  padding: 7px 10px; font: 12.5px var(--font-mono); color: var(--ink);
  outline: none; transition: all 140ms ease; letter-spacing: 0.01em;
}
.name-input:focus { border-color: var(--accent); background: var(--paper-raised); box-shadow: 0 0 0 3px var(--accent-wash); }
.name-input.live { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-wash); }
@keyframes caret { 0%,49%{opacity:1;} 50%,100%{opacity:0;} }

.cat-input {
  background: var(--paper-raised);
  border: 1px solid rgba(28, 25, 23, 0.1); border-radius: 8px;
  padding: 7px 10px; font: 11px var(--font-body); color: var(--ink-muted);
  outline: none; text-align: center; font-weight: 500;
}
.cat-input:focus { border-color: var(--accent); color: var(--accent-deep); box-shadow: 0 0 0 3px var(--accent-wash); }
.qrow.needs-client .cat-input {
  border-color: var(--danger);
  background: var(--danger-wash);
  color: var(--danger);
}
.qrow.needs-client .cat-input::placeholder { color: var(--danger); opacity: 0.7; }

.qrow .skip {
  width: 26px; height: 26px; border-radius: 7px;
  background: var(--paper-raised); border: 1px solid rgba(28, 25, 23, 0.1);
  color: var(--ink-faint); font-size: 14px; line-height: 1; display: grid; place-items: center;
}
.qrow .skip:hover { color: var(--danger); background: var(--danger-wash); border-color: rgba(155, 63, 47, 0.2); }

.drawer-foot {
  padding: 16px 24px; border-top: 1px solid rgba(28, 25, 23, 0.08);
  display: flex; align-items: center; gap: 12px;
}
.drawer-foot .summary { flex: 1; font-size: 12px; color: var(--ink-faint); font-family: var(--font-mono); letter-spacing: 0.02em; }
.drawer-foot .summary b { color: var(--ink); font-weight: 600; }
.btn-ghost {
  padding: 8px 14px; border-radius: 9px; font-size: 12.5px; font-weight: 500;
  background: var(--paper-raised); border: 1px solid rgba(28, 25, 23, 0.1);
  color: var(--ink-muted); font-family: var(--font-body);
}
.btn-ghost:hover { color: var(--ink); background: var(--paper-shade); border-color: rgba(28, 25, 23, 0.15); }
.btn-primary {
  padding: 9px 16px; border-radius: 10px; font-size: 12.5px; font-weight: 600;
  background: var(--accent);
  color: var(--paper-soft);
  border: 1px solid var(--accent-deep);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.12), 0 4px 14px -4px rgba(45, 95, 62, 0.4);
  transition: all 180ms cubic-bezier(.2,.9,.2,1);
  font-family: var(--font-body); letter-spacing: 0.005em;
}
.btn-primary:disabled { opacity: 0.35; filter: saturate(0.4); }
.btn-primary:hover:not(:disabled) { transform: translateY(-1px); background: var(--accent-deep); box-shadow: 0 6px 18px -4px rgba(45, 95, 62, 0.5); }

/* ═══════════════════════════════════════════════════════════════════
   TOOLTIP & TOAST
   ═══════════════════════════════════════════════════════════════════ */
.tip {
  position: fixed; pointer-events: none;
  min-width: 300px; max-width: 380px;
  padding: 14px 16px; border-radius: 14px;
  background: rgba(251, 250, 245, 0.96);
  backdrop-filter: blur(24px) saturate(160%);
  -webkit-backdrop-filter: blur(24px) saturate(160%);
  border: 1px solid rgba(28, 25, 23, 0.1);
  box-shadow: var(--shadow-lg);
  opacity: 0; transform: translateY(4px);
  transition: opacity 160ms ease, transform 160ms ease;
  z-index: 200;
}
.tip.show { opacity: 1; transform: none; }
.tip-hd {
  font-family: var(--font-mono); font-size: 9.5px;
  text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--ink-faint); font-weight: 600; margin-bottom: 4px;
}
.tip-og {
  font-family: var(--font-mono); font-size: 12.5px;
  color: var(--ink); font-weight: 500; word-break: break-all;
  line-height: 1.45; margin-bottom: 12px;
}
.tip-sep { height: 1px; background: rgba(28, 25, 23, 0.08); margin: 10px 0; }
.tip-row { display: flex; justify-content: space-between; gap: 14px; font-size: 11.5px; margin: 5px 0; }
.tip-row .k { color: var(--ink-faint); font-family: var(--font-mono); letter-spacing: 0.04em; text-transform: uppercase; font-size: 10px; font-weight: 500; }
.tip-row .v { color: var(--ink); font-family: var(--font-mono); text-align: right; }

.tip-echoes { display: flex; flex-direction: column; gap: 6px; margin-top: 2px; }
.tip-echo {
  display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
  padding: 4px 8px; border-radius: 6px;
  background: var(--gold-wash); border: 1px solid rgba(184, 131, 62, 0.18);
}
.tip-echo-name {
  font-family: var(--font-mono); font-size: 11px; color: var(--ink);
  min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tip-echo-when {
  font-family: var(--font-mono); font-size: 10px; color: var(--gold);
  font-style: italic; white-space: nowrap;
}
.tip-echo-more {
  font-family: var(--font-display); font-style: italic;
  font-size: 11px; color: var(--ink-faint); text-align: center; padding-top: 2px;
}

.toast {
  position: fixed; bottom: 28px; left: 50%; transform: translate(-50%, 20px);
  padding: 12px 20px; border-radius: 14px;
  background: rgba(28, 25, 23, 0.92);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  border: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow: var(--shadow-lg); font-size: 13.5px;
  color: var(--paper-soft);
  opacity: 0; z-index: 300; pointer-events: none;
  transition: opacity 220ms cubic-bezier(.2,.9,.2,1), transform 220ms cubic-bezier(.2,.9,.2,1);
  display: inline-flex; align-items: center; gap: 10px;
  font-family: var(--font-display); font-style: italic; font-weight: 400;
  letter-spacing: 0.005em;
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast b { font-family: var(--font-body); font-style: normal; font-weight: 600; }
.toast .ok { color: #9FD4A8; font-style: normal; font-family: var(--font-body); font-weight: 600; }
.toast .er { color: #E4A49A; font-style: normal; font-family: var(--font-body); font-weight: 600; }

/* ═══════════════════════════════════════════════════════════════════
   KEYBOARD SHORTCUT OVERLAY
   ═══════════════════════════════════════════════════════════════════ */
.shortcut-scrim {
  position: fixed; inset: 0; background: rgba(28, 25, 23, 0.22);
  backdrop-filter: blur(4px); z-index: 180;
  opacity: 0; pointer-events: none; transition: opacity 180ms ease;
}
.shortcut-scrim.open { opacity: 1; pointer-events: auto; }
.shortcut-overlay {
  position: fixed; top: 50%; left: 50%;
  transform: translate(-50%, -48%); z-index: 200;
  width: min(400px, 90vw); padding: 22px 24px;
  border-radius: 18px; opacity: 0; pointer-events: none;
  transition: opacity 200ms ease, transform 200ms ease;
}
.shortcut-overlay.open {
  opacity: 1; transform: translate(-50%, -50%); pointer-events: auto;
}
.shortcut-hd {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 12px; margin-bottom: 12px;
  border-bottom: 1px solid rgba(28, 25, 23, 0.08);
}
.shortcut-title {
  font-family: var(--font-display); font-weight: 400; font-size: 20px;
  letter-spacing: -0.01em; color: var(--ink);
}
.shortcut-close {
  width: 26px; height: 26px; border-radius: 7px; font-size: 18px; line-height: 1;
  color: var(--ink-faint); background: var(--paper-soft); border: 1px solid rgba(28,25,23,0.08);
}
.shortcut-close:hover { color: var(--ink); background: var(--paper-raised); }
.shortcut-body { display: flex; flex-direction: column; gap: 8px; }
.sk-row {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 2px; font-size: 13px; color: var(--ink-muted);
}
.sk-row span { margin-left: auto; color: var(--ink-muted); }
.sk-row kbd {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 22px; height: 22px; padding: 0 6px;
  background: var(--paper-soft); border: 1px solid rgba(28,25,23,0.1);
  border-bottom-width: 2px; border-radius: 5px;
  font-family: var(--font-mono); font-size: 11px; font-weight: 500;
  color: var(--ink); box-shadow: 0 1px 0 rgba(28,25,23,0.04);
}
.shortcut-foot {
  display: flex; align-items: center; gap: 6px;
  margin-top: 14px; padding-top: 12px;
  border-top: 1px solid rgba(28, 25, 23, 0.08);
  font-family: var(--font-display); font-style: italic;
  font-size: 11.5px; color: var(--ink-faint);
}
.shortcut-foot svg { color: var(--accent); opacity: 0.7; flex-shrink: 0; }

.help-fab {
  position: fixed; bottom: 24px; right: 24px; z-index: 30;
  width: 34px; height: 34px; border-radius: 50%;
  background: var(--paper-raised); border: 1px solid rgba(28,25,23,0.1);
  color: var(--ink-muted); font-weight: 600; font-size: 15px;
  box-shadow: var(--shadow-sm); transition: all 140ms ease;
}
.help-fab:hover {
  color: var(--accent-deep); background: var(--accent-wash);
  border-color: var(--accent-soft); transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}

/* ═══════════════════════════════════════════════════════════════════
   BATCH CLIENT PICKER (top of the upload drawer)
   ═══════════════════════════════════════════════════════════════════ */
.batch-client-box {
  padding: 14px 16px 16px;
  border-radius: 14px;
  background: var(--paper-soft);
  border: 1px solid rgba(28, 25, 23, 0.08);
  margin-bottom: 14px;
}
.bc-label {
  font-family: var(--font-mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--ink-faint); font-weight: 600; margin-bottom: 8px;
}
.bc-row {
  display: flex; align-items: center; gap: 10px;
}
.bc-input {
  flex: 1; min-width: 0;
  padding: 9px 12px; border-radius: 10px;
  background: var(--paper-raised);
  border: 1px solid rgba(28, 25, 23, 0.1);
  font: 14px var(--font-body); color: var(--ink);
  outline: none; transition: all 140ms ease; font-weight: 500;
}
.bc-input:focus {
  border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-wash);
}
.bc-hint {
  font-size: 11px; color: var(--ink-faint);
  font-family: var(--font-display); font-style: italic; white-space: nowrap;
}
.bc-hint.warn { color: var(--danger); }
.bc-hint.info { color: var(--accent); }

.bc-tax {
  margin-top: 10px;
}
.bc-tax:empty { margin-top: 0; }
.bc-tax-label {
  font-family: var(--font-mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--ink-faint); font-weight: 500; margin-bottom: 6px;
}
.bc-tax-pills {
  display: flex; flex-wrap: wrap; gap: 6px;
}
.bc-tax-pill {
  display: inline-flex; align-items: center;
  padding: 4px 10px; border-radius: 999px;
  font: 11.5px var(--font-body); font-weight: 500;
  background: rgba(45, 95, 62, 0.06);
  border: 1px solid rgba(45, 95, 62, 0.18);
  color: var(--accent-deep); letter-spacing: 0.01em;
}
.bc-tax-add {
  padding: 4px 10px; border-radius: 999px;
  font: 11.5px var(--font-display); font-style: italic; font-weight: 400;
  background: transparent; border: 1px dashed rgba(28, 25, 23, 0.18);
  color: var(--ink-faint); cursor: pointer; transition: all 140ms ease;
}
.bc-tax-add:hover {
  border-color: var(--accent); color: var(--accent-deep); background: var(--accent-wash);
}

/* ═══════════════════════════════════════════════════════════════════
   EXPANDABLE CLIENTS + NESTED BUCKETS
   ═══════════════════════════════════════════════════════════════════ */
.cat { grid-template-columns: 14px 1fr auto auto; padding: 8px 12px 8px 10px; }
.cat .chev {
  display: grid; place-items: center; color: var(--ink-faint);
  transition: transform 140ms ease; line-height: 0;
}
.cat .chev.open { transform: rotate(90deg); color: var(--accent); }
.cat .cat-label { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cat.active .cat-label { font-weight: 500; }

.bucket-row {
  display: grid; grid-template-columns: 24px 1fr auto auto;
  align-items: center; gap: 8px; padding: 6px 10px 6px 26px;
  color: var(--ink-faint); font-size: 12px; cursor: pointer;
  border-radius: 8px; transition: all 140ms ease;
  position: relative;
}
.bucket-row::before {
  content: ""; position: absolute; left: 18px; top: -4px; bottom: 50%;
  border-left: 1px solid rgba(28, 25, 23, 0.08);
  border-bottom: 1px solid rgba(28, 25, 23, 0.08);
  border-bottom-left-radius: 6px; width: 8px;
}
.bucket-row:hover { background: rgba(28, 25, 23, 0.03); color: var(--ink); }
.bucket-row.active {
  background: var(--accent-wash); color: var(--accent-deep); font-weight: 500;
}
.bucket-row.active::before { border-color: var(--accent-soft); }
.bucket-row.dragover { background: var(--accent-soft); color: var(--accent-deep); }
.bucket-row .bucket-dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: currentColor; opacity: 0.5; justify-self: center;
}
.bucket-row.active .bucket-dot { opacity: 1; background: var(--accent); }
.bucket-row .count {
  font-family: var(--font-mono); font-size: 10px; color: var(--ink-faint);
  padding: 1px 6px; border-radius: 999px; background: rgba(28, 25, 23, 0.04);
}
.bucket-row.active .count { color: var(--accent-deep); background: var(--accent-soft); }
.bucket-row .del-bucket {
  opacity: 0; color: var(--ink-faint); font-size: 11px; background: transparent; border: none;
  width: 16px; height: 16px; border-radius: 3px; line-height: 1;
  transition: all 140ms ease;
}
.bucket-row:hover .del-bucket { opacity: 1; }
.bucket-row .del-bucket:hover { color: var(--danger); background: var(--danger-wash); }

.bucket-add-row {
  display: grid; grid-template-columns: 24px 1fr auto;
  align-items: center; gap: 8px; padding: 6px 10px 6px 26px;
  color: var(--ink-faint); font-size: 11.5px; cursor: pointer;
  border-radius: 8px; transition: all 140ms ease;
  font-family: var(--font-display); font-style: italic;
  opacity: 0.65; position: relative;
}
.bucket-add-row::before {
  content: ""; position: absolute; left: 18px; top: -4px; bottom: 50%;
  border-left: 1px solid rgba(28, 25, 23, 0.08);
  border-bottom: 1px solid rgba(28, 25, 23, 0.08);
  border-bottom-left-radius: 6px; width: 8px;
}
.bucket-add-row:hover { opacity: 1; color: var(--accent-deep); background: var(--accent-wash); }
.bucket-add-row .bucket-dot.add {
  width: 5px; height: 5px; border-radius: 50%;
  background: currentColor; opacity: 0.5; justify-self: center;
  border: 1px dashed currentColor; background: transparent;
}
.bucket-add-row .plus-tiny {
  font-family: var(--font-mono); font-size: 13px; font-weight: 600;
  font-style: normal;
}

/* ═══════════════════════════════════════════════════════════════════
   SECTION HEADERS (grouped-by-bucket)
   ═══════════════════════════════════════════════════════════════════ */
.section { margin-bottom: 28px; }
.section:last-child { margin-bottom: 0; }
.section-head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px;
  padding-bottom: 8px; border-bottom: 1px solid rgba(28, 25, 23, 0.06);
}
.section-label {
  font-family: var(--font-display); font-weight: 400; font-size: 16px;
  letter-spacing: -0.01em; color: var(--ink);
}
.section-count {
  font-family: var(--font-mono); font-size: 10.5px;
  color: var(--ink-faint); letter-spacing: 0.02em;
}

/* ═══════════════════════════════════════════════════════════════════
   CARD BUCKET PILL + SELECTED STATE
   ═══════════════════════════════════════════════════════════════════ */
.card { position: relative; padding-bottom: 38px; }
.card .bucket-pill {
  position: absolute; bottom: 10px; left: 14px; right: 14px;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 9px; border-radius: 8px;
  background: rgba(28, 25, 23, 0.04);
  border: 1px solid rgba(28, 25, 23, 0.08);
  color: var(--ink-muted); font-family: var(--font-body); font-size: 11px; font-weight: 500;
  cursor: pointer; transition: all 140ms ease; justify-content: flex-start;
}
.card .bucket-pill svg { opacity: 0.55; flex-shrink: 0; }
.card .bucket-pill:hover {
  background: var(--accent-wash); border-color: var(--accent-soft); color: var(--accent-deep);
}
.card .bucket-pill:hover svg { opacity: 0.85; color: var(--accent); }
.card.is-selected {
  border-color: var(--accent); box-shadow: var(--shadow-md), 0 0 0 2px var(--accent-wash);
}

/* ═══════════════════════════════════════════════════════════════════
   BUCKET POPOVER
   ═══════════════════════════════════════════════════════════════════ */
.bucket-popover {
  position: fixed; z-index: 180;
  width: 240px; padding: 10px;
  border-radius: 12px;
  animation: pop-in 140ms cubic-bezier(.2,.9,.2,1);
}
@keyframes pop-in {
  from { opacity: 0; transform: translateY(4px) scale(0.98); }
  to   { opacity: 1; transform: none; }
}
.bp-head {
  display: flex; align-items: baseline; gap: 8px;
  padding: 4px 8px 10px; border-bottom: 1px solid rgba(28, 25, 23, 0.08);
}
.bp-title {
  font-family: var(--font-mono); font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.12em; color: var(--ink-faint); font-weight: 600;
}
.bp-sub { font-size: 11.5px; color: var(--ink-muted); margin-left: auto; font-family: var(--font-mono); }
.bp-options { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
.bp-option {
  display: flex; align-items: center; padding: 7px 10px; border-radius: 7px;
  font-family: var(--font-body); font-size: 13px; color: var(--ink-muted); text-align: left;
  background: transparent; border: none; transition: all 120ms ease;
}
.bp-option:hover { background: var(--accent-wash); color: var(--accent-deep); }
.bp-option.current {
  background: var(--accent-soft); color: var(--accent-deep); font-weight: 500;
}
.bp-option.current::after {
  content: "✓"; margin-left: auto; opacity: 0.7;
}
.bp-option.bp-new {
  font-style: italic; font-family: var(--font-display); color: var(--ink-faint);
  margin-top: 4px; border-top: 1px dashed rgba(28, 25, 23, 0.06); padding-top: 10px;
}

/* ═══════════════════════════════════════════════════════════════════
   BULK ACTION BAR
   ═══════════════════════════════════════════════════════════════════ */
.bulk-bar {
  position: fixed; bottom: 24px; left: 50%;
  transform: translate(-50%, 20px); opacity: 0; pointer-events: none;
  z-index: 60; padding: 8px 10px;
  border-radius: 14px; display: flex; align-items: center; gap: 12px;
  transition: opacity 200ms cubic-bezier(.2,.9,.2,1), transform 200ms cubic-bezier(.2,.9,.2,1);
}
.bulk-bar.show { opacity: 1; transform: translate(-50%, 0); pointer-events: auto; }
.bb-count {
  font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted);
  padding: 0 10px;
}
.bb-count b { color: var(--ink); font-weight: 600; }
.bb-actions { display: flex; align-items: center; gap: 4px; }
.bb-label {
  font-family: var(--font-mono); font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.12em; color: var(--ink-faint); font-weight: 600; padding-right: 6px;
}
.bb-bucket {
  padding: 6px 12px; border-radius: 8px; font-size: 12.5px; font-weight: 500;
  background: var(--paper-soft); border: 1px solid rgba(28, 25, 23, 0.08);
  color: var(--ink); transition: all 140ms ease; font-family: var(--font-body);
}
.bb-bucket:hover {
  background: var(--accent-wash); border-color: var(--accent-soft); color: var(--accent-deep);
}
.bb-clear {
  padding: 6px 10px; border-radius: 8px; font-size: 12px;
  background: transparent; color: var(--ink-faint); border: 1px solid transparent;
  margin-left: 4px;
}
.bb-clear:hover { color: var(--danger); background: var(--danger-wash); }

.undo-link {
  color: var(--accent); font-family: var(--font-body); font-style: normal;
  font-weight: 600; text-decoration: underline; cursor: pointer;
  margin-left: 4px; pointer-events: auto;
}

/* miscellaneous hidden inputs */
.hidden { display: none !important; }
</style>
</head>
<body>

<div class="backdrop"></div>
<div class="aurora"></div>
<div class="strata"></div>

<main class="shell glass">

  <!-- ═══════════════ SIDEBAR ═══════════════ -->
  <aside class="sidebar">
    <div class="brand">
      <div class="mark">
        <svg class="strata-mark" viewBox="0 0 24 24" width="26" height="26" fill="none" aria-hidden="true">
          <line x1="9"  y1="7"  x2="17" y2="7"  stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          <line x1="7"  y1="12" x2="19" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          <line x1="5"  y1="17" x2="21" y2="17" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
        <div class="wordmark">
          <div class="w1">Aprio</div>
          <div class="w2">Strata</div>
        </div>
      </div>
    </div>

    <div class="side-section">
      <div class="side-label-row">
        <div class="side-label">Clients</div>
        <button class="side-add" id="btn-new-client" title="New client">+</button>
      </div>
      <div class="cat-list" id="cat-list"></div>
    </div>

    <div class="side-bottom">
      <button class="btn-deposit" id="btn-open-drawer">
        <span class="plus">+</span> Deposit files
      </button>
      <div class="side-local" title="No files, metadata, or telemetry leaves this Mac.">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        Runs entirely on your machine
      </div>
    </div>
  </aside>

  <!-- ═══════════════ CONTENT ═══════════════ -->
  <section class="content">
    <div class="toolbar">
      <div class="breadcrumb">
        <div class="ctx" id="ctx-title"></div>
        <div class="meta" id="ctx-meta"></div>
      </div>
      <div class="toolbar-spacer"></div>
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="search" placeholder="Search samples…" />
      </div>
    </div>

    <div class="body" id="body">
      <!-- grid / preview / empty state injected here -->
    </div>
  </section>
</main>

<!-- ═══════════════ UPLOAD DRAWER ═══════════════ -->
<div class="drawer-scrim" id="scrim"></div>
<aside class="drawer glass glass--elevated" id="drawer">
  <div class="drawer-head">
    <div class="t">Deposit new samples</div>
    <div class="s">We'll examine each and sort it into its layer.</div>
    <button class="close" id="btn-close-drawer">×</button>
  </div>
  <div class="drawer-body">
    <div class="batch-client-box">
      <div class="bc-label">Client for this batch</div>
      <div class="bc-row">
        <input id="batch-client" class="bc-input" placeholder="Pick or create a client…" list="clients-datalist" />
        <span class="bc-hint" id="batch-client-hint">Required before triage</span>
      </div>
      <div class="bc-tax" id="batch-client-tax"></div>
    </div>
    <div id="drop" class="drop">
      <div class="icon">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>
      </div>
      <strong>Drop files or a folder</strong>
      <div class="hint">click to pick files  ·  drop a folder for its whole contents</div>
      <input id="file" type="file" multiple class="hidden"
        accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.webp,.tif,.tiff" />
    </div>
    <div class="queue" id="queue"></div>
  </div>
  <div class="drawer-foot">
    <div class="summary" id="drawer-summary">0 queued</div>
    <button class="btn-ghost" id="process">Triage</button>
    <button class="btn-primary" id="apply" disabled>Commit</button>
  </div>
</aside>

<div class="tip" id="tip"></div>
<div class="toast" id="toast"></div>
<datalist id="clients-datalist"></datalist>
<datalist id="buckets-datalist"></datalist>

<!-- keyboard shortcut overlay — toggled with ? -->
<div class="shortcut-scrim" id="shortcut-scrim"></div>
<aside class="shortcut-overlay glass glass--elevated" id="shortcut-overlay">
  <div class="shortcut-hd">
    <div class="shortcut-title">Keyboard</div>
    <button class="shortcut-close" id="shortcut-close">×</button>
  </div>
  <div class="shortcut-body">
    <div class="sk-row"><kbd>/</kbd><span>Focus search</span></div>
    <div class="sk-row"><kbd>D</kbd><span>Open deposit drawer</span></div>
    <div class="sk-row"><kbd>⌘</kbd><kbd>↵</kbd><span>Commit (in drawer)</span></div>
    <div class="sk-row"><kbd>Esc</kbd><span>Close drawer / preview / this overlay</span></div>
    <div class="sk-row"><kbd>?</kbd><span>Toggle this help</span></div>
  </div>
  <div class="shortcut-foot">
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    Runs entirely on your machine. No cloud, no telemetry.
  </div>
</aside>
<button class="help-fab" id="help-fab" title="Keyboard shortcuts (?)">?</button>

<script>
// ══════════════════════════════════════════════════════════════════
// STATE
// ══════════════════════════════════════════════════════════════════
const $  = id => document.getElementById(id);
const qs = (sel, ctx=document) => ctx.querySelector(sel);
const esc = s => (s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

const state = {
  files: [],                 // sorted library (from /sorter/files)
  clients: [],               // all known clients (includes empty folders)
  taxonomies: {},            // client_name -> [bucket, bucket, ...]
  activeClient: null,        // client name currently selected
  activeBucket: null,        // null = all buckets for that client; string = specific bucket
  expanded: new Set(),       // clients currently expanded in the sidebar
  search: "",
  previewing: null,
  rows: new Map(),           // upload queue
  selectedCards: new Set(),  // multi-select in content pane (final_names)
  batchClient: "",           // the single client for the current deposit batch
};

// ══════════════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════════════
function fmtSize(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n/1048576).toFixed(2) + " MB";
  return (n/1073741824).toFixed(2) + " GB";
}
function fmtAgo(iso) {
  if (!iso) return "";
  const s = Math.floor((Date.now() - new Date(iso).getTime())/1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s/60) + " min ago";
  if (s < 86400) return Math.floor(s/3600) + " h ago";
  if (s < 604800) return Math.floor(s/86400) + " d ago";
  return new Date(iso).toLocaleDateString();
}

// ══════════════════════════════════════════════════════════════════
// DATA
// ══════════════════════════════════════════════════════════════════
async function loadFiles() {
  try {
    const r = await fetch("/sorter/files");
    const d = await r.json();
    state.files = d.files || [];
    state.clients = d.clients || [];
    state.taxonomies = d.client_taxonomies || {};
  } catch { state.files = []; state.clients = []; state.taxonomies = {}; }
  // Pick a sensible default on first load: client with the most-recent file.
  if (state.activeClient === null) {
    const clients = allClients();
    if (clients.length) {
      const latestByClient = new Map();
      for (const f of state.files) {
        if (!f.client) continue;
        const prev = latestByClient.get(f.client);
        if (!prev || f.applied_at > prev) latestByClient.set(f.client, f.applied_at);
      }
      const mostRecent = [...latestByClient.entries()].sort((a,b) => b[1].localeCompare(a[1]))[0];
      state.activeClient = mostRecent ? mostRecent[0] : clients[0];
      state.expanded.add(state.activeClient);
    }
  }
  renderSidebar();
  render();
  refreshClientDatalist();
}

function bucketsFor(client) {
  return state.taxonomies[client] || ["Finance", "Tax", "Legal"];
}

function allClients() {
  // union of manifest.clients + clients inferred from files
  const s = new Set(state.clients);
  for (const f of state.files) {
    if (!f.is_duplicate && f.client) s.add(f.client);
  }
  s.delete("Unassigned"); // always placed at end
  return [...s].sort((a, b) => a.localeCompare(b));
}

function refreshClientDatalist() {
  const dl = document.getElementById("clients-datalist");
  if (dl) dl.innerHTML = allClients().map(c => `<option value="${esc(c)}"></option>`).join("");
  const bdl = document.getElementById("buckets-datalist");
  if (bdl) {
    const all = new Set();
    for (const c of allClients()) bucketsFor(c).forEach(b => all.add(b));
    ["Finance", "Tax", "Legal", "Unfiled"].forEach(b => all.add(b));
    bdl.innerHTML = [...all].sort().map(b => `<option value="${esc(b)}"></option>`).join("");
  }
}

// ══════════════════════════════════════════════════════════════════
// SIDEBAR
// ══════════════════════════════════════════════════════════════════
function renderSidebar() {
  const list = $("cat-list");
  list.innerHTML = "";

  // Counts: files per client AND per client+bucket combo.
  const clientCounts = new Map();
  const bucketCounts = new Map();
  for (const c of allClients()) clientCounts.set(c, 0);
  for (const f of state.files) {
    const c = f.client || "Unassigned";
    clientCounts.set(c, (clientCounts.get(c) || 0) + 1);
    const b = f.bucket || "Unfiled";
    bucketCounts.set(c + "::" + b, (bucketCounts.get(c + "::" + b) || 0) + 1);
  }
  const hasUnassigned = state.files.some(f => f.client === "Unassigned" || !f.client);

  const entries = [...clientCounts.entries()].sort((a, b) => {
    if (a[0] === "Unassigned") return 1;
    if (b[0] === "Unassigned") return -1;
    return a[0].localeCompare(b[0]);
  }).filter(([c, n]) => c !== "Unassigned" || (hasUnassigned && n > 0));

  for (const [name, n] of entries) {
    const isExpanded = state.expanded.has(name);
    const isActiveClient = state.activeClient === name && state.activeBucket === null;

    const clientRow = document.createElement("div");
    clientRow.className = "cat" + (isActiveClient ? " active" : "");
    clientRow.dataset.client = name;
    const canDelete = name !== "Unassigned" && n === 0;
    clientRow.innerHTML = `
      <div class="chev ${isExpanded ? "open" : ""}">
        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
      <div class="cat-label">${esc(name)}</div>
      ${canDelete ? '<button class="del" title="Remove client folder">×</button>' : ""}
      <div class="count">${n}</div>`;
    list.appendChild(clientRow);

    if (isExpanded) {
      const fromTax = bucketsFor(name);
      const fromFiles = new Set();
      for (const f of state.files) {
        if ((f.client || "Unassigned") === name) fromFiles.add(f.bucket || "Unfiled");
      }
      const bucketsToShow = [...new Set([...fromTax, ...fromFiles])].sort((a, b) => {
        if (a === "Unfiled") return 1; if (b === "Unfiled") return -1;
        return a.localeCompare(b);
      });

      for (const b of bucketsToShow) {
        const bc = bucketCounts.get(name + "::" + b) || 0;
        const isActiveBucket = state.activeClient === name && state.activeBucket === b;
        const canDeleteBucket = bc === 0 && b !== "Unfiled";
        const bRow = document.createElement("div");
        bRow.className = "bucket-row" + (isActiveBucket ? " active" : "");
        bRow.dataset.client = name;
        bRow.dataset.bucket = b;
        bRow.innerHTML = `
          <div class="bucket-dot"></div>
          <div class="bucket-label">${esc(b)}</div>
          ${canDeleteBucket ? '<button class="del-bucket" title="Remove empty bucket">×</button>' : ""}
          <div class="count">${bc}</div>`;
        list.appendChild(bRow);
      }

      const addRow = document.createElement("div");
      addRow.className = "bucket-add-row";
      addRow.dataset.client = name;
      addRow.innerHTML = `<div class="bucket-dot add"></div><div class="bucket-label">Add bucket</div><span class="plus-tiny">+</span>`;
      list.appendChild(addRow);
    }
  }

  wireCategories();
}

function wireCategories() {
  document.querySelectorAll(".cat").forEach(el => {
    const clientName = el.dataset.client;
    if (!clientName) return;
    el.onclick = e => {
      if (e.target.closest(".del")) return;
      // Click on the chevron = just toggle expand. Click anywhere else = also set active.
      const chevClick = e.target.closest(".chev");
      if (state.expanded.has(clientName)) {
        if (chevClick) {
          state.expanded.delete(clientName);
        } else {
          // already active? collapse. Otherwise select.
          if (state.activeClient === clientName && state.activeBucket === null) {
            state.expanded.delete(clientName);
          }
          selectClient(clientName, null);
          return;
        }
      } else {
        state.expanded.add(clientName);
        selectClient(clientName, null);
        return;
      }
      renderSidebar();
      render();
    };
    el.ondragover = ev => { if (state.draggingFinalName) { ev.preventDefault(); el.classList.add("dragover"); } };
    el.ondragleave = () => el.classList.remove("dragover");
    el.ondrop = async ev => {
      ev.preventDefault();
      el.classList.remove("dragover");
      const final_name = state.draggingFinalName;
      if (!final_name) return;
      try {
        await fetch("/sorter/move", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({final_name, client: clientName})
        });
        toast(`Moved to <b>${esc(clientName)}</b>`, "ok");
        await loadFiles();
      } catch { toast("Move failed", "er"); }
    };
    const delBtn = el.querySelector(".del");
    if (delBtn) delBtn.onclick = async ev => {
      ev.stopPropagation();
      if (!confirm(`Remove the empty "${clientName}" folder?`)) return;
      try {
        await fetch("/sorter/client/delete", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({name: clientName})
        });
        if (state.activeClient === clientName) { state.activeClient = null; state.activeBucket = null; }
        state.expanded.delete(clientName);
        await loadFiles();
      } catch { toast("Couldn't remove folder", "er"); }
    };
  });

  document.querySelectorAll(".bucket-row").forEach(el => {
    const { client, bucket } = el.dataset;
    el.onclick = e => {
      if (e.target.closest(".del-bucket")) return;
      selectClient(client, bucket);
    };
    el.ondragover = ev => { if (state.draggingFinalName) { ev.preventDefault(); el.classList.add("dragover"); } };
    el.ondragleave = () => el.classList.remove("dragover");
    el.ondrop = async ev => {
      ev.preventDefault();
      el.classList.remove("dragover");
      const final_name = state.draggingFinalName;
      if (!final_name) return;
      try {
        await Promise.all([
          fetch("/sorter/move", {
            method: "POST", headers: {"Content-Type":"application/json"},
            body: JSON.stringify({final_name, client})
          }),
          fetch("/sorter/bucket/move", {
            method: "POST", headers: {"Content-Type":"application/json"},
            body: JSON.stringify({final_name, bucket})
          }),
        ]);
        toast(`Moved to <b>${esc(client)}</b> · <b>${esc(bucket)}</b>`, "ok");
        await loadFiles();
      } catch { toast("Move failed", "er"); }
    };
    const delBtn = el.querySelector(".del-bucket");
    if (delBtn) delBtn.onclick = async ev => {
      ev.stopPropagation();
      if (!confirm(`Remove the empty "${bucket}" bucket from ${client}?`)) return;
      try {
        const r = await fetch("/sorter/bucket/remove", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({client, bucket})
        });
        const d = await r.json();
        if (!d.ok) { toast(d.error || "Couldn't remove", "er"); return; }
        if (state.activeBucket === bucket) state.activeBucket = null;
        await loadFiles();
      } catch { toast("Couldn't remove bucket", "er"); }
    };
  });

  document.querySelectorAll(".bucket-add-row").forEach(el => {
    const { client } = el.dataset;
    el.onclick = async () => {
      const name = prompt(`New bucket name for ${client}:`);
      if (!name) return;
      try {
        const r = await fetch("/sorter/bucket/add", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({client, bucket: name})
        });
        const d = await r.json();
        if (!d.ok) { toast(d.error || "Invalid bucket name", "er"); return; }
        await loadFiles();
        toast(`Added bucket <b>${esc(d.bucket)}</b>`, "ok");
      } catch { toast("Couldn't add bucket", "er"); }
    };
  });
}

$("btn-new-client").onclick = async () => {
  const name = prompt("New client folder name:");
  if (!name) return;
  try {
    const r = await fetch("/sorter/client/create", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({name})
    });
    const d = await r.json();
    if (!d.ok) { toast("Invalid client name", "er"); return; }
    await loadFiles();
    selectClient(d.name, null);
    state.expanded.add(d.name);
    renderSidebar();
    toast(`Created <b>${esc(d.name)}</b>`, "ok");
  } catch { toast("Couldn't create client", "er"); }
};

function selectClient(client, bucket) {
  state.activeClient = client;
  state.activeBucket = bucket;
  state.previewing = null;
  state.selectedCards.clear();
  renderSidebar();
  render();
}

// ══════════════════════════════════════════════════════════════════
// MAIN RENDER
// ══════════════════════════════════════════════════════════════════
function getVisibleFiles() {
  if (!state.activeClient) return [];
  let arr = state.files.filter(f => (f.client || "Unassigned") === state.activeClient);
  if (state.activeBucket) arr = arr.filter(f => (f.bucket || "Unfiled") === state.activeBucket);
  if (state.search) {
    const q = state.search.toLowerCase();
    arr = arr.filter(f =>
      (f.final_name||"").toLowerCase().includes(q) ||
      (f.original||"").toLowerCase().includes(q) ||
      (f.bucket||"").toLowerCase().includes(q)
    );
  }
  return arr;
}

function render() {
  const files = getVisibleFiles();
  const title = state.activeClient
    ? (state.activeBucket
        ? `${state.activeClient} › ${state.activeBucket}`
        : state.activeClient)
    : "No clients yet";
  $("ctx-title").textContent = title;
  $("ctx-meta").textContent = `${files.length} file${files.length !== 1 ? "s" : ""}`;

  const body = $("body");
  if (state.previewing) { renderPreview(body); return; }
  if (!files.length) {
    const isEmptyLib = !state.activeClient;
    body.innerHTML = `
      <div class="empty">
        <div class="big">${isEmptyLib ? "The library is quiet." : "This folder is bare."}</div>
        <div class="small">${isEmptyLib ? "Create a client in the sidebar, or deposit files to get started." : "Nothing here yet. Drag files in from another folder or deposit new ones."}</div>
        <button class="ghost-btn" onclick="openDrawer()">+ Deposit files</button>
        <div class="local-note">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Everything runs on this machine. No cloud, no telemetry.
        </div>
      </div>`;
    return;
  }

  // If we're at a client level (no specific bucket), group by bucket with section headers.
  if (state.activeClient && !state.activeBucket) {
    const byBucket = new Map();
    for (const f of files) {
      const b = f.bucket || "Unfiled";
      if (!byBucket.has(b)) byBucket.set(b, []);
      byBucket.get(b).push(f);
    }
    const order = [...bucketsFor(state.activeClient), ...[...byBucket.keys()].filter(b => !bucketsFor(state.activeClient).includes(b))];
    const sections = [];
    for (const b of order) {
      const list = byBucket.get(b);
      if (!list || !list.length) continue;
      sections.push(`
        <div class="section">
          <div class="section-head">
            <div class="section-label">${esc(b)}</div>
            <div class="section-count">${list.length}</div>
          </div>
          <div class="grid">${list.map(fileCard).join("")}</div>
        </div>`);
    }
    body.innerHTML = sections.join("");
  } else {
    body.innerHTML = `<div class="grid">${files.map(fileCard).join("")}</div>`;
  }
  body.querySelectorAll(".card").forEach(el => wireCard(el));
}

function fileCard(f) {
  const echoes = f.echo_count || 0;
  const bucket = f.bucket || "Unfiled";
  return `
    <div class="card${state.selectedCards.has(f.final_name) ? " is-selected" : ""}" data-name="${esc(f.final_name)}" data-bucket="${esc(bucket)}" draggable="true">
      ${echoes > 0 ? `<div class="echo-chip" title="${echoes} echo${echoes !== 1 ? "es" : ""}"><span class="ripple"></span>${echoes}</div>` : ""}
      <div class="fname">${esc(f.final_name)}</div>
      <div class="meta">
        <span>${fmtSize(f.size)}</span>
        <span class="sep">·</span>
        <span>${fmtAgo(f.applied_at)}</span>
      </div>
      <button class="bucket-pill" data-role="bucket-pill" title="Click to change bucket">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h18"/></svg>
        <span>${esc(bucket)}</span>
      </button>
    </div>`;
}

function wireCard(el) {
  const name = el.dataset.name;
  const file = state.files.find(f => f.final_name === name);
  if (!file) return;
  let hoverTimer = null, lastEvt = null;
  el.onmouseenter = e => {
    lastEvt = e;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => showTip(lastEvt, file), 500);
  };
  el.onmousemove = e => {
    lastEvt = e;
    if (tip.classList.contains("show")) positionTip(e);
  };
  el.onmouseleave = () => {
    clearTimeout(hoverTimer);
    hoverTimer = null;
    hideTip();
  };
  el.onclick = e => {
    clearTimeout(hoverTimer); hideTip();
    // bucket pill click → open popover, don't open preview
    if (e.target.closest("[data-role='bucket-pill']")) {
      e.stopPropagation();
      openBucketPopover(el, file);
      return;
    }
    // Shift or Cmd/Ctrl click → multi-select instead of preview
    if (e.shiftKey || e.metaKey || e.ctrlKey) {
      e.preventDefault();
      if (state.selectedCards.has(name)) state.selectedCards.delete(name);
      else state.selectedCards.add(name);
      el.classList.toggle("is-selected");
      updateBulkBar();
      return;
    }
    openPreview(file);
  };
  el.ondragstart = ev => {
    clearTimeout(hoverTimer); hideTip();
    state.draggingFinalName = name;
    el.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move";
  };
  el.ondragend = () => {
    state.draggingFinalName = null;
    el.classList.remove("dragging");
  };
}

// ══════════════════════════════════════════════════════════════════
// BUCKET POPOVER (per-card quick reassign)
// ══════════════════════════════════════════════════════════════════
let _bucketPopoverEl = null;
function closeBucketPopover() {
  if (_bucketPopoverEl) { _bucketPopoverEl.remove(); _bucketPopoverEl = null; }
  document.removeEventListener("click", _bucketPopoverOutside, true);
  document.removeEventListener("keydown", _bucketPopoverKeydown);
}
function _bucketPopoverOutside(e) {
  if (_bucketPopoverEl && !_bucketPopoverEl.contains(e.target)) closeBucketPopover();
}
function _bucketPopoverKeydown(e) {
  if (e.key === "Escape") closeBucketPopover();
}
function openBucketPopover(cardEl, file) {
  closeBucketPopover();
  const client = file.client || "Unassigned";
  const current = file.bucket || "Unfiled";
  const buckets = [...new Set([...bucketsFor(client), "Unfiled"])];
  const pop = document.createElement("div");
  pop.className = "bucket-popover glass glass--elevated";
  pop.innerHTML = `
    <div class="bp-head">
      <span class="bp-title">Move to</span>
      <span class="bp-sub">${esc(client)}</span>
    </div>
    <div class="bp-options">
      ${buckets.map(b => `<button class="bp-option${b === current ? " current" : ""}" data-b="${esc(b)}">${esc(b)}</button>`).join("")}
      <button class="bp-option bp-new" data-b="__new__">+ New bucket…</button>
    </div>`;
  document.body.appendChild(pop);
  _bucketPopoverEl = pop;
  // Position above/below the pill
  const r = cardEl.querySelector("[data-role='bucket-pill']").getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x = r.left, y = r.top - ph - 6;
  if (y < 8) y = r.bottom + 6;
  if (x + pw > innerWidth - 8) x = innerWidth - pw - 8;
  pop.style.left = x + "px"; pop.style.top = y + "px";

  pop.querySelectorAll(".bp-option").forEach(btn => {
    btn.onclick = async () => {
      let target = btn.dataset.b;
      if (target === "__new__") {
        target = prompt(`New bucket for ${client}:`);
        if (!target) { closeBucketPopover(); return; }
      }
      if (target === current) { closeBucketPopover(); return; }
      closeBucketPopover();
      await moveBucket(file.final_name, target, current, client);
    };
  });

  setTimeout(() => {
    document.addEventListener("click", _bucketPopoverOutside, true);
    document.addEventListener("keydown", _bucketPopoverKeydown);
  }, 0);
}

async function moveBucket(final_name, target, from, client) {
  try {
    const r = await fetch("/sorter/bucket/move", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({final_name, bucket: target})
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "failed");
    await loadFiles();
    toast(`Moved to <b>${esc(target)}</b>  ·  <a class="undo-link" data-fn="${esc(final_name)}" data-from="${esc(from)}" data-client="${esc(client)}" data-to="${esc(target)}">Undo</a>`, "ok", 6000);
    wireUndoLink();
  } catch { toast("Couldn't move bucket", "er"); }
}

function wireUndoLink() {
  const link = toastEl.querySelector(".undo-link");
  if (!link) return;
  link.onclick = async e => {
    e.preventDefault();
    const { fn, from, client } = link.dataset;
    toastEl.classList.remove("show");
    try {
      await fetch("/sorter/bucket/move", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({final_name: fn, bucket: from})
      });
      await loadFiles();
      toast(`Reverted to <b>${esc(from)}</b>`, "ok");
    } catch { toast("Undo failed", "er"); }
  };
}

function updateBulkBar() {
  let bar = $("bulk-bar");
  const n = state.selectedCards.size;
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "bulk-bar";
    bar.className = "bulk-bar glass glass--elevated";
    document.body.appendChild(bar);
  }
  if (n === 0) { bar.classList.remove("show"); return; }
  // Derive the union of possible buckets from the selected cards' clients
  const clients = new Set();
  for (const name of state.selectedCards) {
    const f = state.files.find(x => x.final_name === name);
    if (f?.client) clients.add(f.client);
  }
  const buckets = new Set();
  for (const c of clients) bucketsFor(c).forEach(b => buckets.add(b));
  buckets.add("Unfiled");
  const bOptions = [...buckets].sort();
  bar.innerHTML = `
    <div class="bb-count"><b>${n}</b> selected</div>
    <div class="bb-actions">
      <div class="bb-label">Move to</div>
      ${bOptions.map(b => `<button class="bb-bucket" data-b="${esc(b)}">${esc(b)}</button>`).join("")}
      <button class="bb-clear">Clear</button>
    </div>`;
  bar.classList.add("show");
  bar.querySelectorAll(".bb-bucket").forEach(btn => {
    btn.onclick = async () => {
      const target = btn.dataset.b;
      const targets = [...state.selectedCards];
      for (const fn of targets) {
        await fetch("/sorter/bucket/move", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({final_name: fn, bucket: target})
        });
      }
      state.selectedCards.clear();
      await loadFiles();
      toast(`Moved <b>${targets.length}</b> file${targets.length !== 1 ? "s" : ""} to <b>${esc(target)}</b>`, "ok");
      updateBulkBar();
    };
  });
  bar.querySelector(".bb-clear").onclick = () => {
    state.selectedCards.clear();
    document.querySelectorAll(".card.is-selected").forEach(c => c.classList.remove("is-selected"));
    updateBulkBar();
  };
}

// ══════════════════════════════════════════════════════════════════
// PREVIEW
// ══════════════════════════════════════════════════════════════════
function openPreview(f) {
  state.previewing = f;
  render();
}
function closePreview() {
  state.previewing = null;
  render();
}

function renderPreview(body) {
  const f = state.previewing;
  const echoes = f.echoes || [];
  let echoSection = "";
  if (echoes.length) {
    const rows = [...echoes].reverse().map(ec => `
      <div class="preview-echo">
        <div class="preview-echo-name">${esc(ec.original)}</div>
        <div class="preview-echo-when">${fmtAgo(ec.uploaded_at)}</div>
      </div>`).join("");
    echoSection = `
      <div class="meta-section-head">Echoes  ·  <em>${echoes.length}</em></div>
      <div class="preview-echoes">${rows}</div>`;
  }
  body.innerHTML = `
    <div class="preview-wrap">
      <div class="preview-frame">
        <iframe src="${f.url}#toolbar=0&navpanes=0" title="${esc(f.final_name)}"></iframe>
      </div>
      <div class="preview-meta glass--inner" style="padding:18px">
        <h3>Sample details</h3>
        <div class="meta-row"><div class="k">New name</div><div class="v">${esc(f.final_name)}</div></div>
        <div class="meta-row"><div class="k">Original</div><div class="v">${esc(f.original || "(unknown)")}</div></div>
        <div class="meta-row"><div class="k">Client</div><div class="v body">${esc(f.client || "Unassigned")}</div></div>
        <div class="meta-row"><div class="k">Size</div><div class="v">${fmtSize(f.size)}</div></div>
        <div class="meta-row"><div class="k">Deposited</div><div class="v body">${fmtAgo(f.applied_at)}</div></div>
        ${f.hash ? `<div class="meta-row"><div class="k">SHA-256</div><div class="v">${esc(f.hash.slice(0,24))}…</div></div>` : ""}
        ${echoSection}
        <div class="preview-actions">
          <button class="pa-btn" onclick="closePreview()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
            Back to ${esc(state.activeCategory || "")}
          </button>
          <a class="pa-btn" href="${f.url}" target="_blank" rel="noopener">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
            Open in new tab
          </a>
        </div>
      </div>
    </div>`;
}

// ══════════════════════════════════════════════════════════════════
// HOVER TOOLTIP
// ══════════════════════════════════════════════════════════════════
const tip = $("tip");
function showTip(e, f) {
  const echoes = f.echoes || [];
  let echoSection = "";
  if (echoes.length) {
    const rows = echoes.slice(-4).reverse().map(ec => `
      <div class="tip-echo">
        <div class="tip-echo-name">${esc(ec.original)}</div>
        <div class="tip-echo-when">${fmtAgo(ec.uploaded_at)}</div>
      </div>`).join("");
    const more = echoes.length > 4 ? `<div class="tip-echo-more">+${echoes.length - 4} earlier</div>` : "";
    echoSection = `
      <div class="tip-sep"></div>
      <div class="tip-hd">Echoes · ${echoes.length}</div>
      <div class="tip-echoes">${rows}${more}</div>`;
  }
  tip.innerHTML = `
    <div class="tip-hd">Sample details</div>
    <div class="tip-og">${esc(f.original || "(unknown)")}</div>
    <div class="tip-sep"></div>
    <div class="tip-row"><div class="k">new</div><div class="v">${esc(f.final_name)}</div></div>
    <div class="tip-row"><div class="k">client</div><div class="v">${esc(f.client || "Unassigned")}</div></div>
    <div class="tip-row"><div class="k">size</div><div class="v">${fmtSize(f.size)}</div></div>
    <div class="tip-row"><div class="k">deposited</div><div class="v">${fmtAgo(f.applied_at)}</div></div>
    ${f.hash ? `<div class="tip-row"><div class="k">sha-256</div><div class="v">${esc(f.hash.slice(0,12))}…</div></div>` : ""}
    ${echoSection}`;
  tip.style.display = "block";
  positionTip(e);
  requestAnimationFrame(() => tip.classList.add("show"));
}
function positionTip(e) {
  const pad = 14;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 10) x = e.clientX - w - pad;
  if (y + h > innerHeight - 10) y = e.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() {
  tip.classList.remove("show");
  setTimeout(() => { if (!tip.classList.contains("show")) tip.style.display = "none"; }, 160);
}

// ══════════════════════════════════════════════════════════════════
// TOAST
// ══════════════════════════════════════════════════════════════════
const toastEl = $("toast");
function toast(html, kind="", ms=2800) {
  toastEl.innerHTML = (kind ? `<span class="${kind}">${kind==="ok"?"●":"!"}</span>` : "") + " " + html;
  toastEl.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove("show"), ms);
}

// ══════════════════════════════════════════════════════════════════
// DRAWER
// ══════════════════════════════════════════════════════════════════
const drawer = $("drawer"), scrim = $("scrim");
function openDrawer() {
  drawer.classList.add("open"); scrim.classList.add("open");
  // If nothing's been set and there's a sidebar-active client, use that as the default.
  if (!state.batchClient && state.activeClient) {
    state.batchClient = state.activeClient;
    $("batch-client").value = state.batchClient;
  }
  renderBatchClientTaxonomy();
  updateSummary();
  setTimeout(() => { if (!state.batchClient) $("batch-client").focus(); }, 250);
}
function closeDrawer() { drawer.classList.remove("open"); scrim.classList.remove("open"); }

// Wire the batch client input (script runs at end of body, DOM is ready).
const _bcInput = $("batch-client");
if (_bcInput) {
  _bcInput.addEventListener("input", e => {
    state.batchClient = e.target.value.trim();
    updateSummary();
  });
}
$("btn-open-drawer").onclick = openDrawer;
$("btn-close-drawer").onclick = closeDrawer;
scrim.onclick = closeDrawer;

// Keyboard shortcuts
const scOverlay = $("shortcut-overlay"), scScrim = $("shortcut-scrim");
function openShortcuts() { scOverlay.classList.add("open"); scScrim.classList.add("open"); }
function closeShortcuts() { scOverlay.classList.remove("open"); scScrim.classList.remove("open"); }
$("help-fab").onclick = openShortcuts;
$("shortcut-close").onclick = closeShortcuts;
scScrim.onclick = closeShortcuts;

document.addEventListener("keydown", e => {
  // Ignore shortcuts when user is typing in an input / textarea / contenteditable
  const inField = e.target.matches("input, textarea, [contenteditable='true']");
  if (e.key === "Escape") {
    if (scOverlay.classList.contains("open")) { closeShortcuts(); return; }
    if (drawer.classList.contains("open")) { closeDrawer(); return; }
    if (state.previewing) { closePreview(); return; }
    return;
  }
  if (inField) return;
  if (e.key === "/") { e.preventDefault(); $("search").focus(); return; }
  if (e.key === "?") { e.preventDefault(); scOverlay.classList.contains("open") ? closeShortcuts() : openShortcuts(); return; }
  if (e.key.toLowerCase() === "d" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); openDrawer(); return; }
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    if (drawer.classList.contains("open") && !$("apply").disabled) {
      e.preventDefault(); $("apply").click();
    }
    return;
  }
});

// drop zone
const drop = $("drop"), fileInput = $("file");
drop.onclick = e => {
  // Skip synthetic clicks that bubble back from the hidden file input.
  if (e.target.closest("input")) return;
  fileInput.click();
};
["dragenter","dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("hover"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", async ev => {
  ev.preventDefault();
  drop.classList.remove("hover");
  if (ev.dataTransfer.items?.length && ev.dataTransfer.items[0].webkitGetAsEntry) {
    const files = await readAllEntries(ev.dataTransfer.items);
    addFiles(files);
  } else addFiles(ev.dataTransfer.files);
});
fileInput.onchange = e => { addFiles(e.target.files); fileInput.value=""; };

async function readAllEntries(items) {
  const files = [];
  async function walk(entry) {
    if (entry.isFile) files.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      while (true) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e);
      }
    }
  }
  const tasks = [];
  for (const it of items) {
    const entry = it.webkitGetAsEntry?.();
    if (entry) tasks.push(walk(entry));
    else if (it.getAsFile) { const f = it.getAsFile(); if (f) files.push(f); }
  }
  await Promise.all(tasks);
  return files;
}

function addFiles(list) {
  let added = 0;
  // If we're currently viewing a specific client folder, every new row gets
  // that client pre-filled — you don't have to retype for each file.
  const contextClient = (state.activeCategory && state.activeCategory !== "__dup__" && state.activeCategory !== "__empty__")
    ? state.activeCategory : "";
  for (const f of list) {
    if (!f.name || f.name.startsWith(".")) continue;
    const id = "loc-" + Math.random().toString(36).slice(2, 10);
    state.rows.set(id, {
      id, file: f, name: f.name, size: f.size,
      status: "queued", accepted: true, final_name: "",
      client: contextClient,
      clientLocked: !!contextClient,   // user's context takes priority over Gemma's guess
    });
    renderQueueRow(id);
    added++;
  }
  if (added) {
    drop.classList.add("compact");
    updateSummary();
    toast(`${added} sample${added !== 1 ? "s" : ""} queued${contextClient ? ` for <b>${esc(contextClient)}</b>` : ""}.`, "ok");
  }
}

function hasBatchClient() {
  return !!(state.batchClient && state.batchClient.trim() && state.batchClient.trim().toLowerCase() !== "unassigned");
}

function updateSummary() {
  const arr = [...state.rows.values()];
  const acceptedRows = arr.filter(r => r.accepted && (r.status === "ready" || r.status === "duplicate"));
  const ready = acceptedRows.length;
  const total = arr.length;
  const bc = hasBatchClient();
  let summary = `<b>${total}</b> queued  ·  <b>${ready}</b> ready to commit`;
  if (!bc) summary += `  ·  <span style="color:var(--danger)">pick a client first</span>`;
  $("drawer-summary").innerHTML = summary;
  $("apply").disabled = ready === 0 || !bc;
  $("apply").title = !bc ? "Pick a client at the top of the drawer first" : "";
  $("process").disabled = !arr.some(r => r.status === "queued") || !bc;
  $("process").title = !bc ? "Pick a client at the top of the drawer first" : "";
  renderBatchClientTaxonomy();
}

function renderBatchClientTaxonomy() {
  const box = $("batch-client-tax");
  if (!box) return;
  const hint = $("batch-client-hint");
  const bc = state.batchClient;
  if (!bc || !hasBatchClient()) {
    box.innerHTML = "";
    if (hint) { hint.textContent = "Required before triage"; hint.className = "bc-hint warn"; }
    return;
  }
  const known = allClients().includes(bc);
  if (hint) {
    hint.textContent = known ? "" : "New client — will be created on commit";
    hint.className = "bc-hint" + (known ? "" : " info");
  }
  const buckets = [...new Set([...bucketsFor(bc), "Unfiled"])];
  box.innerHTML = `
    <div class="bc-tax-label">Taxonomy</div>
    <div class="bc-tax-pills">
      ${buckets.map(b => `<span class="bc-tax-pill">${esc(b)}</span>`).join("")}
      <button class="bc-tax-add" id="bc-tax-add">+ Add bucket</button>
    </div>`;
  $("bc-tax-add").onclick = async () => {
    const name = prompt(`New bucket for ${bc}:`);
    if (!name) return;
    try {
      const r = await fetch("/sorter/bucket/add", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({client: bc, bucket: name})
      });
      const d = await r.json();
      if (!d.ok) { toast(d.error || "Invalid bucket name", "er"); return; }
      await loadFiles();
      renderBatchClientTaxonomy();
    } catch { toast("Couldn't add bucket", "er"); }
  };
}

function renderQueueRow(id) {
  const r = state.rows.get(id);
  let el = document.getElementById("row-" + id);
  if (!el) {
    el = document.createElement("div");
    el.className = "qrow"; el.id = "row-" + id;
    el.innerHTML = `
      <div class="check on" data-role="check"></div>
      <div class="main">
        <div class="top">
          <div class="ofn" data-role="ofn"></div>
          <div class="stat queued" data-role="stat"></div>
          <button class="skip" data-role="skip" title="Remove">×</button>
        </div>
        <div class="bottom">
          <input class="name-input" data-role="name" placeholder="—" disabled />
          <input class="cat-input" data-role="bucket" placeholder="Bucket…" list="buckets-datalist" disabled />
        </div>
      </div>`;
    $("queue").appendChild(el);
    el.querySelector('[data-role="check"]').onclick = e => {
      r.accepted = !r.accepted;
      e.currentTarget.classList.toggle("on", r.accepted);
      el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
      el.classList.toggle("skipped", !r.accepted);
      updateSummary();
    };
    el.querySelector('[data-role="name"]').oninput = e => r.final_name = e.target.value;
    el.querySelector('[data-role="bucket"]').oninput = e => {
      r.bucket = e.target.value;
      r.bucketLocked = true;
    };
    el.querySelector('[data-role="skip"]').onclick = () => {
      state.rows.delete(id); el.remove(); updateSummary();
    };
  }
  qs('[data-role="ofn"]', el).textContent = r.name;
  const statEl = qs('[data-role="stat"]', el);
  statEl.className = "stat " + r.status;
  const labelMap = {queued:"queued", hashing:"hashing", extracting:"reading layers", naming:"examining", ready:"ready", duplicate:"echo", error:"error", accepted:"deposited"};
  let label = labelMap[r.status] || r.status;
  if (r.status === "duplicate" && r.dup_of) label = `echo of ${r.dup_of}`;
  statEl.textContent = label;
  statEl.title = r.status === "duplicate" ? "This file is identical (byte-for-byte) to one already in the library. Approving it will add an echo record to the canonical file." : "";
  const nameInput = qs('[data-role="name"]', el);
  nameInput.value = r.final_name || "";
  nameInput.disabled = !(r.status === "ready" || r.status === "duplicate" || r.status === "error" || r.status === "naming");
  const bucketInput = qs('[data-role="bucket"]', el);
  bucketInput.value = r.bucket || "";
  bucketInput.disabled = !(r.status === "ready" || r.status === "duplicate");
  el.classList.toggle("accepted", r.accepted && (r.status === "ready" || r.status === "duplicate"));
  el.classList.toggle("skipped", !r.accepted);
}


$("process").onclick = async () => {
  const queued = [...state.rows.values()].filter(r => r.status === "queued");
  if (!queued.length) return;
  $("process").disabled = true;

  const fd = new FormData();
  fd.append("batch_client", state.batchClient || "");
  const localIds = [];
  for (const r of queued) {
    fd.append("files", r.file, r.name);
    localIds.push(r.id);
    r.status = "hashing"; renderQueueRow(r.id);
    r.client = state.batchClient;  // lock-in batch client for this row
  }
  updateSummary();

  let resp;
  try { resp = await fetch("/sorter/process", { method: "POST", body: fd }); }
  catch { toast("Network error.", "er"); $("process").disabled = false; return; }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", serverIdx = -1;
  const idMap = {};
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const raw = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!raw) continue;
      let ev; try { ev = JSON.parse(raw); } catch { continue; }
      handleStreamEvent(ev, localIds, idMap, s => serverIdx = s, () => serverIdx);
    }
  }
  updateSummary();
};

function handleStreamEvent(ev, localIds, idMap, setIdx, getIdx) {
  if (ev.type === "row_start") {
    const newIdx = getIdx() + 1; setIdx(newIdx);
    const local = localIds[newIdx];
    idMap[ev.id] = local;
    const r = state.rows.get(local);
    r.server_id = ev.id; r.hash = ev.hash; r.status = "extracting";
    renderQueueRow(local);
  } else if (ev.type === "row_step") {
    const r = state.rows.get(idMap[ev.id]); r.status = ev.step; renderQueueRow(idMap[ev.id]);
  } else if (ev.type === "row_token") {
    // LIVE-TYPING: append token to name input character by character
    const local = idMap[ev.id];
    const r = state.rows.get(local);
    if (!r) return;
    r.live_name = (r.live_name || "") + ev.tok;
    // render the in-progress name in the input (sanitized snake_case approximation)
    const approx = snakify(r.live_name);
    r.final_name = approx;
    const el = document.getElementById("row-" + local);
    if (el) {
      const inp = qs('[data-role="name"]', el);
      inp.disabled = false;
      inp.value = approx;
      inp.classList.add("live");
    }
  } else if (ev.type === "row_done") {
    const r = state.rows.get(idMap[ev.id]);
    if (!r) return;
    const gemmaClient = ev.client || "";
    const newClient = r.clientLocked && r.client
      ? r.client
      : (gemmaClient && gemmaClient !== "Unassigned" ? gemmaClient : (r.client || ""));
    const gemmaBucket = ev.bucket || "";
    const newBucket = r.bucketLocked && r.bucket
      ? r.bucket
      : (gemmaBucket || (r.bucket || ""));
    Object.assign(r, {
      status: ev.status, suggested: ev.suggested, final_name: ev.suggested,
      client: newClient,
      bucket: newBucket,
      gemma_bucket: gemmaBucket,
      preview: ev.preview, dup_of: ev.dup_of, error: ev.error, temp_path: ev.temp_path,
    });
    const el = document.getElementById("row-" + idMap[ev.id]);
    if (el) qs('[data-role="name"]', el).classList.remove("live");
    renderQueueRow(idMap[ev.id]);
  }
  updateSummary();
}

function snakify(s) {
  return (s||"").toLowerCase().replace(/[^a-z0-9_\-\s]/g, "").replace(/\s+/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "").slice(0, 80);
}

$("apply").onclick = async () => {
  const decisions = [];
  for (const r of state.rows.values()) {
    if (!r.temp_path) continue;
    if (!r.accepted) { decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"skip"}); continue; }
    if (r.status === "duplicate") decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"duplicate", dup_of:r.dup_of, client:state.batchClient, bucket:r.bucket, gemma_bucket:r.gemma_bucket, preview:r.preview});
    else if (r.status === "ready" || r.status === "error") decisions.push({id:r.server_id, temp_path:r.temp_path, original:r.name, hash:r.hash, status:"accept", final_name:r.final_name || r.suggested, client:state.batchClient, bucket:r.bucket, gemma_bucket:r.gemma_bucket, preview:r.preview});
  }
  if (!decisions.filter(d => d.status !== "skip").length) return;
  $("apply").disabled = true;
  const resp = await fetch("/sorter/apply", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({decisions}),
  });
  const data = await resp.json();
  let ok = 0, echoes = 0;
  const echoedCanonicals = new Set();
  for (const res of data.results) {
    if (res.ok && res.echoed_into) { echoes++; echoedCanonicals.add(res.echoed_into); }
    else if (res.ok && !res.skipped) ok++;
    if (res.ok && (res.final_path || res.echoed_into)) {
      for (const r of state.rows.values()) if (r.server_id === res.id) { r.status = "accepted"; r.final_path = res.final_path; renderQueueRow(r.id); break; }
    }
  }
  // Pick a message that actually tells the user what happened — especially
  // in the case where everything was an echo (no grid changes, easy to miss).
  let msg;
  if (ok && echoes)   msg = `<b>${ok}</b> new ${ok!==1?"files":"file"} deposited  ·  <b>${echoes}</b> echo${echoes!==1?"es":""} recorded`;
  else if (ok)        msg = `<b>${ok}</b> ${ok!==1?"files":"file"} settled into strata`;
  else if (echoes)    msg = `All <b>${echoes}</b> ${echoes!==1?"files were duplicates":"file was a duplicate"} — recorded as echo${echoes!==1?"es":""} on existing documents`;
  else                msg = `nothing to commit`;
  toast(msg + ".", "ok", 5000);
  await loadFiles();
  // Briefly pulse every canonical card that just received an echo, so the
  // user can see where their uploads landed.
  if (echoedCanonicals.size) {
    requestAnimationFrame(() => {
      for (const name of echoedCanonicals) {
        const card = document.querySelector(`.card[data-name="${CSS.escape(name)}"]`);
        if (card) {
          card.classList.add("just-echoed");
          setTimeout(() => card.classList.remove("just-echoed"), 2400);
        }
      }
    });
  }
  updateSummary();
  $("apply").disabled = false;
};

// search
$("search").oninput = e => {
  state.search = e.target.value;
  render();
};

// ══════════════════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════════════════
loadFiles();
updateSummary();
</script>
</body>
</html>
"""


_FONTS_CSS_PATH = BASE / "fonts" / "fonts.local.css"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def sorter_index():
    font_face_css = ""
    if _FONTS_CSS_PATH.exists():
        font_face_css = _FONTS_CSS_PATH.read_text()
    return SORTER_HTML.replace("__FONT_FACES__", font_face_css)
