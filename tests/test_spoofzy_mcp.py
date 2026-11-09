"""Tests for spoofzy_mcp.py. Run with: python3 -m unittest discover -s tests -v

End-to-end tests start the real server as a subprocess, speak MCP to it over
stdio and point it at an in-process fake of the Spoofzy APIs (no network).
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spoofzy_mcp as mcp  # noqa: E402
from fake_spoofzy import API_KEY, CLOUD_KEY, CONTENT, MASTER_ID, WORKFLOW_ID, FakeSpoofzy  # noqa: E402

JPEG = CONTENT[".jpg"] + b"source-jpeg"
PNG = CONTENT[".png"] + b"source-png"
MP4 = CONTENT[".mp4"] + b"source-mp4"


class McpClient:
    """Minimal MCP stdio client that records progress notifications."""

    def __init__(self, env):
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, str(ROOT / "spoofzy_mcp.py")], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        self.inbox = queue.Queue()
        self.responses = {}
        self.progress = []
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.inbox.put(json.loads(line))

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params=None, timeout=60, request_id=None):
        if request_id is None:
            request_id, self.next_id = self.next_id, self.next_id + 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.wait_for(request_id, timeout)

    def wait_for(self, request_id, timeout=60):
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("no response to {} within {}s".format(request_id, timeout))
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if "id" in message:
                self.responses[message["id"]] = message
            elif message.get("method") == "notifications/progress":
                self.progress.append(message["params"])
        return self.responses.pop(request_id)

    def initialize(self):
        response = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "tests", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, name, arguments, timeout=60, token=None):
        params = {"name": name, "arguments": arguments}
        if token is not None:
            params["_meta"] = {"progressToken": token}
        response = self.request("tools/call", params, timeout)
        if "error" in response:
            raise AssertionError("protocol error: {}".format(response["error"]))
        return response["result"]

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.stderr.seek(0)
        log = self.stderr.read().decode("utf-8", "replace")
        self.stderr.close()
        return log


def text_of(result):
    return result["content"][0]["text"]


class Workspace(unittest.TestCase):
    """A temp folder, a fake Spoofzy and an MCP server connected to it."""

    keys = {"SPOOFZY_CLOUD_API_KEY": CLOUD_KEY}

    def setUp(self):
        self.fake = FakeSpoofzy()
        self.temp = Path(tempfile.mkdtemp(prefix="spoofzy-test-"))
        self.home = self.temp / "home"
        self.home.mkdir()
        self.media = self.temp / "media"
        self.media.mkdir()
        self.client = McpClient(self.env())
        self.client.initialize()

    def env(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("SPOOFZY_")}
        env.update(HOME=str(self.home), SPOOFZY_CONFIG_DIR=str(self.temp / "config"), SPOOFZY_TEST_FAST_POLL="1",
                   SPOOFZY_CLOUD_API_URL=self.fake.base + "/developer/v1", SPOOFZY_API_URL=self.fake.base + "/api/v1",
                   SPOOFZY_WAIT_SECONDS="30", PYTHONDONTWRITEBYTECODE="1")
        env.update(self.keys)
        return env

    def restart(self):
        """Simulate a new session: a fresh server process that has no memory of earlier runs."""
        self.client.close()
        self.client = McpClient(self.env())
        self.client.initialize()

    def tearDown(self):
        log = self.client.close()
        self.fake.close()
        shutil.rmtree(self.temp, ignore_errors=True)
        if "Traceback" in log:
            self.fail("server logged a traceback:\n" + log)

    def write(self, name, data):
        path = self.media / name
        path.write_bytes(data)
        return path

    def assert_progress_increases(self, token):
        values = [item["progress"] for item in self.client.progress if item["progressToken"] == token]
        self.assertTrue(values, "expected progress notifications")
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(values), len(set(values)))


class ProtocolTests(Workspace):
    def test_modern_and_legacy_requests(self):
        modern = {"_meta": {mcp.META_PROTOCOL: "2026-07-28"}}
        discover = self.client.request("server/discover", modern)["result"]
        self.assertEqual(discover["supportedVersions"], ["2026-07-28"])
        self.assertEqual(discover["resultType"], "complete")
        self.assertEqual(discover["_meta"][mcp.META_SERVER]["name"], "spoofzy")
        self.assertIn("ttlMs", discover)
        listing = self.client.request("tools/list", modern)["result"]
        self.assertEqual(listing["resultType"], "complete")
        self.assertEqual(listing["cacheScope"], "public")
        names = [tool["name"] for tool in listing["tools"]]
        self.assertEqual(names[0], "spoofzy_account")
        self.assertEqual(len(names), 9)
        legacy = self.client.request("tools/list")["result"]
        self.assertNotIn("resultType", legacy)
        self.assertEqual([tool["name"] for tool in legacy["tools"]], names)
        call = self.client.request("tools/call", dict(modern, name="spoofzy_account", arguments={}))["result"]
        self.assertEqual(call["resultType"], "complete")
        self.assertFalse(call["isError"])

    def test_errors(self):
        bad_version = self.client.request("tools/list", {"_meta": {mcp.META_PROTOCOL: "1999-01-01"}})
        self.assertEqual(bad_version["error"]["code"], -32022)
        self.assertEqual(bad_version["error"]["data"]["supported"], ["2026-07-28"])
        self.assertEqual(self.client.request("no/such/method")["error"]["code"], -32601)
        self.assertEqual(self.client.request("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.client.request("ping")["result"], {})
        invalid = self.client.call("spoofzy_spoof", {"files": ["/x.jpg"], "look": "wild", "colour": 1})
        self.assertTrue(invalid["isError"])
        self.assertIn("look must be one of", text_of(invalid))
        self.assertIn("colour is not a known option", text_of(invalid))
        self.client.send({"jsonrpc": "2.0", "id": 99, "method": "ping", "params": "nope"})
        self.assertEqual(self.client.wait_for(99)["error"]["code"], -32602)
        self.client.send({"jsonrpc": "2.0", "method": "notifications/unknown"})
        self.assertEqual(self.client.request("ping")["result"], {})

    def test_account(self):
        result = self.client.call("spoofzy_account", {})
        self.assertFalse(result["isError"])
        self.assertIn("Spoofzy Cloud: connected (plan: Spoofzy Pro)", text_of(result))
        self.assertIn("Metadata Cleaner: included", text_of(result))
        self.assertTrue(result["structuredContent"]["backends"]["cloud"]["connected"])


class CloudSpoofTests(Workspace):
    def test_single_version_uses_job_and_saves_next_to_source(self):
        source = self.write("beach photo.jpg", JPEG)
        result = self.client.call("spoofzy_spoof", {"files": [str(source)]}, token="p1")
        self.assertFalse(result["isError"], text_of(result))
        out = self.media / "spoofzy-output"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["beach-photo-spoofzy-01.jpg"])
        run = result["structuredContent"]["runs"][0]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["run_id"].startswith("mjob_"))
        job = [entry for entry in self.fake.requests("POST", "/developer/v1/jobs")][0]
        body = json.loads(job["body"])
        self.assertEqual(body["tool_id"], "spoofer")
        self.assertEqual(body["options"], {"vertical_flip": False})
        self.assertEqual(body["filename"], "beach-photo.jpg")
        upload = next(item for item in self.fake.uploads.values() if item["data"])
        self.assertEqual(upload["data"], JPEG)
        self.assert_progress_increases("p1")
        self.assertIn("1 file saved in", text_of(result))

    def test_many_versions_and_numbering_continues(self):
        source = self.write("clip.mp4", MP4)
        first = self.client.call("spoofzy_spoof", {"file": str(source), "versions": 5, "mirror": True})
        self.assertFalse(first["isError"], text_of(first))
        body = json.loads(self.fake.requests("POST", "/developer/v1/jobs")[0]["body"])
        self.assertEqual(body["tool_id"], "spoofer-many")
        self.assertEqual(body["options"], {"variants": 5, "vertical_flip": True})
        second = self.client.call("spoofzy_spoof", {"file": str(source), "versions": 5, "mirror": True})
        self.assertFalse(second["isError"], text_of(second))
        names = sorted(p.name for p in (self.media / "spoofzy-output").iterdir())
        self.assertEqual(names, ["clip-spoofzy-{:02d}.mp4".format(n) for n in range(1, 11)])
        self.assertEqual(len(self.fake.requests("POST", "/developer/v1/jobs")), 2)

    def test_studio_options_use_a_stack(self):
        source = self.write("reel.jpg", JPEG)
        result = self.client.call("spoofzy_spoof", {
            "files": [str(source)], "versions": 10, "look": "subtle", "export_size": "1080p",
            "watermark": {"text": "@me", "placement": "bottom_right"}, "border": {"color": "pink"},
            "output_dir": str(self.temp / "out")})
        self.assertFalse(result["isError"], text_of(result))
        stage = json.loads(self.fake.requests("POST", "/developer/v1/chains")[0]["body"])["stages"][0]
        self.assertEqual(stage, {"type": "multiply", "versions": 10, "preset": "n", "mirror": False,
                                 "export": "1080p", "watermark": {"text": "@me", "placement": "bottom_right",
                                                                  "mode": "text"},
                                 "border": {"color": "pink"}})
        self.assertEqual(len(list((self.temp / "out").iterdir())), 10)
        self.assertTrue(result["structuredContent"]["runs"][0]["run_id"].startswith("cc_"))

    def test_folder_mislabelled_and_rejected_files(self):
        self.write("a.jpg", JPEG)
        self.write("b.jpg", PNG)  # really a PNG
        self.write("notes.txt", b"hello")
        result = self.client.call("spoofzy_spoof", {"files": [str(self.media)]})
        self.assertFalse(result["isError"], text_of(result))
        uploads = sorted((item["filename"], item["content_type"]) for item in self.fake.uploads.values())
        self.assertEqual(uploads, [("a.jpg", "image/jpeg"), ("b.png", "image/png")])
        names = sorted(p.name for p in (self.media / "spoofzy-output").iterdir())
        self.assertEqual(names, ["a-spoofzy-01.jpg", "b-spoofzy-01.png"])
        fake = self.write("secret.jpg", b"-----BEGIN PRIVATE KEY-----")
        refused = self.client.call("spoofzy_spoof", {"files": [str(fake)]})
        self.assertTrue(refused["isError"])
        self.assertIn("not a supported image or video", text_of(refused))
        missing = self.client.call("spoofzy_spoof", {"files": [str(self.media / "nope.jpg")]})
        self.assertIn("File not found", text_of(missing))
        three = self.client.call("spoofzy_spoof", {"files": [str(self.write("c.jpg", JPEG))], "versions": 3})
        self.assertIn("1, 5, 10, 20, 30, 40 or 50", text_of(three))

    def test_retry_with_request_id_does_not_start_again(self):
        source = self.write("x.jpg", JPEG)
        first = self.client.call("spoofzy_spoof", {"files": [str(source)], "request_id": "retry-me-123"})
        second = self.client.call("spoofzy_spoof", {"files": [str(source)], "request_id": "retry-me-123"})
        self.assertEqual(first["structuredContent"]["runs"][0]["run_id"],
                         second["structuredContent"]["runs"][0]["run_id"])
        self.assertEqual(len(self.fake.requests("POST", "/developer/v1/jobs")), 1)
        self.assertEqual(len(list((self.media / "spoofzy-output").iterdir())), 1)

    def test_retry_after_restart_finds_the_existing_run(self):
        source = self.write("y.jpg", JPEG)
        first = self.client.call("spoofzy_spoof", {"files": [str(source)], "request_id": "restart-abc-1"})
        run_id = first["structuredContent"]["runs"][0]["run_id"]
        self.restart()
        again = self.client.call("spoofzy_spoof", {"files": [str(source)], "request_id": "restart-abc-1"})
        self.assertEqual(again["structuredContent"]["runs"][0]["run_id"], run_id)
        self.assertEqual(len(self.fake.requests("POST", "/developer/v1/jobs")), 1)

    def test_no_wait_then_background_save_and_tools(self):
        source = self.write("later.jpg", JPEG)
        started = self.client.call("spoofzy_spoof", {"files": [str(source)], "wait_seconds": 0})
        self.assertFalse(started["isError"], text_of(started))
        run_id = started["structuredContent"]["runs"][0]["run_id"]
        self.assertIn("Still processing", text_of(started))
        deadline = time.monotonic() + 10
        out = self.media / "spoofzy-output"
        final = out / "later-spoofzy-01.jpg"
        while time.monotonic() < deadline and not final.exists():
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual([p.name for p in out.iterdir()], ["later-spoofzy-01.jpg"])
        status = self.client.call("spoofzy_get_run", {"run_id": run_id})
        self.assertIn("1 file saved", text_of(status))
        listing = self.client.call("spoofzy_list_runs", {})
        self.assertIn(run_id, text_of(listing))

    def test_download_false_then_download_tool(self):
        source = self.write("links.jpg", JPEG)
        result = self.client.call("spoofzy_spoof", {"files": [str(source)], "download": False})
        run = result["structuredContent"]["runs"][0]
        self.assertTrue(run["result_links"])
        self.assertFalse((self.media / "spoofzy-output").exists())
        target = self.temp / "chosen"
        saved = self.client.call("spoofzy_download", {"run_id": run["run_id"], "output_dir": str(target)})
        self.assertFalse(saved["isError"], text_of(saved))
        self.assertEqual([p.name for p in target.iterdir()], ["links-spoofzy-01.jpg"])

    def test_cancel_stops_waiting_without_a_response(self):
        self.fake.polls_to_finish = 10 ** 6
        source = self.write("slow.jpg", JPEG)
        self.client.send({"jsonrpc": "2.0", "id": "slow", "method": "tools/call",
                          "params": {"name": "spoofzy_spoof", "arguments": {"files": [str(source)], "wait_seconds": 60}}})
        deadline = time.monotonic() + 10
        while not self.fake.requests("POST", "/developer/v1/jobs") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.client.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": "slow", "reason": "User stopped"}})
        self.assertEqual(self.client.request("ping", timeout=5)["result"], {})
        with self.assertRaises(AssertionError):
            self.client.wait_for("slow", timeout=1)

    def test_clean_metadata(self):
        source = self.write("gps.jpg", JPEG)
        result = self.client.call("spoofzy_clean_metadata", {"files": [str(source)], "remove_datetime": False})
        self.assertFalse(result["isError"], text_of(result))
        body = json.loads(self.fake.requests("POST", "/developer/v1/jobs")[0]["body"])
        self.assertEqual(body["tool_id"], "metadata-cleaner")
        self.assertEqual(body["options"], {"remove_gps": True, "remove_device": True, "remove_datetime": False})
        self.assertEqual([p.name for p in (self.media / "spoofzy-output").iterdir()], ["gps-clean-01.jpg"])

    def test_workflows_and_library(self):
        listing = self.client.call("spoofzy_list_workflows", {})
        self.assertIn(WORKFLOW_ID, text_of(listing))
        self.assertIn("Spoof ×5 (classic)", text_of(listing))
        source = self.write("wf.jpg", JPEG)
        result = self.client.call("spoofzy_run_workflow", {"workflow_id": WORKFLOW_ID, "files": [str(source)]})
        self.assertFalse(result["isError"], text_of(result))
        names = sorted(p.name for p in (self.media / "spoofzy-output").iterdir())
        self.assertEqual(names, ["wf-caption-spoof-0{}.jpg".format(n) for n in (1, 2, 3)])
        self.assertTrue(self.fake.requests("GET", "/developer/v1/automations/{}/runs/".format(WORKFLOW_ID)))
        wrong = self.client.call("spoofzy_run_workflow", {"workflow_id": WORKFLOW_ID,
                                                          "files": [str(self.write("w.png", PNG))]})
        self.assertIn("this workflow takes JPEG files", text_of(wrong))
        originals = self.client.call("spoofzy_list_saved_originals", {})
        self.assertIn(MASTER_ID, text_of(originals))
        saved = self.client.call("spoofzy_run_workflow", {"workflow_id": WORKFLOW_ID, "saved_original_id": MASTER_ID,
                                                          "output_dir": str(self.temp / "lib")})
        self.assertFalse(saved["isError"], text_of(saved))
        self.assertEqual(len(list((self.temp / "lib").iterdir())), 3)


class WrongKeyTests(Workspace):
    keys = {"SPOOFZY_CLOUD_API_KEY": "spc_" + "0" * 32 + "." + "Z" * 43}

    def test_rejected_key_explains_fix(self):
        result = self.client.call("spoofzy_account", {})
        self.assertTrue(result["isError"])
        self.assertIn("HTTP 401", text_of(result))
        self.assertIn("setup", text_of(result))


class NoKeyTests(Workspace):
    keys = {}

    def test_not_configured(self):
        result = self.client.call("spoofzy_spoof", {"files": ["/tmp/x.jpg"]})
        self.assertTrue(result["isError"])
        self.assertIn("not connected yet", text_of(result))
        self.assertIn("Never paste API keys", text_of(result))


class PrepaidTests(Workspace):
    keys = {"SPOOFZY_API_KEY": API_KEY}

    def test_single_job(self):
        source = self.write("one.mov", CONTENT[".mov"] + b"src")
        result = self.client.call("spoofzy_spoof", {"files": [str(source)], "look": "minimal"})
        self.assertFalse(result["isError"], text_of(result))
        job = self.fake.requests("POST", "/api/v1/spoofer/jobs")[0]
        self.assertEqual(json.loads(job["body"])["options"], {"preset": "minimal", "vertical_flip": False})
        self.assertTrue(job["headers"]["Idempotency-Key"].startswith("mcp-"))
        self.assertEqual([p.name for p in (self.media / "spoofzy-output").iterdir()], ["one-spoofzy-01.mov"])
        self.assertIn("1 prepaid unit", text_of(result))

    def test_versions_without_batches_respect_concurrency(self):
        self.fake.account["concurrency_limit"] = 1
        source = self.write("multi.jpg", JPEG)
        result = self.client.call("spoofzy_spoof", {"files": [str(source)], "versions": 3})
        self.assertFalse(result["isError"], text_of(result))
        self.assertEqual(len(self.fake.api_jobs), 3)
        self.assertEqual(len(list((self.media / "spoofzy-output").iterdir())), 3)
        self.assertEqual(self.fake.account["units_remaining"], 97)

    def test_batch_and_retry(self):
        self.fake.account["batch_limit"] = 10
        source = self.write("batch.jpg", JPEG)
        arguments = {"files": [str(source)], "versions": 4, "export_size": "4k", "request_id": "batch-retry-1"}
        result = self.client.call("spoofzy_spoof", arguments)
        self.assertFalse(result["isError"], text_of(result))
        body = json.loads(self.fake.requests("POST", "/api/v1/spoofer/batches")[0]["body"])
        self.assertEqual(body["options"], {"preset": "standard", "vertical_flip": False, "output_size": "4k"})
        self.assertEqual(len(list((self.media / "spoofzy-output").iterdir())), 4)
        self.restart()
        again = self.client.call("spoofzy_spoof", arguments)
        self.assertEqual(again["structuredContent"]["runs"][0]["run_id"],
                         result["structuredContent"]["runs"][0]["run_id"])
        self.assertEqual(len(self.fake.batches), 1)
        self.assertEqual(len(self.fake.requests("POST", "/api/v1/spoofer/batches")), 1)

    def test_refusals(self):
        source = self.write("r.jpg", JPEG)
        for arguments, expected in (
                ({"look": "subtle"}, "classic and minimal looks"),
                ({"look": "minimal", "mirror": True}, "cannot be combined with mirror"),
                ({"watermark": {"text": "x"}}, "account's saved watermark"),
                ({"versions": 101}, "needs 101 prepaid units"),
                ({"export_size": "4k"}, "needs prepaid batch access")):
            result = self.client.call("spoofzy_spoof", dict(arguments, files=[str(source)]))
            self.assertTrue(result["isError"], arguments)
            self.assertIn(expected, text_of(result))
        heic = self.write("h.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40)
        self.assertIn("takes JPG, PNG, WebP, MP4, M4V and MOV",
                      text_of(self.client.call("spoofzy_spoof", {"files": [str(heic)]})))
        self.assertEqual(self.fake.api_jobs, {})


class UnitTests(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(mcp.sniff(JPEG), "image/jpeg")
        self.assertEqual(mcp.sniff(PNG), "image/png")
        self.assertEqual(mcp.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertEqual(mcp.sniff(b"GIF89a...."), "image/gif")
        self.assertEqual(mcp.sniff(b"\x00\x00\x00\x18ftypheic...."), "image/heic")
        self.assertEqual(mcp.sniff(b"\x00\x00\x00\x18ftypmif1...."), "image/heif")
        self.assertEqual(mcp.sniff(b"\x00\x00\x00\x18ftypavif...."), None)
        self.assertEqual(mcp.sniff(b"\x00\x00\x00\x14ftypqt  ...."), "video/quicktime")
        self.assertEqual(mcp.sniff(b"\x00\x00\x00\x08wide...."), "video/quicktime")
        self.assertEqual(mcp.sniff(MP4), "video/mp4")
        self.assertEqual(mcp.sniff(b"\x1a\x45\xdf\xa3...."), "video/webm")
        self.assertIsNone(mcp.sniff(b"%PDF-1.7"))

    def test_inspect_media_names_and_types(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "Café Été.M4V").write_bytes(MP4)
            cloud = mcp.inspect_media(folder / "Café Été.M4V", "cloud")
            self.assertEqual((cloud.upload_name, cloud.content_type), ("Cafe-Ete.mp4", "video/mp4"))
            prepaid = mcp.inspect_media(folder / "Café Été.M4V", "api")
            self.assertEqual(prepaid.upload_name, "Cafe-Ete.m4v")
            (folder / "photo.jpeg").write_bytes(PNG)
            self.assertEqual(mcp.inspect_media(folder / "photo.jpeg", "cloud").upload_name, "photo.png")
            (folder / "empty.jpg").write_bytes(b"")
            with self.assertRaises(mcp.SpoofzyError):
                mcp.inspect_media(folder / "empty.jpg", "cloud")
        finally:
            shutil.rmtree(folder)

    def test_reserve_and_publish_never_overwrite(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "a-spoofzy-07.jpg").write_bytes(b"old")
            (folder / "a-spoofzy-08.jpg.part").write_bytes(b"in progress")
            reserved = mcp.reserve_names(folder, "a", "spoofzy", [".jpg", ".mp4"])
            self.assertEqual([final.name for final, _ in reserved], ["a-spoofzy-09.jpg", "a-spoofzy-10.mp4"])
            final, part = reserved[0]
            part.write_bytes(b"new")
            final.write_bytes(b"someone else")
            published = mcp.publish(part, final)
            self.assertEqual(published.name, "a-spoofzy-09-2.jpg")
            self.assertEqual(final.read_bytes(), b"someone else")
            self.assertEqual(published.read_bytes(), b"new")
            self.assertFalse(part.exists())
        finally:
            shutil.rmtree(folder)

    def test_settings(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "credentials.json").write_text(json.dumps({"api_key": API_KEY, "output_dir": "/x"}))
            os.chmod(str(folder / "credentials.json"), 0o600)
            settings = mcp.Settings({"SPOOFZY_CONFIG_DIR": str(folder), "SPOOFZY_API_KEY": CLOUD_KEY,
                                     "SPOOFZY_CLOUD_API_KEY": "${SPOOFZY_CLOUD_API_KEY}"})
            self.assertEqual(settings.cloud_key, CLOUD_KEY)  # a Cloud key in the wrong variable still works
            self.assertEqual(settings.api_key, API_KEY)
            self.assertEqual(settings.output_dir, "/x")
            self.assertEqual(settings.notes, [])
            odd = mcp.Settings({"SPOOFZY_CONFIG_DIR": str(folder), "SPOOFZY_CLOUD_API_KEY": "hello",
                                "SPOOFZY_CLOUD_API_URL": "http://evil.example/v1", "SPOOFZY_WAIT_SECONDS": "9999"})
            self.assertTrue(odd.notes)
            self.assertIsNone(odd.cloud_url)
            self.assertEqual(odd.wait_seconds, 3600)
            self.assertFalse(odd.fast_poll)
        finally:
            shutil.rmtree(folder)

    def test_collect_outputs_shapes(self):
        self.assertEqual(len(mcp.collect_outputs({"download_url": "https://a/1.jpg", "url": "https://a/1.jpg"})), 1)
        many = mcp.collect_outputs({"outputs": [{"download_url": "https://a/{}.jpg".format(n)} for n in range(3)],
                                    "zip_url": "https://a/all.zip"})
        self.assertEqual([item["url"] for item in many], ["https://a/0.jpg", "https://a/1.jpg", "https://a/2.jpg"])
        self.assertEqual(mcp.collect_outputs({"zip_url": "https://a/all.zip"})[0]["url"], "https://a/all.zip")
        self.assertEqual(mcp.collect_outputs(None), [])

    def test_validate_and_coerce(self):
        schema = mcp.TOOLS_BY_NAME["spoofzy_spoof"]["inputSchema"]
        args = mcp.coerce({"files": "/a.jpg", "versions": "5", "mirror": "true"}, schema)
        self.assertEqual(args, {"files": ["/a.jpg"], "versions": 5, "mirror": True})
        self.assertEqual(mcp.validate(args, schema), [])
        self.assertTrue(mcp.validate({"request_id": "bad id!"}, schema))
        self.assertTrue(mcp.validate({"versions": True}, schema))

    def test_checked_url(self):
        mcp.checked_url("https://content.spoofzy.com/a.jpg", "download")
        mcp.checked_url("http://127.0.0.1:9/a.jpg", "download")
        for bad in ("http://example.com/a.jpg", "file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
            with self.assertRaises(mcp.SpoofzyError):
                mcp.checked_url(bad, "download")


if __name__ == "__main__":
    unittest.main()
