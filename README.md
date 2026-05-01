# Aprio Strata

> _Documents, layered into strata._

Aprio Strata is a document-ingestion and taxonomy tool for accounting and advisory firms. Users upload client documents, MinerU reads the first three pages, the configured LLM proposes a consistent `snake_case` filename and bucket, and analysts review the result before the document is committed to the firm library.

The Azure version is designed for company deployment: Microsoft Entra ID for sign-in, Azure SQL for metadata, Azure Blob Storage for canonical document bytes, Microsoft Graph for SharePoint/OneDrive import and export, and either Azure OpenAI or Ollama for model calls.

## Architecture

```
Browser
  │  MSAL sign-in + bearer API token
  ▼
Container Apps (api)        ← managed identity, App Insights, /healthz, /readyz
  │  validates Microsoft Entra ID JWTs
  │  runs MinerU on temporary local files
  │  streams Azure OpenAI or Ollama filename/bucket suggestions
  │  writes metadata to Azure SQL
  │  writes accepted documents to Azure Blob Storage
  │  imports/exports SharePoint or OneDrive files through Microsoft Graph
  ▼
Firm document library
```

The app still supports a local development fallback when Azure environment variables are absent. In that mode it uses `sorted/.manifest.json` and local files. Model suggestions use whichever provider `STRATA_AI_PROVIDER` selects.

## Azure Services

- **Microsoft Entra ID**: browser login through MSAL; backend validates access tokens. Azure SQL itself is Entra-only — no SQL passwords anywhere.
- **Azure SQL**: firms, profiles, clients, buckets, documents, echoes, analyst events, and finance extractions. Schema in `azure_schema.sql` includes a `tenant_isolation` security policy created in OFF state — `azure_store.py` already calls `sp_set_session_context` on each connection so flipping the policy to ON is a one-line change.
- **Azure Blob Storage**: canonical private document storage via managed identity. The app serves same-origin authenticated download URLs at `/sorter/blob/...`.
- **Application Insights / Log Analytics**: enabled automatically when `APPLICATIONINSIGHTS_CONNECTION_STRING` is present. Container Apps wires this when bound to the App Insights resource provisioned by `infra/main.bicep`.
- **Microsoft Graph**: SharePoint/OneDrive import via `/sorter/graph/import` and export via `/sorter/graph/export`.
- **Model runtime**: `STRATA_AI_PROVIDER=azure_openai` uses Azure OpenAI; `STRATA_AI_PROVIDER=ollama` uses a local or tenant-hosted Ollama endpoint; `STRATA_AI_PROVIDER=document_intelligence` routes naming through the same Azure OpenAI chat path on Markdown excerpts (full Document Intelligence layout runs use ``strata/ai/document_intelligence.py`` on raw bytes — wire that into workers or future routes as needed).
- **Deep extract**: `POST /sorter/deep-extract/{final_name}` runs MinerU `hybrid-auto-engine` on demand for any file in any bucket, persists the canonical-row artifact to `dbo.financial_extractions`, and saves the raw `source.md` + `source_content_list.json` to blob alongside the artifact for later LLM querying. Results are cached by content hash; a second click streams a `done` event immediately. Analysts download the reviewed XLSX from `/sorter/finance/extractions/{document_id}/export`.

## Configuration

Copy `.env.example` and set the values for the target Azure tenant and resources:

```bash
cp .env.example .env
```

Required for Azure mode:

- `AZURE_TENANT_ID`
- `AZURE_API_CLIENT_ID` (Strata API app registration; falls back to `AZURE_CLIENT_ID` for legacy `.env` files)
- `AZURE_API_AUDIENCE`
- `AZURE_API_SCOPE`
- `AZURE_SQL_SERVER` and `AZURE_SQL_DATABASE` (or `AZURE_SQL_CONNECTION_STRING`)
- `AZURE_STORAGE_CONTAINER`
- `AZURE_STORAGE_ACCOUNT_URL` (managed identity) or `AZURE_STORAGE_CONNECTION_STRING` (local dev only)

Required for hosted Azure OpenAI mode:

