# Spoofzy tools reference

## Which Spoofzy API is used

| | Spoofzy Cloud (`spc_…` key) | Prepaid Spoofer API (`spz_…` key) |
| --- | --- | --- |
| Get a key | Spoofzy Cloud → Developers → Create API key | `/api` in the Spoofzy Telegram bot |
| Billing | The Cloud plan's allowance (Unlimited Spoofer on memberships; Credits otherwise) | Prepaid units: 1 per finished version; failed versions are refunded |
| Versions per file | 1, 5, 10, 20, 30, 40, 50 | 1; 1–300 with batch access; otherwise one job per version (up to 50) |
| Looks | classic, minimal; subtle and stronger with a membership | classic, minimal |
| Export size, watermark, border | With a membership (Studio) | Export size with batch access; watermark is the account's saved one |
| Metadata Cleaner, workflows, saved originals | Yes | No |
| File types | JPG, PNG, WebP, HEIC, GIF, MP4, MOV, M4V, WebM | JPG, PNG, WebP, MP4, M4V, MOV (images ≤ 100 MB, videos ≤ 300 MB) |

With both keys configured, `backend: "auto"` uses Cloud. Pass `backend: "api"`
to use prepaid units instead.

Cloud keys have permissions:

- spoofing and metadata cleaning: "Read jobs and results" and "Upload and run processing"
- workflows: "Read workflows and history" and "Run saved workflows"
- saved originals: "Read saved originals and voices"

Spoofzy Cloud allows 45 API requests per minute per account. The server stays
under that limit by itself.

## spoofzy_spoof

| Argument | Default | Notes |
| --- | --- | --- |
| `files` | required | Absolute paths or folders; up to 20 files. `file` is accepted for a single path. |
| `versions` | 1 | See the table above. |
| `look` | classic | `classic`, `subtle`, `stronger`, `minimal` |
| `mirror` | false | Spoofzy's Mirror switch. Not with `minimal`. |
| `export_size` | original | `original`, `1080p`, `4k` |
| `watermark` | account setting | `{mode: account\|off\|text\|logo, text, placement, size, opacity}`; placement: auto, light, edge, top_left, top_right, bottom_left, bottom_right, center; size: small, medium, large; opacity: subtle, clear |
| `border` | none | `{color: off\|light\|dark\|pink\|blue\|yellow\|purple\|green\|red\|orange, width: slim\|regular\|thick, style: square\|rounded}` |
| `output_dir` | `spoofzy-output` next to each file | Created if needed. |
| `wait_seconds` | server default (often 50) | 0–3600. Unfinished runs keep going and are saved later. |
| `download` | true | `false` returns temporary result links instead of saving. |
| `backend` | auto | `auto`, `cloud`, `api` |
| `request_id` | new | Only to retry an uncertain call. |

How it runs on Cloud: classic look, original size, no watermark or border →
the Spoofer job (1 version) or the many-version job (5–50). Any Studio option →
a Studio run, which also saves those choices as the account's Spoofer
defaults, just like a run in the Cloud app.

## spoofzy_clean_metadata

`files`, `remove_gps` (true), `remove_device` (true), `remove_datetime` (true),
`output_dir`, `wait_seconds`, `download`, `request_id`. Saves
`<name>-clean-01.<ext>`. The originals are never changed. Cloud only.

## spoofzy_run_workflow

`workflow_id` (required) plus exactly one input: `files`, `saved_original_id`
or `text`. `version` defaults to the current one. If the workflow changed since
the user reviewed it, run the current version only after they agree.
Side-by-side workflows, which take two images, are not supported here.

## Checking and downloading

- `spoofzy_get_run {run_id, wait_seconds}` reports status, ready results and
  saved files. It is read-only, except that runs from this session that asked
  for downloads are saved.
- `spoofzy_download {run_id, output_dir, wait_seconds}` saves the results of
  any finished run on the account.
- `spoofzy_list_runs {limit}` shows this session's runs first, then recent
  Cloud jobs and Studio runs.

Run id prefixes: `mjob_` is a Cloud job, `cc_` is a Cloud Studio run or
workflow run, `api_` is a prepaid job, and `batch_` is a prepaid batch. For a
`cc_` run from a workflow key that cannot read jobs, also pass `workflow_id`.

## Statuses

`starting`, `queued`, `waiting` (plan concurrency: queued for a free slot),
`processing`, `completed`, `partial` (some versions failed and were not
charged), `failed`.
