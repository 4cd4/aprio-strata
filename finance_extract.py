"""Finance statement extraction from full MinerU output.

The first implementation is intentionally conservative: it treats MinerU as the
source of text/table structure, normalizes obvious financial statement rows, and
keeps every extracted value traceable to a page/table/column.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mineru_runtime import run_mineru as run_mineru_job


EXTRACTION_VERSION = "finance_extract.v1"
LOCAL_EXTRACTIONS_DIRNAME = ".extractions"

STATEMENT_KEYWORDS = {
    "income_statement": (
        "income statement",
        "statement of operations",
        "statement of income",
        "statements of operations",
        "profit and loss",
        "p&l",
    ),
    "balance_sheet": (
        "balance sheet",
        "statement of financial position",
        "consolidated balance sheets",
        "assets",
        "liabilities",
        "stockholders' equity",
        "shareholders' equity",
    ),
    "cash_flow": (
        "cash flow",
        "cash flows",
        "statement of cash flows",
        "statements of cash flows",
        "operating activities",
        "investing activities",
        "financing activities",
    ),
}

CANONICAL_PATTERNS: list[tuple[str, str]] = [
    (r"\brevenue|net sales|sales\b", "revenue"),
    (r"cost of (revenue|sales)|costs of goods|cogs", "cost_of_revenue"),
    (r"gross profit", "gross_profit"),
    (r"operating income|income from operations", "operating_income"),
    (r"net income|net loss", "net_income"),
    (r"cash and cash equivalents", "cash_and_equivalents"),
    (r"accounts receivable", "accounts_receivable"),
    (r"inventory|inventories", "inventory"),
    (r"total assets", "total_assets"),
    (r"accounts payable", "accounts_payable"),
    (r"total liabilities", "total_liabilities"),
    (r"retained earnings", "retained_earnings"),
    (r"total (stockholders|shareholders).+equity|total equity", "total_equity"),
    (r"net cash provided by.*operating|net cash used in.*operating", "net_cash_operating"),
    (r"net cash provided by.*investing|net cash used in.*investing", "net_cash_investing"),
    (r"net cash provided by.*financing|net cash used in.*financing", "net_cash_financing"),
]

PERIOD_RE = re.compile(r"(?:fy|q[1-4])?\s*(20\d{2}|19\d{2})|(?:\d{1,2}/\d{1,2}/\d{2,4})|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(?:20|19)\d{2}", re.I)
VALUE_RE = re.compile(r"^\(?\$?\s*-?\d[\d,]*(?:\.\d+)?\)?%?$")


async def run_mineru_full(in_path: Path, out_dir: Path, backend: str = "pipeline") -> dict[str, Any]:
    """Full-document MinerU parse (CLI or ``mineru-api`` when ``MINERU_API_URL`` is set)."""
    return await run_mineru_job(in_path, out_dir, backend=backend)


async def extract_financial_statements(
    src: Path,
    *,
    document_id: str,
    final_name: str,
    client: str,
    bucket: str,
    file_hash: str | None,
    work_dir: Path,
) -> dict[str, Any]:
    parse = await run_mineru_full(src, work_dir)
    md_path = parse["markdown_path"]
    markdown = md_path.read_text(encoding="utf-8", errors="replace") if md_path else ""
    content_list = _read_content_list(parse["content_list_path"])
    rows = extract_rows_from_markdown(
        markdown,
        document_id=document_id,
        final_name=final_name,
        client=client,
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "version": EXTRACTION_VERSION,
        "id": uuid.uuid4().hex,
        "document_id": document_id,
        "file": final_name,
        "hash": file_hash,
        "client": client,
        "bucket": bucket,
        "status": "pending_review" if rows else "no_financial_tables_found",
        "created_at": now,
        "updated_at": now,
        "source": {
            "mineru_exit_code": parse["exit_code"],
            "markdown_path": str(md_path) if md_path else None,
            "content_list_path": str(parse["content_list_path"]) if parse["content_list_path"] else None,
            "content_list_items": len(content_list),
        },
        "summary": _summary(rows),
        "rows": rows,
    }


def extract_rows_from_markdown(markdown: str, *, document_id: str, final_name: str, client: str) -> list[dict[str, Any]]:
    tables = _markdown_tables(markdown)
    rows: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables):
        context = _nearby_context(markdown, table["start"])
        statement_type = _statement_type(context + "\n" + "\n".join(table["headers"]))
        if not statement_type:
            continue
        scale = _scale(context)
        currency = _currency(context)
        periods = [_period_label(h) or h.strip() for h in table["headers"][1:]]
        for body_row in table["rows"]:
            if len(body_row) < 2:
                continue
            line_item = _clean_cell(body_row[0])
            if not _valid_line_item(line_item):
                continue
            canonical = _canonical_item(line_item)
            for col_idx, raw_value in enumerate(body_row[1:], start=1):
                value = _parse_number(raw_value)
                if value is None:
                    continue
                period_label = periods[col_idx - 1] if col_idx - 1 < len(periods) else table["headers"][col_idx]
                rows.append({
                    "id": uuid.uuid4().hex,
                    "document_id": document_id,
                    "file": final_name,
                    "client": client,
                    "statement_type": statement_type,
                    "entity": client,
                    "period_end": None,
                    "period_label": period_label,
                    "line_item": line_item,
                    "canonical_item": canonical,
                    "value": value,
                    "currency": currency,
                    "scale": scale,
                    "confidence": _confidence(statement_type, canonical, period_label, context),
                    "source_page": table.get("page"),
                    "source_table_index": table_index,
                    "source_column": table["headers"][col_idx] if col_idx < len(table["headers"]) else period_label,
                    "source_context": context[-500:],
                    "review_status": "pending",
                    "review_note": "",
                })
    return rows


def update_review(artifact: dict[str, Any], updates: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {row.get("id"): row for row in artifact.get("rows", [])}
    allowed = {"line_item", "canonical_item", "value", "currency", "scale", "period_label", "period_end", "review_status", "review_note"}
    for update in updates:
        row_id = update.get("id")
        row = by_id.get(row_id)
        if not row:
            continue
        for key in allowed:
            if key in update:
                row[key] = update[key]
    artifact["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    artifact["summary"] = _summary(artifact.get("rows", []))
    if artifact["summary"]["pending_rows"] == 0 and artifact["summary"]["row_count"] > 0:
        artifact["status"] = "reviewed"
    return artifact


def reviewed_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in artifact.get("rows", []) if r.get("review_status") in ("approved", "reviewed")]


def artifact_to_csv(artifact: dict[str, Any]) -> str:
    rows = reviewed_rows(artifact)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_export_headers())
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in _export_headers()})
    return buf.getvalue()


def artifact_to_xlsx_bytes(artifact: dict[str, Any]) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("Install openpyxl to export finance extraction workbooks") from exc
    rows = reviewed_rows(artifact)
    wb = Workbook()
    ws = wb.active
    ws.title = "normalized_rows"
    headers = _export_headers()
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    summary = wb.create_sheet("summary")
    for key, value in (artifact.get("summary") or {}).items():
        summary.append([key, value])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def write_local_artifact(sorted_dir: Path, artifact: dict[str, Any]) -> Path:
    out_dir = sorted_dir / LOCAL_EXTRACTIONS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{artifact['document_id']}.json"
    path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_local_artifact(sorted_dir: Path, document_id: str) -> dict[str, Any] | None:
    path = sorted_dir / LOCAL_EXTRACTIONS_DIRNAME / f"{document_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_local_artifacts(sorted_dir: Path) -> list[dict[str, Any]]:
    out_dir = sorted_dir / LOCAL_EXTRACTIONS_DIRNAME
    if not out_dir.is_dir():
        return []
    artifacts = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            artifacts.append(_artifact_listing(artifact))
        except Exception:
            continue
    return artifacts


def _read_content_list(path: Path | None) -> list[Any]:
    if not path or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _markdown_tables(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines) - 1:
        if not _is_table_row(lines[i]) or not _is_separator(lines[i + 1]):
            i += 1
            continue
        headers = _split_row(lines[i])
        rows = []
        start = i
        i += 2
        while i < len(lines) and _is_table_row(lines[i]):
            cells = _split_row(lines[i])
            if any(_clean_cell(c) for c in cells):
                rows.append(cells)
            i += 1
        if len(headers) >= 2 and rows:
            tables.append({"headers": headers, "rows": rows, "start": start, "page": _page_before(lines, start)})
        continue
    return tables


def markdown_table_matrices(markdown: str) -> list[list[list[str]]]:
    """Each Markdown pipe table as a matrix: header row followed by body rows."""
    return [[t["headers"]] + t["rows"] for t in _markdown_tables(markdown)]


def _safe_sheet_title(name: str) -> str:
    s = re.sub(r"[\[\]:*?/\\]", "_", name).strip() or "Sheet"
    return s[:31]


def _unique_sheet_name(base: str, used: set[str]) -> str:
    candidate = _safe_sheet_title(base)
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while True:
        suffix = f"_{n}"
        truncated = _safe_sheet_title(base[: max(1, 31 - len(suffix))] + suffix)
        if truncated not in used:
            used.add(truncated)
            return truncated
        n += 1


def tables_workbook_bytes(tables: list[list[list[str]]], *, stub_message: str | None = None) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("Install openpyxl to build extraction workbooks") from exc
    wb = Workbook()
    if wb.active is not None:
        wb.remove(wb.active)
    used: set[str] = set()
    for i, table in enumerate(tables):
        title = _unique_sheet_name(f"Table_{i + 1}", used)
        ws = wb.create_sheet(title=title)
        for row in table:
            ws.append([str(c) if c is not None else "" for c in row])
    if not wb.sheetnames:
        ws = wb.create_sheet(title="Extract")
        ws.append([stub_message or "No pipe-style Markdown tables found"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


async def deep_extract_tables_workbook(
    src: Path,
    *,
    work_dir: Path,
    backend: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Full MinerU parse (default VLM backend), then one sheet per Markdown table."""
    b = (backend or os.environ.get("MINERU_DEEP_BACKEND", "vlm-auto-engine") or "vlm-auto-engine").strip()
    parse = await run_mineru_full(src, work_dir, backend=b)
    md_path = parse.get("markdown_path")
    markdown = md_path.read_text(encoding="utf-8", errors="replace") if md_path else ""
    tables = markdown_table_matrices(markdown)
    stub = (
        "No pipe-style Markdown tables found. "
        "Try another MINERU_DEEP_BACKEND or inspect MinerU Markdown output."
    )
    meta: dict[str, Any] = {
        "mineru_exit_code": parse.get("exit_code"),
        "mineru_backend": b,
        "table_count": len(tables),
        "markdown_path": str(md_path) if md_path else None,
        "content_list_path": str(parse.get("content_list_path")) if parse.get("content_list_path") else None,
    }
    xlsx = tables_workbook_bytes(
        tables,
        stub_message=stub,
    )
    return xlsx, meta


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|") and line.count("|") >= 2


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in cells if c.strip())


