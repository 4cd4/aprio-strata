"""MinerU execution: local CLI subprocess or remote ``mineru-api`` over HTTP.

Set ``MINERU_API_URL`` (e.g. ``http://127.0.0.1:8888`` after ``ssh -L 8888:localhost:8000 ...``)
to send parses to a remote GPU node. Unset to use the ``mineru`` binary on PATH.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx


def mineru_api_base() -> str:
    return (os.environ.get("MINERU_API_URL") or "").strip().rstrip("/")


def mineru_api_timeout() -> float:
    try:
        return float(os.environ.get("MINERU_API_TIMEOUT", "600"))
    except ValueError:
        return 600.0


def _guess_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _write_outputs(out_dir: Path, markdown: str, content_list: Any | None) -> tuple[Path | None, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path: Path | None = None
    if markdown and markdown.strip():
        md_path = out_dir / "mineru_api.md"
        md_path.write_text(markdown, encoding="utf-8")
    cl_path: Path | None = None
    if content_list is not None:
        cl_path = out_dir / "mineru_api_content_list.json"
        cl_path.write_text(json.dumps(content_list, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, cl_path


def _extract_from_dict(obj: dict[str, Any]) -> tuple[str, Any | None]:
    """Best-effort unpack of mineru-api JSON; schema varies by MinerU version."""
    for key in ("markdown", "md", "text", "content"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v, _pick_content_list(obj)
    data = obj.get("data")
    if isinstance(data, dict):
        md, cl = _extract_from_dict(data)
        if md:
            return md, cl if cl is not None else _pick_content_list(obj)
    results = obj.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            return _extract_from_dict(first)
    return "", _pick_content_list(obj)


def _pick_content_list(obj: dict[str, Any]) -> Any | None:
    for key in ("content_list", "middle_json", "json", "layout"):
        v = obj.get(key)
        if v is not None:
            return v
    return None


def _parse_zip_bytes(z: bytes, out_dir: Path) -> tuple[str, Any | None]:
    """If API returns a zip, pull first .md and optional content list JSON."""
    markdown = ""
    content_list: Any | None = None
    with zipfile.ZipFile(BytesIO(z)) as zf:
        names = zf.namelist()
        md_names = sorted(n for n in names if n.lower().endswith(".md"))
        if md_names:
            markdown = zf.read(md_names[0]).decode("utf-8", errors="replace")
        json_names = sorted(n for n in names if "content_list" in n.lower() and n.lower().endswith(".json"))
        if json_names:
            try:
                content_list = json.loads(zf.read(json_names[0]).decode("utf-8"))
            except Exception:
                content_list = None
    return markdown, content_list


async def _run_mineru_http(
    in_path: Path,
    out_dir: Path,
    *,
    backend: str,
    page_start: int | None,
    page_end: int | None,
) -> dict[str, Any]:
    base = mineru_api_base()
    url = f"{base}/file_parse"
    data: dict[str, str] = {
        "return_md": "true",
        "backend": backend,
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "lang_list": (os.environ.get("MINERU_API_LANG_LIST") or "en").strip() or "en",
    }
    if page_start is not None:
        data["start_page"] = str(page_start)
    if page_end is not None:
        data["end_page"] = str(page_end)

    file_bytes = in_path.read_bytes()
    ct = _guess_content_type(in_path)
    files = {"files": (in_path.name, file_bytes, ct)}

    timeout = httpx.Timeout(mineru_api_timeout(), connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, data=data, files=files)

    if r.status_code >= 400:
        detail = (r.text or r.reason_phrase or "")[:2000]
        err_path = out_dir / "mineru_api_error.txt"
        out_dir.mkdir(parents=True, exist_ok=True)
        err_path.write_text(f"HTTP {r.status_code}\n{detail}", encoding="utf-8")
        return {
            "exit_code": 1,
            "markdown_path": None,
            "content_list_path": None,
            "output_dir": out_dir,
            "error": f"mineru-api HTTP {r.status_code}",
        }

    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    markdown = ""
    content_list: Any | None = None

    if ct == "application/json":
        try:
            payload: Any = r.json()
        except Exception:
            payload = {}
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            markdown, content_list = _extract_from_dict(payload[0])
        elif isinstance(payload, dict):
            markdown, content_list = _extract_from_dict(payload)
        elif isinstance(payload, str):
            markdown = payload
    elif ct == "application/zip" or r.content[:2] == b"PK":
        markdown, content_list = _parse_zip_bytes(r.content, out_dir)
    else:
        text = r.text
        if text.strip().startswith("{") or text.strip().startswith("["):
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    markdown, content_list = _extract_from_dict(payload)
            except Exception:
                markdown = text
        else:
            markdown = text

    md_path, cl_path = _write_outputs(out_dir, markdown, content_list)
    ok = bool(md_path and md_path.is_file() and md_path.stat().st_size > 0)
    return {
        "exit_code": 0 if ok else 1,
        "markdown_path": md_path,
        "content_list_path": cl_path,
        "output_dir": out_dir,
    }


async def _run_mineru_subprocess(
    in_path: Path,
    out_dir: Path,
    *,
    backend: str,
    page_start: int | None,
    page_end: int | None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TERM": "dumb", "COLUMNS": "120"}
    cmd: list[str] = [
        "mineru",
        "-p",
        str(in_path),
        "-o",
        str(out_dir),
        "-b",
        backend,
    ]
    if page_start is not None:
        cmd += ["-s", str(page_start)]
    if page_end is not None:
        cmd += ["-e", str(page_end)]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    rc = await proc.wait()
    md = next(iter(sorted(out_dir.rglob("*.md"))), None)
    content_list = next(iter(sorted(out_dir.rglob("*_content_list*.json"))), None)
    return {"exit_code": rc, "markdown_path": md, "content_list_path": content_list, "output_dir": out_dir}


async def run_mineru(
    in_path: Path,
    out_dir: Path,
    *,
    backend: str = "pipeline",
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    """Run MinerU (HTTP API if ``MINERU_API_URL`` else CLI)."""
    if mineru_api_base():
        return await _run_mineru_http(in_path, out_dir, backend=backend, page_start=page_start, page_end=page_end)
    return await _run_mineru_subprocess(in_path, out_dir, backend=backend, page_start=page_start, page_end=page_end)
