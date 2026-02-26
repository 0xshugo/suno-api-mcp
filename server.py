"""Suno MCP Server - Liquid DnB Generator with WAV download support.

Direct Suno API client (no third-party libraries).
Uses new v2-web API endpoint (Feb 2026).
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

from browser_session import BrowserSession
from gdrive_client import GDriveClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("aoi-suno-mcp")

# Refresh token (__client cookie) - long-lived, used to get access tokens
SUNO_REFRESH_TOKEN = os.environ.get("SUNO_REFRESH_TOKEN", "")
# Legacy: direct access token (short-lived, ~1 hour)
SUNO_SESSION_TOKEN = os.environ.get("SUNO_SESSION_TOKEN", "")
DEVICE_ID = os.environ.get("SUNO_DEVICE_ID", str(uuid.uuid4()))
MUSIC_BASE = Path(os.environ.get("MUSIC_BASE", "/data/music"))
GDRIVE_MUSIC_FOLDER_ID = os.environ.get("GDRIVE_MUSIC_FOLDER_ID", "")

AUTH_NOTIFY_WEBHOOK_URL = os.environ.get("AUTH_NOTIFY_WEBHOOK_URL", "")
AUTH_NOTIFY_SLACK_WEBHOOK_URL = os.environ.get("AUTH_NOTIFY_SLACK_WEBHOOK_URL", "")
AUTH_NOTIFY_DISCORD_WEBHOOK_URL = os.environ.get("AUTH_NOTIFY_DISCORD_WEBHOOK_URL", "")
TOOL_RESPONSE_FORMAT = os.environ.get("TOOL_RESPONSE_FORMAT", "text").strip().lower()

SUNO_API_BASE = "https://studio-api.prod.suno.com"
CLERK_BASE = "https://clerk.suno.com"
CLERK_JS_VERSION = "5.56.0"
DEFAULT_MODEL = "chirp-crow"

# Token management
_access_token: str = ""
_token_expires: float = 0.0
_session_id: str = ""
TOKEN_REFRESH_MARGIN = 30  # Refresh when < 30s remaining
MAX_REFRESH_FAILURES = 3

# Auth health state
_auth_state: Literal["ok", "degraded", "reauth_required"] = "ok"
_last_auth_error: str = ""
_last_refresh_at: float | None = None
_consecutive_refresh_failures = 0
_reauth_required_since: float | None = None

# Named output targets
OUTPUT_TARGETS: dict[str, Path] = {
    "ch1": MUSIC_BASE / "ch1",
    "ch2": MUSIC_BASE / "ch2",
    "library": MUSIC_BASE / "library",
    "gdrive": MUSIC_BASE / "library",
}
DEFAULT_OUTPUT = "library"


def _ensure_output_dirs() -> None:
    """Create output directories if they don't exist."""
    for d in set(OUTPUT_TARGETS.values()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Skip if we can't create (e.g., CI environment)
            pass


_gdrive: GDriveClient | None = None


def _get_gdrive() -> GDriveClient:
    global _gdrive
    if _gdrive is None:
        _gdrive = GDriveClient()
    return _gdrive


_browser_session: BrowserSession | None = None


async def _get_browser_session() -> BrowserSession:
    """Lazy-initialize and return the shared BrowserSession."""
    global _browser_session
    if _browser_session is None:
        _browser_session = BrowserSession()
    await _browser_session.initialize()
    return _browser_session


# DnB-flavoured fallback title words
DNB_ADJECTIVES = [
    "Liquid",
    "Deep",
    "Velvet",
    "Cosmic",
    "Astral",
    "Crystal",
    "Neon",
    "Ethereal",
    "Midnight",
    "Solar",
    "Lunar",
    "Silent",
]
DNB_NOUNS = [
    "Flow",
    "Ether",
    "Pulse",
    "Drift",
    "Wave",
    "Haze",
    "Echo",
    "Vapor",
    "Horizon",
    "Current",
    "Storm",
    "Rain",
]

MCP_PORT = int(os.environ.get("MCP_PORT", "8888"))

mcp = FastMCP("aoi-suno-mcp", host="0.0.0.0", port=MCP_PORT)


def _generate_browser_token() -> str:
    """Generate browser-token header value."""
    timestamp_data = json.dumps({"timestamp": int(time.time() * 1000)})
    encoded = base64.b64encode(timestamp_data.encode()).decode()
    return json.dumps({"token": encoded})


def _clerk_headers() -> dict:
    """Headers for Clerk API calls."""
    return {
        "Authorization": SUNO_REFRESH_TOKEN,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }


async def _resolve_session() -> str:
    """Get Clerk session ID from refresh token."""
    global _session_id
    if _session_id:
        return _session_id

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CLERK_BASE}/v1/client",
            params={"_is_native": "true", "_clerk_js_version": CLERK_JS_VERSION},
            headers=_clerk_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        client_obj = data.get("response", data)
        _session_id = client_obj.get("last_active_session_id", "")
        if not _session_id:
            sessions = client_obj.get("sessions", [])
            if sessions:
                _session_id = sessions[0].get("id", "")

        if not _session_id:
            raise RuntimeError("Could not resolve Clerk session ID. Refresh token may be invalid.")

        logger.info("Resolved Clerk session: %s", _session_id[:20] + "...")
        return _session_id