def _split_row(line: str) -> list[str]:
    return [_clean_cell(c) for c in line.strip().strip("|").split("|")]


def _clean_cell(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace("<br>", " ")).strip()


def _nearby_context(markdown: str, line_index: int, radius: int = 8) -> str:
    lines = markdown.splitlines()
    start = max(0, line_index - radius)
    return "\n".join(lines[start:line_index])


def _statement_type(text: str) -> str | None:
    low = text.lower()
    scores = {name: sum(1 for kw in kws if kw in low) for name, kws in STATEMENT_KEYWORDS.items()}
    best = max(scores.items(), key=lambda item: item[1])
    return best[0] if best[1] > 0 else None


def _scale(text: str) -> str:
    low = text.lower()
    if "in millions" in low or "millions" in low:
        return "millions"
    if "in thousands" in low or "thousands" in low:
        return "thousands"
    return "ones"


def _currency(text: str) -> str:
    low = text.lower()
    if "$" in text or "usd" in low or "u.s. dollars" in low:
        return "USD"
    return ""


def _period_label(header: str) -> str | None:
    match = PERIOD_RE.search(header or "")
    return match.group(0).strip() if match else None


def _valid_line_item(line_item: str) -> bool:
    if not line_item or len(line_item) < 3:
        return False
    if VALUE_RE.match(line_item):
        return False
    return bool(re.search(r"[A-Za-z]", line_item))


