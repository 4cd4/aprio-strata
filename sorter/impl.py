"""File sorter: MinerU (first 3 pages) + configurable LLM → snake_case rename.

Mounted as /sorter from app.py.

Flow:
1. User drops files in UI → each upload lands under `inputs/_sorter/` in a human-readable job folder (see `_ingest_triage_to_job_dir`).
2. For each file: hash it, run MinerU on pages 0-2 (CLI or ``mineru-api`` via ``MINERU_API_URL``), read markdown,
   call the configured LLM provider for a snake_case name suggestion.
3. Duplicates (by SHA-256) are flagged against `sorted/.manifest.json`.
4. User reviews, edits, hits Apply — approved copies go to `sorted/`,
   duplicates to `sorted/duplicates/`, manifest updated.

Analyst corrections for training are appended to `sorted/.analyst_events.jsonl`
(legacy `sorted/.bucket_changes.jsonl` is still read for export/summary).
See GET `/sorter/analyst-events/summary` and `/sorter/analyst-events/download`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from azure_store import AuthContext, store as azure_store
from mineru_runtime import mineru_api_base, run_mineru as run_mineru_job
from finance_extract import (
    artifact_to_csv,
    artifact_to_xlsx_bytes,
    avg_seconds_per_page,
    extract_financial_statements,
    list_local_artifacts,
    pdf_page_count,
    read_local_artifact,
    record_completion,
    update_review,
    write_local_artifact,
)

BASE = Path(__file__).resolve().parents[1]  # repo root
SORTER_TMP = BASE / "inputs" / "_sorter"
SORTED = BASE / "sorted"
DUPES = SORTED / "duplicates"
MANIFEST_PATH = SORTED / ".manifest.json"
SORTER_TMP.mkdir(parents=True, exist_ok=True)
SORTED.mkdir(parents=True, exist_ok=True)
DUPES.mkdir(parents=True, exist_ok=True)

STRATA_AI_PROVIDER = (os.environ.get("STRATA_AI_PROVIDER") or "azure_openai").strip().lower()
OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "gemma4:e4b"
AZURE_OPENAI_ENDPOINT = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY") or ""
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or ""
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION") or "2024-10-21"
MAX_MARKDOWN_CHARS = 1800
ALLOWED_AI_PROVIDERS = {"azure_openai", "ollama", "document_intelligence"}
_p = STRATA_AI_PROVIDER.replace("-", "_")
_runtime_ai_provider = (
    "ollama"
    if _p == "ollama"
    else (
        "document_intelligence"
        if _p in ("document_intelligence", "doc_intelligence", "di")
        else "azure_openai"
    )
)

router = APIRouter(prefix="/sorter")


def _azure_active() -> bool:
    return azure_store.enabled


def _require_auth_context(request: Request) -> AuthContext | None:
    if not _azure_active():
        return None
    try:
        return azure_store.auth_context(request.headers.get("authorization"))
    except LookupError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Azure Entra ID authentication required") from exc


def _load_manifest_for(ctx: AuthContext | None) -> dict:
    if ctx and _azure_active():
        return azure_store.load_manifest(ctx.firm_id)
    return _load_manifest()


def _log_event(ctx: AuthContext | None, event: dict) -> None:
    log_training_event(event)
    if ctx and _azure_active():
        try:
            azure_store.log_event(ctx, event)
        except Exception:
            # Local JSONL remains the durable fallback if Azure services are briefly down.
            pass


def _normalize_ai_provider(provider: str | None) -> str:
    p = (provider or "").strip().lower().replace("-", "_")
    if p in ("azure", "azure_openai", "azure_openai_chat"):
        return "azure_openai"
    if p == "ollama":
        return "ollama"
    if p in ("document_intelligence", "doc_intelligence", "di"):
        return "document_intelligence"
    raise ValueError("provider must be azure_openai, ollama, or document_intelligence")


def _effective_provider(provider: str | None = None) -> str:
    if provider:
        return _normalize_ai_provider(provider)
    return _runtime_ai_provider


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


@router.get("/azure/config")
async def azure_config():
    return JSONResponse(azure_store.public_config())


@router.get("/ai/provider")
async def get_ai_provider():
    return JSONResponse({
        "provider": _runtime_ai_provider,
        "default_provider": _normalize_ai_provider(STRATA_AI_PROVIDER),
        "allowed_providers": sorted(ALLOWED_AI_PROVIDERS),
    })


@router.post("/ai/provider")
async def set_ai_provider(payload: dict):
    global _runtime_ai_provider
    requested = payload.get("provider")
    try:
        _runtime_ai_provider = _normalize_ai_provider(requested)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "provider": _runtime_ai_provider})


@router.get("/auth/me")
async def auth_me(request: Request):
    ctx = _require_auth_context(request)
    if not ctx:
        return JSONResponse({"enabled": False, "user": None})
    return JSONResponse({
        "enabled": True,
        "user": {
            "id": ctx.user_id,
            "email": ctx.email,
            "firm_id": ctx.firm_id,
            "role": ctx.role,
        },
    })


@router.post("/auth/bootstrap")
async def auth_bootstrap(request: Request, payload: dict):
    if not _azure_active():
        return JSONResponse({"ok": True, "enabled": False})
    try:
        ctx = azure_store.bootstrap_profile(
            request.headers.get("authorization"),
            payload.get("firm_name") or "My Firm",
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not bootstrap Azure profile") from exc
    return JSONResponse({
        "ok": True,
        "user": {
            "id": ctx.user_id,
            "email": ctx.email,
            "firm_id": ctx.firm_id,
            "role": ctx.role,
        },
    })


# ── filename helpers ──────────────────────────────────────────────────
_SANITIZE_RE = re.compile(r"[^a-z0-9_\-]+")


def sanitize_snake(raw: str, fallback: str) -> str:
    s = (raw or "").strip()
    # strip markdown, quotes, leading hints the model sometimes emits
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


_JOB_SLUG_MAX = 48
_IMG_REF_RE = re.compile(
    r"!\[[^\]]*\]\((images/[^)]+\.(?:jpg|jpeg|png|webp|gif))\)",
    re.I,
)


def _slug_for_job_dir_segment(raw: str) -> str:
    """Filesystem-safe slug for job folder names (Unicode word chars allowed)."""
    s = (raw or "").strip().lower()
    s = re.sub(r"[^\w\-.]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("._-")
    if not s:
        s = "document"
    if len(s) > _JOB_SLUG_MAX:
        s = s[:_JOB_SLUG_MAX].rstrip("_")
    return s


def _first_title_slug_from_markdown(md: str) -> str | None:
    """First markdown heading or substantive line, slugged for use in folder names."""
    for raw in (md or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("<") and "<table" in line.lower():
            continue
        if line.startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
        if not line or len(line) < 3:
            continue
        slug = _slug_for_job_dir_segment(line)
        if len(slug) >= 4:
            return slug
    return None


def _allocate_job_directory(base: str) -> Path:
    """Return ``SORTER_TMP / base`` (or ``base_2``, ``base_3``, …) that does not yet exist."""
    cand = SORTER_TMP / base
    if not cand.exists():
        return cand
    i = 2
    while True:
        c = SORTER_TMP / f"{base}_{i}"
        if not c.exists():
            return c
        i += 1


def _renumber_images_in_auto_folder(md_path: Path) -> None:
    """Rename ``images/<hash>.jpg`` to ``images/image_001.jpg`` in markdown order; fix JSON sidecars."""
    if not md_path.is_file():
        return
    text = md_path.read_text(encoding="utf-8", errors="replace")
    order: list[str] = []
    seen: set[str] = set()
    for m in _IMG_REF_RE.finditer(text):
        rel = m.group(1).replace("\\", "/")
        if rel not in seen:
            seen.add(rel)
            order.append(rel)
    if not order:
        return
    auto_dir = md_path.parent
    auto_resolved = auto_dir.resolve()
    mapping: dict[str, str] = {}
    for i, rel in enumerate(order, start=1):
        old_file = (auto_dir / rel).resolve()
        try:
            old_file.relative_to(auto_resolved)
        except ValueError:
            continue
        if not old_file.is_file():
            continue
        suffix = old_file.suffix.lower() or ".jpg"
        new_rel = f"images/image_{i:03d}{suffix}"
        mapping[rel] = new_rel
        dest = auto_dir / new_rel
        dest.parent.mkdir(exist_ok=True)
        if old_file != dest:
            if dest.exists():
                dest.unlink()
            old_file.rename(dest)
    if not mapping:
        return
    new_text = text
    for old_rel, new_rel in mapping.items():
        new_text = new_text.replace(old_rel, new_rel)
    md_path.write_text(new_text, encoding="utf-8")
    for json_path in auto_dir.glob("*.json"):
        raw = json_path.read_text(encoding="utf-8", errors="replace")
        out = raw
        for old_rel, new_rel in mapping.items():
            out = out.replace(old_rel, new_rel)
        if out != raw:
            json_path.write_text(out, encoding="utf-8")


def _echo_upload_dir(upload_bytes: bytes, safe_name: str, label: str) -> tuple[str, Path]:
    """Materialize bytes under ``SORTER_TMP`` for duplicate / echo flows. Returns ``(dir_name, in_path)``."""
    uniq = uuid.uuid4().hex[:6]
    slug = _slug_for_job_dir_segment(Path(safe_name).stem)
    base = f"{label}__{slug}__{uniq}"
    dest = _allocate_job_directory(base)
    dest.mkdir(parents=True, exist_ok=False)
    in_path = dest / safe_name
    in_path.write_bytes(upload_bytes)
    return dest.name, in_path


async def _ingest_triage_to_job_dir(upload_bytes: bytes, safe_name: str) -> tuple[str, Path, Path | None, str]:
    """Stage under ``_ingest``, run first-pages MinerU, rename to ``first_triage__…`` or ``first_triage_error__…``.

    Returns ``(file_id, in_path, md_path, md_text)`` after the tree is in its final location.
    """
    ingest_root = SORTER_TMP / "_ingest"
    ingest_root.mkdir(parents=True, exist_ok=True)
    staging = ingest_root / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    in_path = staging / safe_name
    in_path.write_bytes(upload_bytes)
    out_dir = staging / "out"
    out_dir.mkdir(exist_ok=True)
    md_path = await run_mineru_first_pages(in_path, out_dir, pages=3)
    md_text = ""
    if md_path and md_path.is_file():
        _renumber_images_in_auto_folder(md_path)
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
    uniq = uuid.uuid4().hex[:6]
    if not md_text.strip():
        base = f"first_triage_error__{_slug_for_job_dir_segment(Path(safe_name).stem)}__{uniq}"
    else:
        tslug = _first_title_slug_from_markdown(md_text) or _slug_for_job_dir_segment(Path(safe_name).stem)
        base = f"first_triage__{tslug}__{uniq}"
    dest = _allocate_job_directory(base)
    shutil.move(str(staging), str(dest))
    file_id = dest.name
    final_in = dest / safe_name
    new_md = next(iter(sorted(dest.rglob("*.md"))), None) if dest.exists() else None
    if new_md and new_md.is_file():
        md_text = new_md.read_text(encoding="utf-8", errors="replace")
    return file_id, final_in, new_md, md_text


# ── mineru + model runtime ────────────────────────────────────────────
async def run_mineru_first_pages(in_path: Path, out_dir: Path, pages: int = 3) -> Path | None:
    """First ``pages`` pages via MinerU CLI or ``mineru-api`` (see ``MINERU_API_URL``)."""
    out = await run_mineru_job(
        in_path,
        out_dir,
        backend="pipeline",
        page_start=0,
        page_end=max(0, pages - 1),
    )
    md = out.get("markdown_path")
    return md if isinstance(md, Path) and md.is_file() else None


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


# ── Analyst / model-correction log (append-only JSONL, local training data) ──
ANALYST_EVENTS_PATH = SORTED / ".analyst_events.jsonl"
LEGACY_ANALYST_LOG = SORTED / ".bucket_changes.jsonl"


def log_training_event(event: dict) -> None:
    """Append one JSON object per line: analyst actions vs model suggestions."""
    event = {**event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with ANALYST_EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _iter_analyst_events() -> Iterator[dict]:
    """Yield parsed events from current and legacy log files."""
    for path in (ANALYST_EVENTS_PATH, LEGACY_ANALYST_LOG):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue


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
# We remove paragraphs that start with these so the model sees the title block
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



from strata.ai.streaming import stream_azure_openai, stream_model, stream_ollama  # noqa: F401



async def suggest_name_and_bucket(
    md_text: str,
    fallback: str,
    emit_token,
    client: str,
    manifest: dict,
    *,
    provider: str | None = None,
):
    """Stream filename tokens, then infer ONE bucket from the given client's taxonomy.
    Client is fixed at the batch level (the user picks it in the drawer).
    Returns (suggested_name, bucket)."""
    eff = _effective_provider(provider)
    prompt = NAME_PROMPT.format(md=_prep_for_llm(md_text))
    raw = ""
    try:
        async for tok in stream_model(prompt, max_tokens=60, temp=0.3, provider=eff):
            raw += tok
            await emit_token(tok)
    except Exception:
        return sanitize_snake(f"error_{Path(fallback).stem}", fallback), _BUCKET_FALLBACK

    first_line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    suggested = sanitize_snake(first_line, fallback)

    try:
        buckets = client_buckets(manifest, client) if client != _CLIENT_FALLBACK else list(DEFAULT_BUCKETS)
        # Always include Unfiled as the "unsure" fallback — the model is explicitly
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
        async for tok in stream_model(b_prompt, max_tokens=10, temp=0.15, provider=eff):
            raw_b += tok
        bucket = sanitize_bucket(raw_b)
    except Exception:
        bucket = _BUCKET_FALLBACK

    return suggested, bucket


# ── routes ────────────────────────────────────────────────────────────
@router.post("/process")
async def process(
    request: Request,
    files: list[UploadFile],
    batch_client: str = Form(""),
    llm_provider: str = Form(""),
):
    """Stream NDJSON: per-file `row_start`, `row_step`, `row_done` events."""
    ctx = _require_auth_context(request)
    try:
        provider = _effective_provider(llm_provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    uploads: list[dict[str, Any]] = []
    for uf in files:
        data = await uf.read()
        file_hash = hashlib.sha256(data).hexdigest()
        safe_name = Path(uf.filename or "upload").name
        uploads.append({
            "name": safe_name,
            "size": len(data),
            "hash": file_hash,
            "data": data,
        })

    async def stream():
        def emit(o: dict) -> bytes:
            return (json.dumps(o) + "\n").encode()

        manifest = _load_manifest_for(ctx)
        # in-batch dedup
        seen_hashes_in_batch: dict[str, str] = {}

        for u in uploads:
            safe_name = u["name"]
            data = u["data"]

            # same file already sorted previously?
            prev = _find_dup(manifest, u["hash"])
            if prev is not None:
                fid, in_path = _echo_upload_dir(data, safe_name, "duplicate_echo")
                u["id"] = fid
                u["path"] = str(in_path)
                yield emit({
                    "type": "row_start",
                    "id": u["id"],
                    "name": u["name"],
                    "size": u["size"],
                    "hash": u["hash"][:12],
                })
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
                fid, in_path = _echo_upload_dir(data, safe_name, "duplicate_batch")
                u["id"] = fid
                u["path"] = str(in_path)
                yield emit({
                    "type": "row_start",
                    "id": u["id"],
                    "name": u["name"],
                    "size": u["size"],
                    "hash": u["hash"][:12],
                })
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

            # First-pages MinerU (and human-readable job dir) before row_start so ``id`` is stable.
            fid, in_path, _md_path, md_text = await _ingest_triage_to_job_dir(data, safe_name)
            u["id"] = fid
            u["path"] = str(in_path)

            yield emit({
                "type": "row_start",
                "id": u["id"],
                "name": u["name"],
                "size": u["size"],
                "hash": u["hash"][:12],
            })
            yield emit({"type": "row_step", "id": u["id"], "step": "extracting"})
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

            # Model pass — stream tokens as they arrive.
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
            # file in this process call. We still let the model predict a per-file
            # bucket against that client's taxonomy.
            bc = sanitize_client(batch_client) if batch_client else _CLIENT_FALLBACK

            async def worker():
                try:
                    result = await suggest_name_and_bucket(md_text, u["name"], emit_token_q, bc, manifest, provider=provider)
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


async def _apply_azure(payload: dict, ctx: AuthContext):
    manifest = azure_store.load_manifest(ctx.firm_id)
    results = []
    for d in payload.get("decisions", []):
        src = Path(d["temp_path"])
        original = d.get("original") or src.name
        if not src.exists():
            results.append({"id": d["id"], "ok": False, "error": "source missing"})
            continue
        full_hash = hashlib.sha256(src.read_bytes()).hexdigest()
        ext = Path(original).suffix or ".bin"
        status = d.get("status", "skip")

        if status == "accept":
            stem = sanitize_snake(d.get("final_name") or "", original)
            client_name = sanitize_client(d.get("client") or "")
            bucket_name = sanitize_bucket(d.get("bucket") or "")
            raw_gemma_name = (d.get("gemma_suggested") or d.get("suggested") or "").strip()
            gemma_stem = sanitize_snake(raw_gemma_name, original) if raw_gemma_name else None
            gemma_bucket_in = sanitize_bucket(d.get("gemma_bucket") or "")
            effective_gemma_bucket = gemma_bucket_in or bucket_name
            client_was_new = client_name != _CLIENT_FALLBACK and client_name not in manifest.get("clients", [])
            taxonomy = manifest.get("client_taxonomies", {}).get(client_name, list(DEFAULT_BUCKETS))
            taxonomy_bucket_new = bucket_name != _BUCKET_FALLBACK and bucket_name not in taxonomy
            doc = azure_store.create_document(
                ctx,
                src=src,
                original=original,
                stem=stem,
                ext=ext,
                file_hash=full_hash,
                client_name=client_name,
                bucket_name=bucket_name,
                gemma_bucket=effective_gemma_bucket,
                gemma_suggested=gemma_stem,
            )
            collision_applied = Path(doc["final_name"]).stem != stem
            _log_event(ctx, {
                "event": "deposit_accept",
                "file": doc["final_name"],
                "hash": full_hash,
                "client": client_name,
                "original_upload": original,
                "analyst_filename_stem": stem,
                "gemma_suggested_stem": gemma_stem,
                "filename_changed_from_gemma": bool(gemma_stem and gemma_stem != stem),
                "collision_suffix_applied": collision_applied,
                "analyst_bucket": bucket_name,
                "gemma_predicted_bucket": effective_gemma_bucket,
                "bucket_changed_from_gemma": bool(gemma_bucket_in and gemma_bucket_in != bucket_name),
                "client_was_new_to_library": client_was_new,
                "bucket_first_added_to_taxonomy": taxonomy_bucket_new,
                "from_bucket": None,
                "to_bucket": bucket_name,
                "markdown_preview": (d.get("preview") or "")[:200],
            })
            result = {"id": d["id"], "ok": True, "final_path": doc["final_name"]}
            results.append(result)

        elif status == "duplicate":
            canonical = azure_store.find_document_by_hash(ctx.firm_id, full_hash)
            if canonical is None:
                results.append({"id": d["id"], "ok": False, "error": "no canonical match for echo"})
            else:
                azure_store.add_echo(ctx, canonical["id"], original)
                _log_event(ctx, {
                    "event": "deposit_duplicate_echo",
                    "file": canonical["final_name"],
                    "hash": full_hash,
                    "client": canonical.get("client_name"),
                    "original_upload": original,
                    "echoed_into": canonical["final_name"],
                    "dup_of": d.get("dup_of"),
                })
                results.append({
                    "id": d["id"],
                    "ok": True,
                    "echoed_into": canonical["final_name"],
                    "note": f"echo of {canonical['final_name']}",
                })
            try: src.unlink()
            except Exception: pass
        else:
            _log_event(ctx, {
                "event": "deposit_skip",
                "original_upload": original,
                "hash": d.get("hash"),
                "temp_path": str(src),
            })
            results.append({"id": d["id"], "ok": True, "skipped": True})

    updated = azure_store.load_manifest(ctx.firm_id)
    return JSONResponse({"results": results, "sorted_count": len(updated["files"])})


@router.post("/apply")
async def apply(request: Request, payload: dict):
    """payload = {decisions: [{id, temp_path, original, status, final_name, hash, dup_of?}]}"""
    ctx = _require_auth_context(request)
    if ctx:
        return await _apply_azure(payload, ctx)
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
            raw_gemma_name = (d.get("gemma_suggested") or d.get("suggested") or "").strip()
            gemma_stem = sanitize_snake(raw_gemma_name, original) if raw_gemma_name else None
            gemma_bucket_in = sanitize_bucket(d.get("gemma_bucket") or "")
            effective_gemma_bucket = gemma_bucket_in or bucket_name
            filename_changed = bool(gemma_stem and gemma_stem != stem)
            bucket_changed = bool(gemma_bucket_in and gemma_bucket_in != bucket_name)
            collision_applied = dest.stem != stem
            manifest.setdefault("clients", [])
            client_was_new = client_name != _CLIENT_FALLBACK and client_name not in manifest["clients"]
            taxonomy_bucket_new = False
            if client_name != _CLIENT_FALLBACK:
                bucket_list = ensure_client_taxonomy(manifest, client_name)
                if bucket_name != _BUCKET_FALLBACK and bucket_name not in bucket_list:
                    bucket_list.append(bucket_name)
                    taxonomy_bucket_new = True
            entry: dict[str, Any] = {
                "hash": full_hash,
                "original": original,
                "final_name": dest.name,
                "client": client_name,
                "bucket": bucket_name,
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "echoes": [],
                "gemma_bucket": effective_gemma_bucket,
            }
            if gemma_stem:
                entry["gemma_suggested"] = gemma_stem
            manifest["files"].append(entry)
            if client_name != _CLIENT_FALLBACK and client_name not in manifest["clients"]:
                manifest["clients"].append(client_name)
            _log_event(ctx, {
                "event": "deposit_accept",
                "file": dest.name,
                "hash": full_hash,
                "client": client_name,
                "original_upload": original,
                "analyst_filename_stem": stem,
                "gemma_suggested_stem": gemma_stem,
                "filename_changed_from_gemma": filename_changed,
                "collision_suffix_applied": collision_applied,
                "analyst_bucket": bucket_name,
                "gemma_predicted_bucket": effective_gemma_bucket,
                "bucket_changed_from_gemma": bucket_changed,
                "client_was_new_to_library": client_was_new,
                "bucket_first_added_to_taxonomy": taxonomy_bucket_new,
                "from_bucket": None,
                "to_bucket": bucket_name,
                "markdown_preview": (d.get("preview") or "")[:200],
            })
            result = {"id": d["id"], "ok": True, "final_path": str(dest.relative_to(BASE))}
            results.append(result)

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
                _log_event(ctx, {
                    "event": "deposit_duplicate_echo",
                    "file": canonical["final_name"],
                    "hash": full_hash,
                    "client": canonical.get("client"),
                    "original_upload": original,
                    "echoed_into": canonical["final_name"],
                    "dup_of": d.get("dup_of"),
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
            _log_event(ctx, {
                "event": "deposit_skip",
                "original_upload": original,
                "hash": d.get("hash"),
                "temp_path": str(src),
            })
            results.append({"id": d["id"], "ok": True, "skipped": True})

    _save_manifest(manifest)
    return JSONResponse({"results": results, "sorted_count": len(manifest["files"])})


@router.get("/manifest")
async def get_manifest(request: Request):
    ctx = _require_auth_context(request)
    return JSONResponse(_load_manifest_for(ctx))


@router.get("/files")
async def list_files(request: Request):
    """Enriched manifest: adds size + disk-verified existence per entry."""
    ctx = _require_auth_context(request)
    manifest = _load_manifest_for(ctx)
    out = []
    for e in manifest["files"]:
        p = SORTED / e["final_name"]
        remote_url = azure_store.file_url(e.get("storage_path") or "") if ctx and e.get("storage_path") else None
        exists = bool(remote_url) or p.is_file()
        echoes = e.get("echoes", []) or []
        out.append({
            **e,
            "client": e.get("client") or _CLIENT_FALLBACK,
            "bucket": e.get("bucket") or _BUCKET_FALLBACK,
            "size": e.get("size") if e.get("size") is not None else (p.stat().st_size if p.is_file() else None),
            "exists": exists,
            "url": remote_url or (f"/sorted/{e['final_name']}" if p.is_file() else None),
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


@router.get("/blob/{storage_path:path}")
async def blob_download(request: Request, storage_path: str):
    ctx = _require_auth_context(request)
    if not ctx:
        raise HTTPException(status_code=404, detail="Azure storage is not configured")
    if not storage_path.startswith(f"{ctx.firm_id}/"):
        raise HTTPException(status_code=403, detail="blob is outside the current firm")
    try:
        chunks, content_type = azure_store.download_blob(storage_path)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Could not fetch blob") from exc
    return StreamingResponse(chunks, media_type=content_type)


@router.post("/deep-extract/{final_name}")
async def deep_extract(request: Request, final_name: str):
    """One-shot MinerU hybrid-auto-engine extraction for any file in any bucket.

    Streams NDJSON: ``meta`` -> ``progress``* -> ``done``. The ``done`` event
    carries the ``document_id`` and a ``download_url`` for the canonical-row
    XLSX export at ``/sorter/finance/extractions/{document_id}/export``.

    Cached by the file's content hash: if an artifact already exists for this
    document with the same hash the stream emits ``meta`` + ``done`` immediately.
    Source ``markdown`` and ``content_list.json`` are persisted as blobs alongside
    the artifact so the raw extraction is queryable later.
    """
    raw = (final_name or "").strip()
    if not raw or any(sep in raw for sep in ("/", "\\")) or ".." in raw:
        raise HTTPException(status_code=400, detail="invalid final_name")
    fn = Path(raw).name
    if fn != raw:
        raise HTTPException(status_code=400, detail="invalid final_name")

    ctx = _require_auth_context(request)
    manifest = _load_manifest_for(ctx)
    entry = next((e for e in manifest.get("files", []) if e.get("final_name") == fn), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="file not in library")

    file_hash = entry.get("hash") or ""
    document_id = entry.get("id") or file_hash  # local-only mode has no UUID
    client_name = entry.get("client") or entry.get("client_name") or "Unassigned"
    bucket_name = entry.get("bucket") or entry.get("bucket_name") or "Unfiled"

    force = (request.query_params.get("force") or "").lower() in ("1", "true", "yes")

    cached = (
        azure_store.get_financial_extraction(ctx, document_id) if ctx
        else read_local_artifact(SORTED, document_id)
    )
    if not force and cached and cached.get("hash") == file_hash:
        async def cached_stream():
            yield (json.dumps({
                "type": "meta",
                "cached": True,
                "document_id": document_id,
            }) + "\n").encode("utf-8")
            yield (json.dumps({
                "type": "done",
                "document_id": document_id,
                "row_count": (cached.get("summary") or {}).get("row_count") or 0,
                "status": cached.get("status"),
                "download_url": f"/sorter/finance/extractions/{document_id}/export?format=xlsx",
                "cached": True,
            }) + "\n").encode("utf-8")
        return StreamingResponse(cached_stream(), media_type="application/x-ndjson")

    work_base = SORTER_TMP / "_deep_extract" / uuid.uuid4().hex
    work_base.mkdir(parents=True, exist_ok=True)
    src_path: Path | None = None
    try:
        local_p = SORTED / fn
        if local_p.is_file():
            src_path = local_p
        elif ctx and entry.get("storage_path") and _azure_active():
            chunks, _ct = azure_store.download_blob(str(entry["storage_path"]))
            tmp_blob = work_base / fn
            tmp_blob.write_bytes(b"".join(chunks))
            src_path = tmp_blob
        else:
            raise HTTPException(status_code=404, detail="source file not available")
    except HTTPException:
        shutil.rmtree(work_base, ignore_errors=True)
        raise

    backend = (os.environ.get("MINERU_DEEP_BACKEND") or "hybrid-auto-engine").strip() or "hybrid-auto-engine"
    pages = pdf_page_count(src_path)
    avg_sec = avg_seconds_per_page(backend)
    eta_seconds = max(5.0, pages * avg_sec) if pages else 60.0

    async def event_stream():
        def _line(payload: dict[str, Any]) -> bytes:
            return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        task: asyncio.Task[Any] | None = None
        try:
            api_base = mineru_api_base()
            yield _line({
                "type": "meta",
                "page_count": pages,
                "eta_seconds": round(eta_seconds, 1),
                "backend": backend,
                "avg_sec_per_page": round(avg_sec, 2),
                "mineru_transport": "api" if api_base else "cli",
                "mineru_api_host": (api_base.split("//", 1)[-1].split("/", 1)[0] if api_base else ""),
                "cached": False,
            })
            mineru_work = work_base / "mineru"
            t0 = time.monotonic()
            task = asyncio.create_task(extract_financial_statements(
                src_path,
                document_id=document_id,
                final_name=fn,
                client=client_name,
                bucket=bucket_name,
                file_hash=file_hash,
                work_dir=mineru_work,
                backend=backend,
            ))
            while not task.done():
                if await request.is_disconnected():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                elapsed = time.monotonic() - t0
                if eta_seconds > 0:
                    pct = min(0.95, elapsed / eta_seconds)
                    if elapsed > eta_seconds:
                        over = elapsed - eta_seconds
                        span = max(eta_seconds * 4.0, 180.0)
                        pct = min(0.99, 0.95 + min(0.04, (over / span) * 0.04))
                else:
                    pct = 0.0
                remaining = max(0.0, eta_seconds - elapsed)
                yield _line({
                    "type": "progress",
                    "elapsed": round(elapsed, 2),
                    "eta_seconds": round(eta_seconds, 1),
                    "remaining": round(remaining, 1),
                    "pct": round(pct, 4),
                    "past_eta": elapsed > eta_seconds if eta_seconds > 0 else False,
                })
            try:
                artifact = await task
            except asyncio.CancelledError:
                return
            except Exception as exc:
                yield _line({"type": "error", "error": f"{exc.__class__.__name__}: {exc}"})
                return

            elapsed = time.monotonic() - t0
            mineru_exit = (artifact.get("source") or {}).get("mineru_exit_code")
            mineru_error = (artifact.get("source") or {}).get("mineru_error")
            if pages > 0 and mineru_exit == 0:
                try:
                    record_completion(backend, elapsed_seconds=elapsed, pages=pages)
                except Exception:
                    pass

            if mineru_exit != 0:
                # Surface the real error to the caller — don't silently save an
                # empty artifact that looks like "doc had no tables".
                yield _line({
                    "type": "error",
                    "error": mineru_error or f"mineru exit_code={mineru_exit}",
                    "mineru_exit_code": mineru_exit,
                    "elapsed": round(elapsed, 2),
                })
                return

            if ctx:
                azure_store.save_financial_extraction(ctx, document_id, artifact)
            else:
                write_local_artifact(SORTED, artifact)

            md_path = (artifact.get("source") or {}).get("markdown_path")
            cl_path = (artifact.get("source") or {}).get("content_list_path")
            try:
                if ctx and _azure_active():
                    base = f"{ctx.firm_id}/{document_id}"
                    if md_path and Path(md_path).is_file():
                        azure_store.write_bytes_blob(
                            f"{base}/source.md",
                            Path(md_path).read_bytes(),
                            content_type="text/markdown",
                        )
                    if cl_path and Path(cl_path).is_file():
                        azure_store.write_bytes_blob(
                            f"{base}/source_content_list.json",
                            Path(cl_path).read_bytes(),
                            content_type="application/json",
                        )
                else:
                    out_dir = SORTED / ".extractions" / "raw" / document_id
                    out_dir.mkdir(parents=True, exist_ok=True)
                    if md_path and Path(md_path).is_file():
                        shutil.copy2(md_path, out_dir / "source.md")
                    if cl_path and Path(cl_path).is_file():
                        shutil.copy2(cl_path, out_dir / "source_content_list.json")
            except Exception:
                # Raw blob is bonus material for downstream LLM use; never block on it.
                pass

            _log_event(ctx, {
                "event": "deep_extract",
                "file": fn,
                "hash": file_hash,
                "client": client_name,
                "bucket": bucket_name,
                "mineru_exit_code": mineru_exit,
                "mineru_backend": backend,
                "row_count": (artifact.get("summary") or {}).get("row_count") or 0,
                "pages": pages,
                "elapsed_seconds": round(elapsed, 2),
                "status": artifact.get("status"),
            })
            yield _line({
                "type": "done",
                "document_id": document_id,
                "row_count": (artifact.get("summary") or {}).get("row_count") or 0,
                "status": artifact.get("status"),
                "download_url": f"/sorter/finance/extractions/{document_id}/export?format=xlsx",
                "elapsed": round(elapsed, 2),
                "mineru_exit_code": mineru_exit,
                "cached": False,
            })
        finally:
            if task is not None and not task.done():
                task.cancel()
            shutil.rmtree(work_base, ignore_errors=True)

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@router.post("/graph/import")
async def graph_import(request: Request, payload: dict):
    """Stage a SharePoint/OneDrive item for the normal Strata review/apply flow."""
    ctx = _require_auth_context(request)
    if not ctx:
        raise HTTPException(status_code=404, detail="Azure Graph import is not configured")
    item_id = (payload.get("item_id") or "").strip()
    if not item_id:
        return JSONResponse({"ok": False, "error": "item_id required"}, status_code=400)
    graph_token = request.headers.get("x-ms-graph-token")
    try:
        imported = azure_store.graph_import_file(ctx, item_id, graph_token=graph_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not import Microsoft Graph file") from exc

    safe_name = Path(imported["name"] or "graph_import").name
    data = imported["bytes"]
    file_hash = hashlib.sha256(data).hexdigest()
    manifest = _load_manifest_for(ctx)
    duplicate = _find_dup(manifest, file_hash)
    if duplicate is not None:
        file_id, in_path = _echo_upload_dir(data, safe_name, "duplicate_echo")
        return JSONResponse({
            "ok": True,
            "status": "duplicate",
            "id": file_id,
            "name": safe_name,
            "size": len(data),
            "hash": file_hash[:12],
            "temp_path": str(in_path),
            "dup_of": duplicate.get("final_name") or duplicate.get("original"),
            "graph_item_id": imported.get("graph_item_id"),
            "graph_drive_id": imported.get("graph_drive_id"),
        })

    file_id, in_path, _md_path, md_text = await _ingest_triage_to_job_dir(data, safe_name)
    if not md_text.strip():
        return JSONResponse({
            "ok": False,
            "status": "error",
            "id": file_id,
            "name": safe_name,
            "temp_path": str(in_path),
            "error": "mineru produced no markdown",
        }, status_code=422)

    async def ignore_token(_: str) -> None:
        return None

    provider = payload.get("llm_provider")
    client = sanitize_client(payload.get("client") or payload.get("batch_client") or "") or _CLIENT_FALLBACK
    try:
        suggested, bucket = await suggest_name_and_bucket(
            md_text,
            safe_name,
            ignore_token,
            client,
            manifest,
            provider=provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({
        "ok": True,
        "status": "ready",
        "id": file_id,
        "name": safe_name,
        "size": len(imported["bytes"]),
        "hash": file_hash[:12],
        "suggested": suggested,
        "client": client,
        "bucket": bucket,
        "preview": md_text[:800],
        "temp_path": str(in_path),
        "graph_item_id": imported.get("graph_item_id"),
        "graph_drive_id": imported.get("graph_drive_id"),
    })


@router.post("/graph/export")
async def graph_export(request: Request, payload: dict):
    """Export an already accepted Strata document to the configured Graph drive."""
    ctx = _require_auth_context(request)
    if not ctx:
        raise HTTPException(status_code=404, detail="Azure Graph export is not configured")
    final_name = payload.get("final_name")
    if not final_name:
        return JSONResponse({"ok": False, "error": "final_name required"}, status_code=400)
    manifest = _load_manifest_for(ctx)
    entry = next((e for e in manifest["files"] if e.get("final_name") == final_name), None)
    if not entry:
        return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)

    tmp_dir = SORTER_TMP / uuid.uuid4().hex[:10]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / Path(final_name).name
    try:
        if entry.get("storage_path"):
            chunks, _content_type = azure_store.download_blob(entry["storage_path"])
            with tmp_path.open("wb") as f:
                for chunk in chunks:
                    f.write(chunk)
        else:
            local = SORTED / final_name
            if not local.is_file():
                return JSONResponse({"ok": False, "error": "source missing"}, status_code=404)
            shutil.copy2(local, tmp_path)
        ref = azure_store.export_file_to_graph(
            ctx,
            tmp_path,
            final_name,
            entry.get("client") or _CLIENT_FALLBACK,
            entry.get("bucket") or _BUCKET_FALLBACK,
            graph_token=request.headers.get("x-ms-graph-token"),
        )
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
    return JSONResponse({"ok": True, **ref})


@router.get("/finance/extractions")
async def finance_extractions(request: Request):
    ctx = _require_auth_context(request)
    if ctx:
        return JSONResponse({"extractions": azure_store.list_financial_extractions(ctx)})
    return JSONResponse({"extractions": list_local_artifacts(SORTED)})


@router.get("/finance/extractions/{document_id}")
async def finance_extraction_get(request: Request, document_id: str):
    ctx = _require_auth_context(request)
    artifact = azure_store.get_financial_extraction(ctx, document_id) if ctx else read_local_artifact(SORTED, document_id)
    if not artifact:
        return JSONResponse({"ok": False, "error": "extraction not found"}, status_code=404)
    return JSONResponse({"ok": True, "extraction": artifact})


@router.post("/finance/extractions/{document_id}/review")
async def finance_extraction_review(request: Request, document_id: str, payload: dict):
    ctx = _require_auth_context(request)
    artifact = azure_store.get_financial_extraction(ctx, document_id) if ctx else read_local_artifact(SORTED, document_id)
    if not artifact:
        return JSONResponse({"ok": False, "error": "extraction not found"}, status_code=404)
    updates = payload.get("rows") or payload.get("updates") or []
    if not isinstance(updates, list):
        return JSONResponse({"ok": False, "error": "rows must be a list"}, status_code=400)
    artifact = update_review(artifact, updates)
    if ctx:
        azure_store.save_financial_extraction(ctx, document_id, artifact)
    else:
        write_local_artifact(SORTED, artifact)
    _log_event(ctx, {
        "event": "financial_extraction_reviewed",
        "file": artifact.get("file"),
        "client": artifact.get("client"),
        "document_id": document_id,
        "updated_rows": len(updates),
        "status": artifact.get("status"),
    })
    return JSONResponse({"ok": True, "extraction": artifact})


@router.get("/finance/extractions/{document_id}/export")
async def finance_extraction_export(request: Request, document_id: str, format: str = "xlsx"):
    return await _finance_export_document(request, document_id, format)


@router.get("/finance/export")
async def finance_export(request: Request, document_id: str, format: str = "xlsx"):
    return await _finance_export_document(request, document_id, format)


async def _finance_export_document(request: Request, document_id: str, format: str):
    ctx = _require_auth_context(request)
    artifact = azure_store.get_financial_extraction(ctx, document_id) if ctx else read_local_artifact(SORTED, document_id)
    if not artifact:
        return JSONResponse({"ok": False, "error": "extraction not found"}, status_code=404)
    stem = sanitize_snake(Path(artifact.get("file") or document_id).stem, document_id)
    if format.lower() == "csv":
        return Response(
            content=artifact_to_csv(artifact),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={stem}_financial_extract.csv"},
        )
    if format.lower() in ("xlsx", "excel"):
        return Response(
            content=artifact_to_xlsx_bytes(artifact),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={stem}_financial_extract.xlsx"},
        )
    return JSONResponse({"ok": False, "error": "format must be xlsx or csv"}, status_code=400)


# ── bucket endpoints ──────────────────────────────────────────────────
@router.post("/bucket/move")
async def bucket_move(request: Request, payload: dict):
    """Reassign a file to a different bucket. payload = {final_name, bucket}"""
    ctx = _require_auth_context(request)
    final_name = payload.get("final_name")
    target = sanitize_bucket(payload.get("bucket") or "")
    if not final_name:
        return JSONResponse({"ok": False, "error": "final_name required"}, status_code=400)
    if ctx:
        manifest = azure_store.load_manifest(ctx.firm_id)
        entry = next((e for e in manifest["files"] if e["final_name"] == final_name), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        from_bucket = entry.get("bucket") or _BUCKET_FALLBACK
        updated = azure_store.update_document_bucket(ctx, final_name, target)
        if updated is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        gemma_b = entry.get("gemma_bucket") or entry.get("gemma_predicted_bucket")
        _log_event(ctx, {
            "event": "reassign",
            "file": final_name,
            "hash": entry.get("hash"),
            "client": entry.get("client") or _CLIENT_FALLBACK,
            "from_bucket": from_bucket,
            "to_bucket": target,
            "gemma_predicted_bucket": gemma_b,
            "bucket_changed_from_gemma": bool(gemma_b and target != gemma_b),
            "bucket_changed_from_previous": from_bucket != target,
        })
        return JSONResponse({"ok": True, "from_bucket": from_bucket, "to_bucket": target})
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
    gemma_b = entry.get("gemma_bucket") or entry.get("gemma_predicted_bucket")
    _log_event(ctx, {
        "event": "reassign",
        "file": final_name,
        "hash": entry.get("hash"),
        "client": client_name,
        "from_bucket": from_bucket,
        "to_bucket": target,
        "gemma_predicted_bucket": gemma_b,
        "bucket_changed_from_gemma": bool(gemma_b and target != gemma_b),
        "bucket_changed_from_previous": from_bucket != target,
    })
    return JSONResponse({"ok": True, "from_bucket": from_bucket, "to_bucket": target})


@router.post("/bucket/add")
async def bucket_add(request: Request, payload: dict):
    """Add a new bucket to a client's taxonomy. payload = {client, bucket}
    Also registers the client if it didn't exist — so adding a bucket for a
    brand-new client in the deposit drawer creates the client too."""
    client_name = sanitize_client(payload.get("client") or "")
    bucket = sanitize_bucket(payload.get("bucket") or "")
    if not client_name or client_name == _CLIENT_FALLBACK:
        return JSONResponse({"ok": False, "error": "client required"}, status_code=400)
    if not bucket or bucket == _BUCKET_FALLBACK:
        return JSONResponse({"ok": False, "error": "bucket required"}, status_code=400)
    ctx = _require_auth_context(request)
    if ctx:
        manifest = azure_store.load_manifest(ctx.firm_id)
        client_was_new_to_list = client_name not in manifest.get("clients", [])
        before = set(manifest.get("client_taxonomies", {}).get(client_name, []))
        client_row = azure_store.ensure_client(ctx.firm_id, client_name)
        azure_store.ensure_bucket(ctx.firm_id, client_row["id"] if client_row else None, bucket)
        after_taxonomy = azure_store.client_buckets(ctx.firm_id, client_name)
        bucket_was_new = bucket not in before
        if bucket_was_new:
            _log_event(ctx, {
                "event": "bucket_created",
                "client": client_name,
                "bucket": bucket,
                "taxonomy_size_after": len(after_taxonomy),
                "source": "bucket_add_endpoint",
            })
        if client_was_new_to_list:
            _log_event(ctx, {
                "event": "client_registered",
                "client": client_name,
                "source": "bucket_add_endpoint",
            })
        return JSONResponse({
            "ok": True,
            "bucket": bucket,
            "taxonomy": after_taxonomy,
            "created_bucket": bucket_was_new,
            "created_client": client_was_new_to_list,
        })
    manifest = _load_manifest()
    manifest.setdefault("clients", [])
    client_was_new_to_list = client_name not in manifest["clients"]
    if client_was_new_to_list:
        manifest["clients"].append(client_name)
    blist = ensure_client_taxonomy(manifest, client_name)
    bucket_was_new = bucket not in blist
    if bucket_was_new:
        blist.append(bucket)
    _save_manifest(manifest)
    if bucket_was_new:
        _log_event(ctx, {
            "event": "bucket_created",
            "client": client_name,
            "bucket": bucket,
            "taxonomy_size_after": len(blist),
            "source": "bucket_add_endpoint",
        })
    if client_was_new_to_list:
        _log_event(ctx, {
            "event": "client_registered",
            "client": client_name,
            "source": "bucket_add_endpoint",
        })
    return JSONResponse({
        "ok": True,
        "bucket": bucket,
        "taxonomy": blist,
        "created_bucket": bucket_was_new,
        "created_client": client_was_new_to_list,
    })


@router.post("/bucket/remove")
async def bucket_remove(request: Request, payload: dict):
    """Remove an empty bucket from a client's taxonomy."""
    client_name = sanitize_client(payload.get("client") or "")
    bucket = sanitize_bucket(payload.get("bucket") or "")
    ctx = _require_auth_context(request)
    if ctx:
        ok = azure_store.remove_bucket(ctx, client_name, bucket)
        if not ok:
            return JSONResponse({"ok": False, "error": "bucket has files — reassign them first"}, status_code=400)
        taxonomy = azure_store.client_buckets(ctx.firm_id, client_name)
        _log_event(ctx, {
            "event": "bucket_removed",
            "client": client_name,
            "bucket": bucket,
            "taxonomy_size_after": len(taxonomy),
        })
        return JSONResponse({"ok": True, "taxonomy": taxonomy})
    manifest = _load_manifest()
    blist = ensure_client_taxonomy(manifest, client_name)
    # refuse to remove if any file still uses this bucket
    in_use = any(e.get("client") == client_name and e.get("bucket") == bucket for e in manifest["files"])
    if in_use:
        return JSONResponse({"ok": False, "error": "bucket has files — reassign them first"}, status_code=400)
    if bucket in blist:
        blist.remove(bucket)
        _save_manifest(manifest)
        _log_event(ctx, {
            "event": "bucket_removed",
            "client": client_name,
            "bucket": bucket,
            "taxonomy_size_after": len(blist),
        })
    return JSONResponse({"ok": True, "taxonomy": blist})


