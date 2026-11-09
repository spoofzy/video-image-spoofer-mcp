# Changelog

## 1.0.0 (2026-11-09)

First open-source release.

- Single-file, dependency-free MCP server (Python 3.9+), supporting the
  2026-07-28 protocol revision and the 2024-11-05 to 2025-11-25 revisions.
- Tools: `spoofzy_account`, `spoofzy_spoof`, `spoofzy_clean_metadata`,
  `spoofzy_get_run`, `spoofzy_download`, `spoofzy_list_runs`,
  `spoofzy_list_workflows`, `spoofzy_run_workflow`,
  `spoofzy_list_saved_originals`.
- Spoofzy Cloud Developer API and prepaid Spoofer API support: uploads, jobs,
  many-version jobs, Studio runs, workflow runs and prepaid batches, with
  idempotent retries.
- Local file safety: media signature checks, never-overwrite saving,
  background completion of long runs.
- Claude skill (`/spoofzy:spoof`), Claude Code plugin and marketplace, and
  Claude Desktop extension (`.mcpb`).
- Command line: `setup`, `doctor`, `spoof`, `clean` and `status`.