async def _refresh_access_token() -> str:
    """Get fresh access token from Clerk."""
    global _access_token, _token_expires
    global _auth_state, _last_auth_error, _last_refresh_at
    global _consecutive_refresh_failures, _reauth_required_since

    session_id = await _resolve_session()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{CLERK_BASE}/v1/client/sessions/{session_id}/tokens",
                params={"_is_native": "true", "_clerk_js_version": CLERK_JS_VERSION},
                headers=_clerk_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            _access_token = data.get("jwt", "")
            if not _access_token:
                raise RuntimeError(f"Token refresh failed: {data}")

            # Clerk tokens last ~60s, refresh at 30s remaining
            _token_expires = time.monotonic() + 55
            _last_refresh_at = time.time()
            _consecutive_refresh_failures = 0
            _last_auth_error = ""
            _auth_state = "ok"
            _reauth_required_since = None
            logger.info("Access token refreshed, expires in ~55s")
            return _access_token
    except Exception as e:
        _consecutive_refresh_failures += 1
        _last_auth_error = str(e)
        _auth_state = "degraded"
        logger.warning(
            "Token refresh failed (%s/%s): %s",
            _consecutive_refresh_failures,
            MAX_REFRESH_FAILURES,
            e,
        )

        if _consecutive_refresh_failures >= MAX_REFRESH_FAILURES:
            _auth_state = "reauth_required"
            if _reauth_required_since is None:
                _reauth_required_since = time.time()
            await _send_auth_notification("clerk_refresh_failed", str(e))
            raise RuntimeError(
                "Authentication refresh failed repeatedly. "
                "Re-authentication required: update SUNO_REFRESH_TOKEN."
            ) from e
        raise


async def _ensure_token() -> str:
    """Ensure we have a valid access token, refreshing if needed."""
    global _access_token, _token_expires

    # If using refresh token flow
    if SUNO_REFRESH_TOKEN:
        if _auth_state == "reauth_required":
            raise RuntimeError(
                "Authentication is in reauth_required state. "
                "Update SUNO_REFRESH_TOKEN and restart the service."
            )
        if not _access_token or time.monotonic() > (_token_expires - TOKEN_REFRESH_MARGIN):
            await _refresh_access_token()
        return _access_token

    # Legacy: use direct session token
    if SUNO_SESSION_TOKEN:
        return SUNO_SESSION_TOKEN

    raise RuntimeError("Neither SUNO_REFRESH_TOKEN nor SUNO_SESSION_TOKEN is set")


async def _get_auth_headers() -> dict:
    """Return headers for Suno API requests (async, handles token refresh)."""
    token = await _ensure_token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "browser-token": _generate_browser_token(),
        "device-id": DEVICE_ID,
        "referring-pathname": "/home",
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }


def _tool_response(message: str, status: str = "ok", **meta: object) -> str:
    """Return tool output in text (default) or JSON format for agent clients."""
    payload: dict[str, object] = {"status": status, "message": message}
    if meta:
        payload["meta"] = meta

    if TOOL_RESPONSE_FORMAT == "json":
        return json.dumps(payload, ensure_ascii=False)

    if not meta:
        return message

    meta_lines = [f"{k}: {v}" for k, v in meta.items()]
    return message + "\n" + "\n".join(meta_lines)


async def _send_auth_notification(reason: str, detail: str = "") -> None:
    """Send optional auth failure notification to generic/slack/discord webhooks."""
    targets = [
        u
        for u in [
            AUTH_NOTIFY_WEBHOOK_URL,
            AUTH_NOTIFY_SLACK_WEBHOOK_URL,
            AUTH_NOTIFY_DISCORD_WEBHOOK_URL,
        ]
        if u
    ]
    if not targets:
        return

    message = (
        "Suno MCP auth state changed to reauth_required. "
        f"reason={reason}. Rotate SUNO_REFRESH_TOKEN and restart service."
    )
    if detail:
        message += f" detail={detail[:300]}"

    body = {"text": message, "content": message}

    async with httpx.AsyncClient() as client:
        for url in targets:
            try:
                await client.post(url, json=body, timeout=10)
            except Exception as e:
                logger.warning("Auth notification failed for %s: %s", url, e)


def _generate_fallback_title() -> str:
    return f"{random.choice(DNB_ADJECTIVES)} {random.choice(DNB_NOUNS)}"


