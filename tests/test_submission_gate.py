import hashlib
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import httpx
import pytest

from applypilot.apply import chrome
from applypilot.apply.gate import PreparedAdapter, SubmissionGate, UnsupportedAdapter, prepare_adapter, validate_payload
from applypilot.apply.mcp_proxy import guard_call
from applypilot.artifacts import write_manifest


@pytest.fixture
def native_adapter():
    return PreparedAdapter("https://example.test/job/1", "https://example.test/apply", (),
                           {"name": "Test Person", "email": "test@example.com", "job_id": "1", "terms": "on"},
                           {"resume": hashlib.sha256(b"%PDF-test").hexdigest()}, "Test Company", "Engineer")


def multipart(fields, pdf=b"%PDF-test"):
    request = httpx.Request("POST", "https://example.test/apply", data=fields,
                            files={"resume": ("resume.pdf", pdf, "application/pdf")})
    return request.headers["content-type"], request.read()


def test_valid_exact_payload(native_adapter):
    content_type, body = multipart(native_adapter.fields)
    validate_payload(content_type, body, native_adapter)


@pytest.mark.parametrize("change", ["wrong_job", "invented_answer", "extra_field", "missing_consent", "wrong_pdf"])
def test_payload_rejects_incorrect_job_answers_consent_or_artifact(native_adapter, change):
    fields = dict(native_adapter.fields)
    if change == "wrong_job":
        fields["job_id"] = "2"
    if change == "invented_answer":
        fields["name"] = "Someone Else"
    if change == "extra_field":
        fields["work_authorization"] = "yes"
    if change == "missing_consent":
        fields.pop("terms")
    content_type, body = multipart(fields, b"%PDF-wrong" if change == "wrong_pdf" else b"%PDF-test")
    with pytest.raises(ValueError):
        validate_payload(content_type, body, native_adapter)


def test_missing_adapter_fails_before_browser(tmp_path):
    with pytest.raises(UnsupportedAdapter, match="No reviewed"):
        prepare_adapter({"url": "https://example.test/1"}, {}, adapter_path=tmp_path / "missing.json")


def test_adapter_requires_explicit_consent_and_correct_identity(tmp_path):
    job = {"url": "https://example.test/job/1", "title": "Engineer", "company": "Test Company"}
    resume = tmp_path / "resume.txt"
    resume.write_text("Test Person factual resume")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-test")
    job["tailored_resume_path"] = str(resume)
    write_manifest(job, resume, kind="resume", approved=True)
    descriptor = {"kind": "native_html_v1", "job_url": job["url"], "application_url": job["url"],
                  "submission_url": "https://example.test/apply", "company": job["company"], "title": job["title"],
                  "fields": {"name": "personal.full_name"}, "consents": {"terms": "legal.terms"},
                  "job_id_field": "job_id", "job_id": "1", "files": {"resume": "resume"}}
    path = tmp_path / "adapters.json"
    path.write_text(json.dumps({"adapters": [descriptor]}))
    profile = {"personal": {"full_name": "Test Person"}, "legal": {"terms": None}}
    with pytest.raises(UnsupportedAdapter, match="explicit scalar"):
        prepare_adapter(job, profile, adapter_path=path)
    profile["legal"]["terms"] = "on"
    assert prepare_adapter(job, profile, adapter_path=path).fields["terms"] == "on"
    descriptor["company"] = "Wrong company"
    path.write_text(json.dumps({"adapters": [descriptor]}))
    with pytest.raises(UnsupportedAdapter, match="employer/title"):
        prepare_adapter(job, profile, adapter_path=path)


def test_mcp_proxy_blocks_code_navigation_and_shortcuts(tmp_path):
    policy = {"urls": ["https://example.test/1"], "file_sha256": []}
    for name, args in [("browser_evaluate", {}), ("browser_run_code", {}),
                       ("browser_navigate", {"url": "javascript:alert(1)"}),
                       ("browser_navigate", {"url": "data:text/html,private"}),
                       ("browser_navigate", {"url": "https://other.test/"}),
                       ("browser_tabs", {"action": "new"}),
                       ("browser_press_key", {"key": "Control+L"})]:
        with pytest.raises(ValueError):
            guard_call(name, args, policy)
    guard_call("browser_navigate", {"url": "https://example.test/1"}, policy)
    document = tmp_path / "private.pdf"
    document.write_bytes(b"not-approved")
    with pytest.raises(ValueError, match="not an approved"):
        guard_call("browser_file_upload", {"paths": [str(document)]}, policy)
    with pytest.raises(ValueError, match="Output filename"):
        guard_call("browser_snapshot", {"filename": "../outside.md"}, policy)


def test_simultaneous_submissions_consume_one_atomic_token(native_adapter):
    gate = SubmissionGate(9999, {"url": native_adapter.application_url}, native_adapter)

    def attempt(_):
        try:
            gate._consume_submission(native_adapter.submission_url, b"verified-payload")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(32)))
    assert sum(results) == 1


