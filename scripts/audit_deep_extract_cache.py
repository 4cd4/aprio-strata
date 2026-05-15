"""
Audit deep-extract cache against the live Azure SQL Server database.

Runs Sections 1 and 2 from audit_deep_extract_cache.sql and prints results.
Prints the Section 3 prune SQL with the live orphan count — copy-paste it
into SSMS or sqlcmd when you are ready to apply.

Usage:
    python scripts/audit_deep_extract_cache.py

Requires the same environment variables as the Strata API:
    AZURE_SQL_CONNECTION_STRING   (preferred)
  or
    AZURE_TENANT_ID + AZURE_SQL_SERVER + AZURE_SQL_DATABASE
      (uses DefaultAzureCredential — run `az login` locally first)

The script never executes a DELETE.  It is read-only except for the copy-paste
SQL block it prints at the end.
"""
from __future__ import annotations

import sys
import textwrap
from typing import Any

# azure_store lives one level up from scripts/
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from azure_store import AzureStore  # noqa: E402


# ---------------------------------------------------------------------------
# SQL — Section 1: hit-rate report
# ---------------------------------------------------------------------------
_SQL_HIT_RATE = """
with per_doc as (
    select
        ae.firm_id,
        ae.[file]   as final_name,
        count(*)    as run_count
      from dbo.analyst_events ae
     where ae.event = 'deep_extract'
     group by ae.firm_id, ae.[file]
)
select
    count(*)                                                          as total_docs_extracted,
    sum(pd.run_count)                                                 as total_extraction_runs,
    count(case when pd.run_count > 1 then 1 end)                     as docs_re_extracted,
    sum(case when pd.run_count > 1 then pd.run_count - 1 else 0 end) as excess_runs,
    cast(
        1.0 * sum(case when pd.run_count > 1 then pd.run_count - 1 else 0 end)
              / nullif(sum(pd.run_count), 0)
        as decimal(5, 4)
    )                                                                 as cache_bust_rate
  from per_doc pd
"""

# ---------------------------------------------------------------------------
# SQL — Section 2: orphan detection
# ---------------------------------------------------------------------------
_SQL_ORPHANS = """
with last_event as (
    select
        ae.firm_id,
        ae.[file]   as final_name,
        ae.hash     as event_hash,
        ae.at       as extracted_at,
        row_number() over (
            partition by ae.firm_id, ae.[file]
            order by ae.at desc
        ) as rn
      from dbo.analyst_events ae
     where ae.event = 'deep_extract'
)
select
    fe.firm_id,
    fe.document_id,
    fe.artifact_path,
    fe.status,
    fe.row_count,
    fe.created_at,
    fe.updated_at,
    d.final_name                                   as document_final_name,
    d.hash                                         as current_doc_hash,
    le.event_hash                                  as last_extracted_hash,
    le.extracted_at                                as last_extracted_at,
    case
        when d.id is null
            then 'missing_document'
        when le.event_hash is null
            then 'no_event_record'
        when d.hash <> le.event_hash
            then 'hash_mismatch'
    end                                            as orphan_reason
  from dbo.financial_extractions fe
  left join dbo.documents d
         on  d.id      = fe.document_id
         and d.firm_id = fe.firm_id
  left join last_event le
         on  le.final_name = d.final_name
         and le.firm_id    = fe.firm_id
         and le.rn         = 1
 where d.id is null
    or (
        d.id is not null
        and (le.event_hash is null or d.hash <> le.event_hash)
       )
 order by fe.firm_id, fe.updated_at desc
"""

# ---------------------------------------------------------------------------
# SQL — Section 3: prune template (printed only, never executed)
# ---------------------------------------------------------------------------
_SQL_PRUNE_TEMPLATE = """\
-- ============================================================
-- Section 3 — Prune orphans  (copy-paste into SSMS / sqlcmd)
-- {orphan_count} orphan row(s) will be deleted.
-- Review Section 2 output above before applying.
-- Change ROLLBACK TRANSACTION to COMMIT TRANSACTION to apply.
-- ============================================================

begin transaction;

select count(*) as orphan_rows_to_delete
  from dbo.financial_extractions fe
 where not exists (
       select 1
         from dbo.documents d
        where d.id      = fe.document_id
          and d.firm_id = fe.firm_id
       );

delete from dbo.financial_extractions
 where not exists (
       select 1
         from dbo.documents d
        where d.id      = financial_extractions.document_id
          and d.firm_id = financial_extractions.firm_id
       );

select @@rowcount as rows_deleted;

rollback transaction;
-- commit transaction;
"""