def _sanitize_filename(name: str) -> str:
    """Remove or replace characters unsafe for filenames."""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip()[:120]


def _try_wav_url(mp3_url: str) -> str | None:
    """Derive a candidate WAV URL from an MP3 URL (Pro account hack)."""
    if not mp3_url:
        return None
    if ".mp3" in mp3_url:
        return mp3_url.replace(".mp3", ".wav")
    return mp3_url + ("&" if "?" in mp3_url else "?") + "format=wav"


def _resolve_output_dir(output_dir: str) -> tuple[Path, bool]:
    """Resolve named output target to (Path, upload_to_gdrive)."""
    target = output_dir.strip().lower() if output_dir else DEFAULT_OUTPUT
    if target in OUTPUT_TARGETS:
        return OUTPUT_TARGETS[target], target == "gdrive"
    raise ValueError(
        f"Unknown output_dir '{output_dir}'. Valid targets: {', '.join(OUTPUT_TARGETS.keys())}"
    )


async def _download_wav(client: httpx.AsyncClient, track: dict, dest_dir: Path) -> Path:
    """Download track as WAV, trying multiple strategies."""
    track_id = track.get("id", "unknown")
    title = track.get("title") or _generate_fallback_title()
    safe_title = _sanitize_filename(title)
    dest = dest_dir / f"{safe_title}_{track_id}.wav"

    audio_url = track.get("audio_url", "")

    # Strategy 1: Direct WAV URL field
    wav_url = track.get("audio_url_wav") or track.get("wav_url")

    # Strategy 2: Derive WAV URL from MP3 URL (Pro account)
    if not wav_url:
        wav_url = _try_wav_url(audio_url)

    if wav_url:
        try:
            resp = await client.head(wav_url, timeout=15, follow_redirects=True)
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "audio" in content_type:
                async with client.stream(
                    "GET", wav_url, timeout=120, follow_redirects=True
                ) as stream:
                    stream.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in stream.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                with open(dest, "rb") as f:
                    header = f.read(4)
                if header == b"RIFF":
                    return dest
        except (httpx.HTTPError, OSError):
            pass

    # Strategy 3: Download MP3 and convert with ffmpeg
    if audio_url:
        mp3_tmp = dest_dir / f"{safe_title}_{track_id}.mp3"
        async with client.stream("GET", audio_url, timeout=120, follow_redirects=True) as stream:
            stream.raise_for_status()
            with open(mp3_tmp, "wb") as f:
                async for chunk in stream.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(mp3_tmp),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                str(dest),
            ],
            capture_output=True,
            text=True,
        )
        mp3_tmp.unlink(missing_ok=True)

        if result.returncode == 0 and dest.exists():
            return dest
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[:500]}")

    raise RuntimeError(f"No audio URL found for track {track_id}")


