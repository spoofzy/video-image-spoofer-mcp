#!/usr/bin/env python3
"""Spoofzy MCP server: make unique versions of images and videos with Spoofzy.

Works with Claude Code, Claude Desktop and other Model Context Protocol (MCP)
clients. Pure Python 3.9+ standard library: nothing to install. It speaks MCP
over stdio (the stateless 2026-07-28 revision and the initialize-based
2024-11-05 to 2025-11-25 revisions) and connects to:

* the Spoofzy Cloud Developer API (keys start with ``spc_``), and/or
* the prepaid Spoofzy Spoofer API (keys start with ``spz_``).

Safety model
------------
* Reads only the image and video files (or folders of them) named in a tool
  call. Each file's signature is checked, so nothing that is not an image or a
  video can be uploaded.
* Sends an API key only to its own Spoofzy API origin, never to storage links.
* Writes results only into the chosen output folder and never overwrites files.

Commands
--------
    python3 spoofzy_mcp.py                   run the MCP server on stdio
    python3 spoofzy_mcp.py setup             save API keys in your user config
    python3 spoofzy_mcp.py doctor            check configuration and connectivity
    python3 spoofzy_mcp.py spoof FILE...     spoof files from the terminal
    python3 spoofzy_mcp.py clean FILE...     strip metadata from the terminal
    python3 spoofzy_mcp.py status RUN_ID     check or download a run
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import getpass
import hashlib
import http.client
import json
import os
import random
import re
import secrets
import ssl
import sys
import threading
import time
import traceback
import types
import unicodedata
import urllib.parse
from pathlib import Path

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_INFO = {"name": "spoofzy", "title": "Spoofzy", "version": __version__}
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

DEFAULT_CLOUD_URL = "https://cloud.spoofzy.com/developer/v1"
DEFAULT_API_URL = "https://api.spoofzy.com/api/v1"
USER_AGENT = "spoofzy-mcp/{} (+https://github.com/spoofzy/video-image-spoofer-mcp)".format(__version__)
CLOUD_KEY_RE = re.compile(r"spc_[a-f0-9]{32}\.[A-Za-z0-9_-]{43}")
API_KEY_RE = re.compile(r"spz_([0-9]+)_[A-Za-z0-9_-]{24,}")
RUN_ID_RE = re.compile(r"(?:mjob|cc|api|batch)_[a-f0-9]{32}")
WORKFLOW_ID_RE = re.compile(r"wa_[a-f0-9]{32}")
MASTER_ID_RE = re.compile(r"[a-f0-9]{32}")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MAX_FILES = 20
DEFAULT_WAIT_SECONDS = 50
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 4 * 1024 ** 3
CLOUD_VERSIONS = (1, 5, 10, 20, 30, 40, 50)
LOOKS = {"classic": "c", "subtle": "n", "stronger": "s", "minimal": "t"}
LOOK_NAMES = {token: name for name, token in LOOKS.items()}
PREPAID_MAX_BYTES = {"image": 100 * 1024 * 1024, "video": 300 * 1024 * 1024}
PREPAID_MAX_JOB_VERSIONS = 50
PREPAID_MAX_BATCH = 300

TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/heic": ".heic", "image/heif": ".heif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}
EXTENSION_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif", ".heic": "image/heic", ".heif": "image/heif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
}
TYPE_FAMILIES = {"image/heic": "heif", "image/heif": "heif", "video/mp4": "iso", "video/quicktime": "iso"}
TYPE_NAMES = {
    "image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WebP", "image/gif": "GIF",
    "image/heic": "HEIC", "image/heif": "HEIF", "video/mp4": "MP4", "video/quicktime": "MOV",
    "video/webm": "WebM",
}
BACKEND_TYPES = {
    "cloud": frozenset(TYPE_EXTENSIONS),
    "api": frozenset({"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime"}),
}
OUTPUT_EXTENSIONS = frozenset(EXTENSION_TYPES) | {".mp3", ".wav", ".ogg", ".m4a", ".zip"}
MIME_EXTENSIONS = dict(TYPE_EXTENSIONS, **{
    "image/jpg": ".jpg", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/mp4": ".m4a", "application/zip": ".zip",
})
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
SUPPORTED_TEXT = "JPG, PNG, WebP, HEIC, GIF, MP4, MOV, M4V and WebM"

INSTRUCTIONS = (
    "Spoofzy makes unique versions of images and videos (\"spoofing\"), strips hidden photo/video "
    "metadata and runs saved Spoofzy Cloud workflows, using the user's own Spoofzy account. "
    "Call spoofzy_account first to see what the account can do and what it costs. Before a run "
    "that uses the account's allowance, confirm the files, number of versions and settings with "
    "the user. Pass absolute file paths (folders work too). Results are saved in a spoofzy-output "
    "folder next to each source file unless output_dir is given; existing files are never "
    "overwritten. Long videos can outlast one call: check them with spoofzy_get_run. Reuse a "
    "request_id only to retry a call whose outcome was uncertain. Never ask the user to paste an "
    "API key into the chat: keys are configured outside the conversation."
)


# ---------------------------------------------------------------------------
# Errors and per-call context
# ---------------------------------------------------------------------------

class SpoofzyError(Exception):
    """A problem whose message is safe and useful to show to the user."""

    def __init__(self, message, code=None, status=None, uncertain=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.uncertain = uncertain


class Cancelled(Exception):
    """The MCP client cancelled the tool call."""


class Context:
    """Cancellation and progress reporting for one piece of work."""

    def __init__(self, notify=None):
        self.cancelled = threading.Event()
        self.detached = False
        self._notify = notify
        self._lock = threading.Lock()
        self._value = 0.0
        self._sent_at = 0.0

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def sleep(self, seconds):
        if self.cancelled.wait(max(0.0, seconds)):
            raise Cancelled()

    def progress(self, fraction, message, force=False):
        """Report progress; values strictly increase, as MCP requires."""
        if self._notify is None or self.detached:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._sent_at < 2.0:
                return
            value = max(self._value + 0.001, round(min(max(fraction, 0.0), 0.97) * 100, 3))
            self._value, self._sent_at = value, now
        try:
            self._notify(value, message)
        except Exception:
            pass


_LOCAL = threading.local()
_IDLE = Context()


def current():
    return getattr(_LOCAL, "context", None) or _IDLE


class using:
    """Make ``context`` the current context of this thread for a block."""

    def __init__(self, context):
        self.context = context
        self.previous = None

    def __enter__(self):
        self.previous = getattr(_LOCAL, "context", None)
        _LOCAL.context = self.context
        return self.context

    def __exit__(self, *exc):
        _LOCAL.context = self.previous
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def clean_value(value):
    """Normalise a configured value; unexpanded ``${...}`` placeholders count as unset."""
    if not isinstance(value, str):
        return ""
    value = value.strip().strip("\"'").strip()
    if not value or value.startswith("${") or value.lower() in {"undefined", "null", "none"}:
        return ""
    return value


def config_dir(env):
    override = clean_value(env.get("SPOOFZY_CONFIG_DIR"))
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(clean_value(env.get("APPDATA")) or Path.home() / "AppData" / "Roaming") / "spoofzy"
    return Path(clean_value(env.get("XDG_CONFIG_HOME")) or Path.home() / ".config") / "spoofzy"


def mask_key(key):
    return "{}…{}".format(key[:8], key[-4:]) if key else "not set"


class Settings:
    """API keys, endpoints and defaults from the environment and the credentials file."""

    def __init__(self, env=None):
        env = dict(os.environ if env is None else env)
        self.config_file = config_dir(env) / "credentials.json"
        self.notes = []
        stored = self._read_stored()
        self.cloud_key = self.api_key = ""
        self.cloud_source = self.api_source = ""
        candidates = (
            ("the SPOOFZY_CLOUD_API_KEY environment variable", env.get("SPOOFZY_CLOUD_API_KEY")),
            ("the SPOOFZY_API_KEY environment variable", env.get("SPOOFZY_API_KEY")),
            (str(self.config_file), stored.get("cloud_api_key")),
            (str(self.config_file), stored.get("api_key")),
        )
        for source, raw in candidates:
            value = clean_value(raw)
            if not value:
                continue
            if CLOUD_KEY_RE.fullmatch(value):
                if not self.cloud_key:
                    self.cloud_key, self.cloud_source = value, source
            elif API_KEY_RE.fullmatch(value):
                if not self.api_key:
                    self.api_key, self.api_source = value, source
            else:
                self.notes.append(
                    "Ignored a value from {}: it is not a Spoofzy key (Cloud keys start with spc_, "
                    "prepaid Spoofer API keys with spz_).".format(source))
        self.url_problems = []
        self.cloud_url = self._base_url(env, "SPOOFZY_CLOUD_API_URL", DEFAULT_CLOUD_URL)
        self.api_url = self._base_url(env, "SPOOFZY_API_URL", DEFAULT_API_URL)
        self.output_dir = clean_value(env.get("SPOOFZY_OUTPUT_DIR")) or clean_value(stored.get("output_dir"))
        backend = clean_value(env.get("SPOOFZY_BACKEND")).lower() or "auto"
        if backend not in ("auto", "cloud", "api"):
            self.notes.append("SPOOFZY_BACKEND must be auto, cloud or api; using auto.")
            backend = "auto"
        self.backend = backend
        self.wait_seconds = self._number(env, "SPOOFZY_WAIT_SECONDS", DEFAULT_WAIT_SECONDS, 0, 3600)
        hosts = [urllib.parse.urlsplit(url or "").hostname for url in (self.cloud_url, self.api_url)]
        self.fast_poll = env.get("SPOOFZY_TEST_FAST_POLL") == "1" and all(h in LOOPBACK_HOSTS for h in hosts)

    def _read_stored(self):
        try:
            if not self.config_file.is_file():
                return {}
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.notes.append("Could not read {}; run setup again.".format(self.config_file))
            return {}
        if not isinstance(data, dict):
            self.notes.append("{} is not a JSON object; run setup again.".format(self.config_file))
            return {}
        if os.name != "nt":
            try:
                if self.config_file.stat().st_mode & 0o077:
                    self.notes.append('{0} can be read by other users; run: chmod 600 "{0}"'.format(self.config_file))
            except OSError:
                pass
        return data

    def _base_url(self, env, name, default):
        raw = clean_value(env.get(name))
        if not raw:
            return default
        parts = urllib.parse.urlsplit(raw)
        secure = parts.scheme == "https" and bool(parts.hostname)
        local = parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS
        if not (secure or local) or parts.query or parts.fragment or parts.username or parts.password:
            self.url_problems.append("{} must be an https:// URL without a query or credentials.".format(name))
            return None
        return raw.rstrip("/")

    def _number(self, env, name, default, low, high):
        raw = clean_value(env.get(name))
        if not raw:
            return default
        try:
            return min(max(int(raw), low), high)
        except ValueError:
            self.notes.append("{} must be a whole number of seconds; using {}.".format(name, default))
            return default


def not_configured_message():
    script = Path(__file__).resolve()
    return (
        "Spoofzy is not connected yet: no API key is configured for this MCP server. To connect it, "
        "the user creates a key and saves it outside the chat:\n"
        "1. Spoofzy Cloud key: open https://cloud.spoofzy.com → Developers → Create API key, with "
        "'Read jobs and results' and 'Upload and run processing' (add the workflow/library permissions "
        "to use saved workflows). A prepaid Spoofer API key comes from /api in the Spoofzy Telegram bot.\n"
        "2. In a terminal run: python3 \"{}\" setup  (or set SPOOFZY_CLOUD_API_KEY / SPOOFZY_API_KEY in "
        "this MCP server's environment).\n"
        "3. Restart the MCP client (or reconnect the server).\n"
        "Never paste API keys into the chat.".format(script))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

CERT_HELP = (
    "TLS certificate verification failed while contacting Spoofzy. If Python was installed from "
    "python.org on macOS, run its 'Install Certificates.command'; otherwise set SSL_CERT_FILE to a CA bundle.")
_SSL_CONTEXT = None
_SSL_LOCK = threading.Lock()


def _ca_bundles():
    bundle = os.environ.get("SSL_CERT_FILE")
    if bundle:
        yield bundle
    try:
        import certifi  # optional; used only when present
        yield certifi.where()
    except Exception:
        pass
    yield from ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                "/opt/homebrew/etc/ca-certificates/cert.pem", "/usr/local/etc/ca-certificates/cert.pem")


def ssl_context():
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            context = ssl.create_default_context()
            try:
                if not context.cert_store_stats().get("x509_ca"):
                    for candidate in _ca_bundles():
                        if candidate and os.path.isfile(candidate):
                            context.load_verify_locations(cafile=candidate)
                            break
            except (ssl.SSLError, OSError):
                pass
            _SSL_CONTEXT = context
        return _SSL_CONTEXT


def checked_url(url, what):
    """Accept only https links (or plain http to this machine, for local testing)."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is not None and host and (parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS)):
        return parts
    raise SpoofzyError("Spoofzy returned an unexpected {} link, so it was not used.".format(what))


