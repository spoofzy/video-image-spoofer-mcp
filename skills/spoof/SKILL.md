---
name: spoof
description: Spoof images and videos with Spoofzy. Makes unique versions of photos, reels and clips, strips hidden metadata (GPS, device, capture dates) and runs saved Spoofzy Cloud workflows, then saves the results next to the originals. Use when the user mentions Spoofzy, spoofing or "uniquifying" media, wants several unique or distinct versions of an image or video, wants to remove photo or video metadata, or wants to run a Spoofzy workflow.
argument-hint: "[files or folder] [number of versions]"
---

# Spoofzy: spoof images and videos

The Spoofzy MCP server gives you `spoofzy_*` tools that upload local media to the
user's own Spoofzy account, process it and save the results locally. Each run
uses the account's allowance (Credits or prepaid units), so be deliberate.

## 1. Check the connection first

Call `spoofzy_account` once per conversation, before the first run. It shows
which Spoofzy API is connected, the plan, which tools are available, their
cost and limits, and where results are saved.

If it says Spoofzy is not connected, give the user the steps it returns and
stop. **Never ask for, accept or repeat an API key in the chat**; keys go in
`python3 spoofzy_mcp.py setup` or the MCP server's environment. If a key
appears in the conversation, tell the user to revoke it in Spoofzy Cloud →
Developers and create a new one.

If the `spoofzy_*` tools are missing entirely, see "Without the MCP tools"
below.

## 2. Confirm before spending

Before `spoofzy_spoof`, `spoofzy_clean_metadata` or `spoofzy_run_workflow`,
confirm in one short message:

- which files (the count, and the folder if there are many),
- how many versions of each, the look and any extras,
- what it costs, per `spoofzy_account` ("included", N Credits, or N prepaid units),
- where results will go (by default a `spoofzy-output` folder next to each file).

One confirmation covers the whole request. Do not re-confirm each file of a
batch the user already approved. Skip it when the user has already given
explicit instructions ("make 5 versions of every video in ~/Reels").

## 3. Pick settings

| User wants | Use |
| --- | --- |
| "spoof this", "make it unique" | `versions: 1`, `look: classic` (the defaults) |
| several distinct copies | `versions`: Cloud allows 1, 5, 10, 20, 30, 40 or 50; for "3", offer 5 |
| keep captions or on-screen text readable | `look: minimal` (no mirror, rotation or crop) |
| lighter or stronger changes | `look: subtle` or `look: stronger` (Cloud membership) |
| flipped copies | `mirror: true` (flips any visible text; not with `minimal`) |
| 1080p or 4K output | `export_size` (Cloud membership, or prepaid batch access) |
| a text watermark or border | `watermark: {"mode": "text", "text": "@handle"}`, `border: {"color": "pink"}` (Cloud membership) |
| remove location, camera or date info | `spoofzy_clean_metadata` |

Details, limits and every option: [reference.md](reference.md).

## 4. Run it

- Pass **absolute paths** in `files`. Folders are fine: every supported file
  directly inside is processed. Supported types are JPG, PNG, WebP, HEIC, GIF,
  MP4, MOV, M4V and WebM.
- One call takes up to 20 files. For more, run groups of 20 one after another
  under the same approval.
- Results are named `<original>-spoofzy-01.jpg`, `-02`, and so on. Existing
  files are never overwritten; numbering continues after earlier runs.
- Use `output_dir` only when the user names a destination.

## 5. Long runs and checking back

Videos and large version counts can take minutes. A call returns after
`wait_seconds` (often about 50 seconds) with whatever has finished. Anything
still processing keeps going on Spoofzy, and its results are saved
automatically when ready while the MCP server runs.

- To wait for a run, call `spoofzy_get_run` with its `run_id` and
  `wait_seconds: 50`. Repeat until it reports the files saved.
- To fetch results from another session, from the Spoofzy Cloud app or from a
  run started with `download: false`, call `spoofzy_download`. Result links
  expire after a few days.
- `spoofzy_list_runs` shows recent runs and their status.

## 6. Retries without paying twice

If a result says an outcome is **uncertain** (for example a network error
right after starting), retry the same call with the `request_id` it returned.
Spoofzy then returns the existing runs instead of starting new ones. Never
leave out `request_id` when retrying the same files after an uncertain
result.

## 7. Workflows and saved originals

Saved Spoofzy Cloud workflows are reusable step stacks (for example Caption →
Spoof ×5). Use `spoofzy_list_workflows` to find one, then
`spoofzy_run_workflow` with `workflow_id` and either local `files`, a
`saved_original_id` (from `spoofzy_list_saved_originals`) or `text` for voice
workflows. A workflow accepts only its saved input type. If a JPEG workflow
gets a PNG, tell the user.

## 8. Report back

Say what was made and where: the folder, a few file names, the count, and any
Credits or units used. Mention failures plainly, with Spoofzy's reason and the
fix. Offer to open the folder. Do not claim any platform will accept,
rank or not detect the files. Spoofzy makes no such guarantee.

## Errors and fixes

| Message contains | Do this |
| --- | --- |
| `not connected yet`, `HTTP 401` | The key is missing, expired or revoked. Give the user the setup steps. |
| `insufficient_scope` | The Cloud key lacks a permission. The user creates a key with the needed permissions. |
| `membership`, `Pro`, `not available on this account` | Explain which plan feature is needed. Offer the classic look, or no extras, instead. |
| `prepaid units`, `quota_or_concurrency_reached` | No units or job slots are free. Wait for running jobs, or top up with `/api` in the Spoofzy Telegram bot. |
| `not a supported image or video` | The file is not real media, or is an unsupported type such as AVIF or PDF. |
| `Permission denied` | macOS privacy settings block the folder. The message explains how to allow access. |

## Responsible use

Only process media the user owns or is authorised to use. This skill
provides spoofing, metadata cleaning and the user's own saved workflows. It
does not offer other Spoofzy tools.

## Without the MCP tools

If the `spoofzy_*` tools are unavailable but you can run shell commands, the
same server works as a command-line tool from this plugin's root folder (two
levels above this file):

```bash
python3 <plugin-root>/spoofzy_mcp.py doctor
python3 <plugin-root>/spoofzy_mcp.py spoof ~/Videos/clip.mp4 --versions 5 --look minimal
python3 <plugin-root>/spoofzy_mcp.py clean ~/Pictures/photo.jpg
python3 <plugin-root>/spoofzy_mcp.py status RUN_ID --download
```

The command-line tool follows the same confirmation rules.