async def _poll_generation(
    client: httpx.AsyncClient, clip_ids: list[str], timeout: int = 300
) -> list[dict]:
    """Poll for generation completion."""
    start_time = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout:
            raise TimeoutError(f"Generation timed out after {timeout}s")

        ids_param = ",".join(clip_ids)
        headers = await _get_auth_headers()
        resp = await client.get(
            f"{SUNO_API_BASE}/api/feed/v2?ids={ids_param}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        clips: list[dict] = data.get("clips", [])
        all_complete = all(
            clip.get("status") in ("complete", "error", "streaming") for clip in clips
        )

        if all_complete:
            return clips

        logger.info("Generation in progress... (%.0fs)", elapsed)
        await asyncio.sleep(5)


@mcp.tool()
async def generate_liquid_dnb(
    tags: str = "Liquid Drum and Bass, atmospheric, deep, rolling bassline",
    title: str = "",
    prompt: str = "",
    instrumental: bool = True,
    output_dir: Literal["ch1", "ch2", "library", "gdrive"] = "library",
    model: Literal["chirp-crow", "chirp-v4", "chirp-v3-5"] = "chirp-crow",
) -> str:
    """Generate a track via Suno and save as WAV.

    Args:
        tags: Style/genre description (e.g. "Liquid Drum and Bass, atmospheric, 174bpm").
              This controls the musical style. Be as specific as possible.
        title: Optional track title. Auto-generated if empty.
        prompt: Lyrics text. Leave empty for instrumental tracks.
        instrumental: If True, generate without vocals (default: True).
        output_dir: Output destination. "ch1" = broadcasting channel 1,
                    "ch2" = broadcasting channel 2, "library" = general library (default),
                    "gdrive" = save to library + upload to Google Drive.
        model: Suno model version. Options: "chirp-crow" (v5, default), "chirp-v4", "chirp-v3-5".
    """
    _ensure_output_dirs()
    dest_dir, upload_gdrive = _resolve_output_dir(output_dir)

    if upload_gdrive and not GDRIVE_MUSIC_FOLDER_ID:
        return "Error: GDRIVE_MUSIC_FOLDER_ID is not configured."

    if not title:
        title = _generate_fallback_title()

    try:
        headers = await _get_auth_headers()

        # Acquire hCaptcha token via browser session
        hcaptcha_token = None
        try:
            browser = await _get_browser_session()
            hcaptcha_token = await browser.get_hcaptcha_token()
        except Exception as e:
            logger.warning("hCaptcha token acquisition failed: %s", e)

        # Build generation payload (v2-web format)
        # Note: All fields must match browser request format exactly
        payload: dict = {
            "token": hcaptcha_token,
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
            "make_instrumental": instrumental,
            "metadata": {
                "web_client_pathname": "/create",
                "is_max_mode": False,
                "is_mumble": False,
                "create_mode": "custom" if prompt else "simple",
            },
            "mv": model,
            "override_fields": [],
            "persona_id": None,
            "prompt": prompt,
            "transaction_uuid": str(uuid.uuid4()),
            "user_uploaded_images_b64": None,
        }

        logger.info(
            "Generating: tags=%s, title=%s, instrumental=%s, model=%s",
            tags,
            title,
            instrumental,
            model,
        )

        async with httpx.AsyncClient() as client:
            # Start generation using v2-web endpoint
            resp = await client.post(
                f"{SUNO_API_BASE}/api/generate/v2-web/",
                headers=headers,
                json=payload,
                timeout=60,
            )

            if resp.status_code == 401:
                global _auth_state, _last_auth_error, _reauth_required_since
                _auth_state = "reauth_required"
                _last_auth_error = "Suno API returned 401 during generation request"
                if _reauth_required_since is None:
                    _reauth_required_since = time.time()
                await _send_auth_notification("suno_generate_401", _last_auth_error)
                return _tool_response(
                    "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                    status="error",
                    code="AUTH_401",
                )
            if resp.status_code == 402:
                return "Error: Insufficient credits."

            # hCaptcha token rejected — fallback to browser-based generation
            if resp.status_code == 422:
                logger.warning(
                    "API returned 422 (likely hCaptcha rejection). "
                    "Falling back to browser-based generation."
                )
                try:
                    browser = await _get_browser_session()
                    bearer_token = await _ensure_token()
                    gen_data = await browser.generate_via_browser(payload, bearer_token)
                    if not gen_data:
                        return (
                            "Error: Generation failed — both direct API (422) and "
                            "browser fallback returned no data."
                        )
                except Exception as fb_err:
                    return f"Error: API returned 422 and browser fallback failed: {fb_err}"
            else:
                resp.raise_for_status()
                gen_data = resp.json()

            clips = gen_data.get("clips", [])
            if not clips:
                return f"Error: No clips returned. Response: {gen_data}"

            clip_ids = [clip["id"] for clip in clips]
            logger.info("Started generation: %s", clip_ids)

            # Poll for completion
            completed_clips = await _poll_generation(client, clip_ids)

            # Process completed tracks
            results = []
            for clip in completed_clips:
                if clip.get("status") == "error":
                    results.append(
                        f"Failed ({clip.get('id')}): {clip.get('error_message', 'Unknown error')}"
                    )
                    continue

                track = {
                    "id": clip["id"],
                    "title": clip.get("title") or title,
                    "audio_url": clip.get("audio_url"),
                }

                try:
                    path = await _download_wav(client, track, dest_dir)
                    audio_url = track.get("audio_url", "")
                    line = f"Saved: {path.name} -> {output_dir}\n  Preview: {audio_url}"

                    # Upload to Google Drive if requested
                    if upload_gdrive:
                        try:
                            gdrive = _get_gdrive()
                            gdrive_resp = await gdrive.upload_file(path, GDRIVE_MUSIC_FOLDER_ID)
                            file_id = gdrive_resp["id"]
                            drive_url = f"https://drive.google.com/file/d/{file_id}/view"
                            line += f"\n  Google Drive: {drive_url}"
                        except Exception as ge:
                            line += f"\n  GDrive upload failed: {ge}"

                    results.append(line)
                except Exception as e:
                    results.append(f"Failed ({track.get('id')}): {e}")

            return "\n".join(results) if results else "Error: No tracks completed"

    except httpx.HTTPStatusError as e:
        logger.exception("API request failed")
        return f"Error: API request failed with status {e.response.status_code}: {e.response.text[:500]}"
    except Exception as e:
        logger.exception("Generation failed")
        return f"Error: {e}"


@mcp.tool()
async def get_credits() -> str:
    """Check remaining Suno credits."""
    try:
        headers = await _get_auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUNO_API_BASE}/api/billing/info/",
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 401:
                global _auth_state, _last_auth_error, _reauth_required_since
                _auth_state = "reauth_required"
                _last_auth_error = "Suno API returned 401 during billing request"
                if _reauth_required_since is None:
                    _reauth_required_since = time.time()
                await _send_auth_notification("suno_billing_401", _last_auth_error)
                return _tool_response(
                    "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                    status="error",
                    code="AUTH_401",
                )

            resp.raise_for_status()
            data = resp.json()

            total = data.get("total_credits_left", "?")
            period = data.get("period", "unknown")
            monthly_usage = data.get("monthly_usage", 0)
            monthly_limit = data.get("monthly_limit", 0)

            return f"Credits: {total} remaining ({period})\nMonthly: {monthly_usage}/{monthly_limit} used"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_auth_status() -> str:
    """Get Suno authentication health status and re-auth guidance."""
    last_refresh = "never"
    if _last_refresh_at:
        age = max(0, int(time.time() - _last_refresh_at))
        last_refresh = f"{age}s ago"

    reauth_since = "n/a"
    if _reauth_required_since:
        age = max(0, int(time.time() - _reauth_required_since))
        reauth_since = f"{age}s ago"

    if TOOL_RESPONSE_FORMAT == "json":
        return _tool_response(
            "Authentication status snapshot.",
            status="ok" if _auth_state == "ok" else "error",
            auth_state=_auth_state,
            last_refresh=last_refresh,
            consecutive_refresh_failures=f"{_consecutive_refresh_failures}/{MAX_REFRESH_FAILURES}",
            reauth_required_since=reauth_since,
            last_auth_error=_last_auth_error or "",
            action=(
                "Rotate SUNO_REFRESH_TOKEN (__client) secret and restart the MCP server."
                if _auth_state == "reauth_required"
                else "none"
            ),
        )

    lines = [
        f"auth_state: {_auth_state}",
        f"last_refresh: {last_refresh}",
        f"consecutive_refresh_failures: {_consecutive_refresh_failures}/{MAX_REFRESH_FAILURES}",
        f"reauth_required_since: {reauth_since}",
    ]
    if _last_auth_error:
        lines.append(f"last_auth_error: {_last_auth_error}")

    if _auth_state == "reauth_required":
        lines.append(
            "action: Rotate SUNO_REFRESH_TOKEN (__client) secret and restart the MCP server."
        )

    return "\n".join(lines)


