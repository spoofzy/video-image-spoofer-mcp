# Video and Image Spoofer for Claude: Spoofzy MCP server and skill

Spoof images and videos with [Spoofzy](https://spoofzy.com) from Claude Code, Claude Desktop or any other
[Model Context Protocol](https://modelcontextprotocol.io) client. Ask Claude for unique versions of your photos and
clips, strip hidden metadata, or run your saved Spoofzy Cloud workflows. Results land next to your originals.

```text
You:     Make 5 unique versions of every video in ~/Reels/drop-12, keep the captions readable.
Claude:  That's 4 videos × 5 versions with the minimal look, included in your plan. Results go to
         ~/Reels/drop-12/spoofzy-output. Go ahead?
You:     yes
Claude:  Done: 20 files saved in ~/Reels/drop-12/spoofzy-output (beach-spoofzy-01.mp4 … sunset-spoofzy-05.mp4).
```

- **Spoof**: make 1, 5, 10, 20, 30, 40 or 50 unique versions per file with the classic, subtle, stronger or minimal
  (text- and layout-preserving) look, plus optional mirror, 1080p/4K export, watermark and border.
- **Clean metadata**: remove GPS location, device details and capture dates from photos and videos.
- **Workflows**: run saved Spoofzy Cloud workflows (for example Caption → Spoof ×5) on local files or saved originals.
- **One file, no dependencies**: [`spoofzy_mcp.py`](spoofzy_mcp.py) needs only Python 3.9 or later.

## 1. Get a Spoofzy API key

Use either key, or both:

| Key | Where | Best for |
| --- | --- | --- |
| **Spoofzy Cloud API key** (`spc_…`) | [Spoofzy Cloud](https://cloud.spoofzy.com) → **Developers** → **Create API key** | Everything: spoofing on your plan, Studio looks, Metadata Cleaner, workflows |
| **Prepaid Spoofer API key** (`spz_…`) | `/api` in the [Spoofzy Telegram bot](https://t.me/SpoofzyBot) | Prepaid spoofing units for automations |

For a Cloud key, tick **Read jobs and results** and **Upload and run processing**. Add **Read workflows and
history** and **Run saved workflows** to use workflows, and **Read saved originals and voices** to list your library.

Keep keys out of chats, prompts and repositories. The steps below store them outside the conversation.

## 2. Install

### Claude Code (plugin: MCP server + skill)

In Claude Code:

```text
/plugin marketplace add spoofzy/video-image-spoofer-mcp
/plugin install spoofzy@spoofzy
```

Or from a terminal:

```bash
claude plugin marketplace add spoofzy/video-image-spoofer-mcp
```

```bash
claude plugin install spoofzy@spoofzy
```

Then save your key (step 3) and restart Claude Code. The skill is available as `/spoofzy:spoof`, and Claude also
uses it automatically when you ask to spoof media.

### Claude Code (MCP server only)

```bash
curl -fsSLo ~/spoofzy_mcp.py https://raw.githubusercontent.com/spoofzy/video-image-spoofer-mcp/main/spoofzy_mcp.py
```

```bash
claude mcp add spoofzy --scope user --env SPOOFZY_WAIT_SECONDS=600 -- python3 ~/spoofzy_mcp.py
```

### Claude Desktop

Download `spoofzy-<version>.mcpb` from [Releases](https://github.com/spoofzy/video-image-spoofer-mcp/releases) and open it. Claude
Desktop asks for your key and stores it in the system keychain. Python 3.9+ must be installed. macOS includes it
with the Xcode Command Line Tools.

Or add it by hand to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "spoofzy": {
      "command": "python3",
      "args": ["/absolute/path/to/spoofzy_mcp.py"]
    }
  }
}
```

### Other MCP clients (Cursor, VS Code, Codex, Windsurf, …)

Any client that runs stdio servers works. With [uv](https://docs.astral.sh/uv/) installed, no download is needed:

```json
{
  "mcpServers": {
    "spoofzy": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/spoofzy/video-image-spoofer-mcp", "spoofzy-mcp"]
    }
  }
}
```

Without uv, use `"command": "python3"` and the path to `spoofzy_mcp.py`, as in the Claude Desktop example.

## 3. Save your key

Run setup once in a terminal. It asks for your key(s) without echoing them and saves them to
`~/.config/spoofzy/credentials.json` (`%APPDATA%\spoofzy` on Windows), readable only by you. Every install on the
machine uses that file.

```bash
python3 ~/spoofzy_mcp.py setup
```

Plugin users can run setup from any copy of the script (the curl command above downloads one) or with
`uvx --from git+https://github.com/spoofzy/video-image-spoofer-mcp spoofzy-mcp setup`. You can also set environment variables
instead: `SPOOFZY_CLOUD_API_KEY` and/or `SPOOFZY_API_KEY`. Check everything with:

```bash
python3 ~/spoofzy_mcp.py doctor
```

## Using it

Just ask:

- "Spoof ~/Desktop/post.jpg"
- "Make 10 versions of ~/Videos/clip.mov, mirrored, exported at 1080p"
- "Make 5 versions of everything in ~/Reels/new with the minimal look"
- "Remove the location and camera info from ~/Pictures/IMG_2041.HEIC"
- "Run my 'Caption + Spoof' workflow on ~/Videos/hook.mp4"
- "Is my Spoofzy run done yet?"

Claude checks your plan with `spoofzy_account`, confirms what will run and what it costs, then saves the results as
`<name>-spoofzy-01.<ext>`, `-02`, … in a `spoofzy-output` folder next to each file. Existing files are never
overwritten. Long videos keep processing after Claude's call returns and are saved automatically when ready, or on
request with `spoofzy_get_run`.

### Tools

| Tool | What it does |
| --- | --- |
| `spoofzy_account` | Connection, plan, available tools, costs, limits and prepaid units |
| `spoofzy_spoof` | Make unique versions of local images/videos (files or folders, up to 20 per call) |
| `spoofzy_clean_metadata` | Strip GPS, device and date metadata (Cloud) |
| `spoofzy_get_run` | Check a run, optionally waiting for it |
| `spoofzy_download` | Save the results of any finished run |
| `spoofzy_list_runs` | Recent runs from this session and Spoofzy Cloud |
| `spoofzy_list_workflows` | Saved Spoofzy Cloud workflows |
| `spoofzy_run_workflow` | Run a saved workflow on local files, a saved original or a script |
| `spoofzy_list_saved_originals` | Originals saved in your Spoofzy library |

Option details and per-plan limits: [skills/spoof/reference.md](skills/spoof/reference.md).

## Command line

The same file is a small CLI:

```bash
python3 spoofzy_mcp.py spoof ~/Videos/clip.mp4 --versions 5 --look minimal
```

```bash
python3 spoofzy_mcp.py clean ~/Pictures/*.jpg --keep-dates
```

```bash
python3 spoofzy_mcp.py status RUN_ID --download
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPOOFZY_CLOUD_API_KEY` | from `setup` | Spoofzy Cloud key (`spc_…`) |
| `SPOOFZY_API_KEY` | from `setup` | Prepaid Spoofer API key (`spz_…`). A Cloud key placed here also works |
| `SPOOFZY_OUTPUT_DIR` | next to each file | Save every result in this folder instead |
| `SPOOFZY_BACKEND` | `auto` | `cloud`, `api` or `auto` (Cloud when configured) |
| `SPOOFZY_WAIT_SECONDS` | `50` | How long a call waits before returning. The Claude Code plugin uses 600; keep 50 for clients that time out calls after 60 seconds |
| `SPOOFZY_CONFIG_DIR` | `~/.config/spoofzy` | Where `setup` stores credentials |

## Privacy and safety

- **Reads only what you name.** Only the image and video files (or folders) passed to a tool are read. Each file's
  signature is checked, so text files, keys, documents and other non-media files cannot be uploaded even if named.
- **Talks only to Spoofzy.** Files go to your own Spoofzy account over HTTPS. API keys go only to the Spoofzy API,
  never to storage links. Redirects are not followed for API calls, and plain-HTTP links are refused.
- **Writes only results.** Files are saved only in the output folder and never overwrite an existing file.
- **No telemetry.** The server collects nothing. Spoofzy processes your files under its
  [Terms](https://spoofzy.com/terms-of-service) and [Privacy Policy](https://spoofzy.com/privacy-policy) and keeps
  results for a limited time.
- **Spending is explicit.** Runs use your plan's allowance. The skill has Claude confirm files, versions and cost
  first, and `request_id` makes retries safe from double charges.

Only process media you own or are authorized to use. Spoofzy does not guarantee how any platform treats an upload.

Found a security issue? See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Spoofzy is not connected yet" | Run `setup` (or set the environment variable) and restart the client |
| HTTP 401 | The key is mistyped, expired (Cloud keys last up to 90 days) or revoked. Create a new one |
| `insufficient_scope` | Create a Cloud key with the permissions listed in step 1 |
| "TLS certificate verification failed" | python.org Python on macOS: run *Install Certificates.command*, or set `SSL_CERT_FILE` |
| "Permission denied" on macOS | Allow the app (for example Claude) under System Settings → Privacy & Security → Files and Folders |
| `python3` not found (Windows) | Install Python from python.org and use `py -3` or `python` as the command |
| Calls time out in your client | Lower `SPOOFZY_WAIT_SECONDS`. Runs continue and can be checked with `spoofzy_get_run` |

## How it works

`spoofzy_mcp.py` is a single-file stdio MCP server. It supports both the stateless 2026-07-28 protocol revision
(`server/discover`, per-request `_meta`) and the initialize-based 2024-11-05 to 2025-11-25 revisions. It talks to:

- the **Spoofzy Cloud Developer API** at `https://cloud.spoofzy.com/developer/v1`: `capabilities`, `uploads`,
  `jobs`, `chains`, `automations`, `masters`
- the **prepaid Spoofer API** at `https://api.spoofzy.com/api/v1`: `spoofer/account`, `spoofer/uploads`,
  `spoofer/jobs`, `spoofer/batches` ([OpenAPI](https://api.spoofzy.com/api/openapi.json))

Each file is uploaded once to a short-lived signed storage link. A job, many-version job, Studio run, workflow run
or prepaid batch is then started with a stable idempotency key. The server polls within Spoofzy's rate limit (45
requests per minute per account) and downloads the results.

## Development

```bash
python3 -m unittest discover -s tests -v
```

```bash
python3 scripts/build_mcpb.py
```

The tests drive the real server over stdio against an in-process fake of both Spoofzy APIs
([tests/fake_spoofzy.py](tests/fake_spoofzy.py)), with no network or account needed. The build script writes the
Claude Desktop bundle to `dist/`. Bump the version in `spoofzy_mcp.py`, `manifest.json`, `pyproject.toml` and both
`.claude-plugin` files together. The build script checks they match.

Repository layout:

```text
spoofzy_mcp.py                 MCP server + CLI (the whole runtime)
skills/spoof/                  Claude skill: SKILL.md and reference.md
.claude-plugin/                Claude Code plugin (with its MCP server config) + marketplace
manifest.json, assets/         Claude Desktop extension (MCPB)
scripts/build_mcpb.py          Builds dist/spoofzy-<version>.mcpb
tests/                         Unit and end-to-end tests
```

## License

[MIT](LICENSE). The license covers the code, not the Spoofzy name or logo.