@router.get("/training/export")
async def training_export(request: Request):
    """Return analyst/model correction events as CSV (includes legacy `.bucket_changes.jsonl`)."""
    from fastapi.responses import Response
    import csv, io
    ctx = _require_auth_context(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "at", "event", "file", "hash", "client", "original_upload",
        "from_name", "to_name", "from_bucket", "to_bucket", "from_client", "to_client",
        "gemma_predicted_bucket", "gemma_suggested_stem", "analyst_filename_stem",
        "filename_changed_from_gemma", "bucket_changed_from_gemma", "markdown_preview", "payload_json",
    ]
    writer.writerow(header)
    events = azure_store.analyst_events(ctx.firm_id) if ctx else list(_iter_analyst_events())
    for e in events:
        writer.writerow([
            e.get("at", ""), e.get("event", ""), e.get("file", ""), e.get("hash", ""),
            e.get("client", ""), e.get("original_upload") or e.get("original_upload_name") or "",
            e.get("from_name", ""), e.get("to_name", ""),
            e.get("from_bucket") or "", e.get("to_bucket") or "",
            e.get("from_client") or "", e.get("to_client") or "",
            e.get("gemma_predicted_bucket") or e.get("gemma_bucket_at_deposit") or "",
            e.get("gemma_suggested_stem") or e.get("gemma_suggested_stem_at_deposit") or "",
            e.get("analyst_filename_stem") or "",
            e.get("filename_changed_from_gemma", e.get("filename_changed_from_gemma_after_rename", "")),
            e.get("bucket_changed_from_gemma", ""),
            (e.get("markdown_preview") or "").replace("\n", " ")[:500],
            json.dumps(e, ensure_ascii=False)[:8000],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=analyst_events.csv"},
    )


@router.get("/analyst-events/summary")
async def analyst_events_summary(request: Request):
    """Aggregate counts for dashboards / training prep (reads local JSONL)."""
    from collections import Counter

    ctx = _require_auth_context(request)
    events = azure_store.analyst_events(ctx.firm_id) if ctx else list(_iter_analyst_events())
    by_event = Counter(e.get("event") or "unknown" for e in events)
    deposits = [e for e in events if e.get("event") in ("deposit_accept", "initial")]
    renames = [e for e in events if e.get("event") == "file_rename"]
    reassigns = [e for e in events if e.get("event") == "reassign"]
    client_moves = [e for e in events if e.get("event") == "client_reassign"]
    bucket_creates = [e for e in events if e.get("event") == "bucket_created"]
    return JSONResponse({
        "total_events": len(events),
        "by_event": dict(by_event),
        "deposit_accept_count": len(deposits),
        "deposit_filename_corrections": sum(1 for e in deposits if e.get("filename_changed_from_gemma")),
        "deposit_bucket_corrections": sum(1 for e in deposits if e.get("bucket_changed_from_gemma")),
        "deposit_collision_suffixes": sum(1 for e in deposits if e.get("collision_suffix_applied")),
        "deposit_new_taxonomy_buckets": sum(1 for e in deposits if e.get("bucket_first_added_to_taxonomy")),
        "post_deposit_renames": len(renames),
        "post_deposit_bucket_reassigns": len(reassigns),
        "post_deposit_reassigns_vs_gemma": sum(1 for e in reassigns if e.get("bucket_changed_from_gemma")),
        "client_reassigns": len(client_moves),
        "buckets_created_via_ui": len(bucket_creates),
        "log_paths": {"current": str(ANALYST_EVENTS_PATH), "legacy": str(LEGACY_ANALYST_LOG)},
        "azure_enabled": bool(ctx),
    })


@router.get("/analyst-events/download")
async def analyst_events_download(request: Request):
    """Download the raw append-only JSONL (and legacy file concatenated if present)."""
    from fastapi.responses import Response
    ctx = _require_auth_context(request)
    if ctx:
        body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in azure_store.analyst_events(ctx.firm_id))
    else:
        parts: list[str] = []
        for path in (ANALYST_EVENTS_PATH, LEGACY_ANALYST_LOG):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        body = "".join(parts) if parts else ""
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=analyst_events.jsonl"},
    )