@mcp.tool()
async def get_cookie_capture_helper() -> str:
    """Return an easy copy/paste helper to capture Suno __client cookie without manual cookie hunting."""
    snippet = """javascript:(async()=>{
try{
  const clientCookie=document.cookie.split('; ').find(v=>v.startsWith('__client='));
  if(!clientCookie){alert('__client cookie not found. Make sure you are logged in on suno.com');return;}
  const refreshToken=clientCookie.split('=')[1];
  const url='https://clerk.suno.com/v1/client?_is_native=true&_clerk_js_version=5.56.0';
  const res=await fetch(url,{headers:{Authorization:refreshToken}});
  const data=await res.json();
  const sessionId=data?.response?.last_active_session_id||'';
  const out=`SUNO_REFRESH_TOKEN=${refreshToken}\nSUNO_SESSION_ID=${sessionId}`;
  await navigator.clipboard.writeText(out);
  alert('Copied SUNO_REFRESH_TOKEN (+ session id) to clipboard.');
}catch(e){alert('Failed: '+e);}
})();"""

    return (
        "Cookie取得を簡単化するヘルパーです。\n\n"
        "1) suno.com にログイン済みブラウザでブックマークを新規作成\n"
        "2) URL欄に下記の `javascript:` から始まる文字列を貼り付け\n"
        "3) suno.com を開いた状態でそのブックマークを実行\n"
        "4) クリップボードへ `SUNO_REFRESH_TOKEN=...` が自動コピーされます\n\n"
        "--- bookmarklet ---\n"
        f"{snippet}\n"
        "--- end ---\n\n"
        "備考: session id も同時にコピーしますが、通常運用では SUNO_REFRESH_TOKEN のみ設定すればOKです。"
    )


