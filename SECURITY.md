# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/spoofzy/video-image-spoofer-mcp/security/advisories/new)
(Security → Report a vulnerability), not in a public issue. Include steps to
reproduce and the version (`python3 spoofzy_mcp.py --version`).

If you accidentally published a Spoofzy API key, revoke it right away: Cloud
keys in Spoofzy Cloud → Developers, prepaid Spoofer API keys through `/api` in
the Spoofzy Telegram bot. Then create a new key.

## What the server can access

- **Reads** only the image and video files (or folders of them) passed to a tool.
  It checks each file's signature, so non-media files are refused.
- **Sends** those files and your API key only to the Spoofzy APIs
  (`cloud.spoofzy.com`, `api.spoofzy.com`) and to the signed storage links they
  return. The key is never sent to storage links. HTTPS is required.
- **Writes** results only into the chosen output folder, without overwriting
  existing files, and stores keys only in the credentials file that `setup`
  creates with owner-only permissions.
- Runs no shell commands and collects no telemetry.

`SPOOFZY_CLOUD_API_URL` and `SPOOFZY_API_URL` override the API addresses for
testing. They accept only `https://` URLs, or `http://` on this machine.
