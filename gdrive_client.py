"""Google Drive API client using OAuth2 refresh token authentication.

Uses httpx for HTTP. No external auth libraries required.
"""

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("gdrive-client")

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GDriveClient:
    """Minimal Google Drive client with OAuth2 refresh token auth."""

    def __init__(self):
        self._client_id = os.environ.get("GDRIVE_CLIENT_ID", "")
        self._client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "")
        self._refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "")
        self._access_token: str | None = None
        self._token_expiry: float = 0

    async def authenticate(self) -> str:
        """Obtain (or return cached) access token via refresh token."""
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token

        if not all([self._client_id, self._client_secret, self._refresh_token]):
            raise RuntimeError(
                "GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, and GDRIVE_REFRESH_TOKEN must all be set"
            )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token_data = resp.json()

        if "access_token" not in token_data:
            raise RuntimeError(f"Auth failed: {token_data}")

        self._access_token = token_data["access_token"]
        self._token_expiry = time.time() + token_data.get("expires_in", 3600)
        logger.info("Google Drive authenticated via OAuth2 refresh token")
        return self._access_token

    async def _headers(self) -> dict[str, str]:
        token = await self.authenticate()
        return {"Authorization": f"Bearer {token}"}

    async def get_or_create_folder(self, folder_name: str, parent_id: str) -> str:
        """Find existing folder by name under parent, or create it. Returns folder ID."""
        headers = await self._headers()
        query = (
            f"name='{folder_name}' "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            f"and trashed=false"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{DRIVE_API}/files",
                params={"q": query, "fields": "files(id,name)"},
                headers=headers,
            )
            resp.raise_for_status()
            files = resp.json().get("files", [])

            if files:
                logger.info("Found existing folder '%s' (%s)", folder_name, files[0]["id"])
                return files[0]["id"]

            # Create folder
            resp = await client.post(
                f"{DRIVE_API}/files",
                json={
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                headers={**headers, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            folder_id = resp.json()["id"]
            logger.info("Created folder '%s' (%s)", folder_name, folder_id)
            return folder_id

    async def upload_file(self, file_path: str | Path, folder_id: str) -> dict:
        """Upload a file to Google Drive via multipart upload. Returns API response dict."""
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        headers = await self._headers()
        file_content = file_path.read_bytes()

        metadata = json.dumps(
            {
                "name": file_path.name,
                "parents": [folder_id],
            }
        ).encode()

        boundary = "-------314159265358979323846"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n").encode()
        body += metadata
        body += (f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body += file_content
        body += f"\r\n--{boundary}--".encode()

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{DRIVE_UPLOAD_API}/files?uploadType=multipart",
                content=body,
                headers={
                    **headers,
                    "Content-Type": f'multipart/related; boundary="{boundary}"',
                },
            )
            resp.raise_for_status()
            result = resp.json()

        if "id" not in result:
            raise RuntimeError(f"Upload failed: {result}")

        logger.info("Uploaded %s -> %s (id=%s)", file_path.name, folder_id, result["id"])
        return result

    async def list_files(self, folder_id: str) -> list[dict]:
        """List files in a folder."""
        headers = await self._headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{DRIVE_API}/files",
                params={
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "fields": "files(id,name,size,createdTime,mimeType)",
                },
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("files", [])
