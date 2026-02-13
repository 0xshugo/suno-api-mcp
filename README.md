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

### Google Drive Integration (Optional)

| Variable | Description |
|----------|-------------|
| `GDRIVE_CLIENT_ID` | Google OAuth2 client ID |
| `GDRIVE_CLIENT_SECRET` | Google OAuth2 client secret |
| `GDRIVE_REFRESH_TOKEN` | Google OAuth2 refresh token |
| `GDRIVE_MUSIC_FOLDER_ID` | Target folder ID in Google Drive |

## Getting Your Suno Refresh Token

1. Log in to [suno.com](https://suno.com) in your browser
2. Open Developer Tools (F12) → Application → Cookies
3. Find the `__client` cookie (this is your refresh token, valid for ~1 year)
4. Copy the value and set it as `SUNO_REFRESH_TOKEN`

Note: The `__session` cookie is the access token (valid ~1 hour). The server automatically refreshes this using the refresh token.

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

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| 401 Unauthorized | Token expired | Update SUNO_REFRESH_TOKEN with fresh `__client` cookie |
| "tags too long" | Tags > 200 chars | Shorten to 50-100 characters |
| Timeout | Generation slow | Wait up to 5 minutes, retry if needed |
| ffmpeg error | Missing ffmpeg | Install ffmpeg: `apt install ffmpeg` |

## License

MIT
