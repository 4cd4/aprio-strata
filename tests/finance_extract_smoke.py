import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finance_extract import artifact_to_csv, extract_rows_from_markdown, update_review


SAMPLE = """
# Consolidated Statements of Operations

In thousands, except per share amounts

|  | Year Ended December 31, 2024 | Year Ended December 31, 2023 |
| --- | ---: | ---: |
| Revenue | $1,200 | $1,000 |
| Cost of revenue | (400) | (350) |
| Gross profit | 800 | 650 |
| Net income | 125 | 95 |
"""


def main() -> int:
    rows = extract_rows_from_markdown(SAMPLE, document_id="doc1", final_name="sample.pdf", client="Acme")
    assert len(rows) == 8, rows
    assert rows[0]["statement_type"] == "income_statement"
    assert rows[0]["canonical_item"] == "revenue"
    assert rows[1]["value"] == 1000.0
    artifact = {"document_id": "doc1", "file": "sample.pdf", "rows": rows}
    artifact = update_review(artifact, [{"id": row["id"], "review_status": "approved"} for row in rows[:2]])
    csv_body = artifact_to_csv(artifact)
    assert "revenue" in csv_body
    assert csv_body.count("\n") == 3
    print("finance extraction smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