def _parse_number(raw: Any) -> float | None:
    s = _clean_cell(raw)
    if not s or not VALUE_RE.match(s.replace("—", "-")):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("%", "").replace("—", "-").strip()
    if s in ("", "-", "--"):
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _canonical_item(line_item: str) -> str:
    low = line_item.lower()
    for pattern, canonical in CANONICAL_PATTERNS:
        if re.search(pattern, low):
            return canonical
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_")[:80] or "unknown"


def _confidence(statement_type: str, canonical: str, period_label: str, context: str) -> float:
    score = 0.55
    if statement_type:
        score += 0.15
    if canonical != "unknown":
        score += 0.15
    if _period_label(period_label):
        score += 0.1
    if _currency(context):
        score += 0.05
    return min(score, 0.95)


def _page_before(lines: list[str], start: int) -> int | None:
    for idx in range(start, max(-1, start - 80), -1):
        match = re.search(r"page\s+(\d+)", lines[idx], re.I)
        if match:
            return int(match.group(1))
    return None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_statement: dict[str, int] = {}
    for row in rows:
        by_statement[row.get("statement_type") or "unknown"] = by_statement.get(row.get("statement_type") or "unknown", 0) + 1
    return {
        "row_count": len(rows),
        "pending_rows": sum(1 for r in rows if r.get("review_status") == "pending"),
        "approved_rows": sum(1 for r in rows if r.get("review_status") in ("approved", "reviewed")),
        "by_statement": by_statement,
    }


def _artifact_listing(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": artifact.get("document_id"),
        "file": artifact.get("file"),
        "client": artifact.get("client"),
        "status": artifact.get("status"),
        "created_at": artifact.get("created_at"),
        "updated_at": artifact.get("updated_at"),
        "summary": artifact.get("summary") or {},
    }


def _export_headers() -> list[str]:
    return [
        "document_id",
        "file",
        "client",
        "statement_type",
        "entity",
        "period_end",
        "period_label",
        "line_item",
        "canonical_item",
        "value",
        "currency",
        "scale",
        "confidence",
        "source_page",
        "source_table_index",
        "source_column",
        "review_status",
        "review_note",
    ]