def http_open(method, parts, headers, body=None, timeout=60):
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout,
                                                 context=ssl_context())
    else:
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        connection.putrequest(method, target, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                connection.send(body)
            else:
                for chunk in body:
                    connection.send(chunk)
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


class RateLimiter:
    """At most ``per_minute`` requests in any rolling minute (shared by all threads)."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.calls = collections.deque()
        self.lock = threading.Lock()

    def wait(self, context):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                delay = 60 - (now - self.calls[0]) + 0.05
            context.sleep(min(delay, 5))


ERROR_HINTS = {
    401: "Check the API key: it may be mistyped, expired or revoked. Create a new key, save it with "
         "`spoofzy_mcp.py setup` (or the environment variable) and restart the MCP client.",
    "insufficient_scope": "This key is missing a permission for this action. Create a Spoofzy Cloud key "
                          "(Cloud → Developers → Create API key) that includes it.",
    "quota_or_concurrency_reached": "No prepaid units or free job slots right now. Wait for running jobs to "
                                    "finish, or top up with /api in the Spoofzy Telegram bot.",
    "batch_not_enabled": "Batches of this size are not enabled for this prepaid API account.",
    "idempotency_conflict": "That request_id was already used for different settings or files. Leave "
                            "request_id empty to start a new run.",
    "invalid_upload_size": "The file is empty or larger than this plan allows.",
    "file_too_large": "The file is larger than this plan allows.",
}
# Errors that retrying cannot fix, even though their HTTP status (429/503) usually means "try again".
FINAL_CODES = frozenset({
    "quota_or_concurrency_reached", "batch_not_enabled", "processing_unavailable", "preset_unavailable",
    "worker_unavailable", "tool_unavailable", "pro_required", "membership_required",
})
# Errors that will fail every file the same way, so a multi-file call stops at the first one.
SYSTEMIC_CODES = frozenset({
    "invalid_api_key", "authentication_required", "insufficient_scope", "pro_required",
    "membership_required", "cloud_admin_only", "account_not_linked", "processing_unavailable",
    "not_found", "tool_unavailable", "insufficient_units", "batch_not_enabled", "early_access",
})


def api_error(label, status, data):
    detail = data.get("error") if isinstance(data, dict) else None
    code = message = None
    if isinstance(detail, dict):
        code = detail.get("code") if isinstance(detail.get("code"), str) else None
        message = detail.get("message") if isinstance(detail.get("message"), str) else None
    elif isinstance(detail, str):
        message = detail
    text = "{} refused the request (HTTP {}{})".format(label, status, ", " + code if code else "")
    text += (": " + message.strip()[:400]) if message else "."
    hint = ERROR_HINTS.get(code) or ERROR_HINTS.get(status)
    if hint:
        text += " " + hint
    return SpoofzyError(text, code=code, status=status, uncertain=status >= 500)


def retry_delay(retry_after, attempt):
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = min(2 ** (attempt + 1), 20)
    return min(max(seconds, 0.5), 30) + random.random() * 0.5


class ApiClient:
    """JSON over HTTPS to one Spoofzy API, with its key, rate limit and safe retries."""

    def __init__(self, label, base_url, key, per_minute):
        self.label = label
        self.base_url = base_url
        self.key = key
        self.limiter = RateLimiter(per_minute)

    def call(self, method, path, body=None, headers=None, attempts=1):
        parts = urllib.parse.urlsplit(self.base_url + "/" + path)
        request_headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json",
                           "User-Agent": USER_AGENT}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(payload))
        if headers:
            request_headers.update(headers)
        context = current()
        failure, delay = None, 0.0
        for attempt in range(max(1, attempts)):
            context.check()
            if attempt:
                context.sleep(delay)
            self.limiter.wait(context)
            try:
                connection, response = http_open(method, parts, request_headers, payload, timeout=45)
                try:
                    raw = response.read(MAX_JSON_BYTES + 1)
                    status = response.status
                    retry_after = response.getheader("Retry-After")
                finally:
                    connection.close()
            except ssl.SSLCertVerificationError:
                raise SpoofzyError(CERT_HELP, code="tls_error") from None
            except (OSError, http.client.HTTPException) as error:
                failure = SpoofzyError("Could not reach {} ({}).".format(self.label, type(error).__name__),
                                       code="network_error", uncertain=method != "GET")
                delay = min(2 ** attempt, 8) + random.random()
                continue
            if len(raw) > MAX_JSON_BYTES:
                raise SpoofzyError("{} sent an unexpectedly large response.".format(self.label))
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                data = None
            if 200 <= status < 300:
                if not isinstance(data, dict):
                    raise SpoofzyError("{} returned an unreadable response (HTTP {}).".format(self.label, status))
                return data
            failure = api_error(self.label, status, data)
            if status in (429, 500, 502, 503, 504) and failure.code not in FINAL_CODES:
                delay = retry_delay(retry_after, attempt)
                continue
            raise failure
        raise failure


def put_file(ticket, media, label, on_progress=None):
    """Upload a local file to the signed storage ticket returned by Spoofzy."""
    if not isinstance(ticket, dict) or not isinstance(ticket.get("url"), str) or not ticket.get("id"):
        raise SpoofzyError("{} returned an unexpected upload ticket.".format(label))
    if str(ticket.get("method") or "PUT").upper() != "PUT":
        raise SpoofzyError("{} asked for an unsupported upload method.".format(label))
    limit = ticket.get("max_bytes")
    if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < media.size:
        raise SpoofzyError("{} is {}, over the {} limit for this tool on this account.".format(
            media.path.name, human_size(media.size), human_size(limit)), code="file_too_large")
    parts = checked_url(ticket["url"], "upload")
    supplied = ticket.get("headers") if isinstance(ticket.get("headers"), dict) else {}
    headers = {str(k): str(v) for k, v in supplied.items()}
    if not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = media.content_type
    headers["Content-Length"] = str(media.size)
    context = current()

    def chunks():
        remaining = media.size
        with open(media.path, "rb") as handle:
            while remaining > 0:
                context.check()
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    raise SpoofzyError("{} changed while it was being uploaded.".format(media.path.name))
                remaining -= len(block)
                if on_progress:
                    on_progress((media.size - remaining) / media.size)
                yield block

    failure = None
    for attempt in range(3):
        context.check()
        if attempt:
            context.sleep(2 * attempt)
        try:
            connection, response = http_open("PUT", parts, headers, chunks(), timeout=120)
            try:
                response.read(65536)
                status = response.status
            finally:
                connection.close()
        except ssl.SSLCertVerificationError:
            raise SpoofzyError(CERT_HELP, code="tls_error") from None
        except (OSError, http.client.HTTPException) as error:
            failure = SpoofzyError("Uploading {} failed ({}). Check the connection and try again.".format(
                media.path.name, type(error).__name__), code="upload_failed")
            continue
        if status in (200, 201, 204):
            return
        failure = SpoofzyError("Uploading {} to Spoofzy storage failed (HTTP {}). Try again.".format(
            media.path.name, status), code="upload_failed", status=status)
        if status < 500:
            break
    raise failure


# ---------------------------------------------------------------------------
# Local media files
# ---------------------------------------------------------------------------

def human_size(size):
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit == "bytes" else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{} bytes".format(size)


def sniff(head):
    """Media type from a file's first bytes, or None when it is not supported media."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return None
        if brand in HEIF_BRANDS:
            return "image/heic" if brand in (b"heic", b"heix", b"heim", b"heis") else "image/heif"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        return "video/quicktime"
    return None


def safe_stem(name):
    """An ASCII file-name stem that is safe on every platform."""
    stem = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return stem[:80].rstrip("-._") or "media"


class Media:
    """A validated local image or video, ready to upload."""

    def __init__(self, path, size, content_type, upload_name, stem):
        self.path = path
        self.size = size
        self.content_type = content_type
        self.upload_name = upload_name
        self.stem = stem
        self.kind = "video" if content_type.startswith("video/") else "image"


def absolute_path(raw):
    path = Path(os.path.expanduser(str(raw).strip()))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def expand_paths(values):
    """Turn file and folder arguments into a de-duplicated list of files."""
    found, seen = [], set()
    for raw in values:
        path = absolute_path(raw)
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            try:
                entries = sorted(child for child in path.iterdir()
                                 if not child.name.startswith(".") and child.suffix.lower() in EXTENSION_TYPES
                                 and child.is_file())
            except PermissionError:
                raise SpoofzyError(permission_message(path)) from None
            if not entries:
                raise SpoofzyError("No supported images or videos ({}) were found directly inside {}.".format(
                    SUPPORTED_TEXT, path))
            candidates = entries
        else:
            candidates = [path]
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    if not found:
        raise SpoofzyError("Pass at least one file path in files.")
    if len(found) > MAX_FILES:
        raise SpoofzyError("That is {} files; one call handles up to {}. Process them in smaller groups.".format(
            len(found), MAX_FILES))
    return found


def permission_message(path):
    return ("Permission denied reading {}. On macOS, allow the app that runs this MCP server (for example "
            "Claude) to access that folder in System Settings → Privacy & Security → Files and Folders, or "
            "move the file somewhere it can read.".format(path))


def inspect_media(path, backend):
    """Validate a local file for upload: it must be real, readable, supported media."""
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SpoofzyError("File not found: {}. Pass an absolute path.".format(path)) from None
    if not path.is_file():
        raise SpoofzyError("{} is not a file.".format(path))
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(64)
    except PermissionError:
        raise SpoofzyError(permission_message(path)) from None
    except OSError as error:
        raise SpoofzyError("Could not read {} ({}).".format(path, type(error).__name__)) from None
    if size <= 0:
        raise SpoofzyError("{} is empty.".format(path.name))
    detected = sniff(head)
    if not detected:
        raise SpoofzyError("{} is not a supported image or video ({}).".format(path.name, SUPPORTED_TEXT))
    extension = path.suffix.lower()
    declared = EXTENSION_TYPES.get(extension)
    same_family = declared and TYPE_FAMILIES.get(declared) and TYPE_FAMILIES.get(declared) == TYPE_FAMILIES.get(detected)
    if declared == detected or same_family:
        content_type, upload_extension = declared, extension
    else:
        # A mislabelled file (for example a PNG named .jpg) is sent under its real type.
        content_type, upload_extension = detected, TYPE_EXTENSIONS[detected]
    if backend == "cloud" and upload_extension == ".m4v":
        upload_extension = ".mp4"
    if content_type not in BACKEND_TYPES[backend]:
        if backend == "api":
            raise SpoofzyError("{} is {}; the prepaid Spoofer API takes JPG, PNG, WebP, MP4, M4V and MOV. Use a "
                               "Spoofzy Cloud key for {} files, or convert it.".format(
                                   path.name, TYPE_NAMES.get(content_type, content_type), TYPE_NAMES.get(content_type)))
        raise SpoofzyError("{} is not supported by Spoofzy Cloud.".format(path.name))
    kind = "video" if content_type.startswith("video/") else "image"
    if backend == "api" and size > PREPAID_MAX_BYTES[kind]:
        raise SpoofzyError("{} is {}; the prepaid Spoofer API accepts {}s up to {}.".format(
            path.name, human_size(size), kind, human_size(PREPAID_MAX_BYTES[kind])), code="file_too_large")
    stem = safe_stem(path.stem)
    return Media(path, size, content_type, stem + upload_extension, stem)


# ---------------------------------------------------------------------------
# Saving results
# ---------------------------------------------------------------------------

_NAME_LOCK = threading.Lock()


def output_extension(output, fallback):
    url_path = urllib.parse.unquote(urllib.parse.urlsplit(str(output.get("url") or "")).path)
    for candidate in (output.get("name"), url_path):
        if isinstance(candidate, str):
            extension = os.path.splitext(candidate)[1].lower()
            extension = ".jpg" if extension == ".jpeg" else extension
            if extension in OUTPUT_EXTENSIONS:
                return extension
    mime = str(output.get("mime") or "").split(";")[0].strip().lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    fallback = ".jpg" if fallback == ".jpeg" else fallback
    return fallback if fallback in OUTPUT_EXTENSIONS else ".bin"


def reserve_names(folder, stem, tag, extensions):
    """Claim the next free ``<stem>-<tag>-NN<ext>`` names in ``folder``; never reuses a name."""
    pattern = re.compile(re.escape(stem) + "-" + re.escape(tag) + r"-(\d+)(?:\.[A-Za-z0-9]+)+$")
    with _NAME_LOCK:
        highest = 0
        for entry in os.listdir(folder):
            match = pattern.match(entry)
            if match:
                highest = max(highest, int(match.group(1)))
        width = max(2, len(str(highest + len(extensions))))
        reserved, number = [], highest
        for extension in extensions:
            while True:
                number += 1
                final = folder / "{}-{}-{:0{}d}{}".format(stem, tag, number, width, extension)
                part = folder / (final.name + ".part")
                if final.exists() or part.exists():
                    continue
                try:
                    os.close(os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                except FileExistsError:
                    continue
                reserved.append((final, part))
                break
        return reserved


def publish(part, final, content_type=""):
    """Move a finished download into place without ever replacing an existing file."""
    if final.suffix == ".bin":
        guessed = MIME_EXTENSIONS.get(content_type.split(";")[0].strip().lower())
        if guessed:
            final = final.with_suffix(guessed)
    candidate = final
    for attempt in range(2, 200):
        try:
            os.link(str(part), str(candidate))
        except FileExistsError:
            candidate = final.with_name("{}-{}{}".format(final.stem, attempt, final.suffix))
            continue
        except OSError:
            # File systems without hard links (for example exFAT): rename while the name is free.
            if candidate.exists():
                candidate = final.with_name("{}-{}{}".format(final.stem, attempt, final.suffix))
                continue
            os.replace(str(part), str(candidate))
            return candidate
        os.unlink(str(part))
        return candidate
    raise SpoofzyError("Could not find a free file name in {}.".format(final.parent))


def download(url, part):
    """Download one result link into ``part``; storage links never receive an API key."""
    context = current()
    failure = None
    for attempt in range(3):
        if attempt:
            context.sleep(2 * attempt)
        try:
            return _download_once(url, part, context)
        except ssl.SSLCertVerificationError:
            raise SpoofzyError(CERT_HELP, code="tls_error") from None
        except SpoofzyError as error:
            if error.status and error.status < 500 and error.status != 429:
                raise
            failure = error
        except (OSError, http.client.HTTPException) as error:
            failure = SpoofzyError("Downloading a result failed ({}).".format(type(error).__name__))
    raise failure


def _download_once(url, part, context):
    location = url
    for _ in range(5):
        parts = checked_url(location, "download")
        connection, response = http_open("GET", parts, {"User-Agent": USER_AGENT, "Accept": "*/*"}, None,
                                         timeout=90)
        try:
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader("Location")
                if not target:
                    raise SpoofzyError("A result link redirected without a destination.")
                location = urllib.parse.urljoin(location, target)
                continue
            if response.status != 200:
                raise SpoofzyError(
                    "Downloading a result failed (HTTP {}). Result links expire; fetch fresh ones with "
                    "spoofzy_download.".format(response.status), status=response.status)
            size = 0
            with open(part, "wb") as handle:
                while True:
                    context.check()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise SpoofzyError("A result is larger than 4 GB, so it was not saved.")
                    handle.write(block)
            if size == 0:
                raise SpoofzyError("A result file was empty.", status=502)
            return response.getheader("Content-Type") or ""
        finally:
            connection.close()
    raise SpoofzyError("A result link redirected too many times.")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

KIND_LABELS = {
    "job": "Spoofzy Cloud job", "stack": "Spoofzy Cloud Studio run", "workflow": "Spoofzy Cloud workflow run",
    "api-job": "Spoofer API job", "api-batch": "Spoofer API batch",
}
TERMINAL = frozenset({"completed", "partial", "failed"})


class Run:
    """One piece of Spoofzy processing that can be polled and downloaded."""

    def __init__(self, backend, kind, run_id, source=None, stem=None, tag="spoofzy", request_id=None,
                 workflow_id=None, expected=None, out_dir=None, filename=None):
        self.backend = backend
        self.kind = kind
        self.id = run_id
        self.source = source
        self.filename = filename or (source.name if source else None)
        self._stem = stem
        self.tag = tag
        self.auto_save = False
        self.request_id = request_id
        self.workflow_id = workflow_id
        self.expected = expected
        self.out_dir = out_dir
        self.status = "starting"
        self.message = ""
        self.uncertain = False
        self.outputs = []
        self.saved_map = {}
        self.download_error = ""
        self.credits = None
        self.units = None
        self.created = time.time()
        self.empty_ready_polls = 0
        self.save_lock = threading.Lock()

    @property
    def stem(self):
        if self._stem:
            return self._stem
        return safe_stem(Path(self.filename).stem if self.filename else (self.id or "spoofzy"))

    @property
    def terminal(self):
        return self.status in TERMINAL

    @property
    def saved(self):
        return [self.saved_map[index] for index in sorted(self.saved_map)]

    @property
    def fully_saved(self):
        return bool(self.outputs) and len(self.saved_map) >= len(self.outputs)

    def set(self, status, message=""):
        self.status = status
        self.message = str(message or "")[:600]

    @classmethod
    def failed(cls, backend, source, request_id, message, uncertain=False):
        run = cls(backend, "file", None, source=source, request_id=request_id)
        run.set("failed", message)
        run.uncertain = uncertain
        return run

    def public(self):
        data = {"run_id": self.id, "type": KIND_LABELS.get(self.kind, "file"), "status": self.status}
        if self.source:
            data["source"] = str(self.source)
        elif self.filename:
            data["filename"] = self.filename
        if self.message:
            data["message"] = self.message
        if self.uncertain:
            data["outcome_uncertain"] = True
        if self.expected:
            data["versions"] = self.expected
        if self.id:
            data["outputs_ready"] = len(self.outputs)
        saved = self.saved
        if saved:
            data["saved_folder"] = str(saved[0].parent)
            data["saved_count"] = len(saved)
            data["saved_files"] = [str(path) for path in saved[:20]]
            if len(saved) > 20:
                data["saved_files_truncated"] = True
        elif self.outputs and self.terminal:
            data["result_links"] = [output["url"] for output in self.outputs[:10]]
        if self.download_error:
            data["download_error"] = self.download_error
        if self.credits is not None:
            data["credits"] = self.credits
        if self.units is not None:
            data["units_charged"] = self.units
        if self.workflow_id:
            data["workflow_id"] = self.workflow_id
        if self.request_id:
            data["request_id"] = self.request_id
        return data


class Registry:
    """Runs seen by this server process, so later calls can find, poll and save them."""

    def __init__(self, limit=500):
        self._runs = collections.OrderedDict()
        self._lock = threading.Lock()
        self._limit = limit

    def add(self, run):
        if not run.id:
            return run
        with self._lock:
            existing = self._runs.get(run.id)
            if existing is not None:
                return existing
            self._runs[run.id] = run
            while len(self._runs) > self._limit:
                self._runs.popitem(last=False)
            return run

    def get(self, run_id):
        with self._lock:
            return self._runs.get(run_id)

    def for_request(self, backend, request_id):
        with self._lock:
            return [run for run in self._runs.values() if run.backend == backend and run.request_id == request_id]

    def recent(self, limit):
        with self._lock:
            return list(reversed(self._runs.values()))[:limit]


def collect_outputs(value):
    """Result files from a job's ``result`` object, whatever its exact shape."""
    found, archives, seen = [], [], set()

    def walk(item):
        if isinstance(item, dict):
            url = item.get("download_url") or item.get("url")
            if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in seen:
                seen.add(url)
                size = item.get("size")
                if not isinstance(size, int) and isinstance(item.get("size_mb"), (int, float)):
                    size = int(item["size_mb"] * 1024 * 1024)
                found.append({"url": url, "name": item.get("name") or item.get("filename"), "size": size,
                              "mime": item.get("mime")})
            zip_url = item.get("zip_url")
            if isinstance(zip_url, str) and zip_url.startswith(("https://", "http://")):
                archives.append({"url": zip_url, "name": "spoofzy.zip"})
            for key, child in item.items():
                if key not in ("download_url", "url", "preview_url", "zip_url"):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found or archives[:1]


def apply_cloud_job(run, entry):
    start = entry.get("start") if isinstance(entry.get("start"), dict) else {}
    job = entry.get("job") if isinstance(entry.get("job"), dict) else None
    if isinstance(start.get("credits"), int):
        run.credits = start["credits"]
    if job is None:
        if start.get("status") == "rejected":
            run.set("failed", start.get("reason") or "Spoofzy did not start this run. Any Credits were returned.")
        else:
            run.set("starting", start.get("reason") or "")
        return
    run.filename = run.filename or job.get("filename")
    status = str(job.get("status") or "queued")
    if status == "ready":
        run.outputs = collect_outputs(job.get("result"))
        if run.outputs:
            run.set("completed")
        else:
            # A finished job's output link can lag its status briefly.
            run.empty_ready_polls += 1
            if run.empty_ready_polls >= 5:
                run.set("failed", "Spoofzy reported this job finished but returned no result files.")
            else:
                run.set("processing", "Finishing up")
    elif status == "failed":
        run.set("failed", job.get("error") or start.get("reason") or "Processing failed. Any Credits were returned.")
    else:
        run.set("processing" if status == "processing" else "queued")


def apply_cloud_chain(run, chain):
    if not isinstance(chain, dict):
        raise SpoofzyError("Spoofzy Cloud returned an unexpected run.")
    run.filename = run.filename or chain.get("filename")
    if isinstance(chain.get("creditCost"), int):
        run.credits = chain["creditCost"]
    if isinstance(chain.get("count"), int) and chain["count"] > 0:
        run.expected = chain["count"]
    outputs = [{"url": item["url"], "name": item.get("name"), "size": item.get("size"), "mime": item.get("mime")}
               for item in chain.get("files") or [] if isinstance(item, dict) and isinstance(item.get("url"), str)]
    if not outputs:
        outputs = [{"url": item["url"], "name": "spoofzy-part-{:02d}.zip".format(index + 1)}
                   for index, item in enumerate(chain.get("archives") or [])
                   if isinstance(item, dict) and isinstance(item.get("url"), str)]
    status = str(chain.get("status") or "queued")
    reason = chain.get("reason") or ""
    if status == "completed":
        run.outputs = outputs
        run.set("completed" if outputs else "failed", "" if outputs else (reason or "The run finished without files."))
    elif status == "failed":
        run.outputs = outputs
        run.set("partial" if outputs else "failed", reason or "A step could not finish. Unused Credits were not charged.")
    elif status == "waiting":
        capacity = chain.get("capacity") if isinstance(chain.get("capacity"), dict) else {}
        run.set("waiting", "Waiting for a free processing slot on this plan." if capacity.get("queued") else reason)
    else:
        run.set("processing" if status == "processing" else "queued", reason)


def apply_api_job(run, job):
    if not isinstance(job, dict):
        raise SpoofzyError("The Spoofer API returned an unexpected job.")
    if isinstance(job.get("units_charged"), int):
        run.units = job["units_charged"]
    status = str(job.get("status") or "queued")
    if status == "completed":
        url = (job.get("result") or {}).get("download_url") if isinstance(job.get("result"), dict) else None
        run.outputs = [{"url": url}] if isinstance(url, str) and url else []
        run.set("completed" if run.outputs else "failed", "" if run.outputs else "The job finished without a file.")
    elif status == "failed":
        error = job.get("error") if isinstance(job.get("error"), dict) else {}
        run.units = 0
        run.set("failed", error.get("message") or "The Spoofer could not complete this job. The unit was returned.")
    else:
        run.set("processing" if status == "processing" else "queued")


def apply_api_batch(run, batch):
    if not isinstance(batch, dict):
        raise SpoofzyError("The Spoofer API returned an unexpected batch.")
    jobs = sorted((job for job in batch.get("jobs") or [] if isinstance(job, dict)), key=lambda job: job.get("index", 0))
    run.outputs = [{"url": job["result"]["download_url"]} for job in jobs
                   if job.get("status") == "completed" and isinstance(job.get("result"), dict)
                   and isinstance(job["result"].get("download_url"), str)]
    if isinstance(batch.get("variants"), int):
        run.expected = batch["variants"]
    if isinstance(batch.get("units_reserved"), int):
        run.units = batch["units_reserved"] - int(batch.get("units_refunded") or 0)
    counts = batch.get("counts") if isinstance(batch.get("counts"), dict) else {}
    summary = "{} done, {} failed, {} in progress".format(counts.get("completed", 0), counts.get("failed", 0),
                                                          counts.get("queued", 0)) if counts else ""
    status = str(batch.get("status") or "processing")
    if status in TERMINAL:
        run.set(status, "" if status == "completed" else summary + ". Failed versions refunded their units.")
    else:
        run.set("processing", summary)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def plan_of(**fields):
    return types.SimpleNamespace(**fields)


class CloudBackend:
    """The Spoofzy Cloud Developer API (https://cloud.spoofzy.com/developer/v1)."""

    name = "cloud"
    label = "Spoofzy Cloud"

    def __init__(self, settings):
        if not settings.cloud_key:
            raise SpoofzyError(not_configured_message(), code="not_configured")
        if not settings.cloud_url:
            raise SpoofzyError(" ".join(settings.url_problems), code="bad_configuration")
        # Spoofzy allows 45 requests a minute per account, shared by all of its keys.
        self.client = ApiClient(self.label, settings.cloud_url, settings.cloud_key, per_minute=40)
        self._capabilities = None
        self._capabilities_at = 0.0
        self._lock = threading.Lock()

    # -- account ----------------------------------------------------------
    def capabilities(self, refresh=False):
        with self._lock:
            fresh = self._capabilities is not None and time.monotonic() - self._capabilities_at < 120
            if fresh and not refresh:
                return self._capabilities
        data = self.client.call("GET", "capabilities", attempts=3)
        items = {item["id"]: item for item in data.get("capabilities") or []
                 if isinstance(item, dict) and isinstance(item.get("id"), str)}
        with self._lock:
            self._capabilities, self._capabilities_at = items, time.monotonic()
        return items

    def require(self, tool, purpose):
        capability = self.capabilities().get(tool)
        if capability is not None and not capability.get("available"):
            raise SpoofzyError("{} is not available on this Spoofzy account: {}".format(
                purpose, capability.get("reason") or "it is not included in the current plan."),
                code=capability.get("reasonCode") or "tool_unavailable")
        return capability or {}

    # -- spoofing -----------------------------------------------------------
    def plan_spoof(self, options, file_count):
        versions = options["versions"]
        if versions not in CLOUD_VERSIONS:
            raise SpoofzyError("Spoofzy Cloud makes 1, 5, 10, 20, 30, 40 or 50 versions per file, so {} is not "
                               "possible. Pick one of those (for example 5, then keep the ones you need).".format(versions),
                               code="invalid_versions")
        studio = (options["look"] != "classic" or options["export_size"] != "original"
                  or bool(options.get("watermark")) or bool(options.get("border")))
        capabilities = self.capabilities()
        tool = "spoofer" if versions == 1 else "spoofer-many"
        job = capabilities.get(tool) or {}
        stack = capabilities.get("chains") or {}
        if not studio and job.get("available"):
            job_options = {"vertical_flip": options["mirror"]}
            if versions > 1:
                job_options["variants"] = versions
            return plan_of(mode="job", tool=tool, options=job_options, versions=versions,
                           credits=job.get("creditCost") or job.get("standardCreditCost") or 0)
        if stack.get("available"):
            stage = {"type": "multiply", "versions": versions, "preset": LOOKS[options["look"]],
                     "mirror": options["mirror"], "export": options["export_size"]}
            if options.get("watermark"):
                stage["watermark"] = options["watermark"]
            if options.get("border"):
                stage["border"] = options["border"]
            return plan_of(mode="stack", stage=stage, versions=versions, credits=0)
        if studio:
            reason = stack.get("reason") or "it needs a Spoofzy membership."
            raise SpoofzyError("Studio looks, export sizes, watermarks and borders are not available on this account: "
                               "{} Use look=classic and export_size=original without watermark/border, or upgrade "
                               "in Spoofzy Cloud.".format(reason), code=stack.get("reasonCode") or "membership_required")
        reason = job.get("reason") or "it is not included in the current plan."
        raise SpoofzyError("The Spoofer{} is not available on this account: {}".format(
            "" if versions == 1 else " (several versions)", reason), code=job.get("reasonCode") or "tool_unavailable")

    def upload(self, media, tool_id, on_progress=None):
        data = self.client.call("POST", "uploads", {"tool_id": tool_id, "filename": media.upload_name,
                                                    "content_type": media.content_type}, attempts=3)
        ticket = data.get("upload")
        put_file(ticket, media, self.label, on_progress)
        return ticket["id"]

    def start_spoof(self, media, request_id, plan, sink, on_progress=None):
        if plan.mode == "job":
            sink(self.start_job(media, plan.tool, plan.options, request_id, "spoofzy", on_progress, plan.versions))
        else:
            upload_id = self.upload(media, "spoofer", on_progress)
            body = {"request_id": request_id, "upload_id": upload_id, "filename": media.upload_name,
                    "stages": [plan.stage]}
            sink(self._start_chain(body, media, request_id, plan.versions))

    def start_job(self, media, tool, options, request_id, tag, on_progress=None, versions=None):
        upload_id = self.upload(media, tool, on_progress)
        body = {"request_id": request_id, "tool_id": tool, "upload_id": upload_id,
                "filename": media.upload_name, "options": options}
        try:
            entry = self.client.call("POST", "jobs", body, attempts=4)
        except SpoofzyError as error:
            if error.code == "idempotency_conflict":
                found = self.find_runs(request_id, media, tag)
                if found:
                    return found[0]
            raise
        start = entry.get("start") if isinstance(entry.get("start"), dict) else {}
        run_id = start.get("jobId") or (entry.get("job") or {}).get("id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise SpoofzyError("Spoofzy Cloud accepted the run but returned no job id.", uncertain=True)
        run = Run(self.name, "job", run_id, source=media.path, stem=media.stem, tag=tag, request_id=request_id,
                  expected=versions)
        apply_cloud_job(run, entry)
        return run

    def _start_chain(self, body, media, request_id, versions):
        try:
            data = self.client.call("POST", "chains", body, attempts=4)
        except SpoofzyError as error:
            if error.code == "idempotency_conflict":
                found = self.find_runs(request_id, media, "spoofzy")
                if found:
                    return found[0]
            raise
        chain = data.get("chain")
        if not isinstance(chain, dict) or not RUN_ID_RE.fullmatch(str(chain.get("id") or "")):
            raise SpoofzyError("Spoofzy Cloud accepted the run but returned no run id.", uncertain=True)
        run = Run(self.name, "stack", chain["id"], source=media.path, stem=media.stem, request_id=request_id,
                  expected=versions)
        apply_cloud_chain(run, chain)
        return run

    def find_runs(self, request_id, media, tag):
        """Runs this account already started with ``request_id`` (for safe retries)."""
        found = []
        for path, field in (("jobs", "entries"), ("chains", "chains")):
            try:
                rows = self.client.call("GET", path, attempts=2).get(field) or []
            except SpoofzyError:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if field == "entries":
                    start = row.get("start") if isinstance(row.get("start"), dict) else {}
                    if start.get("requestId") == request_id and RUN_ID_RE.fullmatch(str(start.get("jobId") or "")):
                        run = Run(self.name, "job", start["jobId"], source=media.path if media else None,
                                  stem=media.stem if media else None, tag=tag, request_id=request_id)
                        apply_cloud_job(run, row)
                        found.append(run)
                elif row.get("requestId") == request_id and RUN_ID_RE.fullmatch(str(row.get("id") or "")):
                    run = Run(self.name, "stack", row["id"], source=media.path if media else None,
                              stem=media.stem if media else None, tag=tag, request_id=request_id)
                    apply_cloud_chain(run, row)
                    found.append(run)
        return found

    # -- workflows ---------------------------------------------------------
    def workflow(self, workflow_id):
        data = self.client.call("GET", "automations/" + workflow_id, attempts=3)
        workflow = data.get("workflow")
        if not isinstance(workflow, dict):
            raise SpoofzyError("Spoofzy Cloud returned an unexpected workflow.")
        return workflow

    def start_workflow(self, workflow_id, version, request_id, tag, media=None, master_id=None, text=None,
                       on_progress=None):
        body = {"request_id": request_id, "version": version}
        if media is not None:
            ticket = self.client.call("POST", "automations/{}/uploads".format(workflow_id), {
                "version": version, "filename": media.upload_name, "content_type": media.content_type}, attempts=3)
            put_file(ticket.get("upload"), media, self.label, on_progress)
            body.update(upload_id=ticket["upload"]["id"], filename=media.upload_name)
        elif master_id is not None:
            body["master_id"] = master_id
        else:
            body["text"] = text
        data = self.client.call("POST", "automations/{}/runs".format(workflow_id), body, attempts=4)
        chain = data.get("chain")
        if not isinstance(chain, dict) or not RUN_ID_RE.fullmatch(str(chain.get("id") or "")):
            raise SpoofzyError("Spoofzy Cloud accepted the workflow run but returned no run id.", uncertain=True)
        if media is not None:
            stem = media.stem
        else:
            stem = "saved-" + master_id[:8] if master_id else "script"
        run = Run(self.name, "workflow", chain["id"], source=media.path if media else None, stem=stem, tag=tag,
                  request_id=request_id, workflow_id=workflow_id)
        apply_cloud_chain(run, chain)
        return run

    # -- polling ----------------------------------------------------------
    def refresh(self, run):
        if run.kind == "job":
            apply_cloud_job(run, self.client.call("GET", "jobs/" + run.id, attempts=3))
        elif run.kind == "workflow" and run.workflow_id:
            data = self.client.call("GET", "automations/{}/runs/{}".format(run.workflow_id, run.id), attempts=3)
            apply_cloud_chain(run, data.get("chain"))
        else:
            apply_cloud_chain(run, self.client.call("GET", "chains/" + run.id, attempts=3).get("chain"))


class PrepaidBackend:
    """The prepaid Spoofzy Spoofer API (https://api.spoofzy.com/api/v1)."""

    name = "api"
    label = "Spoofzy Spoofer API"

    def __init__(self, settings):
        if not settings.api_key:
            raise SpoofzyError(not_configured_message(), code="not_configured")
        if not settings.api_url:
            raise SpoofzyError(" ".join(settings.url_problems), code="bad_configuration")
        self.client = ApiClient(self.label, settings.api_url, settings.api_key, per_minute=100)
        self.owner = API_KEY_RE.fullmatch(settings.api_key).group(1)
        self.slot_poll_seconds = 0.05 if settings.fast_poll else 5.0

    def account(self):
        data = self.client.call("GET", "spoofer/account", attempts=3)
        account = data.get("account")
        if not isinstance(account, dict):
            raise SpoofzyError("The Spoofer API returned an unexpected account.")
        return account

    @staticmethod
    def key_for(request_id, version=None):
        return "mcp-" + request_id + ("-v{}".format(version) if version else "")

    def job_id(self, key):
        return "api_" + hashlib.sha256("{}:{}".format(int(self.owner), key).encode("utf-8")).hexdigest()[:32]

    def batch_id(self, key):
        return "batch_" + hashlib.sha256("{}:{}".format(int(self.owner), key).encode("utf-8")).hexdigest()[:32]

    def plan_spoof(self, options, file_count):
        if options["look"] in ("subtle", "stronger"):
            raise SpoofzyError("The prepaid Spoofer API has the classic and minimal looks. Use a Spoofzy Cloud key "
                               "for the {} look.".format(options["look"]), code="unsupported_option")
        if options.get("watermark") or options.get("border"):
            raise SpoofzyError("The prepaid Spoofer API always uses the account's saved watermark; per-run "
                               "watermarks and borders need a Spoofzy Cloud key.", code="unsupported_option")
        preset = "minimal" if options["look"] == "minimal" else "standard"
        if preset == "minimal" and options["mirror"]:
            raise SpoofzyError("look=minimal keeps the existing layout, so it cannot be combined with mirror=true.",
                               code="unsupported_option")
        versions, export = options["versions"], options["export_size"]
        if not 1 <= versions <= PREPAID_MAX_BATCH:
            raise SpoofzyError("The prepaid Spoofer API makes 1 to 300 versions per file.", code="invalid_versions")
        account = self.account()
        units = account.get("units_remaining")
        needed = versions * file_count
        if isinstance(units, int) and units < needed:
            raise SpoofzyError("This needs {} prepaid units, but the account has {} left. Top up with /api in the "
                               "Spoofzy Telegram bot, or make fewer versions.".format(needed, units),
                               code="insufficient_units")
        batch_limit = account.get("batch_limit") if isinstance(account.get("batch_limit"), int) else 0
        use_batch = (versions > 1 or export != "original") and batch_limit >= versions
        if export != "original" and not use_batch:
            raise SpoofzyError("export_size {} needs prepaid batch access, which this account does not have for {} "
                               "version(s). Use original, or a Spoofzy Cloud key.".format(export, versions),
                               code="batch_not_enabled")
        job_options = {"preset": preset, "vertical_flip": options["mirror"]}
        if use_batch:
            return plan_of(mode="api-batch", versions=versions, credits=0,
                           options=dict(job_options, output_size=export))
        if versions > PREPAID_MAX_JOB_VERSIONS:
            raise SpoofzyError("Without batch access the prepaid Spoofer API runs one job per version, up to {} "
                               "per file.".format(PREPAID_MAX_JOB_VERSIONS), code="batch_not_enabled")
        return plan_of(mode="api-job" if versions == 1 else "api-jobs", versions=versions, credits=0,
                       options=job_options)

    def upload(self, media, on_progress=None):
        data = self.client.call("POST", "spoofer/uploads", {"filename": media.upload_name,
                                                             "content_type": media.content_type}, attempts=3)
        ticket = data.get("upload")
        put_file(ticket, media, self.label, on_progress)
        return ticket["id"]

    def existing(self, request_id, plan, media):
        """Already-started runs for an explicit request id (job and batch ids are derived from the key)."""
        found = []
        if plan.mode == "api-batch":
            candidates = [("api-batch", self.batch_id(self.key_for(request_id)))]
        elif plan.mode == "api-job":
            candidates = [("api-job", self.job_id(self.key_for(request_id)))]
        else:
            candidates = [("api-job", self.job_id(self.key_for(request_id, n))) for n in range(1, plan.versions + 1)]
        for kind, run_id in candidates:
            run = Run(self.name, kind, run_id, source=media.path, stem=media.stem, request_id=request_id,
                      expected=plan.versions if kind == "api-batch" else 1)
            try:
                self.refresh(run)
            except SpoofzyError as error:
                if error.status == 404:
                    break
                raise
            found.append(run)
        return found

    def start_spoof(self, media, request_id, plan, sink, on_progress=None, already=()):
        if len(already) >= (plan.versions if plan.mode == "api-jobs" else 1):
            return
        upload_id = self.upload(media, on_progress)
        if plan.mode == "api-batch":
            key = self.key_for(request_id)
            body = {"upload_id": upload_id, "filename": media.upload_name, "variants": plan.versions,
                    "options": plan.options}
            try:
                data = self.client.call("POST", "spoofer/batches", body, headers={"Idempotency-Key": key}, attempts=4)
                batch = data.get("batch")
                run_id = batch.get("id") if isinstance(batch, dict) else None
            except SpoofzyError as error:
                if error.code != "idempotency_conflict":
                    raise
                batch, run_id = None, self.batch_id(key)
            if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
                raise SpoofzyError("The Spoofer API accepted the batch but returned no id.", uncertain=True)
            run = Run(self.name, "api-batch", run_id, source=media.path, stem=media.stem, request_id=request_id,
                      expected=plan.versions)
            if batch:
                apply_api_batch(run, batch)
            else:
                self.refresh(run)
            sink(run)
            return
        mine = list(already)
        first = len(already) + 1 if plan.mode == "api-jobs" else 1
        last = plan.versions if plan.mode == "api-jobs" else 1
        for version in range(first, last + 1):
            key = self.key_for(request_id, version if plan.mode == "api-jobs" else None)
            while True:
                try:
                    data = self.client.call("POST", "spoofer/jobs", {
                        "upload_id": upload_id, "filename": media.upload_name, "options": plan.options},
                        headers={"Idempotency-Key": key}, attempts=4)
                    break
                except SpoofzyError as error:
                    # Concurrency limit: wait for one of this call's own jobs to finish, then retry the same key.
                    if error.code != "quota_or_concurrency_reached" or not any(not r.terminal for r in mine):
                        raise
                    self.wait_for_slot(mine)
            job = data.get("job")
            if not isinstance(job, dict) or not RUN_ID_RE.fullmatch(str(job.get("id") or "")):
                raise SpoofzyError("The Spoofer API accepted the job but returned no id.", uncertain=True)
            run = Run(self.name, "api-job", job["id"], source=media.path, stem=media.stem, request_id=request_id,
                      expected=1)
            apply_api_job(run, job)
            mine.append(run)
            sink(run)

    def wait_for_slot(self, runs):
        context = current()
        deadline = time.monotonic() + 20 * 60
        context.progress(0.3, "Waiting for a free Spoofer API job slot")
        while time.monotonic() < deadline:
            context.sleep(self.slot_poll_seconds)
            for run in runs:
                if not run.terminal:
                    self.refresh(run)
                    if run.terminal:
                        return
        raise SpoofzyError("No Spoofer API job slot became free in 20 minutes.", code="quota_or_concurrency_reached")

    def refresh(self, run):
        if run.kind == "api-batch":
            apply_api_batch(run, self.client.call("GET", "spoofer/batches/" + run.id, attempts=3).get("batch"))
        else:
            apply_api_job(run, self.client.call("GET", "spoofer/jobs/" + run.id, attempts=3).get("job"))


# ---------------------------------------------------------------------------
# Tool argument schemas
# ---------------------------------------------------------------------------

FILES = {
    "type": "array", "minItems": 1, "maxItems": MAX_FILES,
    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
    "description": "Absolute paths of local images/videos, or folders (every supported file directly inside). "
                   "Up to {} files per call. Supported: {}.".format(MAX_FILES, SUPPORTED_TEXT),
}
FILE = {"type": "string", "minLength": 1, "maxLength": 4096, "description": "One file path; same as files: [path]."}
OUTPUT_DIR = {"type": "string", "minLength": 1, "maxLength": 4096,
              "description": "Folder for the results (created if needed). Default: a spoofzy-output folder next to "
                             "each source file, or the configured SPOOFZY_OUTPUT_DIR."}
WAIT = {"type": "integer", "minimum": 0, "maximum": 3600,
        "description": "Seconds to wait for processing before returning (default from SPOOFZY_WAIT_SECONDS, "
                       "normally 50). Unfinished runs keep processing and are saved when ready while this server "
                       "runs; check them with spoofzy_get_run."}
DOWNLOAD = {"type": "boolean", "description": "Save the results locally (default true). False returns temporary "
                                              "result links instead."}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$",
              "description": "Only to retry a call whose outcome was uncertain: pass the request_id it returned and "
                             "Spoofzy returns the same runs instead of charging again. Leave empty for new runs."}
BACKEND = {"type": "string", "enum": ["auto", "cloud", "api"],
           "description": "Which Spoofzy API to use: cloud (Spoofzy Cloud key), api (prepaid Spoofer API key) or "
                          "auto (default: Cloud when configured)."}
RUN_ID = {"type": "string", "pattern": "^(mjob|cc|api|batch)_[a-f0-9]{32}$",
          "description": "A run id from an earlier result (mjob_…, cc_…, api_… or batch_…)."}
WORKFLOW_ID = {"type": "string", "pattern": "^wa_[a-f0-9]{32}$", "description": "A workflow id (wa_…)."}
WATERMARK = {
    "type": "object", "additionalProperties": False,
    "description": "Spoofzy Cloud Studio only. Default: the account's saved watermark setting.",
    "properties": {
        "mode": {"type": "string", "enum": ["account", "off", "text", "logo"],
                 "description": "account = saved setting, off = none, text = the text below, logo = the Cloud logo."},
        "text": {"type": "string", "maxLength": 128, "description": "Watermark text (mode text)."},
        "placement": {"type": "string", "enum": ["auto", "light", "edge", "top_left", "top_right", "bottom_left",
                                                 "bottom_right", "center"]},
        "size": {"type": "string", "enum": ["small", "medium", "large"]},
        "opacity": {"type": "string", "enum": ["subtle", "clear"]},
    },
}
BORDER = {
    "type": "object", "additionalProperties": False,
    "description": "Spoofzy Cloud Studio only. Adds a coloured border.",
    "properties": {
        "color": {"type": "string", "enum": ["off", "light", "dark", "pink", "blue", "yellow", "purple", "green",
                                            "red", "orange"]},
        "width": {"type": "string", "enum": ["slim", "regular", "thick"]},
        "style": {"type": "string", "enum": ["square", "rounded"]},
    },
}


def object_schema(properties, required=()):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


TOOLS = [
    {
        "name": "spoofzy_account",
        "title": "Spoofzy account and limits",
        "description": "Check the Spoofzy connection: which keys are configured, the plan, which tools are "
                       "available (Spoofer, many versions, Studio looks, Metadata Cleaner, workflows), what they "
                       "cost, file limits, prepaid units and where results are saved. Call it first in a "
                       "conversation and whenever a run is refused. Free and read-only.",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        "handler": "tool_account",
    },
    {
        "name": "spoofzy_spoof",
        "title": "Spoof images and videos",
        "description": "Make unique versions of local images or videos with the Spoofzy Spoofer. Uploads each "
                       "file to the user's Spoofzy account, processes it and saves the results as "
                       "<name>-spoofzy-NN.<ext> in a spoofzy-output folder next to the source (or output_dir). "
                       "Spoofzy Cloud makes 1, 5, 10, 20, 30, 40 or 50 versions per file; the prepaid Spoofer API "
                       "makes 1 (up to 300 with batch access, otherwise one job per version). This uses the "
                       "account's allowance: confirm the files, versions and settings with the user first. Returns "
                       "when done or after wait_seconds; unfinished runs keep going and can be checked with "
                       "spoofzy_get_run.",
        "inputSchema": object_schema({
            "files": FILES, "file": FILE,
            "versions": {"type": "integer", "minimum": 1, "maximum": PREPAID_MAX_BATCH,
                         "description": "Versions per file (default 1). Spoofzy Cloud: 1, 5, 10, 20, 30, 40 or 50."},
            "look": {"type": "string", "enum": list(LOOKS),
                     "description": "classic (default) = standard Spoofzy processing. subtle = lighter changes. "
                                    "stronger = adds small rotation, perspective and framing changes. minimal = "
                                    "keeps existing text and layout (no mirror, rotation or crop), best for "
                                    "captioned reels and images. subtle/stronger need a Cloud membership."},
            "mirror": {"type": "boolean", "description": "Mirror the media like Spoofzy's Mirror switch (default "
                                                         "false). Flips any visible text; not allowed with minimal."},
            "export_size": {"type": "string", "enum": ["original", "1080p", "4k"],
                            "description": "Output size (default original). Other sizes need Cloud Studio or "
                                           "prepaid batch access."},
            "watermark": WATERMARK, "border": BORDER, "output_dir": OUTPUT_DIR, "wait_seconds": WAIT,
            "download": DOWNLOAD, "backend": BACKEND, "request_id": REQUEST_ID,
        }),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
        "handler": "tool_spoof",
    },
    {
        "name": "spoofzy_clean_metadata",
        "title": "Strip photo and video metadata",
        "description": "Remove hidden metadata (GPS location, device and camera details, capture dates) from "
                       "local photos or videos with Spoofzy's Metadata Cleaner (needs a Spoofzy Cloud key). Saves "
                       "cleaned copies as <name>-clean-NN.<ext> in a spoofzy-output folder next to each source (or "
                       "output_dir); originals are never modified. Uses the account's allowance: confirm first.",
        "inputSchema": object_schema({
            "files": FILES, "file": FILE,
            "remove_gps": {"type": "boolean", "description": "Remove location data (default true)."},
            "remove_device": {"type": "boolean", "description": "Remove device/camera details (default true)."},
            "remove_datetime": {"type": "boolean", "description": "Remove capture dates (default true)."},
            "output_dir": OUTPUT_DIR, "wait_seconds": WAIT, "download": DOWNLOAD, "request_id": REQUEST_ID,
        }),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
        "handler": "tool_clean",
    },
    {
        "name": "spoofzy_get_run",
        "title": "Check a Spoofzy run",
        "description": "Check a Spoofzy run by id: status, how many results are ready and which files were "
                       "saved. Can wait for it to finish (wait_seconds). Runs started in this session are saved "
                       "automatically when they finish; use spoofzy_download for other runs. Read-only.",
        "inputSchema": object_schema({"run_id": RUN_ID, "workflow_id": WORKFLOW_ID, "wait_seconds": WAIT},
                                     required=["run_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        "handler": "tool_get_run",
    },
    {
        "name": "spoofzy_download",
        "title": "Download Spoofzy results",
        "description": "Save the results of a finished Spoofzy run into a local folder (never overwrites files). "
                       "Use it for runs started with download=false, in the Spoofzy Cloud app, by a workflow or in "
                       "an earlier session. Result links expire after a few days, so download promptly.",
        "inputSchema": object_schema({"run_id": RUN_ID, "workflow_id": WORKFLOW_ID, "output_dir": OUTPUT_DIR,
                                      "wait_seconds": WAIT}, required=["run_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
        "handler": "tool_download",
    },
    {
        "name": "spoofzy_list_runs",
        "title": "Recent Spoofzy runs",
        "description": "List recent Spoofzy runs: this session's runs (with their saved files) plus recent Spoofzy "
                       "Cloud jobs and Studio runs. Read-only.",
        "inputSchema": object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                                "description": "Most runs to list (default 10)."}}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        "handler": "tool_list_runs",
    },
    {
        "name": "spoofzy_list_workflows",
        "title": "Saved Spoofzy workflows",
        "description": "List saved Spoofzy Cloud workflows (reusable step stacks such as Caption → Spoof ×5): id, "
                       "current version, input type, steps and outputs per run. Needs a Cloud key with 'Read "
                       "workflows and history'. Read-only.",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        "handler": "tool_list_workflows",
    },
    {
        "name": "spoofzy_run_workflow",
        "title": "Run a Spoofzy workflow",
        "description": "Run a saved Spoofzy Cloud workflow on local files, on a saved original from the Spoofzy "
                       "library (saved_original_id) or on a script (text, for voice workflows), then save the "
                       "results. Uses the account's allowance: confirm with the user first. Needs a Cloud key with "
                       "'Run saved workflows' (plus 'Read workflows and history' to find the current version).",
        "inputSchema": object_schema({
            "workflow_id": WORKFLOW_ID, "files": FILES, "file": FILE,
            "saved_original_id": {"type": "string", "pattern": "^[a-f0-9]{32}$",
                                  "description": "Id from spoofzy_list_saved_originals."},
            "text": {"type": "string", "minLength": 1, "maxLength": 5000,
                     "description": "Script for workflows that take text."},
            "version": {"type": "integer", "minimum": 1,
                        "description": "Workflow version (default: the current version)."},
            "output_dir": OUTPUT_DIR, "wait_seconds": WAIT, "download": DOWNLOAD, "request_id": REQUEST_ID,
        }, required=["workflow_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
        "handler": "tool_run_workflow",
    },
    {
        "name": "spoofzy_list_saved_originals",
        "title": "Saved Spoofzy originals",
        "description": "List the originals saved in the user's private Spoofzy Cloud library (id, name, type), for "
                       "use with spoofzy_run_workflow. Needs a Cloud key with 'Read saved originals and voices'. "
                       "Read-only.",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        "handler": "tool_list_saved_originals",
    },
]
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def tool_listing():
    return [{key: tool[key] for key in ("name", "title", "description", "inputSchema", "annotations")}
            for tool in TOOLS]


def coerce(value, schema):
    """Forgive common model slips: "5" for 5, "true" for true, a single path for a list."""
    kind = schema.get("type")
    if kind == "integer" and isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    if kind == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if kind == "array" and isinstance(value, str):
        return [value]
    if kind == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        return {key: coerce(item, properties[key]) if key in properties else item for key, item in value.items()}
    if kind == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [coerce(item, schema["items"]) for item in value]
    return value


def validate(value, schema, path="arguments"):
    """Check ``value`` against the small JSON Schema subset used by these tools."""
    kind = schema.get("type")
    errors = []
    if kind == "object":
        if not isinstance(value, dict):
            return ["{} must be an object".format(path)]
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append("{}.{} is required".format(path, name))
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(item, properties[name], "{}.{}".format(path, name)))
            elif schema.get("additionalProperties") is False:
                errors.append("{}.{} is not a known option (known: {})".format(
                    path, name, ", ".join(properties) or "none"))
        return errors
    if kind == "array":
        if not isinstance(value, list):
            return ["{} must be a list".format(path)]
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10 ** 6):
            errors.append("{} must have {} to {} items".format(path, schema.get("minItems", 0),
                                                               schema.get("maxItems", "any")))
        for index, item in enumerate(value):
            errors.extend(validate(item, schema.get("items", {}), "{}[{}]".format(path, index)))
        return errors
    if kind == "string":
        if not isinstance(value, str):
            return ["{} must be a string".format(path)]
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10 ** 9):
            errors.append("{} must be {} to {} characters".format(path, schema.get("minLength", 0),
                                                                 schema.get("maxLength", "any")))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append("{} has an invalid format".format(path))
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return ["{} must be a whole number".format(path)]
        if value < schema.get("minimum", -10 ** 18) or value > schema.get("maximum", 10 ** 18):
            errors.append("{} must be between {} and {}".format(path, schema.get("minimum", "-"),
                                                                schema.get("maximum", "-")))
    elif kind == "boolean":
        if not isinstance(value, bool):
            return ["{} must be true or false".format(path)]
    if "enum" in schema and value not in schema["enum"]:
        errors.append("{} must be one of: {}".format(path, ", ".join(map(str, schema["enum"]))))
    return errors


# ---------------------------------------------------------------------------
# Background completion
# ---------------------------------------------------------------------------

class Finisher:
    """Keeps polling runs a call stopped waiting for, and saves their results when ready."""

    def __init__(self, app):
        self.app = app
        self.pending = {}
        self.condition = threading.Condition()
        self.thread = None

    def adopt(self, runs, download):
        now = time.monotonic()
        with self.condition:
            for run in runs:
                if not run.id:
                    continue
                if run.terminal and (not download or not run.outputs or run.fully_saved):
                    continue
                self.pending[run.id] = [run, download, now + self.app.poll_delay(run, 0), now + 6 * 3600]
            if self.pending and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._loop, name="spoofzy-finisher", daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def _loop(self):
        with using(Context()):
            while True:
                with self.condition:
                    if not self.pending:
                        self.thread = None
                        return
                    item = min(self.pending.values(), key=lambda entry: entry[2])
                    delay = item[2] - time.monotonic()
                    if delay > 0:
                        self.condition.wait(min(delay, 30))
                        continue
                run, download, _, give_up = item
                done = False
                try:
                    if not run.terminal:
                        self.app.refresh(run)
                    if run.terminal:
                        if download and run.outputs and not run.fully_saved:
                            self.app.save(run)
                        done = True
                    else:
                        done = time.monotonic() > give_up
                except SpoofzyError as error:
                    run.download_error = str(error)
                    done = error.status in (401, 403, 404) or time.monotonic() > give_up
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    done = time.monotonic() > give_up
                with self.condition:
                    if done:
                        self.pending.pop(run.id, None)
                    else:
                        item[2] = time.monotonic() + self.app.poll_delay(run, 99)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class ToolResult:
    def __init__(self, text, data=None, error=False):
        self.text = text
        self.data = data if data is not None else {}
        self.error = error


def list_names(paths):
    names = [path.name for path in paths]
    if len(names) <= 6:
        return ", ".join(names)
    return "{}, {} … {}".format(names[0], names[1], names[-1])


def plural(count, word):
    return "{} {}{}".format(count, word, "" if count == 1 else "s")


class App:
    """The Spoofzy tools, shared by the MCP server and the command line."""

    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.registry = Registry()
        self.finisher = Finisher(self)
        self._backends = {}
        self._lock = threading.Lock()

    # -- backends ---------------------------------------------------------------
    def backend(self, name):
        with self._lock:
            if name not in self._backends:
                self._backends[name] = (CloudBackend if name == "cloud" else PrepaidBackend)(self.settings)
            return self._backends[name]

    def pick(self, requested=None):
        requested = (requested or "auto").lower()
        if requested == "auto":
            requested = self.settings.backend
        if requested == "auto":
            if self.settings.cloud_key:
                requested = "cloud"
            elif self.settings.api_key:
                requested = "api"
            else:
                raise SpoofzyError(not_configured_message(), code="not_configured")
        if requested == "cloud" and not self.settings.cloud_key:
            raise SpoofzyError("No Spoofzy Cloud key is configured. " + not_configured_message(), code="not_configured")
        if requested == "api" and not self.settings.api_key:
            raise SpoofzyError("No prepaid Spoofer API key is configured. " + not_configured_message(),
                               code="not_configured")
        return self.backend(requested)

    def cloud(self, purpose):
        if not self.settings.cloud_key:
            raise SpoofzyError("{} needs a Spoofzy Cloud key (spc_…). {}".format(purpose, not_configured_message()),
                               code="not_configured")
        return self.backend("cloud")

    def refresh(self, run):
        self.backend(run.backend).refresh(run)

    # -- polling and saving --------------------------------------------------
    def poll_delay(self, run, polls):
        if self.settings.fast_poll:
            return 0.05
        if run.kind == "api-batch":
            return 10.0
        return (3.0, 3.0, 5.0, 5.0, 8.0)[polls] if polls < 5 else 10.0

    def output_folder(self, explicit, source):
        if explicit:
            return absolute_path(explicit)
        if self.settings.output_dir:
            return absolute_path(self.settings.output_dir)
        if source is not None:
            return Path(source).parent / "spoofzy-output"
        downloads = Path.home() / "Downloads"
        return (downloads if downloads.is_dir() else Path.home()) / "spoofzy-output"

    def ensure_folder(self, folder):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not os.access(str(folder), os.W_OK):
                raise PermissionError(str(folder))
            return folder
        except OSError:
            fallback = self.output_folder(None, None)
            if fallback == folder:
                raise SpoofzyError("Cannot write to {}. Pass an output_dir you can write to.".format(folder)) from None
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def save(self, run, folder=None):
        """Download a finished run's outputs that are not saved yet (or all of them into ``folder``)."""
        with run.save_lock:
            if folder is not None:
                run.saved_map = {}
            missing = [index for index in range(len(run.outputs)) if index not in run.saved_map]
            if not missing:
                return run.saved
            target = self.ensure_folder(folder or run.out_dir or self.output_folder(None, run.source))
            run.out_dir = target
            fallback = run.source.suffix.lower() if run.source else ""
            context = current()
            failures = []
            for attempt in range(2):
                outputs = run.outputs
                reserved = reserve_names(target, run.stem, run.tag,
                                         [output_extension(outputs[index], fallback) for index in missing])

                def fetch(position):
                    index = missing[position]
                    final, part = reserved[position]
                    with using(context):
                        try:
                            content_type = download(outputs[index]["url"], part)
                            return index, publish(part, final, content_type), None
                        except SpoofzyError as error:
                            return index, None, error
                        finally:
                            if part.exists():
                                try:
                                    part.unlink()
                                except OSError:
                                    pass

                failures = []
                workers = min(4, len(missing))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for done, (index, path, error) in enumerate(pool.map(fetch, range(len(missing))), 1):
                        if path is not None:
                            run.saved_map[index] = path
                        elif error is not None:
                            failures.append(error)
                        context.progress(0.95 + 0.02 * done / len(missing),
                                         "Saving results ({}/{})".format(done, len(missing)))
                context.check()
                missing = [index for index in range(len(run.outputs)) if index not in run.saved_map]
                expired = any(error.status in (401, 403, 404) for error in failures)
                if not missing or not expired or attempt:
                    break
                # Result links can expire; ask Spoofzy for fresh ones once.
                self.refresh(run)
            run.download_error = ""
            if failures:
                run.download_error = "{} of {} files could not be saved: {}".format(
                    len(failures), len(run.outputs), failures[0])
                if not run.saved_map:
                    raise SpoofzyError(run.download_error)
            return run.saved

    def wait(self, runs, seconds, download, label, adopt=True):
        """Poll runs until they finish or ``seconds`` pass; save results as they arrive.

        Runs still unfinished at the end are handed to the background finisher (when ``adopt``),
        which saves their results when they are ready.
        """
        context = current()
        if adopt:
            for run in runs:
                run.auto_save = run.auto_save or download
        active = [run for run in runs if run.id and not run.terminal]
        total = max(1, len([run for run in runs if run.id]))
        polls = {run.id: 0 for run in active}
        due = {run.id: time.monotonic() + self.poll_delay(run, 0) for run in active}
        deadline = time.monotonic() + max(0, seconds)
        try:
            if download:
                for run in runs:
                    if run.id and run.terminal and run.outputs and not run.fully_saved:
                        self._save_quietly(run)
            while active:
                now = time.monotonic()
                if now >= deadline:
                    break
                run = min(active, key=lambda item: due[item.id])
                if due[run.id] > now:
                    context.sleep(min(due[run.id], deadline) - now)
                    continue
                try:
                    self.refresh(run)
                except SpoofzyError as error:
                    if error.status in (401, 403, 404):
                        run.set("failed" if error.status == 404 else run.status, str(error))
                        active.remove(run)
                    else:
                        due[run.id] = time.monotonic() + 10
                    continue
                polls[run.id] += 1
                if run.terminal:
                    active.remove(run)
                    if download and run.outputs:
                        self._save_quietly(run)
                else:
                    due[run.id] = time.monotonic() + self.poll_delay(run, polls[run.id])
                finished = total - len(active)
                waiting = [item for item in active if item.status == "waiting"]
                message = "{}: {}/{} finished".format(label, finished, total)
                if waiting:
                    message += " (waiting for a free processing slot)"
                context.progress(0.3 + 0.65 * finished / total, message)
        finally:
            if adopt:
                self.finisher.adopt([run for run in runs if run.id], download)

    def _save_quietly(self, run):
        try:
            self.save(run)
        except SpoofzyError as error:
            run.download_error = str(error)

    def submit(self, backend, medias, request_id, explicit, start, label, finder=None):
        """Upload and start each file in turn; returns its runs (plus failed placeholders)."""
        context = current()
        runs, total = [], len(medias)
        for index, media in enumerate(medias):
            file_request = request_id if total == 1 else "{}-{}".format(request_id, index + 1)
            context.progress(0.3 * index / total, "{}: uploading {} ({}/{})".format(
                label, media.path.name, index + 1, total), force=True)
            known = self.registry.for_request(backend.name, file_request)
            if known and explicit:
                runs.extend(known)
                continue

            def sink(run, media=media):
                runs.append(self.registry.add(run))

            def uploaded(fraction, index=index):
                context.progress(0.3 * (index + fraction) / total, "{}: uploading {} ({}/{})".format(
                    label, media.path.name, index + 1, total))

            try:
                found = finder(media, file_request) if explicit and finder else []
                for run in found:
                    sink(run)
                start(media, file_request, sink, uploaded, found)
            except Cancelled:
                raise
            except SpoofzyError as error:
                systemic = error.status == 401 or error.code in SYSTEMIC_CODES
                # Fail the whole call only when nothing started and a retry id is not needed.
                if (systemic or total == 1) and not runs and not error.uncertain:
                    raise
                runs.append(Run.failed(backend.name, media.path, file_request, str(error), error.uncertain))
                if systemic:
                    for rest in medias[index + 1:]:
                        runs.append(Run.failed(backend.name, rest.path, None, "Not started: " + str(error)))
                    break
        return runs

    def finish_options(self, args):
        wait = args.get("wait_seconds")
        return (self.settings.wait_seconds if wait is None else wait), args.get("download", True) is not False

    def assign_folders(self, runs, explicit):
        for run in runs:
            if run.id and run.out_dir is None:
                run.out_dir = self.output_folder(explicit, run.source)

    def report(self, headline, runs, request_id, notes=()):
        lines = [headline]
        for run in runs:
            lines.append(self.describe(run))
        started = [run for run in runs if run.id]
        pending = [run for run in started if not run.terminal]
        if pending:
            lines.append("Still processing: {}. Spoofzy keeps working on them; this server saves the results "
                         "when they finish. Check with spoofzy_get_run.".format(", ".join(run.id for run in pending)))
        if any(run.uncertain for run in runs):
            lines.append("Some outcomes are uncertain. Retry with request_id={} to resume them without paying "
                         "twice.".format(request_id))
        lines.extend(note for note in notes if note)
        data = {"request_id": request_id, "runs": [run.public() for run in runs]}
        return ToolResult("\n".join(lines), data, error=not started)

    @staticmethod
    def describe(run):
        name = run.source.name if run.source else (run.filename or run.id or "file")
        cost = ""
        if run.credits:
            cost = " · {} Credits".format(run.credits)
        elif run.units:
            cost = " · {}".format(plural(run.units, "prepaid unit"))
        if run.status in ("completed", "partial"):
            saved = run.saved
            if saved:
                line = "✓ {} → {} saved in {}: {}".format(name, plural(len(saved), "file"), saved[0].parent,
                                                          list_names(saved))
                if not run.fully_saved and run.download_error:
                    line += " ({})".format(run.download_error)
            elif run.download_error:
                line = "✓ {} finished, but saving failed: {} Retry with spoofzy_download.".format(
                    name, run.download_error)
            else:
                line = "✓ {} finished: {} ready. Save with spoofzy_download.".format(
                    name, plural(len(run.outputs), "file"))
            if run.status == "partial":
                line += " Some versions failed: " + run.message
        elif run.status == "failed":
            line = "✗ {} failed: {}".format(name, run.message or "unknown error")
        else:
            line = "… {} is {}{}".format(name, run.status, ": " + run.message if run.message else "")
        if run.id:
            line += " (run {}{})".format(run.id, cost)
        return line

    # -- tools --------------------------------------------------------------------
    def tool_account(self, args):
        settings = self.settings
        data = {"server_version": __version__, "backends": {}, "default_output":
                settings.output_dir or "a spoofzy-output folder next to each source file"}
        lines = []
        notes = settings.notes + settings.url_problems
        if not settings.cloud_key and not settings.api_key:
            data["configured"] = False
            text = not_configured_message()
            if notes:
                text += "\nNotes: " + " ".join(notes)
            return ToolResult(text, data)
        if settings.cloud_key:
            try:
                capabilities = self.cloud("Spoofzy Cloud").capabilities(refresh=True)
                data["backends"]["cloud"] = {"connected": True, "key": mask_key(settings.cloud_key),
                                             "tools": capabilities}
                lines.append(self.describe_cloud(capabilities))
            except SpoofzyError as error:
                data["backends"]["cloud"] = {"connected": False, "key": mask_key(settings.cloud_key),
                                             "error": str(error)}
                lines.append("Spoofzy Cloud: not connected. " + str(error))
        if settings.api_key:
            try:
                account = self.backend("api").account()
                data["backends"]["api"] = dict(account, connected=True, key=mask_key(settings.api_key))
                lines.append(self.describe_prepaid(account))
            except SpoofzyError as error:
                data["backends"]["api"] = {"connected": False, "key": mask_key(settings.api_key), "error": str(error)}
                lines.append("Prepaid Spoofer API: not connected. " + str(error))
        lines.append("Results are saved to {}.".format(data["default_output"]))
        if notes:
            lines.append("Notes: " + " ".join(notes))
            data["notes"] = notes
        connected = any(item.get("connected") for item in data["backends"].values())
        return ToolResult("\n".join(lines), data, error=not connected)

    @staticmethod
    def describe_cloud(capabilities):
        def state(tool):
            item = capabilities.get(tool)
            if not item:
                return None
            if not item.get("available"):
                return "unavailable ({})".format(item.get("reason") or "not on this plan")
            cost = item.get("creditCost") or item.get("standardCreditCost") or 0
            return "{} Credits per run".format(cost) if cost else "included"

        plan = next((item.get("plan") for item in capabilities.values() if item.get("plan")), None)
        parts = []
        for tool, label in (("spoofer", "Spoofer"), ("spoofer-many", "Spoofer, 5–50 versions"),
                            ("chains", "Studio looks/watermarks/borders"), ("metadata-cleaner", "Metadata Cleaner"),
                            ("automations", "workflows"), ("saved-masters", "saved originals")):
            value = state(tool)
            if value:
                parts.append("{}: {}".format(label, value))
        limit = max((item.get("maxBytes") or 0) for item in capabilities.values()) if capabilities else 0
        text = "Spoofzy Cloud: connected{}. ".format(" (plan: {})".format(plan) if plan else "")
        text += "; ".join(parts) + "." if parts else "No tools reported."
        if limit:
            text += " Largest upload: {}.".format(human_size(limit))
        return text

    @staticmethod
    def describe_prepaid(account):
        expires = account.get("expires_at")
        when = time.strftime("%Y-%m-%d", time.gmtime(expires)) if isinstance(expires, int) else "unknown"
        batch = account.get("batch_limit") or 0
        return ("Prepaid Spoofer API: connected. {} left; {} of {} job slots busy; {}; access expires {}.".format(
            plural(account.get("units_remaining") or 0, "unit"), account.get("active_jobs", 0),
            account.get("concurrency_limit", "?"),
            "batches up to {} versions".format(batch) if batch else "batches not enabled (one job per version)",
            when))

    def spoof_options(self, args):
        look = args.get("look") or "classic"
        watermark = dict(args.get("watermark") or {})
        if watermark:
            mode = watermark.get("mode") or ("text" if watermark.get("text") else "account")
            watermark["mode"] = "inherit" if mode == "account" else mode
            if watermark["mode"] == "text" and not str(watermark.get("text") or "").strip():
                raise SpoofzyError("watermark.mode text needs watermark.text.")
            if watermark == {"mode": "inherit"}:
                watermark = {}
        border = dict(args.get("border") or {})
        if border and border.get("color", "off") == "off":
            border = {}
        elif border:
            border.setdefault("color", "light")
        return {"versions": args.get("versions", 1), "look": look, "mirror": bool(args.get("mirror", False)),
                "export_size": args.get("export_size") or "original", "watermark": watermark, "border": border}

    def tool_spoof(self, args):
        backend = self.pick(args.get("backend"))
        options = self.spoof_options(args)
        if options["look"] == "minimal" and options["mirror"]:
            raise SpoofzyError("look=minimal keeps the existing layout, so it cannot be combined with mirror=true.")
        medias = [inspect_media(path, backend.name) for path in expand_paths(self.file_arguments(args))]
        plan = backend.plan_spoof(options, len(medias))
        request_id, explicit = self.request_id(args)
        wait, download = self.finish_options(args)
        if backend.name == "cloud":
            def start(media, file_request, sink, uploaded, found):
                if not found:
                    backend.start_spoof(media, file_request, plan, sink, uploaded)
            finder = lambda media, file_request: backend.find_runs(file_request, media, "spoofzy")
        else:
            def start(media, file_request, sink, uploaded, found):
                backend.start_spoof(media, file_request, plan, sink, uploaded, already=found)
            finder = lambda media, file_request: backend.existing(file_request, plan, media)
        runs = self.submit(backend, medias, request_id, explicit, start, "Spoofing", finder)
        self.assign_folders(runs, args.get("output_dir"))
        self.wait(runs, wait, download, "Spoofing")
        settings = [options["look"] + " look"]
        if options["mirror"]:
            settings.append("mirrored")
        if options["export_size"] != "original":
            settings.append(options["export_size"])
        headline = "{}: {} × {} ({}).".format(
            backend.label, plural(len(medias), "file"), plural(plan.versions, "version"), ", ".join(settings))
        result = self.report(headline, runs, request_id)
        result.data.update(backend=backend.name, versions=plan.versions, look=options["look"],
                           mirror=options["mirror"], export_size=options["export_size"])
        return result

    def tool_clean(self, args):
        backend = self.cloud("Metadata Cleaner")
        backend.require("metadata-cleaner", "Metadata Cleaner")
        options = {key: args.get(key, True) is not False for key in ("remove_gps", "remove_device", "remove_datetime")}
        if not any(options.values()):
            raise SpoofzyError("Turn on at least one of remove_gps, remove_device or remove_datetime.")
        medias = [inspect_media(path, "cloud") for path in expand_paths(self.file_arguments(args))]
        request_id, explicit = self.request_id(args)
        wait, download = self.finish_options(args)

        def start(media, file_request, sink, uploaded, found):
            if not found:
                sink(backend.start_job(media, "metadata-cleaner", options, file_request, "clean", uploaded))

        finder = lambda media, file_request: backend.find_runs(file_request, media, "clean")
        runs = self.submit(backend, medias, request_id, explicit, start, "Cleaning metadata", finder)
        self.assign_folders(runs, args.get("output_dir"))
        self.wait(runs, wait, download, "Cleaning metadata")
        removed = [label for key, label in (("remove_gps", "location"), ("remove_device", "device details"),
                                            ("remove_datetime", "dates")) if options[key]]
        headline = "Spoofzy Metadata Cleaner: {} (removing {}).".format(plural(len(medias), "file"),
                                                                        ", ".join(removed))
        return self.report(headline, runs, request_id)

    def run_for(self, run_id, workflow_id=None):
        run = self.registry.get(run_id)
        if run is not None:
            if workflow_id and not run.workflow_id:
                run.workflow_id = workflow_id
            return run
        if run_id.startswith(("mjob_", "cc_")):
            self.cloud("This run")
            kind = "job" if run_id.startswith("mjob_") else ("workflow" if workflow_id else "stack")
            return Run("cloud", kind, run_id, workflow_id=workflow_id)
        if not self.settings.api_key:
            raise SpoofzyError("{} is a prepaid Spoofer API run, but no prepaid API key (spz_…) is configured.".format(
                run_id), code="not_configured")
        return Run("api", "api-batch" if run_id.startswith("batch_") else "api-job", run_id)

    def poll_once_or_wait(self, run, seconds):
        self.refresh(run)
        if seconds and not run.terminal:
            self.wait([run], seconds, download=False, label="Waiting", adopt=False)

    def tool_get_run(self, args):
        run = self.run_for(args["run_id"], args.get("workflow_id"))
        self.poll_once_or_wait(run, args.get("wait_seconds") or 0)
        if run.auto_save and run.terminal and run.outputs and not run.fully_saved:
            # This session's call asked for the files; save them now rather than at the next poll.
            self._save_quietly(run)
        result = ToolResult(self.describe(run), {"run": run.public()})
        if not run.terminal:
            result.text += "\nIt is still running; check again in a little while."
        elif run.outputs and not run.saved:
            result.text += "\nSave the results with spoofzy_download."
        return result

    def tool_download(self, args):
        run = self.run_for(args["run_id"], args.get("workflow_id"))
        self.registry.add(run)
        self.poll_once_or_wait(run, args.get("wait_seconds") or 0)
        if not run.terminal:
            return ToolResult(self.describe(run) + "\nNot finished yet; nothing to download. Try again later or pass "
                                                   "wait_seconds.", {"run": run.public()})
        if not run.outputs:
            return ToolResult(self.describe(run) + "\nThis run has no result files to download.",
                              {"run": run.public()}, error=run.status == "failed")
        folder = absolute_path(args["output_dir"]) if args.get("output_dir") else None
        self.save(run, folder)
        return ToolResult(self.describe(run), {"run": run.public()})

    def tool_list_runs(self, args):
        limit = args.get("limit") or 10
        rows, seen, notes = [], set(), []
        for run in self.registry.recent(limit):
            rows.append(run)
            seen.add(run.id)
        if self.settings.cloud_key and self.settings.cloud_url:
            client = self.backend("cloud").client
            for path, field in (("jobs", "entries"), ("chains", "chains")):
                try:
                    items = client.call("GET", path, attempts=2).get(field) or []
                except SpoofzyError as error:
                    if error.code not in ("membership_required", "pro_required", "tool_unavailable"):
                        notes.append("Could not list Cloud {}: {}".format(path, error))
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        if field == "entries":
                            run_id = (item.get("start") or {}).get("jobId")
                            if not RUN_ID_RE.fullmatch(str(run_id or "")) or run_id in seen:
                                continue
                            run = Run("cloud", "job", run_id)
                            apply_cloud_job(run, item)
                            run.created = (item.get("job") or {}).get("created_at") or 0
                        else:
                            run_id = item.get("id")
                            if not RUN_ID_RE.fullmatch(str(run_id or "")) or run_id in seen:
                                continue
                            run = Run("cloud", "stack", run_id)
                            apply_cloud_chain(run, item)
                            run.created = item.get("createdAt") or 0
                    except SpoofzyError:
                        continue
                    seen.add(run_id)
                    rows.append(run)
        session = rows[:len([row for row in rows if self.registry.get(row.id) is not None])]
        others = sorted(rows[len(session):], key=lambda run: run.created or 0, reverse=True)
        rows = (session + others)[:limit]
        if not rows:
            text = "No Spoofzy runs found yet."
        else:
            lines = ["Recent Spoofzy runs (newest first; this session's runs first):"]
            for run in rows:
                when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run.created)) if run.created else ""
                saved = " · {} saved in {}".format(plural(len(run.saved), "file"), run.saved[0].parent) if run.saved else ""
                lines.append("- {} · {} · {} · {}{}{}".format(run.id, KIND_LABELS.get(run.kind, run.kind), run.status,
                                                              run.filename or "", " · " + when if when else "", saved))
            text = "\n".join(lines)
        if notes:
            text += "\n" + "\n".join(notes)
        return ToolResult(text, {"runs": [run.public() for run in rows]})

    @staticmethod
    def describe_stages(stages):
        names = {"clean": "Metadata Cleaner", "voice": "Voice note", "pair": "Side by side",
                 "privacy": "Smart Blur", "synthid": "SynthID step"}
        parts = []
        for stage in stages or []:
            if not isinstance(stage, dict):
                continue
            kind = stage.get("type")
            if kind == "multiply":
                parts.append("Spoof ×{} ({})".format(stage.get("versions", 1), LOOK_NAMES.get(stage.get("preset"), "classic")))
            elif kind == "caption":
                parts.append("Caption ×{}".format(len(stage.get("projects") or []) or 1))
            elif kind == "retake":
                parts.append("AI Retake ×{}".format(stage.get("takes", 1)))
            elif kind == "editor":
                parts.append("Edit ({})".format(stage.get("mode", "1_to_1")))
            else:
                parts.append(names.get(kind, str(kind or "step")))
        return " → ".join(parts) or "no steps"

    def tool_list_workflows(self, args):
        data = self.cloud("Workflows").client.call("GET", "automations", attempts=3)
        workflows = [item for item in data.get("workflows") or [] if isinstance(item, dict)]
        rows = [{"workflow_id": item.get("id"), "name": item.get("name"), "version": item.get("version"),
                 "input_type": item.get("contentType"), "steps": self.describe_stages(item.get("stages")),
                 "outputs_per_run": item.get("outputCount"), "enabled": item.get("enabled")} for item in workflows]
        if not rows:
            return ToolResult("No saved workflows. Create one in Spoofzy Cloud → Automations.", {"workflows": []})
        lines = ["Saved Spoofzy workflows:"]
        for row in rows:
            lines.append("- {} · {} · v{} · input {} · {} · {} per run{}".format(
                row["name"], row["workflow_id"], row["version"], row["input_type"], row["steps"],
                plural(row["outputs_per_run"] or 1, "output"), "" if row["enabled"] else " · paused"))
        return ToolResult("\n".join(lines), {"workflows": rows})

    def tool_run_workflow(self, args):
        backend = self.cloud("Workflows")
        workflow_id = args["workflow_id"]
        version = args.get("version")
        workflow = None
        try:
            workflow = backend.workflow(workflow_id)
        except SpoofzyError as error:
            if not (error.status == 403 and version):
                if error.status == 403:
                    raise SpoofzyError("This key cannot read workflow definitions. Pass the workflow's version "
                                       "number, or use a key with 'Read workflows and history'.",
                                       code="insufficient_scope", status=403) from None
                raise
        content_type = None
        name = "workflow"
        if workflow is not None:
            if workflow.get("archived"):
                raise SpoofzyError("That workflow is archived.")
            if not workflow.get("enabled"):
                raise SpoofzyError("That workflow is paused. Enable it in Spoofzy Cloud first.")
            current_version = workflow.get("version")
            if version is None:
                version = current_version
            elif version != current_version:
                raise SpoofzyError("Version {} is not the workflow's current version ({}). Review the change, then "
                                   "run the current version.".format(version, current_version), code="state_conflict")
            content_type = workflow.get("contentType")
            name = workflow.get("name") or name
            stages = workflow.get("stages") or []
            if stages and isinstance(stages[0], dict) and stages[0].get("type") == "pair":
                raise SpoofzyError("Side-by-side workflows take two images at once; run them in Spoofzy Cloud.")
        tag = safe_stem(name.lower())[:30] or "workflow"
        paths = self.file_arguments(args, required=False)
        inputs = [bool(paths), bool(args.get("saved_original_id")), bool(args.get("text"))]
        if sum(inputs) != 1:
            raise SpoofzyError("Give exactly one input: files, saved_original_id or text.")
        request_id, explicit = self.request_id(args)
        wait, download = self.finish_options(args)
        if paths:
            medias = [inspect_media(path, "cloud") for path in expand_paths(paths)]
            if content_type and content_type != "text/plain":
                for media in medias:
                    if media.content_type != content_type:
                        raise SpoofzyError("{} is {}, but this workflow takes {} files.".format(
                            media.path.name, TYPE_NAMES.get(media.content_type, media.content_type),
                            TYPE_NAMES.get(content_type, content_type)))

            def start(media, file_request, sink, uploaded, found):
                sink(backend.start_workflow(workflow_id, version, file_request, tag, media=media, on_progress=uploaded))

            runs = self.submit(backend, medias, request_id, explicit, start, "Running workflow")
        else:
            known = self.registry.for_request("cloud", request_id) if explicit else []
            if known:
                runs = known
            else:
                try:
                    run = backend.start_workflow(workflow_id, version, request_id, tag,
                                                 master_id=args.get("saved_original_id"), text=args.get("text"))
                except SpoofzyError as error:
                    if not error.uncertain:
                        raise
                    run = Run.failed("cloud", None, request_id, str(error), True)
                runs = [self.registry.add(run)]
        self.assign_folders(runs, args.get("output_dir"))
        self.wait(runs, wait, download, "Running workflow")
        return self.report("Spoofzy workflow \"{}\" (v{}).".format(name, version), runs, request_id)

    def tool_list_saved_originals(self, args):
        data = self.cloud("The saved-originals library").client.call("GET", "masters", attempts=3)
        items = [item for item in data.get("masters") or [] if isinstance(item, dict)]
        rows = [{"saved_original_id": item.get("id"), "name": item.get("name"), "filename": item.get("filename"),
                 "type": item.get("mime")} for item in items]
        if not rows:
            return ToolResult("No saved originals in the Spoofzy library.", {"saved_originals": []})
        lines = ["Saved originals:"] + ["- {} · {} · {}".format(row["name"], row["saved_original_id"], row["type"])
                                        for row in rows]
        return ToolResult("\n".join(lines), {"saved_originals": rows})

    # -- argument helpers -------------------------------------------------------
    @staticmethod
    def file_arguments(args, required=True):
        values = list(args.get("files") or [])
        if args.get("file"):
            values.append(args["file"])
        if required and not values:
            raise SpoofzyError("Pass the media to process in files (absolute paths or folders).")
        return values

    @staticmethod
    def request_id(args):
        given = args.get("request_id")
        if given:
            return given, True
        return "spz" + secrets.token_hex(10), False


# ---------------------------------------------------------------------------
# MCP server (stdio)
# ---------------------------------------------------------------------------

def rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class Server:
    """Newline-delimited JSON-RPC over stdio; each tool call runs on its own thread."""

    def __init__(self, app, reader=None, writer=None):
        self.app = app
        self.reader = reader or sys.stdin.buffer
        self.writer = writer or sys.stdout.buffer
        self.write_lock = threading.Lock()
        self.calls = {}
        self.calls_lock = threading.Lock()
        self.threads = []

    def send(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        with self.write_lock:
            self.writer.write(line.encode("utf-8"))
            self.writer.flush()

    def serve(self):
        while True:
            line = self.reader.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = self.reader.readline(MAX_MESSAGE_BYTES + 1)
                self.send(rpc_error(None, -32700, "Message too large"))
                continue
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.send(rpc_error(None, -32700, "Parse error"))
                continue
            try:
                self.dispatch(message)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                if isinstance(message, dict) and "id" in message:
                    self.send(rpc_error(message.get("id"), -32603, "Internal error"))
        self.shutdown()

    def shutdown(self):
        with self.calls_lock:
            for context in self.calls.values():
                context.detached = True
                context.cancelled.set()
        deadline = time.monotonic() + 3
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    @staticmethod
    def key(request_id):
        return (type(request_id).__name__, request_id)

    def dispatch(self, message):
        if isinstance(message, list):
            self.send(rpc_error(None, -32600, "Batch requests are not supported"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self.send(rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request"))
            return
        if "method" not in message:
            return  # A response to a request this server never sends.
        method = message.get("method")
        params = message.get("params")
        params = {} if params is None else params
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            self.send(rpc_error(None, -32600, "Invalid Request: id must be a string or integer"))
            return
        if not isinstance(method, str) or not isinstance(params, dict):
            if has_id:
                self.send(rpc_error(request_id, -32600 if not isinstance(method, str) else -32602, "Invalid Request"))
            return
        if not has_id:
            self.notification(method, params)
        else:
            self.request(request_id, method, params)

    def notification(self, method, params):
        if method != "notifications/cancelled":
            return  # notifications/initialized and others need no action.
        with self.calls_lock:
            context = self.calls.get(self.key(params.get("requestId")))
        if context is None:
            return
        context.detached = True  # Never answer a cancelled request.
        reason = str(params.get("reason") or "").lower()
        if "timeout" not in reason and "timed out" not in reason:
            # The user stopped the call. A client timeout instead lets the work finish quietly, so
            # uploads already under way are not wasted and results are still saved.
            context.cancelled.set()

    def respond(self, request_id, result, modern):
        if modern:
            result = dict(result, resultType="complete")
            meta = dict(result.get("_meta") or {})
            meta[META_SERVER] = {"name": SERVER_INFO["name"], "version": __version__}
            result["_meta"] = meta
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def request(self, request_id, method, params):
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(META_PROTOCOL)
        modern = version is not None
        if modern and version not in MODERN_VERSIONS:
            self.send(rpc_error(request_id, -32022, "Unsupported protocol version",
                                {"supported": list(MODERN_VERSIONS), "requested": version}))
            return
        if method == "server/discover":
            self.respond(request_id, {"supportedVersions": list(MODERN_VERSIONS), "capabilities": {"tools": {}},
                                      "instructions": INSTRUCTIONS, "ttlMs": 3600000, "cacheScope": "public"}, True)
        elif method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            self.respond(request_id, {"protocolVersion": negotiated, "capabilities": {"tools": {"listChanged": False}},
                                      "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}, False)
        elif method == "ping":
            self.respond(request_id, {}, modern)
        elif method == "tools/list":
            result = {"tools": tool_listing()}
            if modern:
                result.update(ttlMs=3600000, cacheScope="public")
            self.respond(request_id, result, modern)
        elif method == "tools/call":
            self.start_call(request_id, params, meta, modern)
        else:
            self.send(rpc_error(request_id, -32601, "Method not found: " + method))

    def start_call(self, request_id, params, meta, modern):
        tool = TOOLS_BY_NAME.get(params.get("name"))
        if tool is None:
            self.send(rpc_error(request_id, -32602, "Unknown tool: {}".format(params.get("name"))))
            return
        token = meta.get("progressToken")
        context = Context()
        if isinstance(token, (str, int)) and not isinstance(token, bool):
            def notify(value, message):
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": value, "total": 100, "message": message}})
            context = Context(notify)
        with self.calls_lock:
            self.calls[self.key(request_id)] = context
        thread = threading.Thread(target=self.run_call, args=(request_id, tool, params.get("arguments"), context, modern),
                                  name="spoofzy-call", daemon=True)
        self.threads = [item for item in self.threads if item.is_alive()] + [thread]
        thread.start()

    def run_call(self, request_id, tool, arguments, context, modern):
        result = None
        with using(context):
            try:
                arguments = {} if arguments is None else arguments
                arguments = coerce(arguments, tool["inputSchema"])
                problems = validate(arguments, tool["inputSchema"])
                if problems:
                    raise SpoofzyError("Invalid arguments: " + "; ".join(problems[:8]))
                outcome = getattr(self.app, tool["handler"])(arguments)
                result = {"content": [{"type": "text", "text": outcome.text}], "structuredContent": outcome.data,
                          "isError": bool(outcome.error)}
            except Cancelled:
                result = None
            except SpoofzyError as error:
                data = {"error": {"message": str(error), "code": error.code, "status": error.status}}
                result = {"content": [{"type": "text", "text": str(error)}], "structuredContent": data, "isError": True}
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                text = "Unexpected error in the Spoofzy MCP server ({}). Details were written to its log.".format(
                    type(error).__name__)
                result = {"content": [{"type": "text", "text": text}], "isError": True}
            finally:
                with self.calls_lock:
                    self.calls.pop(self.key(request_id), None)
        if result is not None and not context.detached:
            self.respond(request_id, result, modern)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def cli_context():
    last = {"message": None}

    def notify(value, message):
        if message and message != last["message"]:
            last["message"] = message
            print("  {}".format(message), file=sys.stderr)
    return Context(notify)


def cmd_setup(args):
    settings = Settings()
    path = settings.config_file
    stored = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            stored = {}
    print("Spoofzy MCP setup. Keys are saved in {} (readable only by you).".format(path))
    print("Spoofzy Cloud key: https://cloud.spoofzy.com → Developers → Create API key.")
    print("Prepaid Spoofer API key: run /api in the Spoofzy Telegram bot (t.me/SpoofzyBot).")
    print("Press Enter to keep the current value; type - to remove it.\n")

    def ask(label, field, pattern):
        while True:
            current_value = clean_value(stored.get(field))
            shown = mask_key(current_value) if current_value else "none"
            answer = getpass.getpass("{} [{}]: ".format(label, shown)).strip()
            if not answer:
                return
            if answer == "-":
                stored.pop(field, None)
                return
            if pattern.fullmatch(answer):
                stored[field] = answer
                return
            print("  That does not look like a valid key; try again (or press Enter to skip).")

    ask("Spoofzy Cloud API key (spc_…)", "cloud_api_key", CLOUD_KEY_RE)
    ask("Prepaid Spoofer API key (spz_…)", "api_key", API_KEY_RE)
    folder = input("Default output folder [{}]: ".format(stored.get("output_dir") or "next to each file")).strip()
    if folder == "-":
        stored.pop("output_dir", None)
    elif folder:
        stored["output_dir"] = str(absolute_path(folder))
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(str(path.parent), 0o700)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=2)
    if os.name != "nt":
        os.chmod(str(path), 0o600)
    print("\nSaved. Checking the connection…")
    return cmd_doctor(args, header=False)


def cmd_doctor(args, header=True):
    settings = Settings()
    if header:
        print("spoofzy-mcp {} · Python {} · {}".format(__version__, sys.version.split()[0], sys.platform))
        print("Credentials file: {}{}".format(settings.config_file, "" if settings.config_file.is_file() else " (not created)"))
        print("Cloud key: {}{}".format(mask_key(settings.cloud_key), " (from {})".format(settings.cloud_source) if settings.cloud_key else ""))
        print("Prepaid API key: {}{}".format(mask_key(settings.api_key), " (from {})".format(settings.api_source) if settings.api_key else ""))
    with using(cli_context()):
        result = App(settings).tool_account({})
    print(result.text)
    return 1 if result.error else 0


def run_cli_tool(handler, arguments):
    app = App()
    context = cli_context()
    with using(context):
        try:
            result = getattr(app, handler)(arguments)
        except SpoofzyError as error:
            print(str(error), file=sys.stderr)
            return 1
        except Cancelled:
            return 130
    print(result.text)
    pending = [run for run in app.registry.recent(500) if not run.terminal]
    if pending:
        print("(Runs still processing on Spoofzy; check later with: spoofzy_mcp.py status RUN_ID --download)")
    return 1 if result.error else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spoofzy-mcp", description="Spoofzy MCP server and command line.")
    parser.add_argument("--version", action="version", version="spoofzy-mcp " + __version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the MCP server on stdio (the default)")
    commands.add_parser("setup", help="save your Spoofzy API key(s) in your user config")
    commands.add_parser("doctor", help="check configuration and connectivity")
    spoof = commands.add_parser("spoof", help="spoof local images or videos")
    spoof.add_argument("files", nargs="+")
    spoof.add_argument("-n", "--versions", type=int, default=1)
    spoof.add_argument("--look", choices=list(LOOKS), default="classic")
    spoof.add_argument("--mirror", action="store_true")
    spoof.add_argument("--export-size", choices=["original", "1080p", "4k"], default="original")
    spoof.add_argument("--watermark-text")
    spoof.add_argument("--backend", choices=["auto", "cloud", "api"], default="auto")
    clean = commands.add_parser("clean", help="strip metadata from local photos or videos")
    clean.add_argument("files", nargs="+")
    clean.add_argument("--keep-gps", action="store_true")
    clean.add_argument("--keep-device", action="store_true")
    clean.add_argument("--keep-dates", action="store_true")
    status = commands.add_parser("status", help="check a run, optionally downloading its results")
    status.add_argument("run_id")
    status.add_argument("--download", action="store_true")
    for sub in (spoof, clean, status):
        sub.add_argument("-o", "--output-dir")
        sub.add_argument("--wait", type=int, default=900, help="seconds to wait (default 900)")
    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        if sys.stdin.isatty():
            print("Spoofzy MCP server {} is waiting for an MCP client on stdio (Ctrl+C to stop). "
                  "Run with --help for terminal commands.".format(__version__), file=sys.stderr)
        try:
            Server(App()).serve()
        except KeyboardInterrupt:
            pass
        return 0
    if command == "setup":
        return cmd_setup(args)
    if command == "doctor":
        return cmd_doctor(args)
    common = {"wait_seconds": max(0, min(args.wait, 3600))}
    if args.output_dir:
        common["output_dir"] = args.output_dir
    if command == "spoof":
        arguments = dict(common, files=args.files, versions=args.versions, look=args.look, mirror=args.mirror,
                         export_size=args.export_size, backend=args.backend)
        if args.watermark_text:
            arguments["watermark"] = {"mode": "text", "text": args.watermark_text}
        return run_cli_tool("tool_spoof", arguments)
    if command == "clean":
        return run_cli_tool("tool_clean", dict(common, files=args.files, remove_gps=not args.keep_gps,
                                               remove_device=not args.keep_device,
                                               remove_datetime=not args.keep_dates))
    handler = "tool_download" if args.download else "tool_get_run"
    return run_cli_tool(handler, dict(common, run_id=args.run_id))


if __name__ == "__main__":
    sys.exit(main())
