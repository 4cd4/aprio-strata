-- =============================================================================
-- Deep-extract cache audit + orphan prune
-- Target: Azure SQL Server (T-SQL)  — run via SSMS, sqlcmd, or the helper
--         script  scripts/audit_deep_extract_cache.py
--
-- IMPORTANT — schema-dialect mismatch
--   azure_schema.sql describes a PostgreSQL schema (gen_random_uuid, pgcrypto,
--   timestamptz) and references AZURE_POSTGRES_DSN.  azure_store.py connects to
--   Azure SQL Server via pyodbc + ODBC Driver 18 and issues T-SQL (dbo. prefix,
--   merge with holdlock, sysdatetimeoffset, top 1).  The queries below are
--   written in T-SQL for the SQL Server database that azure_store.py actually
--   uses.  If you are running against a PostgreSQL backend instead, replace
--   T-SQL-isms (top N → LIMIT N, sysdatetimeoffset → now(), etc.) accordingly.
--
-- Background (commit a5c6749)
--   POST /sorter/deep-extract/{final_name} caches results in
--   dbo.financial_extractions keyed by (firm_id, document_id).  The artifact
--   JSON blob ({firm_id}/{document_id}/financial_extract.json) carries a "hash"
--   field; the endpoint reuses an existing artifact only when
--   cached["hash"] == documents.hash.  Stale artifacts accumulate when:
--     • a document row is deleted (cascade may not be active on SQL Server)
--     • a file is re-uploaded under the same final_name with different content
--       (new document row, old extraction row orphaned)
--     • the analyst calls the endpoint with ?force=true repeatedly
-- =============================================================================


-- =============================================================================
-- Section 1 — Deep-extract hit-rate report
-- =============================================================================
-- deep_extract events are written to dbo.analyst_events ONLY when extraction
-- actually runs (cache miss path).  Cache hits — where the stored artifact hash
-- matches documents.hash — return immediately and produce no event row.
--
-- Definitions used below:
--   total_docs_extracted  — distinct documents that have had at least one
--                           logged extraction run
--   total_extraction_runs — all logged runs (each = a cache miss)
--   docs_re_extracted     — documents with more than one logged run; these had
--                           at least one cache bust (hash changed, force=true,
--                           or first artifact was missing)
--   excess_runs           — total extra runs beyond the first per document
--   cache_bust_rate       — excess_runs / total_extraction_runs; measures what
--                           fraction of runs were redundant re-extractions
--
-- Join: analyst_events.file = documents.final_name within the same firm_id.
-- Limitation: if a document is renamed after extraction the historical event
-- row retains the old final_name and will not join to the updated documents row.
-- =============================================================================

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
  from per_doc pd;


-- =============================================================================
-- Section 2 — Orphan detection (read-only)
-- =============================================================================
-- Returns dbo.financial_extractions rows that are stale or dangling.
--
-- Case (a) — missing document
--   The dbo.documents row no longer exists.  azure_schema.sql declares
--   document_id references documents(id) on delete cascade, but this may not
--   be enforced on the SQL Server production schema.  If it is enforced, this
--   query returns zero rows for case (a); non-zero means orphans exist.
--
-- Case (b) — hash mismatch
--   The artifact JSON blob (artifact_path in blob storage) carries a "hash"
--   field that the endpoint compares to documents.hash at request time.  Blob
--   storage is not queryable from SQL, so a direct hash comparison is not
--   possible.  As a best-effort proxy we compare documents.hash (current) to
--   the hash recorded on the most recent deep_extract analyst event for that
--   document.  If they differ the artifact is effectively stale — the next
--   request will detect the mismatch and re-extract.  The old blob is dead
--   weight but the cache is NOT incorrectly served.
--   If no analyst_event record exists for a financial_extraction row (possible
--   for rows created before event logging was added) the hash check is skipped
--   and orphan_reason is set to 'no_event_record'.
-- =============================================================================

with last_event as (
    -- Most recent deep_extract event per (firm_id, final_name)
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
 where d.id is null                                                    -- (a) missing document
    or (
        d.id is not null
        and (le.event_hash is null or d.hash <> le.event_hash)
       )                                                               -- (b) hash mismatch proxy
 order by fe.firm_id, fe.updated_at desc;


-- =============================================================================
-- Section 3 — Prune orphans  [COMMENTED OUT BY DEFAULT — read instructions]
-- =============================================================================
-- This section deletes the orphan rows identified by Section 2 case (a):
-- financial_extractions rows where the parent document no longer exists.
--
-- HOW TO USE (dry-run first):
--   1. Run the block as-is.  The BEGIN TRANSACTION + ROLLBACK means nothing is
--      committed.  The SELECT inside shows the count of rows that WOULD be
--      deleted.
--   2. Review the count.  Cross-check against Section 2 output.
--   3. When satisfied, uncomment the DELETE statement and change ROLLBACK
--      TRANSACTION to COMMIT TRANSACTION at the bottom, then re-run.
--
-- NOTE: this only prunes case (a) orphans (missing document).  Case (b) rows
-- (hash mismatch) are NOT pruned here because the cache self-heals on the next
-- request — the endpoint detects the mismatch, re-extracts, and overwrites the
-- blob and the dbo.financial_extractions row.  Pruning case (b) rows would just
-- force an unnecessary re-extraction on the next API call.
--
-- The artifact blobs in Azure Blob Storage are NOT deleted by this script.
-- After verifying the prune, delete orphan blobs separately using:
--   az storage blob delete-batch --source <container> --pattern "<firm_id>/<document_id>/*"
-- for each (firm_id, document_id) pair that was pruned.
-- =============================================================================

begin transaction;

-- Step 1 — count rows that will be deleted (always runs; safe)
select count(*) as orphan_rows_to_delete
  from dbo.financial_extractions fe
 where not exists (
       select 1
         from dbo.documents d
        where d.id      = fe.document_id
          and d.firm_id = fe.firm_id
       );

-- Step 2 — uncomment the DELETE below when ready to apply
/*
delete from dbo.financial_extractions
 where not exists (
       select 1
         from dbo.documents d
        where d.id      = financial_extractions.document_id
          and d.firm_id = financial_extractions.firm_id
       );

select @@rowcount as rows_deleted;
*/

-- Step 3 — change to COMMIT TRANSACTION when ready to apply
rollback transaction;
-- commit transaction;