@pytest.mark.parametrize("received", [False, True])
def test_confirmation_get_requires_actual_redirect(native_adapter, received):
    from dataclasses import replace
    adapter = replace(native_adapter, confirmation_urls=("https://example.test/thanks",))
    gate = SubmissionGate(9999, {"url": adapter.application_url}, adapter)
    gate._record(response_received=received)
    route = Mock()
    route.request.method = "GET"
    route.request.url = adapter.confirmation_urls[0]
    gate._route(route)
    route.fetch.assert_not_called()
    route.abort.assert_called_once()


@pytest.mark.browser
@pytest.mark.parametrize("visible_company", ["Test Company", "Wrong Company"])
def test_native_gate_sends_only_one_verified_submission(tmp_path, monkeypatch, visible_company):
    from playwright.sync_api import sync_playwright

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'''<h1>Engineer at Test Company</h1>
            <form action="/apply" method="post" enctype="multipart/form-data">
            <input name="job_id" type="hidden" value="1">
            <input name="name"><input name="email"><input name="terms" type="checkbox">
            <input name="resume" type="file"><button>Submit</button></form>
            <script>document.body.dataset.scriptRan='yes'; fetch('/unreviewed', {method:'POST'});</script>'''.replace(
                b"Engineer at Test Company", f"Engineer at {visible_company}".encode()))

        def do_POST(self):
            requests.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<h1>Application received: Engineer at Test Company</h1><p>ID: fixture-1</p>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    adapter = PreparedAdapter(origin + "/job/1", origin + "/apply", (),
                              {"name": "Test Person", "email": "test@example.com", "job_id": "1", "terms": "on"},
                              {"resume": hashlib.sha256(b"%PDF-test").hexdigest()}, "Test Company", "Engineer")
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "chrome")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    worker = 988
    process = chrome.launch_chrome(worker, port=port, headless=True)
    try:
        job = {"url": adapter.application_url, "claim_token": "fixture-attempt"}
        evidence_path = tmp_path / "gate-evidence.json"
        with SubmissionGate(port, job, adapter, evidence_path=evidence_path) as gate:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                page = browser.contexts[0].pages[0]
                page.goto(adapter.application_url)
                assert page.locator("body").get_attribute("data-script-ran") is None
                page.locator('[name="name"]').fill("Test Person")
                page.locator('[name="email"]').fill("test@example.com")
                page.locator('[name="terms"]').check()
                page.locator('[name="resume"]').set_input_files({"name": "resume.pdf", "mimeType": "application/pdf",
                                                                "buffer": b"%PDF-test"})
                page.get_by_role("button", name="Submit").click()
                if visible_company == "Wrong Company":
                    assert gate.evidence()["submission_attempted"] is False
                    assert any("Visible employer/title" in item["reason"] for item in gate.evidence()["blocked"])
                    assert not [r for r in requests if r[0] == "POST"]
                    return
                page.get_by_text("ID: fixture-1").wait_for()
                evidence = gate.evidence()
                assert evidence["request_validated"] is True
                assert evidence["response_received"] is True
                assert evidence["response_status"] == 200
                assert evidence["attempt_id"] == "fixture-attempt"
                assert evidence["confirmation_url"] == adapter.submission_url
                assert json.loads(evidence_path.read_text())["request_validated"] is True
                # Reload the form and try a second submission; the one-use gate rejects it.
                page.goto(adapter.application_url)
                page.locator('[name="name"]').fill("Test Person")
                page.locator('[name="email"]').fill("test@example.com")
                page.locator('[name="terms"]').check()
                page.locator('[name="resume"]').set_input_files({"name": "resume.pdf", "mimeType": "application/pdf",
                                                                "buffer": b"%PDF-test"})
                page.get_by_role("button", name="Submit").click()
                assert any("already consumed" in item["reason"] for item in gate.evidence()["blocked"])
    finally:
        chrome.cleanup_worker(worker, process)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert [r for r in requests if r[0] == "POST"] == [("POST", "/apply")]

def test_redirect_error_page_never_becomes_verified_receipt(native_adapter):
    from dataclasses import replace
    adapter = replace(native_adapter, confirmation_urls=("https://example.test/thanks",))
    gate = SubmissionGate(9999, {"url": adapter.application_url}, adapter)
    gate._record(response_received=True, redirect_confirmation_url=adapter.confirmation_urls[0])
    route = Mock()
    route.request.method = "GET"
    route.request.url = adapter.confirmation_urls[0]
    route.fetch.return_value.status = 500
    route.fetch.return_value.headers = {"content-type": "text/html"}
    gate._route(route)
    assert gate.evidence()["confirmation_response_status"] == 500
    assert "confirmation_url" not in gate.evidence()
