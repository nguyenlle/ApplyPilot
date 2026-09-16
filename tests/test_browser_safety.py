import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from applypilot.apply import chrome, dryrun, prompt


@pytest.fixture
def profile():
    return {"personal": {"full_name": "Test Person", "email": "test@example.com", "password": "NEVEREXPOSE"},
            "work_authorization": {"require_sponsorship": None}, "compensation": {"salary_expectation": None}}


def test_prompt_no_fabricated_defaults_or_secrets(tmp_path, monkeypatch, profile):
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    monkeypatch.setattr(prompt.config, "load_search_config", lambda: {})
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", tmp_path / "workers")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"pdf-A")
    text = prompt.build_prompt({"url": "https://example.com/1", "title": "Engineer",
                                "tailored_resume_path": str(source)}, "Factual resume", dry_run=True)
    assert "NEVEREXPOSE" not in text
    assert "Age 18+: Yes" not in text
    assert "Felony: No" not in text
    assert "available immediately" not in text
    assert "answer YES" not in text
    assert "RESULT:APPLIED" not in text
    assert "RESULT_JSON:" in text
    assert "needs_input" in text
    assert "\"require_sponsorship\": null" in text
    assert "captcha_blocked" in text
    assert "api.capsolver" not in text


def test_concurrent_job_uploads_cannot_overwrite(tmp_path, monkeypatch, profile):
    monkeypatch.setattr(prompt.config, "load_profile", lambda: profile)
    monkeypatch.setattr(prompt.config, "load_search_config", lambda: {})
    monkeypatch.setattr(prompt.config, "APPLY_WORKER_DIR", tmp_path / "workers")
    jobs = []
    for n in range(8):
        resume = tmp_path / f"{n}.pdf"
        resume.write_bytes(f"job-{n}".encode())
        jobs.append({"url": f"https://example.com/{n}", "title": "Same title", "tailored_resume_path": str(resume)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda j: prompt.build_prompt(j, "factual"), jobs))
    manifests = list((tmp_path / "workers").rglob("manifest.json"))
    assert len(manifests) == 8
    for path in manifests:
        manifest = json.loads(path.read_text())
        from pathlib import Path
        expected = "job-" + manifest["job_url"].rsplit("/", 1)[1]
        assert Path(manifest["files"]["resume"]["path"]).read_bytes() == expected.encode()


def test_profile_never_reads_personal_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path)
    forbidden = Mock(side_effect=AssertionError("Personal profile must not be accessed"))
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", forbidden)
    path = chrome.setup_worker_profile(0)
    (path / "session-marker").write_text("preserve")
    assert chrome.setup_worker_profile(0) == path
    assert (path / "session-marker").read_text() == "preserve"
    forbidden.assert_not_called()


def test_cleanup_cannot_kill_unowned_process(monkeypatch):
    killer = Mock()
    monkeypatch.setattr(chrome, "_kill_process_tree", killer)
    chrome.cleanup_worker(900, Mock())
    chrome.kill_all_chrome()
    killer.assert_not_called()


def test_profile_lock_exclusive_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path)
    first = chrome._acquire_profile_lock(0)
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            chrome._acquire_profile_lock(0)
    finally:
        first.close()
    again = chrome._acquire_profile_lock(0)
    again.close()


def test_busy_port_refuses_without_process_killing(monkeypatch):
    monkeypatch.setattr(chrome, "_port_available", lambda port: False)
    killer = Mock()
    monkeypatch.setattr(chrome, "_kill_process_tree", killer)
    with pytest.raises(RuntimeError, match="occupied"):
        chrome.launch_chrome(500)
    killer.assert_not_called()


def test_exact_field_mapping_does_not_guess_names_or_legal(profile):
    assert dryrun._field_value(["employer name"], profile) == (None, None)
    assert dryrun._field_value(["first name"], profile) == (None, None)
    assert dryrun._field_value(["authorized to work"], profile) == (None, None)
    assert dryrun._field_value(["Citizenship", "email"], profile) == (None, None)
    assert dryrun._field_value(["Email address"], profile) == ("email", "test@example.com")


@pytest.mark.browser
def test_browser_dryrun_fills_without_autosave_or_submit(tmp_path, profile):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path))
            port = self.server.server_port
            doc = f'''<html><body><h1>Application for Test Employer</h1>
            <form action="http://127.0.0.1:{port}/submit" method="post">
            <label>Email address<input name="email" type="email" required></label>
            <label>Full name<input name="fullname" required></label>
            <label>Citizenship<input name="citizenship" required></label>
            <label>Legal consent<input type="checkbox" required></label>
            <button type="submit">Submit application</button></form>
            <script>document.querySelector('[name=email]').addEventListener('input', () => {{
            const proof = document.createElement('p'); proof.textContent='AUTOSUBMIT_ATTEMPTED';
            document.body.appendChild(proof);
            fetch('http://127.0.0.1:{port}/autosave', {{method:'POST',body:'private'}}).catch(()=>{{}});
            document.querySelector('form').submit();
            new Image().src='http://127.0.0.1:{port}/tracking?private=1';
            }});</script></body></html>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(doc.encode())

        def do_POST(self):
            requests.append(("POST", self.path))
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = dryrun.run_dry_run({"url": f"http://127.0.0.1:{server.server_port}/job", "title": "Test"},
                                    profile=profile, output_dir=tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert requests == [("GET", "/job")]
    assert result["status"] == "dry_run"
    assert result["verification_state"] == "not_submitted"
    assert len(result["filled_fields"]) == 2
    assert len(result["missing_fields"]) == 2
    assert (tmp_path / "form.png").is_file()
    assert (tmp_path / "evidence.json").is_file()
    assert "AUTOSUBMIT_ATTEMPTED" in (tmp_path / "page.txt").read_text()

@pytest.mark.browser
def test_owned_chrome_graceful_close_preserves_dedicated_session(tmp_path, monkeypatch):
    import socket
    import time
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "profiles")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    worker_id = 872
    first = chrome.launch_chrome(worker_id, port=port, headless=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            browser.contexts[0].add_cookies([{"name": "test_session", "value": "retained",
                                            "domain": "example.test", "path": "/", "expires": time.time() + 3600}])
    finally:
        chrome.cleanup_worker(worker_id, first)
    assert first.poll() is not None
    second = chrome.launch_chrome(worker_id, port=port, headless=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            cookies = browser.contexts[0].cookies()
            assert any(c["name"] == "test_session" and c["value"] == "retained" for c in cookies)
    finally:
        chrome.cleanup_worker(worker_id, second)
    assert second.poll() is not None

def test_unix_cleanup_includes_descendants_with_separate_groups(monkeypatch):
    import signal
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(chrome.platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome.subprocess, "run", lambda *a, **kw: Mock(stdout="900 1\n901 900\n902 901\n999 1\n"))
    killed = []
    monkeypatch.setattr(chrome.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(chrome.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(chrome.os, "killpg", lambda pid, sig: killed.append(("group", pid)), raising=False)
    chrome._kill_process_tree(900)
    assert killed == [902, 901, ("group", 900)]
