"""Azure Document Intelligence helpers (layout / read).

Requires ``AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`` and ``AZURE_DOCUMENT_INTELLIGENCE_KEY``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _endpoint() -> str:
    return (os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") or "").strip().rstrip("/")


def _key() -> str:
    return (os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY") or "").strip()


def analyze_read_layout_bytes(file_bytes: bytes, *, content_type: str = "application/pdf") -> dict[str, Any]:
    """Run ``prebuilt-read`` on raw bytes; returns the analyzer result as a dict."""
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    ep = _endpoint()
    key = _key()
    if not ep or not key:
        raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY are required")

    client = DocumentIntelligenceClient(endpoint=ep, credential=AzureKeyCredential(key))
    poller = client.begin_analyze_document(
        model_id="prebuilt-read",
        analyze_request=file_bytes,
        content_type=content_type,
    )
    result = poller.result()
    return result.as_dict() if hasattr(result, "as_dict") else dict(result)


def layout_text_from_result(result: dict[str, Any]) -> str:
    """Best-effort flatten of read result content to plain text."""
    analyze = result.get("analyzeResult") or result.get("analyze_result") or {}
    blocks: list[str] = []
    for page in analyze.get("pages") or []:
        for line in page.get("lines") or []:
            t = (line.get("content") or "").strip()
            if t:
                blocks.append(t)
    return "\n".join(blocks)


def analyze_read_layout_path(path: Path) -> str:
    """Convenience: file path → concatenated line text."""
    ct = "application/pdf"
    suf = path.suffix.lower()
    if suf in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        ct = f"image/{suf.lstrip('.')}"
    data = path.read_bytes()
    result = analyze_read_layout_bytes(data, content_type=ct)
    return layout_text_from_result(result)
