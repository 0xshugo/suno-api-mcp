import base64
import json
import os
import threading
import time
import uuid
from typing import Any, cast

import requests  # type: ignore[import-untyped]

_suno_cookie = os.getenv("SUNO_COOKIE")
if not _suno_cookie:
    raise ValueError("SUNO_COOKIE environment variable is required")
SUNO_COOKIE: str = _suno_cookie


class SunoV2Client:
    CLERK_BASE = "https://clerk.suno.com"
    STUDIO_BASE = "https://studio-api.prod.suno.com"
    CLERK_JS_VERSION = "5.56.0"

    def __init__(self) -> None:
        self.cookie = SUNO_COOKIE
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                ),
                "Referer": "https://suno.com/",
                "Origin": "https://suno.com",
            }
        )

        self.device_id = str(uuid.uuid4())
        self.access_token = ""
        self._stop_event = threading.Event()

        self.refresh_token = self._extract_refresh_token(self.cookie)
        self.session_id = self._extract_session_id_from_cookie(self.cookie) or self._fetch_session_id()

        self._refresh_access_token()

        self._keep_alive_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
        self._keep_alive_thread.start()

    def _extract_refresh_token(self, cookie: str) -> str:
        cookie = cookie.strip()
        if "=" not in cookie:
            return cookie

        parts = [part.strip() for part in cookie.split(";")]
        for part in parts:
            if part.startswith("__client="):
                return part.split("=", 1)[1]
        return cookie

    def _extract_session_id_from_cookie(self, cookie: str) -> str:
        # Best-effort extraction if the cookie string already includes a session identifier
        markers = ["sess_", "session_"]
        for marker in markers:
            idx = cookie.find(marker)
            if idx != -1:
                end = idx
                allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                while end < len(cookie) and cookie[end] in allowed:
                    end += 1
                return cookie[idx:end]
        return ""

    def _clerk_url(self, path: str) -> str:
        return (
            f"{self.CLERK_BASE}{path}"
            f"?_is_native=true&_clerk_js_version={self.CLERK_JS_VERSION}"
        )

    def _fetch_session_id(self) -> str:
        response = self.session.get(
            self._clerk_url("/v1/client"),
            headers={
                "Authorization": self.refresh_token,
                "Cookie": f"__client={self.refresh_token}",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = cast(dict[str, Any], response.json())
        session_id = cast(
            str | None,
            data.get("response", {}).get("last_active_session_id")
            or data.get("last_active_session_id"),
        )
        if not session_id:
            raise RuntimeError("Unable to resolve last_active_session_id from Clerk API response")
        return session_id

    def _refresh_access_token(self) -> None:
        response = self.session.post(
            self._clerk_url(f"/v1/client/sessions/{self.session_id}/tokens"),
            headers={
                "Authorization": self.refresh_token,
                "Cookie": f"__client={self.refresh_token}",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = cast(dict[str, Any], response.json())
        token = cast(str | None, data.get("jwt"))
        if not token:
            raise RuntimeError(f"No jwt found in Clerk token response: {data}")
        self.access_token = token

    def _keep_alive_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._refresh_access_token()
            except Exception as exc:
                print(f"[WARN] Suno token refresh failed: {exc}")
            self._stop_event.wait(40)

    def _get_auth_headers(self) -> dict[str, str]:
        timestamp_json = json.dumps({"timestamp": int(time.time() * 1000)})
        encoded = base64.b64encode(timestamp_json.encode()).decode()
        browser_token = json.dumps({"token": encoded})
        return {
            "Authorization": f"Bearer {self.access_token}",
            "device-id": self.device_id,
            "browser-token": browser_token,
            "Content-Type": "application/json",
        }

    def generate(self, prompt: str, tags: str, title: str, make_instrumental: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": None,
            "generation_type": "TEXT",
            "title": title,
            "tags": tags,
            "negative_tags": "",
            "artist_clip_id": None,
            "artist_end_s": None,
            "artist_start_s": None,
            "continue_at": None,
            "continue_clip_id": None,
            "continued_aligned_prompt": None,
            "cover_clip_id": None,
            "cover_end_s": None,
            "cover_start_s": None,
            "make_instrumental": make_instrumental,
            "metadata": {
                "web_client_pathname": "/create",
                "is_max_mode": False,
                "is_mumble": False,
                "create_mode": "simple",
            },
            "mv": "chirp-v4",
            "override_fields": [],
            "persona_id": None,
            "prompt": prompt,
            "transaction_uuid": str(uuid.uuid4()),
            "user_uploaded_images_b64": None,
        }

        response = self.session.post(
            f"{self.STUDIO_BASE}/api/generate/v2-web/",
            headers=self._get_auth_headers(),
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def close(self) -> None:
        self._stop_event.set()
        self.session.close()


if __name__ == "__main__":
    client = SunoV2Client()
    print("SunoV2Client initialized. Access token keep-alive started.")
    client.close()
