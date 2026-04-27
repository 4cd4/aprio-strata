# Aprio Strata

> _Documents, layered into strata._

Aprio Strata is a document-ingestion and taxonomy tool for accounting and advisory firms. Users upload client documents, MinerU reads the first three pages, the configured LLM proposes a consistent `snake_case` filename and bucket, and analysts review the result before the document is committed to the firm library.

The Azure version is designed for company deployment: Microsoft Entra ID for sign-in, Azure Database for PostgreSQL for metadata, Azure Blob Storage for canonical document bytes, Microsoft Graph for SharePoint/OneDrive import and export, and either Azure OpenAI or Ollama for model calls.

## Architecture

```
Browser
  │  MSAL sign-in + bearer API token
  ▼
FastAPI
  │  validates Microsoft Entra ID JWTs
  │  runs MinerU on temporary local files
  │  streams Azure OpenAI or Ollama filename/bucket suggestions
  │  writes metadata to Azure PostgreSQL
  │  writes accepted documents to Azure Blob Storage
  │  imports/exports SharePoint or OneDrive files through Microsoft Graph
  ▼
Firm document library
```

The app still supports a local development fallback when Azure environment variables are absent. In that mode it uses `sorted/.manifest.json` and local files. Model suggestions use whichever provider `STRATA_AI_PROVIDER` selects.

## Azure Services

- **Microsoft Entra ID**: browser login through MSAL; backend validates access tokens.
- **Azure Database for PostgreSQL**: firms, profiles, clients, buckets, documents, echoes, and analyst events. Run `azure_schema.sql` in the target database.
- **Azure Blob Storage**: canonical private document storage. The app serves same-origin authenticated download URLs at `/sorter/blob/...`.
- **Microsoft Graph**: SharePoint/OneDrive import via `/sorter/graph/import` and export via `/sorter/graph/export`.
- **Model runtime**: `STRATA_AI_PROVIDER=azure_openai` uses Azure OpenAI; `STRATA_AI_PROVIDER=ollama` uses a local or tenant-hosted Ollama endpoint.

## Configuration

Copy `.env.example` and set the values for the target Azure tenant and resources:

```bash
cp .env.example .env
```

Required for Azure mode:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_API_AUDIENCE`
- `AZURE_API_SCOPE`
- `AZURE_POSTGRES_DSN`
- `AZURE_STORAGE_CONTAINER`
- `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_STORAGE_ACCOUNT_URL`

Required for hosted Azure OpenAI mode:

- `STRATA_AI_PROVIDER=azure_openai`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`

Required for Ollama mode:

- `STRATA_AI_PROVIDER=ollama`
- `OLLAMA_URL`
- `OLLAMA_MODEL`

Required for Graph import/export:

- `MICROSOFT_GRAPH_DRIVE_ID`
- `MICROSOFT_GRAPH_ROOT_FOLDER`
- Optional app-only credentials: `MICROSOFT_GRAPH_CLIENT_ID`, `MICROSOFT_GRAPH_CLIENT_SECRET`

## Model Data Retention

Azure OpenAI mode sends document excerpts to Azure OpenAI for naming and classification. That is enterprise-controlled in Azure, but it should not be described as zero data retention unless the customer has the right Azure OpenAI configuration and Microsoft terms.

Ollama mode keeps model inference out of hosted OpenAI/Azure OpenAI services. For a zero model-retention posture, run Ollama on the same machine, a private VM, or a tenant-controlled GPU host, then set:

```bash
STRATA_AI_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
```

In Ollama mode, the model prompts do not leave that Ollama host. If Entra ID, Blob Storage, PostgreSQL, or Graph are still enabled, document metadata and accepted files still live in the company's Azure tenant.

## Finance Extraction

Accepted documents whose final bucket is `Finance` are full-parsed with MinerU after commit. Strata then extracts candidate financial statement tables, normalizes rows, and stores a sidecar `financial_extract.json` artifact for review.

The first version exports reviewed normalized data rather than writing directly into a financial model:

- `GET /sorter/finance/extractions` lists extraction artifacts.
- `GET /sorter/finance/extractions/{document_id}` returns normalized rows.
- `POST /sorter/finance/extractions/{document_id}/review` updates row values/statuses.
- `GET /sorter/finance/extractions/{document_id}/export?format=xlsx` exports approved rows to Excel.
- `GET /sorter/finance/extractions/{document_id}/export?format=csv` exports approved rows to CSV.

Rows are exported only after they are marked `approved` or `reviewed`. Local mode stores artifacts under `sorted/.extractions/`; Azure mode stores JSON artifacts beside the document blob and tracks them in the `financial_extractions` table.

## Run Locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`.

The first MinerU run downloads model weights into the runtime cache. In Azure, use a container image or persistent volume strategy that avoids re-downloading weights on every cold start.

## Container

```bash
docker build -t aprio-strata .
docker run --env-file .env -p 8765:8765 aprio-strata
```

For Azure Container Apps or App Service for Containers, prefer managed identity for Blob Storage where possible and put secrets in Key Vault or platform-managed secret settings.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `/` | Focus search |
| `D` | Open deposit drawer |
| `⌘↵` | Commit in drawer |
| `Esc` | Close drawer, preview, or shortcut overlay |
| `?` | Toggle keyboard help |

## Files

- `app.py`: FastAPI app shell and MinerU playground.
- `sorter.py`: Strata routes, UI, upload/apply flow, and configurable model calls.
- `finance_extract.py`: full MinerU finance extraction, review updates, and Excel/CSV export helpers.
- `azure_store.py`: Entra ID auth, PostgreSQL persistence, Blob Storage, and Graph helpers.
- `azure_schema.sql`: Azure PostgreSQL schema.
- `requirements.txt`: Python dependencies.
- `Dockerfile`: Container entrypoint for Azure deployment.

## License

MIT