@mcp.tool()
async def validate_suno_refresh_token(candidate: str) -> str:
    """Validate a user-provided Suno refresh token and classify expiry/failure causes."""
    token = candidate.strip()
    if not token:
        return _tool_response("Empty token.", status="error", reason="empty_input")

    # Allow input like '__client=xxxx' or 'Cookie: __client=xxxx; ...'
    m = re.search(r"__client=([^;\s]+)", token)
    if m:
        token = m.group(1)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLERK_BASE}/v1/client",
                params={"_is_native": "true", "_clerk_js_version": CLERK_JS_VERSION},
                headers={"Authorization": token},
                timeout=20,
            )

            if resp.status_code == 401:
                return _tool_response(
                    "Invalid or expired token (401). Please re-login on suno.com and recapture __client.",
                    status="error",
                    reason="expired_or_invalid",
                    classification="reauth_required",
                )
            if resp.status_code == 403:
                return _tool_response(
                    "Token rejected (403). Account protection or permission mismatch is likely.",
                    status="error",
                    reason="forbidden",
                    classification="security_policy",
                )
            if resp.status_code == 429:
                return _tool_response(
                    "Rate limited by Clerk (429). Retry later and avoid repeated attempts.",
                    status="error",
                    reason="rate_limited",
                    classification="retry_later",
                )

            resp.raise_for_status()
            data = resp.json()
            client_obj = data.get("response", data)
            session_id = client_obj.get("last_active_session_id") or "(not found)"

            sessions = client_obj.get("sessions", [])
            expires_at = None
            for sess in sessions:
                if sess.get("id") == session_id:
                    expires_at = sess.get("expire_at") or sess.get("expires_at")
                    break

            return _tool_response(
                "Token is valid. Set SUNO_REFRESH_TOKEN in your secret manager and restart MCP server.",
                status="ok",
                classification="valid",
                last_active_session_id=session_id,
                expires_at=expires_at or "unknown",
            )
    except httpx.TimeoutException:
        return _tool_response(
            "Validation timed out. Network or Clerk availability issue.",
            status="error",
            reason="timeout",
            classification="transient",
        )
    except httpx.HTTPStatusError as e:
        return _tool_response(
            f"HTTP error during validation: {e.response.status_code}",
            status="error",
            reason="http_error",
            classification="unknown_http",
        )
    except Exception as e:
        return _tool_response(
            f"Validation error: {e}",
            status="error",
            reason="unexpected",
            classification="unexpected",
        )


