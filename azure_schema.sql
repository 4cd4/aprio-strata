-- Aprio Strata Azure PostgreSQL schema.
-- Run against the Azure Database for PostgreSQL database configured by AZURE_POSTGRES_DSN.

create extension if not exists pgcrypto;

create table if not exists firms (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now()
);

create table if not exists profiles (
  -- Microsoft Entra object id (oid claim). This is text because Entra object ids
  -- are UUID-shaped today but should not be coupled to a PostgreSQL uuid parser.
  id text primary key,
  firm_id uuid not null references firms(id) on delete cascade,
  email text,
  display_name text,
  role text not null default 'member' check (role in ('owner', 'admin', 'member', 'analyst')),
  created_at timestamptz not null default now()
);

create table if not exists clients (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  unique (firm_id, name)
);

create table if not exists buckets (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  client_id uuid not null references clients(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  unique (firm_id, client_id, name)
);

create table if not exists documents (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  hash text not null,
  original text,
  final_name text not null,
  client_id uuid references clients(id) on delete set null,
  bucket_id uuid references buckets(id) on delete set null,
  client_name text not null default 'Unassigned',
  bucket_name text not null default 'Unfiled',
  gemma_bucket text,
  gemma_suggested text,
  storage_bucket text not null default 'firm-documents',
  storage_path text not null,
  graph_drive_id text,
  graph_item_id text,
  size bigint,
  applied_at timestamptz not null default now(),
  created_by text references profiles(id) on delete set null,
  unique (firm_id, hash),
  unique (firm_id, final_name)
);

create table if not exists document_echoes (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  original text,
  uploaded_at timestamptz not null default now(),
  source text not null default 'deposit',
  created_by text references profiles(id) on delete set null
);

create table if not exists analyst_events (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  user_id text references profiles(id) on delete set null,
  event text not null,
  file text,
  hash text,
  client text,
  payload jsonb not null default '{}'::jsonb,
  at timestamptz not null default now()
);

create table if not exists financial_extractions (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  status text not null default 'pending_review',
  artifact_bucket text not null default 'firm-documents',
  artifact_path text not null,
  row_count integer not null default 0,
  pending_rows integer not null default 0,
  approved_rows integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  created_by text references profiles(id) on delete set null,
  unique (firm_id, document_id)
);

create index if not exists idx_profiles_firm_id on profiles(firm_id);
create index if not exists idx_clients_firm_id on clients(firm_id);
create index if not exists idx_buckets_firm_client on buckets(firm_id, client_id);
create index if not exists idx_documents_firm_hash on documents(firm_id, hash);
create index if not exists idx_documents_firm_client_bucket on documents(firm_id, client_name, bucket_name);
create index if not exists idx_documents_graph_item on documents(graph_drive_id, graph_item_id);
create index if not exists idx_document_echoes_firm_doc on document_echoes(firm_id, document_id);
create index if not exists idx_analyst_events_firm_at on analyst_events(firm_id, at);
create index if not exists idx_analyst_events_payload on analyst_events using gin(payload);
create index if not exists idx_financial_extractions_firm_status on financial_extractions(firm_id, status);
