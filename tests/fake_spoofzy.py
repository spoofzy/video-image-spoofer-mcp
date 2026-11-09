"""An in-process stand-in for the Spoofzy Cloud and prepaid Spoofer APIs, for tests.

It follows the real request contracts (exact JSON fields, Bearer keys, idempotency
rules, error shapes and response layouts) closely enough to exercise the MCP
server end to end, without network access or a Spoofzy account. Jobs finish
after ``polls_to_finish`` status reads.
"""
import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLOUD_KEY = "spc_" + "a1" * 16 + "." + "B" * 43
API_KEY = "spz_4242_" + "x" * 30
OWNER = 4242
WORKFLOW_ID = "wa_" + "c" * 32
MASTER_ID = "f" * 32

CONTENT = {
    ".jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 60,
    ".png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 60,
    ".mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 60,
    ".mov": b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 60,
}
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4",
              "video/quicktime": ".mov", "image/heic": ".heic", "image/gif": ".gif", "video/webm": ".webm"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FakeSpoofzy:
    def __init__(self):
        self.lock = threading.RLock()
        self.polls_to_finish = 2
        self.capabilities = [
            {"id": "spoofer", "available": True, "mode": "job", "plan": "Spoofzy Pro", "maxBytes": 100 * 1024 * 1024,
             "creditCost": 0},
            {"id": "spoofer-many", "available": True, "mode": "job", "plan": "Spoofzy Pro",
             "maxBytes": 100 * 1024 * 1024, "creditCost": 0},
            {"id": "metadata-cleaner", "available": True, "mode": "job", "plan": "Spoofzy Pro",
             "maxBytes": 100 * 1024 * 1024, "creditCost": 0},
            {"id": "chains", "available": True, "mode": "chain"},
            {"id": "automations", "available": True, "mode": "library"},
            {"id": "saved-masters", "available": True, "mode": "library"},
        ]
        self.account = {"units_remaining": 100, "active_jobs": 0, "concurrency_limit": 2, "batch_limit": 0,
                        "expires_at": 1893456000}
        self.uploads = {}
        self.jobs = {}
        self.job_requests = {}
        self.chains = {}
        self.chain_requests = {}
        self.api_jobs = {}
        self.batches = {}
        self.workflow = {"id": WORKFLOW_ID, "name": "Caption + Spoof", "contentType": "image/jpeg",
                         "stages": [{"type": "multiply", "versions": 5, "preset": "c"}], "enabled": True,
                         "outputCount": 3, "version": 3, "createdAt": 0, "updatedAt": 0, "archived": False,
                         "completionUrl": ""}
        self.masters = [{"id": MASTER_ID, "name": "Beach master", "filename": "beach.jpg", "mime": "image/jpeg"}]
        self.log = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:{}".format(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- helpers ------------------------------------------------------------------------
    def requests(self, method=None, prefix=""):
        with self.lock:
            return [entry for entry in self.log if (method is None or entry["method"] == method)
                    and entry["path"].startswith(prefix)]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, data, headers=None):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def blob(self, data, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    fake.route(self, method, body)
                except AssertionError as error:
                    self.reply(400, {"error": {"code": "invalid_request", "message": "fake: " + str(error)}})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PUT(self):
                self.handle_any("PUT")

        return Handler

    def ticket(self, prefix, filename, content_type, **extra):
        upload_id = prefix + uuid.uuid4().hex
        with self.lock:
            self.uploads[upload_id] = dict(filename=filename, content_type=content_type, data=None, **extra)
        return {"id": upload_id, "method": "PUT", "url": "{}/storage/{}".format(self.base, upload_id),
                "headers": {"Content-Type": content_type}, "expires_in_seconds": 900, "max_bytes": 100 * 1024 * 1024}

    def file_url(self, name):
        return "{}/files/{}".format(self.base, name)

    # -- routing ------------------------------------------------------------------------
    def route(self, h, method, body):
        path = h.path
        with self.lock:
            self.log.append({"method": method, "path": path, "headers": dict(h.headers),
                             "body": body if method != "PUT" else len(body)})
        if method == "PUT" and path.startswith("/storage/"):
            upload = self.uploads.get(path.rsplit("/", 1)[-1])
            assert upload is not None, "unknown upload"
            assert h.headers.get("Content-Type") == upload["content_type"], "content type mismatch"
            assert int(h.headers.get("Content-Length")) == len(body), "length mismatch"
            assert "Authorization" not in h.headers, "storage must not receive API keys"
            upload["data"] = body
            h.send_response(200)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        if method == "GET" and path.startswith("/files/"):
            assert "Authorization" not in h.headers, "storage must not receive API keys"
            name = path.rsplit("/", 1)[-1]
            extension = name[name.rfind("."):]
            return h.blob(CONTENT.get(extension, b"\x00" * 64) + name.encode(), "application/octet-stream")
        data = json.loads(body) if body else None
        if path.startswith("/developer/v1/"):
            if h.headers.get("Authorization") != "Bearer " + CLOUD_KEY:
                return h.reply(401, {"error": {"message": "Use a Spoofzy Cloud API key."}})
            return self.cloud(h, method, path[len("/developer/v1/"):], data)
        if path.startswith("/api/v1/"):
            if h.headers.get("Authorization") != "Bearer " + API_KEY:
                return h.reply(401, {"error": {"code": "invalid_api_key",
                                               "message": "That API key is invalid, expired, or disabled."}})
            return self.prepaid(h, method, path[len("/api/v1/"):], data, h.headers.get("Idempotency-Key"))
        h.reply(404, {"error": {"message": "This API route is not available."}})

    # -- Cloud Developer API ---------------------------------------------------------------
    def cloud(self, h, method, path, data):
        if method == "GET" and path == "capabilities":
            return h.reply(200, {"capabilities": self.capabilities})
        if method == "POST" and path == "uploads":
            assert set(data) == {"tool_id", "filename", "content_type"}, "upload fields"
            assert EXTENSIONS.get(data["content_type"]) and data["filename"].endswith(EXTENSIONS[data["content_type"]]) \
                or data["filename"].endswith(".jpeg"), "filename/type mismatch"
            return h.reply(200, {"upload": self.ticket("fup_", data["filename"], data["content_type"],
                                                       tool=data["tool_id"])})
        if method == "POST" and path == "jobs":
            assert set(data) == {"request_id", "tool_id", "upload_id", "filename", "options"}, "job fields"
            with self.lock:
                known = self.job_requests.get(data["request_id"])
                if known:
                    if known[0] != digest(data):
                        return h.reply(409, {"error": {"code": "idempotency_conflict",
                                                       "message": "This request key belongs to different tool settings."}})
                    return h.reply(200, self.job_entry(known[1]))
                upload = self.uploads.get(data["upload_id"])
                assert upload and upload["data"], "upload not received"
                assert upload["filename"] == data["filename"] and upload["tool"] == data["tool_id"], "upload mismatch"
                job_id = "mjob_" + uuid.uuid4().hex
                self.jobs[job_id] = {"tool": data["tool_id"], "options": data["options"], "polls": 0,
                                     "filename": data["filename"], "request_id": data["request_id"],
                                     "created": int(time.time()), "type": upload["content_type"]}
                self.job_requests[data["request_id"]] = (digest(data), job_id)
                return h.reply(200, self.job_entry(job_id))
        if method == "GET" and path == "jobs":
            with self.lock:
                return h.reply(200, {"entries": [self.job_entry(job_id) for job_id in reversed(list(self.jobs))][:25]})
        match = re.fullmatch(r"jobs/(mjob_[a-f0-9]{32})", path)
        if method == "GET" and match:
            with self.lock:
                if match.group(1) not in self.jobs:
                    return h.reply(404, {"error": {"code": "not_found", "message": "Job not found."}})
                self.jobs[match.group(1)]["polls"] += 1
                return h.reply(200, self.job_entry(match.group(1)))
        if method == "POST" and path == "chains":
            assert set(data) == {"request_id", "upload_id", "filename", "stages"}, "chain fields"
            stage = data["stages"][0]
            assert stage["type"] == "multiply" and stage["versions"] in (1, 5, 10, 20, 30, 40, 50), "stage"
            return h.reply(200, self.new_chain(data, stage["versions"]))
        if method == "GET" and path == "chains":
            with self.lock:
                return h.reply(200, {"chains": [self.chain_public(cid) for cid in reversed(list(self.chains))][:25]})
        match = re.fullmatch(r"chains/(cc_[a-f0-9]{32})", path)
        if method == "GET" and match:
            return self.poll_chain(h, match.group(1))
        if method == "GET" and path == "automations":
            return h.reply(200, {"workflows": [self.workflow]})
        if method == "GET" and path == "automations/" + WORKFLOW_ID:
            return h.reply(200, {"workflow": self.workflow})
        if method == "POST" and path == "automations/{}/uploads".format(WORKFLOW_ID):
            assert set(data) == {"version", "filename", "content_type"}, "workflow upload fields"
            if data["version"] != self.workflow["version"]:
                return h.reply(409, {"error": {"code": "state_conflict", "message": "Review the current version."}})
            assert data["content_type"] == self.workflow["contentType"], "workflow input type"
            return h.reply(200, {"upload": self.ticket("fup_", data["filename"], data["content_type"],
                                                       tool="spoofer", workflow=WORKFLOW_ID)})
        if method == "POST" and path == "automations/{}/runs".format(WORKFLOW_ID):
            assert data["version"] == self.workflow["version"], "workflow version"
            if "upload_id" in data:
                upload = self.uploads.get(data["upload_id"])
                assert upload and upload["data"] and upload.get("workflow") == WORKFLOW_ID, "workflow upload"
            else:
                assert data.get("master_id") == MASTER_ID, "master"
            return h.reply(200, self.new_chain(data, self.workflow["outputCount"], workflow=WORKFLOW_ID))
        match = re.fullmatch(r"automations/{}/runs/(cc_[a-f0-9]{{32}})".format(WORKFLOW_ID), path)
        if method == "GET" and match:
            return self.poll_chain(h, match.group(1))
        if method == "GET" and path == "masters":
            return h.reply(200, {"masters": self.masters})
        return h.reply(404, {"error": {"code": "not_found", "message": "This API operation is not available."}})

    def job_entry(self, job_id):
        job = self.jobs[job_id]
        done = job["polls"] >= self.polls_to_finish
        result = None
        extension = EXTENSIONS[job["type"]]
        if done and job["tool"] == "spoofer-many":
            urls = [self.file_url("{}-{}{}".format(job_id, n, extension)) for n in range(job["options"]["variants"])]
            result = {"outputs": [{"download_url": url, "url": url} for url in urls]}
        elif done:
            result = {"download_url": self.file_url(job_id + extension), "size_mb": 0.01}
        return {"start": {"requestId": job["request_id"], "jobId": job_id, "status": "started", "reason": "",
                          "channel": "api", "credits": 0},
                "job": {"id": job_id, "tool_id": job["tool"], "filename": job["filename"],
                        "status": "ready" if done else ("processing" if job["polls"] else "queued"),
                        "created_at": job["created"], "updated_at": "", "error": "", "result": result}}

    def new_chain(self, data, count, workflow=None):
        with self.lock:
            key = (workflow, data["request_id"])
            known = self.chain_requests.get(key)
            if known:
                return {"chain": self.chain_public(known)}
            if "upload_id" in data:
                upload = self.uploads.get(data["upload_id"])
                assert upload and upload["data"], "upload not received"
                assert upload["filename"] == data["filename"], "filename mismatch"
                filename, content_type = upload["filename"], upload["content_type"]
            else:
                filename, content_type = "beach.jpg", "image/jpeg"
            chain_id = "cc_" + uuid.uuid4().hex
            self.chains[chain_id] = {"request": data, "count": count, "polls": 0, "filename": filename,
                                     "type": content_type, "created": int(time.time()), "workflow": workflow}
            self.chain_requests[key] = chain_id
            return {"chain": self.chain_public(chain_id)}

    def chain_public(self, chain_id):
        chain = self.chains[chain_id]
        done = chain["polls"] >= self.polls_to_finish
        extension = EXTENSIONS[chain["type"]]
        stem = chain["filename"].rsplit(".", 1)[0]
        files = [{"name": "{:02d}_{}_{}{}".format(n + 1, stem, chain_id[3:15], extension),
                  "url": self.file_url("{}-{}{}".format(chain_id, n, extension)),
                  "preview_url": self.file_url("{}-{}{}".format(chain_id, n, extension)), "size": 64,
                  "mime": chain["type"]} for n in range(chain["count"])] if done else []
        return {"id": chain_id, "requestId": chain["request"]["request_id"], "filename": chain["filename"],
                "status": "completed" if done else ("processing" if chain["polls"] else "queued"), "stage": 0,
                "createdAt": chain["created"], "count": chain["count"], "creditCost": 0, "reason": "",
                "files": files, "archives": [], "archiveStatus": "none", "capacity": None}

    def poll_chain(self, h, chain_id):
        with self.lock:
            if chain_id not in self.chains:
                return h.reply(404, {"error": {"code": "not_found", "message": "Run not found."}})
            self.chains[chain_id]["polls"] += 1
            return h.reply(200, {"chain": self.chain_public(chain_id)})

    # -- prepaid Spoofer API ---------------------------------------------------------------
    def prepaid(self, h, method, path, data, key):
        if method == "GET" and path == "spoofer/account":
            return h.reply(200, {"account": self.account})
        if method == "POST" and path == "spoofer/uploads":
            assert set(data) == {"filename", "content_type"}, "upload fields"
            assert data["content_type"] in ("image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime")
            return h.reply(201, {"upload": self.ticket("upl_", data["filename"], data["content_type"])})
        if method == "POST" and path == "spoofer/jobs":
            assert key and 12 <= len(key) <= 128, "idempotency key"
            options = data.get("options", {})
            assert options.get("preset", "standard") in ("standard", "minimal", "text_preserving"), "preset"
            assert isinstance(options.get("vertical_flip", False), bool), "flip"
            job_id = "api_" + hashlib.sha256("{}:{}".format(OWNER, key).encode()).hexdigest()[:32]
            with self.lock:
                if job_id in self.api_jobs:
                    return h.reply(200, {"job": self.api_job(job_id)})
                active = sum(1 for job in self.api_jobs.values() if job["polls"] < self.polls_to_finish)
                if active >= self.account["concurrency_limit"] or self.account["units_remaining"] < 1:
                    return h.reply(429, {"error": {"code": "quota_or_concurrency_reached",
                                                   "message": "No unit or job slot is available."}})
                upload = self.uploads.get(data["upload_id"])
                assert upload and upload["data"], "upload not received"
                self.account["units_remaining"] -= 1
                self.api_jobs[job_id] = {"polls": 0, "type": upload["content_type"], "options": options}
                return h.reply(202, {"job": self.api_job(job_id)})
        match = re.fullmatch(r"spoofer/jobs/(api_[a-f0-9]{32})", path)
        if method == "GET" and match:
            with self.lock:
                if match.group(1) not in self.api_jobs:
                    return h.reply(404, {"error": {"code": "not_found", "message": "Job not found."}})
                self.api_jobs[match.group(1)]["polls"] += 1
                return h.reply(200, {"job": self.api_job(match.group(1))})
        if method == "POST" and path == "spoofer/batches":
            assert key and 12 <= len(key) <= 128, "idempotency key"
            assert set(data) <= {"upload_id", "filename", "variants", "options", "completion"}, "batch fields"
            batch_id = "batch_" + hashlib.sha256("{}:{}".format(OWNER, key).encode()).hexdigest()[:32]
            with self.lock:
                known = self.batches.get(batch_id)
                if known:
                    if known["digest"] != digest(data):
                        return h.reply(409, {"error": {"code": "idempotency_conflict",
                                                       "message": "That key is already bound to different batch parameters."}})
                    return h.reply(200, {"batch": self.batch(batch_id)})
                if data["variants"] > self.account["batch_limit"]:
                    return h.reply(403, {"error": {"code": "batch_not_enabled",
                                                   "message": "This account is not enabled for that batch size."}})
                upload = self.uploads.get(data["upload_id"])
                assert upload and upload["data"], "upload not received"
                self.batches[batch_id] = {"digest": digest(data), "variants": data["variants"], "polls": 0,
                                          "type": upload["content_type"], "options": data.get("options", {})}
                return h.reply(202, {"batch": {"id": batch_id, "status": "processing", "variants": data["variants"],
                                               "units_reserved": data["variants"],
                                               "status_url": "/api/v1/spoofer/batches/" + batch_id}})
        match = re.fullmatch(r"spoofer/batches/(batch_[a-f0-9]{32})", path)
        if method == "GET" and match:
            with self.lock:
                if match.group(1) not in self.batches:
                    return h.reply(404, {"error": {"code": "not_found", "message": "Batch not found."}})
                self.batches[match.group(1)]["polls"] += 1
                return h.reply(200, {"batch": self.batch(match.group(1))})
        return h.reply(404, {"error": {"code": "not_found", "message": "Not found."}})

    def api_job(self, job_id):
        job = self.api_jobs[job_id]
        done = job["polls"] >= self.polls_to_finish
        result = {"id": job_id, "operation": "spoofer.standard", "status": "completed" if done else "queued",
                  "created_at": int(time.time()), "units_charged": 1}
        if done:
            result["result"] = {"download_url": self.file_url(job_id + EXTENSIONS[job["type"]])}
        return result

    def batch(self, batch_id):
        batch = self.batches[batch_id]
        done = batch["polls"] >= self.polls_to_finish
        extension = EXTENSIONS[batch["type"]]
        jobs = []
        for index in range(batch["variants"]):
            job = {"id": "api_" + hashlib.sha256("{}:{}".format(batch_id, index).encode()).hexdigest()[:32],
                   "index": index, "status": "completed" if done else "queued", "units_charged": 1}
            if done:
                job["result"] = {"download_url": self.file_url("{}-{}{}".format(batch_id, index, extension))}
            jobs.append(job)
        counts = {"queued": 0 if done else batch["variants"], "completed": batch["variants"] if done else 0, "failed": 0}
        return {"id": batch_id, "status": "completed" if done else "processing", "variants": batch["variants"],
                "created_at": int(time.time()), "counts": counts, "units_reserved": batch["variants"],
                "units_refunded": 0, "jobs": jobs, "completion": {"status": "not_configured", "attempts": 0}}