- `STRATA_AI_PROVIDER=azure_openai` (or `document_intelligence` / `ollama` — see Azure Services above)
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`

Optional — Document Intelligence (layout / read on raw files):

- `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` (e.g. `https://<resource>.cognitiveservices.azure.com/`)
- `AZURE_DOCUMENT_INTELLIGENCE_KEY`

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

If you want to run *against* Azure SQL from your laptop, install the Microsoft ODBC driver first:

```bash
brew install msodbcsql18           # macOS
# Debian/Ubuntu: follow https://learn.microsoft.com/sql/connect/odbc/linux-mac/install-microsoft-odbc-driver-sql-server
az login                            # DefaultAzureCredential picks up the CLI session
```

The first MinerU run downloads model weights into the runtime cache. In Azure, use a container image or persistent volume strategy that avoids re-downloading weights on every cold start.

## Deploy to Azure (`azd up`)

`infra/main.bicep` provisions a Log Analytics workspace, Application Insights, Key Vault, Storage Account, Azure SQL server + database (Entra-only auth), Azure Container Registry, a Container Apps environment, the `api` Container App, and the role assignments needed for the workload's user-assigned managed identity.

```bash
# Once per machine:
brew install azure-dev sqlcmd       # macOS; or follow Microsoft docs

# Inside the repo:
azd auth login
azd env new aprio-strata-dev        # pick a name; can be anything
azd env set AZURE_LOCATION eastus2
azd env set AZURE_PRINCIPAL_ID "$(az ad signed-in-user show --query id -o tsv)"
azd env set AZURE_PRINCIPAL_TYPE User
azd env set AZURE_TENANT_ID "$(az account show --query tenantId -o tsv)"
# Strata API app registration (created separately in Entra):
azd env set AZURE_API_CLIENT_ID <api-app-client-id>   # parameter name in Bicep is entraClientId
azd env set AZURE_API_AUDIENCE  api://<api-app-client-id>
# Optional: point at an existing Azure OpenAI / Foundry deployment
azd env set AZURE_OPENAI_ENDPOINT   https://<aoai>.openai.azure.com
azd env set AZURE_OPENAI_DEPLOYMENT <chat-deployment-name>

azd up                              # builds the image, pushes to ACR, deploys
```

The `postprovision` hook applies `azure_schema.sql` to the new database via `sqlcmd -G` (Entra auth). When that finishes, `azd` reports the API URL — that's the Container App ingress.

## Container

```bash
docker build -t aprio-strata .
docker run --env-file .env -p 8765:8765 aprio-strata
```

The Dockerfile installs `msodbcsql18` so `pyodbc` can talk to Azure SQL with `Authentication=ActiveDirectoryDefault`. In Container Apps, the user-assigned managed identity provisioned by Bicep is what the SQL driver picks up.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `/` | Focus search |
| `D` | Open deposit drawer |
| `⌘↵` | Commit in drawer |
| `Esc` | Close drawer, preview, or shortcut overlay |
| `?` | Toggle keyboard help |

## Files

- `app.py`: FastAPI app shell, MinerU playground, App Insights wiring, `/healthz` and `/readyz` probes.
- `sorter/impl.py`: Strata routes, upload/apply flow, and configurable model calls (HTML lives in `sorter/templates/index.html`, AI streaming in `strata/ai/streaming.py`).
- `strata/`: shared platform code (AI providers) so Aprio can grow shared services without touching every route.
- `finance_extract.py`: on-demand MinerU `hybrid-auto-engine` extraction, canonical-row builder, review updates, and Excel/CSV export helpers.
- `azure_store.py`: Entra ID auth, Azure SQL persistence (pyodbc), Blob Storage, and Graph helpers.
- `azure_schema.sql`: Azure SQL (T-SQL) schema, including a disabled tenant-isolation security policy.
- `infra/main.bicep`: full deployment graph (Log Analytics, App Insights, Key Vault, Storage, SQL, ACR, Container Apps env + api app, MI + RBAC).
- `azure.yaml`: `azd up` orchestration.
- `requirements.txt`: Python dependencies.
- `Dockerfile`: Container entrypoint for Azure deployment (includes `msodbcsql18`).

## License

MIT