@router.post("/client/create")
async def create_client(request: Request, payload: dict):
    name = sanitize_client(payload.get("name") or "")
    if not name or name == _CLIENT_FALLBACK:
        return JSONResponse({"ok": False, "error": "invalid client name"}, status_code=400)
    ctx = _require_auth_context(request)
    if ctx:
        manifest = azure_store.load_manifest(ctx.firm_id)
        created = name not in manifest.get("clients", [])
        azure_store.ensure_client(ctx.firm_id, name)
        if created:
            _log_event(ctx, {"event": "client_created", "client": name, "source": "client_create_endpoint"})
        return JSONResponse({"ok": True, "name": name, "created": created})
    manifest = _load_manifest()
    manifest.setdefault("clients", [])
    if name not in manifest["clients"]:
        manifest["clients"].append(name)
        _save_manifest(manifest)
        _log_event(ctx, {"event": "client_created", "client": name, "source": "client_create_endpoint"})
        return JSONResponse({"ok": True, "name": name, "created": True})
    return JSONResponse({"ok": True, "name": name, "created": False})


@router.post("/client/delete")
async def delete_client(request: Request, payload: dict):
    """Remove an empty client from the list. Files remain; they get their client reset to Unassigned."""
    name = sanitize_client(payload.get("name") or "")
    ctx = _require_auth_context(request)
    if ctx:
        azure_store.remove_client(ctx, name)
        return JSONResponse({"ok": True})
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
async def move_file(request: Request, payload: dict):
    """Reassign a file's client. payload = {final_name, client}"""
    final_name = payload.get("final_name")
    client_name = sanitize_client(payload.get("client") or "")
    if not final_name:
        return JSONResponse({"ok": False, "error": "final_name required"}, status_code=400)
    ctx = _require_auth_context(request)
    if ctx:
        manifest = azure_store.load_manifest(ctx.firm_id)
        entry = next((e for e in manifest["files"] if e["final_name"] == final_name), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        from_client = entry.get("client") or _CLIENT_FALLBACK
        updated = azure_store.update_document_client(ctx, final_name, client_name)
        if updated is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        if from_client != client_name:
            _log_event(ctx, {
                "event": "client_reassign",
                "file": final_name,
                "from_client": from_client,
                "to_client": client_name,
            })
        return JSONResponse({"ok": True, "client": client_name})
    manifest = _load_manifest()
    hit = False
    from_client = _CLIENT_FALLBACK
    for e in manifest["files"]:
        if e["final_name"] == final_name:
            from_client = e.get("client") or _CLIENT_FALLBACK
            e["client"] = client_name
            hit = True
            break
    if not hit:
        return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
    manifest.setdefault("clients", [])
    if client_name != _CLIENT_FALLBACK and client_name not in manifest["clients"]:
        manifest["clients"].append(client_name)
    _save_manifest(manifest)
    if from_client != client_name:
        _log_event(ctx, {
            "event": "client_reassign",
            "file": final_name,
            "from_client": from_client,
            "to_client": client_name,
        })
    return JSONResponse({"ok": True, "client": client_name})


@router.post("/rename")
async def rename_file(request: Request, payload: dict):
    """Rename an already-sorted file. payload = {final_name, new_name}"""
    final_name = payload.get("final_name")
    new_stem = sanitize_snake(payload.get("new_name") or "", final_name or "")
    if not final_name or not new_stem:
        return JSONResponse({"ok": False, "error": "bad input"}, status_code=400)
    ctx = _require_auth_context(request)
    if ctx:
        manifest = azure_store.load_manifest(ctx.firm_id)
        entry = next((e for e in manifest["files"] if e["final_name"] == final_name), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        ext = Path(final_name).suffix
        new_final_name = azure_store.resolve_remote_name(ctx.firm_id, new_stem, ext)
        updated = azure_store.rename_document(ctx, final_name, new_final_name)
        if updated is None:
            return JSONResponse({"ok": False, "error": "not in manifest"}, status_code=404)
        _log_event(ctx, {
            "event": "file_rename",
            "from_name": final_name,
            "to_name": new_final_name,
            "hash": entry.get("hash"),
            "gemma_suggested_stem_at_deposit": entry.get("gemma_suggested"),
            "gemma_bucket_at_deposit": entry.get("gemma_bucket"),
            "filename_changed_from_gemma_after_rename": bool(
                entry.get("gemma_suggested") and entry.get("gemma_suggested") != Path(new_final_name).stem
            ),
        })
        return JSONResponse({"ok": True, "final_name": new_final_name, "url": azure_store.file_url(updated.get("storage_path") or entry.get("storage_path") or "")})
    src = SORTED / final_name
    if not src.is_file():
        return JSONResponse({"ok": False, "error": "source missing"}, status_code=404)
    ext = src.suffix
    dest = resolve_collision(SORTED, new_stem, ext)
    src.rename(dest)
    manifest = _load_manifest()
    entry_snapshot: dict[str, Any] = {}
    for e in manifest["files"]:
        if e["final_name"] == final_name:
            entry_snapshot = {
                "hash": e.get("hash"),
                "gemma_suggested": e.get("gemma_suggested"),
                "gemma_bucket": e.get("gemma_bucket"),
            }
            e["final_name"] = dest.name
            break
    _save_manifest(manifest)
    _log_event(ctx, {
        "event": "file_rename",
        "from_name": final_name,
        "to_name": dest.name,
        "hash": entry_snapshot.get("hash"),
        "gemma_suggested_stem_at_deposit": entry_snapshot.get("gemma_suggested"),
        "gemma_bucket_at_deposit": entry_snapshot.get("gemma_bucket"),
        "filename_changed_from_gemma_after_rename": bool(
            entry_snapshot.get("gemma_suggested")
            and entry_snapshot.get("gemma_suggested") != Path(dest.name).stem
        ),
    })
    return JSONResponse({"ok": True, "final_name": dest.name, "url": f"/sorted/{dest.name}"})





_FONTS_CSS_PATH = BASE / "fonts" / "fonts.local.css"
_STRATA_INDEX_HTML = Path(__file__).resolve().parent / "templates" / "index.html"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def sorter_index():
    font_face_css = ""
    if _FONTS_CSS_PATH.exists():
        font_face_css = _FONTS_CSS_PATH.read_text()
    html = _STRATA_INDEX_HTML.read_text(encoding="utf-8")
    html = html.replace("__FONT_FACES__", font_face_css)
    return html
