"""Azure-backed persistence, auth, storage, and Graph integration for Strata.

The module is intentionally import-safe when Azure dependencies are not yet
installed. Azure mode only turns on when the required environment variables are
present; otherwise the app keeps using the local manifest paths in ``sorter.py``.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx


DEFAULT_BUCKETS = ["Finance", "Tax", "Legal"]
DEFAULT_CONTAINER = "firm-documents"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


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
        self.client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
        self.api_audience = os.environ.get("AZURE_API_AUDIENCE", self.client_id).strip()
        self.api_scope = os.environ.get("AZURE_API_SCOPE", f"api://{self.client_id}/access_as_user").strip()
        self.redirect_uri = os.environ.get("AZURE_REDIRECT_URI", "").strip()
        self.postgres_dsn = os.environ.get("AZURE_POSTGRES_DSN", "").strip()
        self.storage_container = os.environ.get("AZURE_STORAGE_CONTAINER", DEFAULT_CONTAINER).strip()
        self.storage_connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        self.storage_account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL", "").strip()
        self.graph_site_id = os.environ.get("MICROSOFT_GRAPH_SITE_ID", "").strip()
        self.graph_drive_id = os.environ.get("MICROSOFT_GRAPH_DRIVE_ID", "").strip()
        self.graph_root_folder = os.environ.get("MICROSOFT_GRAPH_ROOT_FOLDER", "Strata").strip().strip("/")
        self.graph_client_id = os.environ.get("MICROSOFT_GRAPH_CLIENT_ID", self.client_id).strip()
        self.graph_client_secret = os.environ.get("MICROSOFT_GRAPH_CLIENT_SECRET", "").strip()
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0
        self._graph_app_token: tuple[str, float] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.postgres_dsn)

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

    def _connect(self):
        self._check_enabled()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] to use Azure PostgreSQL") from exc
        return psycopg.connect(self.postgres_dsn, row_factory=dict_row)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return [dict(row) for row in cur.fetchall()]

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def _jwks_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    def _issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def _jwks_payload(self) -> dict[str, Any]:
        now = time.time()
        if self._jwks and now - self._jwks_at < 3600:
            return self._jwks
        with httpx.Client(timeout=20) as client:
            r = client.get(self._jwks_url())
            r.raise_for_status()
            self._jwks = r.json()
            self._jwks_at = now
            return self._jwks

    def _decode_token(self, token: str) -> dict[str, Any]:
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as exc:
            raise RuntimeError("Install PyJWT[crypto] to validate Entra ID tokens") from exc

        jwk_client = PyJWKClient(self._jwks_url())
        signing_key = jwk_client.get_signing_key_from_jwt(token)
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
        profile = self._one(
            "select id, firm_id, role, email, display_name from profiles where id = %s limit 1",
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
        existing = self._one("select id, firm_id, role, email, display_name from profiles where id = %s limit 1", (user_id,))
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
            "insert into firms (name) values (%s) returning id, name",
            ((firm_name or "My Firm").strip() or "My Firm",),
        )
        assert firm is not None
        self._execute(
            """
            insert into profiles (id, firm_id, email, display_name, role)
            values (%s, %s, %s, %s, 'owner')
            """,
            (user_id, firm["id"], email, name),
        )
        return AuthContext(user_id=user_id, email=email, firm_id=str(firm["id"]), role="owner", name=name, access_token=token)

    def ensure_client(self, firm_id: str, name: str) -> dict[str, Any] | None:
        if not name or name == "Unassigned":
            return None
        row = self._one("select * from clients where firm_id = %s and name = %s limit 1", (firm_id, name))
        if row:
            return row
        return self._one(
            "insert into clients (firm_id, name) values (%s, %s) on conflict (firm_id, name) do update set name = excluded.name returning *",
            (firm_id, name),
        )

    def ensure_bucket(self, firm_id: str, client_id: str | None, name: str) -> dict[str, Any] | None:
        if not client_id or not name or name == "Unfiled":
            return None
        row = self._one(
            "select * from buckets where firm_id = %s and client_id = %s and name = %s limit 1",
            (firm_id, client_id, name),
        )
        if row:
            return row
        return self._one(
            """
            insert into buckets (firm_id, client_id, name)
            values (%s, %s, %s)
            on conflict (firm_id, client_id, name) do update set name = excluded.name
            returning *
            """,
            (firm_id, client_id, name),
        )

    def client_buckets(self, firm_id: str, client: str) -> list[str]:
        if not client or client == "Unassigned":
            return list(DEFAULT_BUCKETS)
        rows = self._query(
            """
            select b.name
            from buckets b
            join clients c on c.id = b.client_id
            where b.firm_id = %s and c.name = %s
            order by b.name asc
            """,
            (firm_id, client),
        )
        names = [r["name"] for r in rows]
        return names or list(DEFAULT_BUCKETS)

    def load_manifest(self, firm_id: str) -> dict[str, Any]:
        docs = self._query("select * from documents where firm_id = %s order by applied_at desc", (firm_id,))
        echoes = self._query("select * from document_echoes where firm_id = %s order by uploaded_at desc", (firm_id,))
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for echo in echoes:
            by_doc.setdefault(str(echo["document_id"]), []).append({
                "original": echo.get("original"),
                "uploaded_at": _iso(echo.get("uploaded_at")),
                "source": echo.get("source") or "deposit",
            })
        clients = self._query("select id, name from clients where firm_id = %s order by name asc", (firm_id,))
        buckets = self._query("select client_id, name from buckets where firm_id = %s order by name asc", (firm_id,))
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

    def file_url(self, storage_path: str) -> str | None:
        if not storage_path:
            return None
        return f"/sorter/blob/{quote(storage_path, safe='')}"

    def _remote_name_available(self, firm_id: str, final_name: str) -> bool:
        row = self._one("select id from documents where firm_id = %s and final_name = %s limit 1", (firm_id, final_name))
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
        final_name = self.resolve_remote_name(ctx.firm_id, stem, ext)
        doc_id = str(uuid.uuid4())
        storage_path = f"{ctx.firm_id}/{doc_id}/{final_name}"
        data = src.read_bytes()
        content_type = mimetypes.guess_type(final_name)[0] or "application/octet-stream"
        blob = self.blob_client(storage_path)
        blob.upload_blob(data, overwrite=False, content_type=content_type)
        graph_ref = self.export_file_to_graph(ctx, src, final_name, client_name, bucket_name)
        client_row = self.ensure_client(ctx.firm_id, client_name)
        bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, bucket_name)
        doc = self._one(
            """
            insert into documents (
              id, firm_id, hash, original, final_name, client_id, bucket_id,
              client_name, bucket_name, gemma_bucket, gemma_suggested,
              storage_bucket, storage_path, graph_drive_id, graph_item_id, size, created_by
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning *
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
        return self._one("select * from documents where firm_id = %s and hash = %s limit 1", (firm_id, file_hash))

    def add_echo(self, ctx: AuthContext, document_id: str, original: str) -> None:
        self._execute(
            """
            insert into document_echoes (firm_id, document_id, original, source, created_by)
            values (%s, %s, %s, 'deposit', %s)
            """,
            (ctx.firm_id, document_id, original, ctx.user_id),
        )

    def save_financial_extraction(self, ctx: AuthContext, document_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        artifact_path = f"{ctx.firm_id}/{document_id}/financial_extract.json"
        self.write_json_blob(artifact_path, artifact)
        summary = artifact.get("summary") or {}
        row = self._one(
            """
            insert into financial_extractions (
              firm_id, document_id, status, artifact_bucket, artifact_path,
              row_count, pending_rows, approved_rows, created_by, updated_at
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (firm_id, document_id) do update set
              status = excluded.status,
              artifact_path = excluded.artifact_path,
              row_count = excluded.row_count,
              pending_rows = excluded.pending_rows,
              approved_rows = excluded.approved_rows,
              updated_at = now()
            returning *
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
            "select * from financial_extractions where firm_id = %s and document_id = %s limit 1",
            (ctx.firm_id, document_id),
        )
        if not row:
            return None
        return self.read_json_blob(row.get("artifact_path") or "")

    def list_financial_extractions(self, ctx: AuthContext) -> list[dict[str, Any]]:
        rows = self._query(
            """
            select fe.*, d.final_name, d.client_name, d.bucket_name
            from financial_extractions fe
            join documents d on d.id = fe.document_id
            where fe.firm_id = %s
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

    def update_document_bucket(self, ctx: AuthContext, final_name: str, bucket_name: str) -> dict[str, Any] | None:
        doc = self._one("select * from documents where firm_id = %s and final_name = %s limit 1", (ctx.firm_id, final_name))
        if not doc:
            return None
        client_row = self.ensure_client(ctx.firm_id, doc.get("client_name") or "Unassigned")
        bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, bucket_name)
        return self._one(
            """
            update documents
            set bucket_id = %s, bucket_name = %s
            where firm_id = %s and final_name = %s
            returning *
            """,
            (bucket_row["id"] if bucket_row else None, bucket_name, ctx.firm_id, final_name),
        )

    def update_document_client(self, ctx: AuthContext, final_name: str, client_name: str) -> dict[str, Any] | None:
        doc = self._one("select * from documents where firm_id = %s and final_name = %s limit 1", (ctx.firm_id, final_name))
        if not doc:
            return None
        client_row = self.ensure_client(ctx.firm_id, client_name)
        bucket_row = self.ensure_bucket(ctx.firm_id, str(client_row["id"]) if client_row else None, doc.get("bucket_name") or "Unfiled")
        return self._one(
            """
            update documents
            set client_id = %s, bucket_id = %s, client_name = %s
            where firm_id = %s and final_name = %s
            returning *
            """,
            (client_row["id"] if client_row else None, bucket_row["id"] if bucket_row else None, client_name, ctx.firm_id, final_name),
        )

    def rename_document(self, ctx: AuthContext, final_name: str, new_final_name: str) -> dict[str, Any] | None:
        doc = self._one("select * from documents where firm_id = %s and final_name = %s limit 1", (ctx.firm_id, final_name))
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
            update documents
            set final_name = %s, storage_path = coalesce(nullif(%s, ''), storage_path)
            where firm_id = %s and final_name = %s
            returning *
            """,
            (new_final_name, new_path, ctx.firm_id, final_name),
        )

    def remove_client(self, ctx: AuthContext, name: str) -> None:
        self._execute(
            "update documents set client_id = null, client_name = 'Unassigned' where firm_id = %s and client_name = %s",
            (ctx.firm_id, name),
        )
        self._execute("delete from clients where firm_id = %s and name = %s", (ctx.firm_id, name))

    def remove_bucket(self, ctx: AuthContext, client: str, bucket: str) -> bool:
        in_use = self._one(
            "select id from documents where firm_id = %s and client_name = %s and bucket_name = %s limit 1",
            (ctx.firm_id, client, bucket),
        )
        if in_use:
            return False
        client_row = self._one("select id from clients where firm_id = %s and name = %s limit 1", (ctx.firm_id, client))
        if client_row:
            self._execute(
                "delete from buckets where firm_id = %s and client_id = %s and name = %s",
                (ctx.firm_id, client_row["id"], bucket),
            )
        return True

    def log_event(self, ctx: AuthContext, event: dict[str, Any]) -> None:
        self._execute(
            """
            insert into analyst_events (firm_id, user_id, event, file, hash, client, payload, at)
            values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                ctx.firm_id,
                ctx.user_id,
                event.get("event") or "unknown",
                event.get("file") or event.get("to_name") or event.get("from_name"),
                event.get("hash"),
                event.get("client") or event.get("to_client") or event.get("from_client"),
                json.dumps(event),
                event.get("at") or datetime.now(timezone.utc),
            ),
        )

    def analyst_events(self, firm_id: str) -> list[dict[str, Any]]:
        rows = self._query("select * from analyst_events where firm_id = %s order by at asc", (firm_id,))
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            payload.setdefault("event", row.get("event"))
            payload.setdefault("at", _iso(row.get("at")))
            payload.setdefault("file", row.get("file"))
            payload.setdefault("hash", row.get("hash"))
            payload.setdefault("client", row.get("client"))
            out.append(payload)
        return out

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
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": self.graph_client_id,
                    "client_secret": self.graph_client_secret,
                    "grant_type": "client_credentials",
                    "scope": "https://graph.microsoft.com/.default",
                },
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
    ) -> dict[str, str | None]:
        if not self.graph_drive_id:
            return {"drive_id": None, "item_id": None}
        graph_path = self._graph_path(ctx, client_name, bucket_name, final_name)
        url = f"{GRAPH_BASE}/drives/{self.graph_drive_id}/root:/{graph_path}:/content"
        headers = self._graph_headers(ctx, graph_token)
        headers["Content-Type"] = mimetypes.guess_type(final_name)[0] or "application/octet-stream"
        with httpx.Client(timeout=120) as client:
            r = client.put(url, headers=headers, content=src.read_bytes())
            r.raise_for_status()
            item = r.json()
        return {"drive_id": item.get("parentReference", {}).get("driveId") or self.graph_drive_id, "item_id": item.get("id")}

    def graph_import_file(self, ctx: AuthContext, item_id: str, graph_token: str | None = None) -> dict[str, Any]:
        if not self.graph_drive_id:
            raise RuntimeError("MICROSOFT_GRAPH_DRIVE_ID is not configured")
        headers = self._graph_headers(ctx, graph_token)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            meta = client.get(f"{GRAPH_BASE}/drives/{self.graph_drive_id}/items/{item_id}", headers=headers)
            meta.raise_for_status()
            item = meta.json()
            content = client.get(f"{GRAPH_BASE}/drives/{self.graph_drive_id}/items/{item_id}/content", headers=headers)
            content.raise_for_status()
        return {"name": item.get("name") or item_id, "bytes": content.content, "graph_item_id": item_id, "graph_drive_id": self.graph_drive_id}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


store = AzureStore()
