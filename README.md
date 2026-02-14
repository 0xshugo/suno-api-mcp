# Suno API MCP Server

An MCP (Model Context Protocol) server for generating music via the Suno AI API.

## Features

- **Music Generation**: Generate tracks using Suno's AI models (chirp-v4, chirp-v3-5, chirp-crow)
- **WAV Output**: Automatically downloads tracks in WAV format (via direct download or ffmpeg conversion)
- **Multiple Output Targets**: Save to different directories (library, ch1, ch2) or Google Drive
- **Auto Token Refresh**: Supports Clerk-based authentication with automatic token refresh
- **MCP Compatible**: Works with any MCP-compatible client via SSE or stdio transport

## Requirements

- Python 3.12+
- ffmpeg (for MP3 to WAV conversion fallback)
- Suno account with API access

## Quickstart (Minimal Setup)

```bash
# 1) Configure env
cp .env.example .env

# 2) Set at least this value in .env
# SUNO_REFRESH_TOKEN=...

# 3) Start server
python server.py          # SSE
# or
python server.py --stdio  # stdio
```

After startup, run these MCP tools in order to confirm setup:
1. `get_auth_status` → expect `auth_state: ok`
2. `validate_suno_refresh_token` → expect `classification=valid`
3. `get_credits` → credits are returned successfully

## Installation

### Using Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/0xshugo/suno-api-mcp.git
cd suno-api-mcp

# Copy and configure environment
cp .env.example suno_cookie.env
# Edit suno_cookie.env with your Suno refresh token

# Build and run
docker compose up -d
```

### Manual Installation

```bash
# Clone and setup
git clone https://github.com/0xshugo/suno-api-mcp.git
cd suno-api-mcp

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Run server
python server.py          # SSE transport (default)
python server.py --stdio  # stdio transport
```

## Configuration

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `SUNO_REFRESH_TOKEN` | Clerk refresh token (`__client` cookie from Suno, ~1 year validity) |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SUNO_SESSION_TOKEN` | - | Legacy: direct session token (~1 hour validity) |
| `SUNO_DEVICE_ID` | auto-generated | Device identifier |
| `MCP_PORT` | 8888 | Server port (SSE mode) |
| `MUSIC_BASE` | /data/music | Base directory for music output |
| `LOG_LEVEL` | INFO | Logging level |
| `AUTH_NOTIFY_WEBHOOK_URL` | - | Generic webhook URL for `reauth_required` alerts |
| `AUTH_NOTIFY_SLACK_WEBHOOK_URL` | - | Slack incoming webhook URL for auth alerts |
| `AUTH_NOTIFY_DISCORD_WEBHOOK_URL` | - | Discord webhook URL for auth alerts |
| `TOOL_RESPONSE_FORMAT` | text | `text` or `json` (agent-friendly responses) |

### Notification Configuration Examples (Optional)

Slack:
```env
AUTH_NOTIFY_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Discord:
```env
AUTH_NOTIFY_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abcdef
```

Generic webhook:
```env
AUTH_NOTIFY_WEBHOOK_URL=https://your-endpoint.example.com/webhook
```

### Google Drive Integration (Optional)

| Variable | Description |
|----------|-------------|
| `GDRIVE_CLIENT_ID` | Google OAuth2 client ID |
| `GDRIVE_CLIENT_SECRET` | Google OAuth2 client secret |
| `GDRIVE_REFRESH_TOKEN` | Google OAuth2 refresh token |
| `GDRIVE_MUSIC_FOLDER_ID` | Target folder ID in Google Drive |

## Getting Your Suno Refresh Token

### Easiest way (recommended)
Use MCP tool `get_cookie_capture_helper`.
It returns a bookmarklet that:
- reads `__client` from your logged-in `suno.com` tab,
- fetches `last_active_session_id`,
- copies `SUNO_REFRESH_TOKEN=...` to clipboard.

### Manual way (fallback)
1. Log in to [suno.com](https://suno.com) in your browser.
2. Open Developer Tools (F12) → Application → Cookies.
3. Find the `__client` cookie (refresh token).
4. Copy the value and set it as `SUNO_REFRESH_TOKEN`.

### GUI step examples (OS / Browser)
- **Windows + Chrome/Edge**: `F12` → **Application** tab → **Cookies** → `https://suno.com` → `__client`
- **macOS + Chrome/Edge**: `⌥⌘I` → **Application** tab → **Cookies** → `https://suno.com` → `__client`
- **macOS + Safari**: Safari Settings → Advanced → enable *Show Develop menu* → Develop → *Show Web Inspector* → Storage/Cookies → `__client`