# ---------------------------------------------------------------------------
# Count query for the prune block
# ---------------------------------------------------------------------------
_SQL_ORPHAN_COUNT = """
select count(*) as cnt
  from dbo.financial_extractions fe
 where not exists (
       select 1
         from dbo.documents d
        where d.id      = fe.document_id
          and d.firm_id = fe.firm_id
       )
"""


def _print_row(row: dict[str, Any]) -> None:
    for k, v in row.items():
        print(f"  {k}: {v}")


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (no rows)")
        return
    for i, row in enumerate(rows, 1):
        print(f"  --- row {i} ---")
        _print_row(row)


def main() -> None:
    store = AzureStore()
    if not store.enabled:
        print(
            "ERROR: Azure integration is not configured.\n"
            "Set AZURE_SQL_CONNECTION_STRING (or AZURE_TENANT_ID + AZURE_SQL_SERVER"
            " + AZURE_SQL_DATABASE) and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=" * 70)
    print("Section 1 — Deep-extract hit-rate report")
    print("=" * 70)
    print(
        "NOTE: deep_extract events are only logged on cache misses.\n"
        "      Cache hits are silent — they do not appear in analyst_events.\n"
        "      'cache_bust_rate' = fraction of runs that were re-extractions\n"
        "      on a document already extracted at least once.\n"
    )
    try:
        hit_rows = store._query(_SQL_HIT_RATE)
        _print_table(hit_rows)
    except Exception as exc:
        print(f"  ERROR running hit-rate query: {exc}", file=sys.stderr)

    print()
    print("=" * 70)
    print("Section 2 — Orphan detection")
    print("=" * 70)
    print(
        "Rows where:\n"
        "  (a) orphan_reason='missing_document'  — document row deleted, artifact stranded\n"
        "  (b) orphan_reason='hash_mismatch'     — document content changed (proxy via events)\n"
        "  (c) orphan_reason='no_event_record'   — no analyst event; hash check not possible\n"
        "\n"
        "IMPORTANT: hash comparison for (b) is a proxy — the canonical hash lives\n"
        "in the artifact blob, not in the SQL table.  The cache self-heals on the\n"
        "next API call for (b) and (c); only (a) rows are pruned by Section 3.\n"
    )
    try:
        orphan_rows = store._query(_SQL_ORPHANS)
        _print_table(orphan_rows)
    except Exception as exc:
        print(f"  ERROR running orphan query: {exc}", file=sys.stderr)
        orphan_rows = []

    # Count only case (a) — missing_document — for the prune SQL
    try:
        count_row = store._query(_SQL_ORPHAN_COUNT)
        orphan_count = (count_row[0].get("cnt") or 0) if count_row else 0
    except Exception as exc:
        print(f"  ERROR counting orphans: {exc}", file=sys.stderr)
        orphan_count = "?"

    print()
    print("=" * 70)
    print("Section 3 — Prune SQL (NOT executed — copy-paste to apply)")
    print("=" * 70)
    print(
        "Only case (a) orphans (missing_document) are pruned.\n"
        "Case (b)/(c) rows are excluded — the cache self-heals on the next request.\n"
        "\n"
        "After pruning, delete the orphan blobs from Azure Blob Storage:\n"
        "  az storage blob delete-batch --source <container> \\\n"
        "    --pattern '<firm_id>/<document_id>/*'\n"
        "for each (firm_id, document_id) pair listed in Section 2 above.\n"
    )
    print(_SQL_PRUNE_TEMPLATE.format(orphan_count=orphan_count))


if __name__ == "__main__":
    main()