@mcp.tool()
async def list_tracks(output_dir: str = "library") -> str:
    """List WAV tracks in a given output directory.

    Args:
        output_dir: Target directory. "ch1", "ch2", "library" (default), or "gdrive".
    """
    if output_dir.strip().lower() == "gdrive":
        if not GDRIVE_MUSIC_FOLDER_ID:
            return "Error: GDRIVE_MUSIC_FOLDER_ID is not configured."
        try:
            gdrive = _get_gdrive()
            gdrive_files = await gdrive.list_files(GDRIVE_MUSIC_FOLDER_ID)
            if not gdrive_files:
                return "No tracks in gdrive."
            lines: list[str] = [f"[gdrive] {len(gdrive_files)} files:"]
            for gf in gdrive_files:
                size_mb = int(gf.get("size", 0)) / (1024 * 1024)
                lines.append(f"  {gf['name']}  ({size_mb:.1f} MB)  id={gf['id']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing gdrive: {e}"

    dest_dir, _ = _resolve_output_dir(output_dir)
    local_files = sorted(dest_dir.glob("*.wav"))
    if not local_files:
        return f"No tracks in {output_dir}."
    lines = [f"[{output_dir}] {len(local_files)} tracks:"]
    for lf in local_files:
        size_mb = lf.stat().st_size / (1024 * 1024)
        lines.append(f"  {lf.name}  ({size_mb:.1f} MB)")
    return "\n".join(lines)


@mcp.tool()
async def delete_track(filename: str, output_dir: str = "library") -> str:
    """Delete a WAV track from an output directory.

    Args:
        filename: Exact filename to delete (e.g. "Midnight Current_25e55801....wav").
        output_dir: Target directory. "ch1", "ch2", "library" (default), or "gdrive".
    """
    dest_dir, _ = _resolve_output_dir(output_dir)
    target = dest_dir / filename
    if target.resolve().parent != dest_dir.resolve():
        return "Error: invalid path."
    if not target.exists():
        return f"Not found: {filename} in {output_dir}."
    target.unlink()
    return f"Deleted: {filename} from {output_dir}."


# ---------------------------------------------------------------------------
# Library / Feed v2 API tools
# ---------------------------------------------------------------------------

FEED_PAGE_SIZE = 20  # Suno returns 20 clips per page


async def _fetch_feed_page(
    client: httpx.AsyncClient,
    page: int = 0,
    ids: str | None = None,
) -> dict:
    """Fetch a single page from /api/feed/v2 with auto token refresh."""
    params: dict[str, str] = {}
    if ids:
        params["ids"] = ids
    else:
        params["page"] = str(page)

    for attempt in range(2):
        headers = await _get_auth_headers()
        resp = await client.get(
            f"{SUNO_API_BASE}/api/feed/v2",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            if attempt == 0:
                # Force token refresh on first 401
                global _access_token, _token_expires
                _access_token = ""
                _token_expires = 0.0
                continue
            raise RuntimeError("AUTH_401")
        resp.raise_for_status()
        if not resp.content:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return {"clips": [], "has_more": False}
        return resp.json()

    return {"clips": [], "has_more": False}


def _format_clip_summary(clip: dict) -> str:
    """One-line summary of a clip."""
    cid = clip.get("id", "?")
    title = clip.get("title", "Untitled")
    status = clip.get("status", "?")
    model = clip.get("model_name", "?")
    created = (clip.get("created_at") or "?")[:19]
    duration = ""
    md = clip.get("metadata")
    if isinstance(md, dict) and md.get("duration"):
        dur = md["duration"]
        mins, secs = divmod(int(dur), 60)
        duration = f" {mins}:{secs:02d}"
    return f"{cid} | {title} | {status} | {model} | {created}{duration}"


def _format_clip_detail(clip: dict) -> str:
    """Multi-line detail of a clip."""
    lines = [
        f"id: {clip.get('id')}",
        f"title: {clip.get('title')}",
        f"status: {clip.get('status')}",
        f"model: {clip.get('model_name')}",
        f"created_at: {clip.get('created_at')}",
        f"audio_url: {clip.get('audio_url')}",
        f"image_url: {clip.get('image_url')}",
        f"is_public: {clip.get('is_public')}",
        f"play_count: {clip.get('play_count')}",
        f"display_tags: {clip.get('display_tags')}",
    ]
    md = clip.get("metadata")
    if isinstance(md, dict):
        lines.append(f"duration: {md.get('duration')}")
        lines.append(f"tags: {md.get('tags', '')}")
        prompt = str(md.get("prompt", ""))
        if prompt:
            lines.append(f"prompt: {prompt[:200]}")
        lines.append(f"make_instrumental: {md.get('make_instrumental')}")
    return "\n".join(lines)


@mcp.tool()
async def get_library(
    page: int = 0,
    fetch_all: bool = False,
    max_pages: int = 50,
) -> str:
    """List clips in your Suno library (all tracks you've ever generated).

    Args:
        page: Page number (0-indexed, 20 clips per page). Ignored if fetch_all=True.
        fetch_all: If True, fetch ALL pages and return complete list (slower).
        max_pages: Safety limit when fetch_all=True (default 50 = up to 1000 clips).
    """
    try:
        async with httpx.AsyncClient() as client:
            if not fetch_all:
                data = await _fetch_feed_page(client, page=page)
                clips = data.get("clips", [])
                has_more = data.get("has_more", False)
                current_page = data.get("current_page", page)

                if not clips:
                    return f"No clips on page {page}."

                lines = [f"Page {current_page} ({len(clips)} clips, has_more={has_more}):"]
                for c in clips:
                    lines.append(f"  {_format_clip_summary(c)}")
                return "\n".join(lines)

            # Fetch all pages
            all_clips: list[dict] = []
            current = 0
            while current < max_pages:
                data = await _fetch_feed_page(client, page=current)
                clips = data.get("clips", [])
                if not clips:
                    break
                all_clips.extend(clips)
                if not data.get("has_more", False):
                    break
                current += 1
                await asyncio.sleep(0.5)  # Rate limit protection

            if not all_clips:
                return "Library is empty."

            lines = [f"Total: {len(all_clips)} clips ({current + 1} pages fetched):"]
            for c in all_clips:
                lines.append(f"  {_format_clip_summary(c)}")
            return "\n".join(lines)

    except RuntimeError as e:
        if "AUTH_401" in str(e):
            return _tool_response(
                "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                status="error",
                code="AUTH_401",
            )
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_clip(clip_id: str) -> str:
    """Get detailed information about a specific Suno clip by its ID.

    Args:
        clip_id: The UUID of the clip (e.g. "eb479212-e649-4ccc-8a06-93e1ff09de0f").
    """
    try:
        async with httpx.AsyncClient() as client:
            data = await _fetch_feed_page(client, ids=clip_id.strip())
            clips = data.get("clips", [])
            if not clips:
                return f"Clip not found: {clip_id}"
            return _format_clip_detail(clips[0])
    except RuntimeError as e:
        if "AUTH_401" in str(e):
            return _tool_response(
                "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                status="error",
                code="AUTH_401",
            )
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def download_clips(
    clip_ids: str = "",
    output_dir: Literal["ch1", "ch2", "library", "gdrive"] = "library",
    page: int = 0,
    download_all: bool = False,
    max_pages: int = 50,
    delay: float = 1.5,
) -> str:
    """Download clips from your Suno library as WAV files.

    Args:
        clip_ids: Comma-separated clip IDs to download. If empty and download_all=False,
                  downloads clips from the specified page.
        output_dir: Output destination. "ch1", "ch2", "library" (default), "gdrive".
        page: Page number to download (0-indexed). Ignored if clip_ids or download_all is set.
        download_all: If True, download ALL clips in your library (use with caution).
        max_pages: Safety limit for download_all (default 50 = up to 1000 clips).
        delay: Seconds to wait between downloads to avoid rate limiting (default 1.5).
    """
    _ensure_output_dirs()
    dest_dir, upload_gdrive = _resolve_output_dir(output_dir)

    if upload_gdrive and not GDRIVE_MUSIC_FOLDER_ID:
        return "Error: GDRIVE_MUSIC_FOLDER_ID is not configured."

    try:
        async with httpx.AsyncClient() as client:
            # Resolve which clips to download
            clips_to_download: list[dict] = []

            if clip_ids:
                # Specific clip IDs
                data = await _fetch_feed_page(client, ids=clip_ids.strip())
                clips_to_download = data.get("clips", [])
            elif download_all:
                # All pages
                current = 0
                while current < max_pages:
                    data = await _fetch_feed_page(client, page=current)
                    clips = data.get("clips", [])
                    if not clips:
                        break
                    clips_to_download.extend(clips)
                    if not data.get("has_more", False):
                        break
                    current += 1
                    await asyncio.sleep(0.5)
            else:
                # Single page
                data = await _fetch_feed_page(client, page=page)
                clips_to_download = data.get("clips", [])

            if not clips_to_download:
                return "No clips to download."

            # Filter to complete clips with audio
            downloadable = [
                c for c in clips_to_download if c.get("status") == "complete" and c.get("audio_url")
            ]

            if not downloadable:
                return f"No downloadable clips found ({len(clips_to_download)} total, none complete with audio)."

            results: list[str] = []
            success_count = 0
            fail_count = 0

            for i, clip in enumerate(downloadable):
                track = {
                    "id": clip["id"],
                    "title": clip.get("title") or "Untitled",
                    "audio_url": clip.get("audio_url"),
                    "audio_url_wav": clip.get("audio_url_wav"),
                    "wav_url": clip.get("wav_url"),
                }

                try:
                    path = await _download_wav(client, track, dest_dir)
                    line = f"OK: {path.name}"

                    if upload_gdrive:
                        try:
                            gdrive = _get_gdrive()
                            gdrive_resp = await gdrive.upload_file(path, GDRIVE_MUSIC_FOLDER_ID)
                            file_id = gdrive_resp["id"]
                            line += f" -> GDrive({file_id})"
                        except Exception as ge:
                            line += f" (GDrive failed: {ge})"

                    results.append(line)
                    success_count += 1
                except Exception as e:
                    results.append(f"FAIL: {track['title']} ({clip['id'][:8]}...): {e}")
                    fail_count += 1

                # Rate limit
                if i < len(downloadable) - 1:
                    await asyncio.sleep(delay)

            summary = f"Downloaded {success_count}/{len(downloadable)} clips"
            if fail_count:
                summary += f" ({fail_count} failed)"
            results.insert(0, summary)
            return "\n".join(results)

    except RuntimeError as e:
        if "AUTH_401" in str(e):
            return _tool_response(
                "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                status="error",
                code="AUTH_401",
            )
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_playlists() -> str:
    """List all playlists in your Suno account."""
    try:
        headers = await _get_auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUNO_API_BASE}/api/playlist/me/",
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 401:
                return _tool_response(
                    "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                    status="error",
                    code="AUTH_401",
                )
            resp.raise_for_status()
            data = resp.json()

            playlists = data.get("playlists", [])
            if not playlists:
                return "No playlists found."

            lines = [f"{len(playlists)} playlists:"]
            for p in playlists:
                pid = p.get("id", "?")
                name = p.get("name", "Untitled")
                song_count = p.get("song_count", p.get("num_total_results", "?"))
                is_public = p.get("is_public", False)
                vis = "public" if is_public else "private"
                lines.append(f"  {pid} | {name} | {song_count} songs | {vis}")
            return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_playlist_clips(
    playlist_id: str,
    page: int = 0,
) -> str:
    """List clips in a specific Suno playlist.

    Args:
        playlist_id: The UUID of the playlist.
        page: Page number (0-indexed).
    """
    try:
        headers = await _get_auth_headers()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUNO_API_BASE}/api/playlist/{playlist_id.strip()}/",
                headers=headers,
                params={"page": str(page)},
                timeout=30,
            )
            if resp.status_code == 401:
                return _tool_response(
                    "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
                    status="error",
                    code="AUTH_401",
                )
            if resp.status_code == 404:
                return f"Playlist not found: {playlist_id}"
            resp.raise_for_status()
            data = resp.json()

            name = data.get("name", "Untitled")
            has_more = data.get("has_more", False)
            playlist_clips = data.get("playlist_clips", [])

            if not playlist_clips:
                return f"No clips in playlist '{name}' on page {page}."

            lines = [f"Playlist: {name} (page {page}, has_more={has_more}):"]
            for pc in playlist_clips:
                clip = pc.get("clip", pc)
                lines.append(f"  {_format_clip_summary(clip)}")
            return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    import sys

    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse")