Note: The short-lived access token is auto-refreshed from this refresh token.

## Available Tools

### `generate_liquid_dnb`

Generate a music track via Suno.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tags` | string | "Liquid Drum and Bass, atmospheric..." | Style/genre description (recommended: 50-100 chars) |
| `title` | string | auto-generated | Track title |
| `prompt` | string | "" | Lyrics (empty for instrumental) |
| `instrumental` | boolean | true | Generate without vocals |
| `output_dir` | string | "library" | Output target: "library", "ch1", "ch2", "gdrive" |
| `model` | string | "chirp-v4" | Model: "chirp-v4", "chirp-v3-5", "chirp-crow" |

**Tag Guidelines:**
- Recommended length: 50-100 characters
- Maximum: ~120 characters (200+ will fail)
- Format: Comma-separated keywords
- Example: `"Liquid DnB, atmospheric, deep rolling bass, 174bpm"`

### `get_credits`

Check remaining Suno credits.

### `get_auth_status`

Show authentication health (`ok` / `degraded` / `reauth_required`) and operator action hints.

### `get_cookie_capture_helper`

Return GUI-friendly instructions + bookmarklet for quick `SUNO_REFRESH_TOKEN` capture.

### `validate_suno_refresh_token`

Validate pasted token and classify failure reasons:
- `expired_or_invalid` (401)
- `forbidden` (403)
- `rate_limited` (429)
- `transient` (timeout/network)

When `TOOL_RESPONSE_FORMAT=json`, output is structured for agent clients (e.g. OpenClaw).

Example JSON response:
```json
{
  "status": "error",
  "message": "Authentication failed (401). Please update SUNO_REFRESH_TOKEN and restart.",
  "meta": {
    "code": "AUTH_401",
    "classification": "reauth_required"
  }
}
```

### `list_tracks`

List WAV tracks in an output directory.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `output_dir` | string | "library" | Target: "library", "ch1", "ch2", "gdrive" |

### `delete_track`

Delete a track from an output directory.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `filename` | string | required | Exact filename to delete |
| `output_dir` | string | "library" | Target directory |

## Output Directories

| Target | Path | Description |
|--------|------|-------------|
| `library` | /data/music/library | General library (default) |
| `ch1` | /data/music/ch1 | Broadcasting channel 1 |
| `ch2` | /data/music/ch2 | Broadcasting channel 2 |
| `gdrive` | library + Google Drive | Save locally + upload to GDrive |

## MCP Client Example

```javascript
// Connect to SSE endpoint
const response = await fetch('http://localhost:8888/sse');
// Parse SSE for message endpoint URL
// Send JSON-RPC requests to message endpoint
```

See [MCP Documentation](https://modelcontextprotocol.io/) for protocol details.

## Future Roadmap (Memo)

The following ideas are intentionally deferred until user feedback on the minimal set:
- Interactive onboarding CLI (`onboarding.py`) to guide `.env` setup and token validation.
- One-command startup healthcheck script for Docker/manual modes.
- Extended runbook for auth incident handling with copy-paste recovery flows.

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| 401 Unauthorized | Token expired/invalid | Use `get_cookie_capture_helper` then `validate_suno_refresh_token`, update `SUNO_REFRESH_TOKEN` |
| "tags too long" | Tags > 200 chars | Shorten to 50-100 characters |
| Timeout | Generation slow | Wait up to 5 minutes, retry if needed |
| ffmpeg error | Missing ffmpeg | Install ffmpeg: `apt install ffmpeg` |

## License

MIT
