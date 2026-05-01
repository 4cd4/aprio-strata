"""Azure-backed persistence, auth, storage, and Graph integration for Strata.

The module is intentionally import-safe when Azure dependencies are not yet
installed. Azure mode only turns on when the required environment variables are
present; otherwise the app keeps using the local manifest paths in ``sorter.py``.

Database: Azure SQL via ``pyodbc`` with ``Authentication=ActiveDirectoryDefault``
(managed identity in Container Apps, ``az login`` locally). All SQL is T-SQL.
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx


DEFAULT_BUCKETS = ["Finance", "Tax", "Legal"]
DEFAULT_CONTAINER = "firm-documents"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

# Reuse a single HTTP client for all Graph and OAuth-token calls. New clients per
# request would force a TLS handshake every time — Graph endpoints sit behind
# global LB/CDN where keep-alive matters.
_GRAPH_HTTP = httpx.Client(timeout=120, follow_redirects=True)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    email: str
    firm_id: str
    role: str = "member"
    name: str = ""
    access_token: str = ""


class AzureStore:
    def __init__(self) -> None:
        self.tenant_id = os.environ.get("AZURE_TENANT_ID", "").strip()
        # AZURE_API_CLIENT_ID is the Strata API app registration (used for JWT
        # audience checks and MSAL). AZURE_CLIENT_ID is reserved for the
        # DefaultAzureCredential managed-identity hint and is read directly
        # by azure-identity / msodbcsql18; we don't need to read it here.
        # AZURE_CLIENT_ID is honored as a fallback for backward compatibility.
        self.client_id = (
            os.environ.get("AZURE_API_CLIENT_ID", "").strip()
            or os.environ.get("AZURE_CLIENT_ID", "").strip()
        )
        self.api_audience = os.environ.get("AZURE_API_AUDIENCE", self.client_id).strip()
        self.api_scope = os.environ.get("AZURE_API_SCOPE", f"api://{self.client_id}/access_as_user").strip()
        self.redirect_uri = os.environ.get("AZURE_REDIRECT_URI", "").strip()
        self.sql_server = os.environ.get("AZURE_SQL_SERVER", "").strip()
        self.sql_database = os.environ.get("AZURE_SQL_DATABASE", "").strip()
        self.sql_connection_string = os.environ.get("AZURE_SQL_CONNECTION_STRING", "").strip()
        self.storage_container = os.environ.get("AZURE_STORAGE_CONTAINER", DEFAULT_CONTAINER).strip()
        self.storage_connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        self.storage_account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL", "").strip()
        self.graph_site_id = os.environ.get("MICROSOFT_GRAPH_SITE_ID", "").strip()
        self.graph_drive_id = os.environ.get("MICROSOFT_GRAPH_DRIVE_ID", "").strip()
        self.graph_root_folder = os.environ.get("MICROSOFT_GRAPH_ROOT_FOLDER", "Strata").strip().strip("/")
        self.graph_client_id = os.environ.get("MICROSOFT_GRAPH_CLIENT_ID", self.client_id).strip()
        self.graph_client_secret = os.environ.get("MICROSOFT_GRAPH_CLIENT_SECRET", "").strip()
        self._graph_app_token: tuple[str, float] | None = None
        self._jwk_client: Any | None = None
        self._sql_credential: Any | None = None
        self._tls_local = threading.local()

    @property
    def enabled(self) -> bool:
        return bool(self.tenant_id and self.client_id and self._sql_configured())

    def _sql_configured(self) -> bool:
        if self.sql_connection_string:
            return True
        return bool(self.sql_server and self.sql_database)

    def public_config(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tenant_id": self.tenant_id if self.enabled else "",
            "client_id": self.client_id if self.enabled else "",
            "authority": f"https://login.microsoftonline.com/{self.tenant_id}" if self.enabled else "",
            "redirect_uri": self.redirect_uri if self.enabled else "",
            "api_scope": self.api_scope if self.enabled else "",
            "graph_scopes": ["User.Read", "Files.ReadWrite.All", "Sites.ReadWrite.All"] if self.enabled else [],
            "graph_enabled": bool(self.enabled and self.graph_drive_id),
        }

    def _check_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("Azure integration is not configured")

    # ------------------------------------------------------------------
    # Database (Azure SQL via pyodbc)
    # ------------------------------------------------------------------

    def _connection_string(self) -> str:
        if self.sql_connection_string:
            return self.sql_connection_string
        # No Authentication= here: msodbcsql18 on Linux doesn't support
        # ActiveDirectoryDefault. We pass an AAD bearer token via the
        # SQL_COPT_SS_ACCESS_TOKEN attribute below.
        return (
            f"Driver={{{ODBC_DRIVER}}};"
            f"Server=tcp:{self.sql_server},1433;"
            f"Database={self.sql_database};"
            "Encrypt=yes;TrustServerCertificate=no;"
            "Connection Timeout=30;"
        )

    def _aad_token_struct(self) -> bytes:
        """Acquire an Entra access token for Azure SQL and pack it for ODBC.

        msodbcsql18 expects the token as a UTF-16-LE byte string prefixed by a
        little-endian uint32 length, passed via the connection attribute
        ``SQL_COPT_SS_ACCESS_TOKEN`` (1256). DefaultAzureCredential picks up
        the user-assigned MI in Container Apps (via AZURE_CLIENT_ID) and falls
        through to az-cli auth locally.
        """
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError("Install azure-identity to use Entra auth for Azure SQL") from exc
        if self._sql_credential is None:
            self._sql_credential = DefaultAzureCredential()
        token = self._sql_credential.get_token("https://database.windows.net/.default").token
        token_bytes = token.encode("utf-16-le")
        from struct import pack
        return pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    def _connect(self):
        self._check_enabled()
        try:
            import pyodbc
        except ImportError as exc:
            raise RuntimeError("Install pyodbc + msodbcsql18 to use Azure SQL") from exc
        # 1256 = SQL_COPT_SS_ACCESS_TOKEN — msodbc connection attribute that
        # accepts a pre-acquired AAD bearer token instead of user/pwd auth.
        attrs = {1256: self._aad_token_struct()} if not self.sql_connection_string else None
        conn = pyodbc.connect(self._connection_string(), autocommit=True, attrs_before=attrs)
        firm_id = getattr(self._tls_local, "firm_id", None)
        if firm_id:
            with conn.cursor() as cur:
                cur.execute("exec sys.sp_set_session_context @key = N'firm_id', @value = ?", firm_id)
        return conn

    @contextmanager
    def session(self):
        """Reuse a single SQL connection for every query inside the block.

        ODBC connect + AAD auth costs hundreds of ms per call, so wrapping
        multi-query methods (load_manifest, auth_context, ensure_*) in a session
        collapses N round-trips into one. Nested ``session()`` calls are no-ops
        and safely share the outermost connection.
        """
        if getattr(self._tls_local, "conn", None) is not None:
            yield
            return
        conn = self._connect()
        self._tls_local.conn = conn
        try:
            yield
        finally:
            self._tls_local.conn = None
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _rows_to_dicts(cursor) -> list[dict[str, Any]]:
        if cursor.description is None:
            return []
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _exec(self, sql: str, params: tuple[Any, ...], op):
        conn = getattr(self._tls_local, "conn", None)
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return op(cur)
        with self._connect() as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                return op(cur)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._exec(sql, params, self._rows_to_dicts)

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._exec(sql, params, lambda _cur: None)

    def use_firm(self, firm_id: str | None) -> None:
        """Bind subsequent connections from this thread to a firm for RLS.

        The schema's ``tenant_isolation`` security policy is created in OFF
        state. Setting this value still issues ``sp_set_session_context`` on
        every new connection so that flipping the policy to ON is a one-line
        change with no application work required.
        """
        self._tls_local.firm_id = firm_id

    # ------------------------------------------------------------------
    # Microsoft Entra ID auth
    # ------------------------------------------------------------------

    def _jwks_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    def _issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def _decode_token(self, token: str) -> dict[str, Any]:
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as exc:
            raise RuntimeError("Install PyJWT[crypto] to validate Entra ID tokens") from exc

        # PyJWKClient caches signing keys for its lifetime — instantiating it
        # per request triggers a fresh JWKS fetch on every authenticated call.
        if self._jwk_client is None:
            self._jwk_client = PyJWKClient(self._jwks_url())
        signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        audiences = [a for a in {self.api_audience, self.client_id, f"api://{self.client_id}"} if a]
        last_error: Exception | None = None
        for audience in audiences:
            try:
                return jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience=audience,
                    issuer=self._issuer(),
                    options={"require": ["exp", "iat"]},
                )
            except Exception as exc:
                last_error = exc
        raise PermissionError(f"invalid Entra token: {last_error}") from last_error

    def auth_context(self, authorization: str | None) -> AuthContext:
        self._check_enabled()
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PermissionError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        claims = self._decode_token(token)
        user_id = claims.get("oid") or claims.get("sub")
        email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
        name = claims.get("name") or email
        if not user_id:
            raise PermissionError("Entra token did not include an object id")
        with self.session():
            profile = self._one(
                "select top 1 id, firm_id, role, email, display_name from dbo.profiles where id = ?",
                (user_id,),
            )
        if not profile:
            raise LookupError("Azure user has no firm profile")
        return AuthContext(
            user_id=user_id,
            email=email or profile.get("email") or "",
            firm_id=str(profile["firm_id"]),
            role=profile.get("role") or "member",
            name=name or profile.get("display_name") or "",
            access_token=token,
        )

    def bootstrap_profile(self, authorization: str | None, firm_name: str) -> AuthContext:
        self._check_enabled()
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PermissionError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        claims = self._decode_token(token)
        user_id = claims.get("oid") or claims.get("sub")
        email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
        name = claims.get("name") or email
        if not user_id:
            raise PermissionError("Entra token did not include an object id")
        with self.session():
            existing = self._one(
                "select top 1 id, firm_id, role, email, display_name from dbo.profiles where id = ?",
                (user_id,),
            )
            if existing:
                return AuthContext(
                    user_id=user_id,
                    email=email or existing.get("email") or "",
                    firm_id=str(existing["firm_id"]),
                    role=existing.get("role") or "member",
                    name=name or existing.get("display_name") or "",
                    access_token=token,
                )
            firm = self._one(
                "insert into dbo.firms (name) output inserted.id, inserted.name values (?)",
                ((firm_name or "My Firm").strip() or "My Firm",),
            )
            assert firm is not None
            self._execute(
                "insert into dbo.profiles (id, firm_id, email, display_name, role) values (?, ?, ?, ?, 'owner')",
                (user_id, firm["id"], email, name),
            )
        return AuthContext(user_id=user_id, email=email, firm_id=str(firm["id"]), role="owner", name=name, access_token=token)

    # ------------------------------------------------------------------
    # Clients / buckets (taxonomy)
    # ------------------------------------------------------------------

    def ensure_client(self, firm_id: str, name: str) -> dict[str, Any] | None:
        if not name or name == "Unassigned":
            return None
        row = self._one(
            "select top 1 * from dbo.clients where firm_id = ? and name = ?",
            (firm_id, name),
        )
        if row:
            return row
        # MERGE pattern is fine here: the unique (firm_id, name) constraint
        # backstops any race between concurrent inserts.
        return self._one(
            """
            merge into dbo.clients with (holdlock) as target
            using (select cast(? as uniqueidentifier) as firm_id, cast(? as nvarchar(255)) as name) as source
              on  target.firm_id = source.firm_id
              and target.name    = source.name
            when matched then update set name = source.name
            when not matched then insert (firm_id, name) values (source.firm_id, source.name)
            output inserted.*;
            """,
            (firm_id, name),
        )

    def ensure_bucket(self, firm_id: str, client_id: str | None, name: str) -> dict[str, Any] | None:
        if not client_id or not name or name == "Unfiled":
            return None
        row = self._one(
            "select top 1 * from dbo.buckets where firm_id = ? and client_id = ? and name = ?",
            (firm_id, client_id, name),
        )
        if row:
            return row
        return self._one(
            """
            merge into dbo.buckets with (holdlock) as target
            using (select cast(? as uniqueidentifier) as firm_id,
                          cast(? as uniqueidentifier) as client_id,
                          cast(? as nvarchar(255))    as name) as source
              on  target.firm_id   = source.firm_id
              and target.client_id = source.client_id
              and target.name      = source.name
            when matched then update set name = source.name
            when not matched then insert (firm_id, client_id, name)
              values (source.firm_id, source.client_id, source.name)
            output inserted.*;
            """,
            (firm_id, client_id, name),
        )

    def client_buckets(self, firm_id: str, client: str) -> list[str]:
        if not client or client == "Unassigned":
            return list(DEFAULT_BUCKETS)
        rows = self._query(
            """
            select b.name
              from dbo.buckets b
              join dbo.clients c on c.id = b.client_id
             where b.firm_id = ?
               and c.name    = ?
             order by b.name asc
            """,
            (firm_id, client),
        )
        names = [r["name"] for r in rows]
        return names or list(DEFAULT_BUCKETS)

    # ------------------------------------------------------------------
    # Manifest aggregation
    # ------------------------------------------------------------------

    def load_manifest(self, firm_id: str) -> dict[str, Any]:
        with self.session():
            docs = self._query("select * from dbo.documents where firm_id = ? order by applied_at desc", (firm_id,))
            echoes = self._query("select * from dbo.document_echoes where firm_id = ? order by uploaded_at desc", (firm_id,))
            clients = self._query("select id, name from dbo.clients where firm_id = ? order by name asc", (firm_id,))
            buckets = self._query("select client_id, name from dbo.buckets where firm_id = ? order by name asc", (firm_id,))
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for echo in echoes:
            by_doc.setdefault(str(echo["document_id"]), []).append({
                "original": echo.get("original"),
                "uploaded_at": _iso(echo.get("uploaded_at")),
                "source": echo.get("source") or "deposit",
            })
        client_by_id = {str(c["id"]): c["name"] for c in clients}
        taxonomies: dict[str, list[str]] = {c["name"]: [] for c in clients}
        for bucket in buckets:
            cname = client_by_id.get(str(bucket.get("client_id")))
            if cname:
                taxonomies.setdefault(cname, []).append(bucket["name"])
        files = []
        for doc in docs:
            files.append({
                "id": str(doc["id"]),
                "hash": doc["hash"],
                "original": doc.get("original"),
                "final_name": doc.get("final_name"),
                "client": doc.get("client_name") or "Unassigned",
                "bucket": doc.get("bucket_name") or "Unfiled",
                "applied_at": _iso(doc.get("applied_at")),
                "echoes": by_doc.get(str(doc["id"]), []),
                "gemma_bucket": doc.get("gemma_bucket"),
                "gemma_suggested": doc.get("gemma_suggested"),
                "storage_bucket": doc.get("storage_bucket"),
                "storage_path": doc.get("storage_path"),
                "graph_drive_id": doc.get("graph_drive_id"),
                "graph_item_id": doc.get("graph_item_id"),
                "size": doc.get("size"),
            })
        return {"files": files, "clients": [c["name"] for c in clients], "client_taxonomies": taxonomies}

    # ------------------------------------------------------------------
    # Blob storage
    # ------------------------------------------------------------------

    def blob_client(self, storage_path: str):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobClient
        except ImportError as exc:
            raise RuntimeError("Install azure-storage-blob and azure-identity to use Azure Blob Storage") from exc
        if self.storage_connection_string:
            return BlobClient.from_connection_string(
                self.storage_connection_string,
                container_name=self.storage_container,
                blob_name=storage_path,
            )
        if not self.storage_account_url:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL is required")
        return BlobClient(
            account_url=self.storage_account_url,
            container_name=self.storage_container,
            blob_name=storage_path,
            credential=DefaultAzureCredential(),
        )

    def download_blob(self, storage_path: str) -> tuple[Iterator[bytes], str]:
        blob = self.blob_client(storage_path)
        props = blob.get_blob_properties()
        content_type = props.content_settings.content_type or mimetypes.guess_type(storage_path)[0] or "application/octet-stream"
        stream = blob.download_blob()
        return stream.chunks(), content_type

    def read_json_blob(self, storage_path: str) -> dict[str, Any] | None:
        if not storage_path:
            return None
        blob = self.blob_client(storage_path)
        try:
            data = blob.download_blob().readall()
        except Exception:
            return None
        return json.loads(data.decode("utf-8"))

    def write_json_blob(self, storage_path: str, payload: dict[str, Any]) -> None:
        blob = self.blob_client(storage_path)
        blob.upload_blob(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            overwrite=True,
            content_type="application/json",
        )

    def write_bytes_blob(self, storage_path: str, data: bytes, *, content_type: str | None = None) -> None:
        blob = self.blob_client(storage_path)
        ct = content_type or mimetypes.guess_type(storage_path)[0] or "application/octet-stream"
        blob.upload_blob(data, overwrite=True, content_type=ct)

    def file_url(self, storage_path: str) -> str | None:
        if not storage_path:
            return None
        return f"/sorter/blob/{quote(storage_path, safe='')}"

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def _remote_name_available(self, firm_id: str, final_name: str) -> bool:
        row = self._one(
            "select top 1 id from dbo.documents where firm_id = ? and final_name = ?",
            (firm_id, final_name),
        )
        return row is None

    def resolve_remote_name(self, firm_id: str, stem: str, ext: str) -> str:
        candidate = f"{stem}{ext}"
        if self._remote_name_available(firm_id, candidate):
            return candidate
        i = 2
        while True:
            candidate = f"{stem}_{i}{ext}"
            if self._remote_name_available(firm_id, candidate):
                return candidate
            i += 1

    def create_document(
        self,
        ctx: AuthContext,
        *,
        src: Path,
        original: str,
        stem: str,
        ext: str,
        file_hash: str,
        client_name: str,
        bucket_name: str,
        gemma_bucket: str | None,
        gemma_suggested: str | None,
    ) -> dict[str, Any]:
        with self.session():
            final_name = self.resolve_remote_name(ctx.firm_id, stem, ext)
            doc_id = str(uuid.uuid4())
            storage_path = f"{ctx.firm_id}/{doc_id}/{final_name}"
            data = src.read_bytes()
            content_type = mimetypes.guess_type(final_name)[0] or "application/octet-stream"
            blob = self.blob_client(storage_path)
            blob.upload_blob(data, overwrite=False, content_type=content_type)
            graph_ref = self.export_file_to_graph(
                ctx, src, final_name, client_name, bucket_name, data=data
            )
            client_row = self.ensure_client(ctx.firm_id, client_name)
            bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, bucket_name)
            doc = self._one(
                """
                insert into dbo.documents (
                  id, firm_id, hash, original, final_name, client_id, bucket_id,
                  client_name, bucket_name, gemma_bucket, gemma_suggested,
                  storage_bucket, storage_path, graph_drive_id, graph_item_id, size, created_by
                )
                output inserted.*
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    ctx.firm_id,
                    file_hash,
                    original,
                    final_name,
                    client_row["id"] if client_row else None,
                    bucket_row["id"] if bucket_row else None,
                    client_name,
                    bucket_name,
                    gemma_bucket,
                    gemma_suggested,
                    self.storage_container,
                    storage_path,
                    graph_ref.get("drive_id"),
                    graph_ref.get("item_id"),
                    len(data),
                    ctx.user_id,
                ),
            )
        assert doc is not None
        return doc

    def find_document_by_hash(self, firm_id: str, file_hash: str) -> dict[str, Any] | None:
        return self._one(
            "select top 1 * from dbo.documents where firm_id = ? and hash = ?",
            (firm_id, file_hash),
        )

    def add_echo(self, ctx: AuthContext, document_id: str, original: str) -> None:
        self._execute(
            """
            insert into dbo.document_echoes (firm_id, document_id, original, source, created_by)
            values (?, ?, ?, 'deposit', ?)
            """,
            (ctx.firm_id, document_id, original, ctx.user_id),
        )

    # ------------------------------------------------------------------
    # Finance extraction
    # ------------------------------------------------------------------

    def save_financial_extraction(self, ctx: AuthContext, document_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        artifact_path = f"{ctx.firm_id}/{document_id}/financial_extract.json"
        self.write_json_blob(artifact_path, artifact)
        summary = artifact.get("summary") or {}
        row = self._one(
            """
            merge into dbo.financial_extractions with (holdlock) as target
            using (
              select
                cast(? as uniqueidentifier) as firm_id,
                cast(? as uniqueidentifier) as document_id,
                cast(? as nvarchar(64))     as status,
                cast(? as nvarchar(255))    as artifact_bucket,
                cast(? as nvarchar(1024))   as artifact_path,
                cast(? as int)              as row_count,
                cast(? as int)              as pending_rows,
                cast(? as int)              as approved_rows,
                cast(? as nvarchar(64))     as created_by
            ) as source
              on target.firm_id = source.firm_id and target.document_id = source.document_id
            when matched then update set
                status         = source.status,
                artifact_path  = source.artifact_path,
                row_count      = source.row_count,
                pending_rows   = source.pending_rows,
                approved_rows  = source.approved_rows,
                updated_at     = sysdatetimeoffset()
            when not matched then insert (
                firm_id, document_id, status, artifact_bucket, artifact_path,
                row_count, pending_rows, approved_rows, created_by, updated_at
            ) values (
                source.firm_id, source.document_id, source.status, source.artifact_bucket,
                source.artifact_path, source.row_count, source.pending_rows,
                source.approved_rows, source.created_by, sysdatetimeoffset()
            )
            output inserted.*;
            """,
            (
                ctx.firm_id,
                document_id,
                artifact.get("status") or "pending_review",
                self.storage_container,
                artifact_path,
                summary.get("row_count") or 0,
                summary.get("pending_rows") or 0,
                summary.get("approved_rows") or 0,
                ctx.user_id,
            ),
        )
        assert row is not None
        return row

    def get_financial_extraction(self, ctx: AuthContext, document_id: str) -> dict[str, Any] | None:
        row = self._one(
            "select top 1 * from dbo.financial_extractions where firm_id = ? and document_id = ?",
            (ctx.firm_id, document_id),
        )
        if not row:
            return None
        return self.read_json_blob(row.get("artifact_path") or "")

    def list_financial_extractions(self, ctx: AuthContext) -> list[dict[str, Any]]:
        rows = self._query(
            """
            select fe.*, d.final_name, d.client_name, d.bucket_name
              from dbo.financial_extractions fe
              join dbo.documents d on d.id = fe.document_id
             where fe.firm_id = ?
             order by fe.updated_at desc
            """,
            (ctx.firm_id,),
        )
        return [
            {
                "document_id": str(row["document_id"]),
                "file": row.get("final_name"),
                "client": row.get("client_name"),
                "bucket": row.get("bucket_name"),
                "status": row.get("status"),
                "created_at": _iso(row.get("created_at")),
                "updated_at": _iso(row.get("updated_at")),
                "summary": {
                    "row_count": row.get("row_count") or 0,
                    "pending_rows": row.get("pending_rows") or 0,
                    "approved_rows": row.get("approved_rows") or 0,
                },
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def update_document_bucket(self, ctx: AuthContext, final_name: str, bucket_name: str) -> dict[str, Any] | None:
        with self.session():
            doc = self._one(
                "select top 1 * from dbo.documents where firm_id = ? and final_name = ?",
                (ctx.firm_id, final_name),
            )
            if not doc:
                return None
            client_row = self.ensure_client(ctx.firm_id, doc.get("client_name") or "Unassigned")
            bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, bucket_name)
            return self._one(
                """
                update dbo.documents
                   set bucket_id   = ?,
                       bucket_name = ?
                 output inserted.*
                 where firm_id    = ?
                   and final_name = ?
                """,
                (bucket_row["id"] if bucket_row else None, bucket_name, ctx.firm_id, final_name),
            )

    def update_document_client(self, ctx: AuthContext, final_name: str, client_name: str) -> dict[str, Any] | None:
        with self.session():
            doc = self._one(
                "select top 1 * from dbo.documents where firm_id = ? and final_name = ?",
                (ctx.firm_id, final_name),
            )
            if not doc:
                return None
            client_row = self.ensure_client(ctx.firm_id, client_name)
            bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, doc.get("bucket_name") or "Unfiled")
            return self._one(
                """
                update dbo.documents
                   set client_id   = ?,
                       bucket_id   = ?,
                       client_name = ?
                 output inserted.*
                 where firm_id    = ?
                   and final_name = ?
                """,
                (client_row["id"] if client_row else None, bucket_row["id"] if bucket_row else None, client_name, ctx.firm_id, final_name),
            )

    def rename_document(self, ctx: AuthContext, final_name: str, new_final_name: str) -> dict[str, Any] | None:
        with self.session():
            doc = self._one(
                "select top 1 * from dbo.documents where firm_id = ? and final_name = ?",
                (ctx.firm_id, final_name),
            )
            if not doc:
                return None
            old_path = doc.get("storage_path") or ""
            new_path = "/".join(old_path.split("/")[:-1] + [new_final_name]) if old_path else ""
            if old_path and new_path != old_path:
                src = self.blob_client(old_path)
                dst = self.blob_client(new_path)
                data = src.download_blob().readall()
                content_type = mimetypes.guess_type(new_final_name)[0] or "application/octet-stream"
                dst.upload_blob(data, overwrite=False, content_type=content_type)
                src.delete_blob()
            return self._one(
                """
                update dbo.documents
                   set final_name   = ?,
                       storage_path = coalesce(nullif(?, ''), storage_path)
                 output inserted.*
                 where firm_id    = ?
                   and final_name = ?
                """,
                (new_final_name, new_path, ctx.firm_id, final_name),
            )

    def remove_client(self, ctx: AuthContext, name: str) -> None:
        self._execute(
            """
            update dbo.documents
               set client_id   = null,
                   client_name = 'Unassigned'
             where firm_id     = ?
               and client_name = ?
            """,
            (ctx.firm_id, name),
        )
        self._execute(
            "delete from dbo.clients where firm_id = ? and name = ?",
            (ctx.firm_id, name),
        )

    def remove_bucket(self, ctx: AuthContext, client: str, bucket: str) -> bool:
        with self.session():
            in_use = self._one(
                """
                select top 1 id from dbo.documents
                 where firm_id     = ?
                   and client_name = ?
                   and bucket_name = ?
                """,
                (ctx.firm_id, client, bucket),
            )
            if in_use:
                return False
            client_row = self._one(
                "select top 1 id from dbo.clients where firm_id = ? and name = ?",
                (ctx.firm_id, client),
            )
            if client_row:
                self._execute(
                    "delete from dbo.buckets where firm_id = ? and client_id = ? and name = ?",
                    (ctx.firm_id, client_row["id"], bucket),
                )
        return True

    # ------------------------------------------------------------------
    # Analyst events
    # ------------------------------------------------------------------

    def log_event(self, ctx: AuthContext, event: dict[str, Any]) -> None:
        at = event.get("at") or datetime.now(timezone.utc)
        self._execute(
            """
            insert into dbo.analyst_events (firm_id, user_id, event, [file], hash, client, payload, at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.firm_id,
                ctx.user_id,
                event.get("event") or "unknown",
                event.get("file") or event.get("to_name") or event.get("from_name"),
                event.get("hash"),
                event.get("client") or event.get("to_client") or event.get("from_client"),
                json.dumps(event),
                at,
            ),
        )

    def analyst_events(self, firm_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "select * from dbo.analyst_events where firm_id = ? order by at asc",
            (firm_id,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            payload_raw = row.get("payload") or "{}"
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else dict(payload_raw)
            except json.JSONDecodeError:
                payload = {}
            payload.setdefault("event", row.get("event"))
            payload.setdefault("at", _iso(row.get("at")))
            payload.setdefault("file", row.get("file"))
            payload.setdefault("hash", row.get("hash"))
            payload.setdefault("client", row.get("client"))
            out.append(payload)
        return out

    # ------------------------------------------------------------------
    # Microsoft Graph
    # ------------------------------------------------------------------

    def _graph_headers(self, ctx: AuthContext | None = None, graph_token: str | None = None) -> dict[str, str]:
        token = graph_token or self._graph_app_access_token() or (ctx.access_token if ctx else "")
        if not token:
            raise RuntimeError("Microsoft Graph token is not available")
        return {"Authorization": f"Bearer {token}"}

    def _graph_app_access_token(self) -> str | None:
        if not (self.tenant_id and self.graph_client_id and self.graph_client_secret):
            return None
        now = time.time()
        if self._graph_app_token and self._graph_app_token[1] > now + 60:
            return self._graph_app_token[0]
        r = _GRAPH_HTTP.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.graph_client_id,
                "client_secret": self.graph_client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        self._graph_app_token = (token, now + int(data.get("expires_in", 3600)))
        return token

    def _graph_path(self, ctx: AuthContext, client_name: str, bucket_name: str, final_name: str) -> str:
        parts = [self.graph_root_folder, ctx.firm_id, client_name or "Unassigned", bucket_name or "Unfiled", final_name]
        return "/".join(quote(p.strip("/"), safe="") for p in parts if p)

    def export_file_to_graph(
        self,
        ctx: AuthContext,
        src: Path,
        final_name: str,
        client_name: str,
        bucket_name: str,
        graph_token: str | None = None,
        *,
        data: bytes | None = None,
    ) -> dict[str, str | None]:
        if not self.graph_drive_id:
            return {"drive_id": None, "item_id": None}
        graph_path = self._graph_path(ctx, client_name, bucket_name, final_name)
        url = f"{GRAPH_BASE}/drives/{self.graph_drive_id}/root:/{graph_path}:/content"
        headers = self._graph_headers(ctx, graph_token)
        headers["Content-Type"] = mimetypes.guess_type(final_name)[0] or "application/octet-stream"
        body = data if data is not None else src.read_bytes()
        r = _GRAPH_HTTP.put(url, headers=headers, content=body)
        r.raise_for_status()
        item = r.json()
        return {"drive_id": item.get("parentReference", {}).get("driveId") or self.graph_drive_id, "item_id": item.get("id")}

    def graph_import_file(self, ctx: AuthContext, item_id: str, graph_token: str | None = None) -> dict[str, Any]:
        if not self.graph_drive_id:
            raise RuntimeError("MICROSOFT_GRAPH_DRIVE_ID is not configured")
        headers = self._graph_headers(ctx, graph_token)
        meta = _GRAPH_HTTP.get(f"{GRAPH_BASE}/drives/{self.graph_drive_id}/items/{item_id}", headers=headers)
        meta.raise_for_status()
        item = meta.json()
        content = _GRAPH_HTTP.get(f"{GRAPH_BASE}/drives/{self.graph_drive_id}/items/{item_id}/content", headers=headers)
        content.raise_for_status()
        return {"name": item.get("name") or item_id, "bytes": content.content, "graph_item_id": item_id, "graph_drive_id": self.graph_drive_id}

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def ping_db(self) -> None:
        """Probe the database; raises if unreachable. Used by /readyz."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


store = AzureStore()
