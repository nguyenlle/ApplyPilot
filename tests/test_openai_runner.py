import asyncio
import hashlib
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from applypilot.apply import chrome
from applypilot.apply import openai_runner as runner


def function_call(name="browser_snapshot", arguments="{}", call_id="call-1"):
    return {"type": "function_call", "id": "fc-1", "name": name, "arguments": arguments, "call_id": call_id}


def response(output, status="completed"):
    return {"id": "response-test", "status": status, "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}


def final_result(status="needs_input"):
    return {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": 'RESULT_JSON:' + json.dumps({"status": status, "missing_fields": []})}]}


class FakeMCP:
    instances: ClassVar[list] = []

    def __init__(self, *args):
        self.calls = []
        self.closed = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def list_tools(self):
        return [{"name": "browser_snapshot", "description": "Read page", "inputSchema": {
                    "type": "object", "properties": {"filename": {"type": "string"}}}},
                {"name": "browser_evaluate", "inputSchema": {"type": "object", "properties": {}}}]

    def request(self, method, params):
        self.calls.append((method, params))
        return {"content": [{"type": "text", "text": "Visible fixture page. Required work authorization missing."}]}


@pytest.fixture
def setup_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-not-a-real-key")
    monkeypatch.setattr(runner, "MCPClient", FakeMCP)
    FakeMCP.instances.clear()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"urls": ["https://example.test/job"], "file_sha256": []}))
    return {"prompt": "Factual fixture application", "port": 9222, "policy_path": policy,
            "model": "gpt-4o-mini", "timeout_seconds": 10, "max_steps": 4, "max_output_tokens": 500,
            "log_path": tmp_path / "trace.json"}


@pytest.fixture
def response_server(monkeypatch):
    responses = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"payload": payload, "authorization": self.headers.get("Authorization"), "path": self.path})
            status, body, delay = responses.pop(0)
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(runner, "RESPONSES_URL", f"http://127.0.0.1:{server.server_port}/v1/responses")
    try:
        yield responses, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_responses_loop_preserves_reasoning_filters_tools_and_records_usage(setup_runner, response_server):
    responses, requests = response_server
    reasoning = {"type": "reasoning", "id": "reasoning-1", "summary": [], "encrypted_content": "fixture-ciphertext"}
    responses.extend([(200, response([reasoning, function_call()]), 0), (200, response([final_result()]), 0)])
    result = runner.run_agent(**setup_runner)
    assert result["result"]["status"] == "needs_input"
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}
    assert result["steps"] == 2
    assert requests[0]["path"] == "/v1/responses"
    payload = requests[0]["payload"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["store"] is False
    assert payload["parallel_tool_calls"] is False
    assert [tool["name"] for tool in payload["tools"]] == ["browser_snapshot"]
    assert payload["tools"][0]["strict"] is False
    history = requests[1]["payload"]["input"]
    assert history[1] == reasoning
    assert history[-1]["type"] == "function_call_output"
    assert history[-1]["call_id"] == "call-1"
    assert history[-1]["output"][0]["type"] == "input_text"
    assert FakeMCP.instances[0].closed
    assert len(FakeMCP.instances[0].calls) == 1
    assert "synthetic-secret-not-a-real-key" not in Path(result["trace_path"]).read_text()


@pytest.mark.parametrize("call", [function_call("browser_evaluate"), function_call(arguments="[]"),
                                 function_call(arguments='{bad'), function_call(arguments='{"a":1,"a":2}'),
                                 function_call(arguments='{"a":NaN}'), function_call(call_id=""),
                                 function_call(arguments='{"filename":"../outside.md"}')])
def test_malformed_or_forbidden_tool_never_executes(setup_runner, response_server, call):
    responses, requests = response_server
    responses.append((200, response([call]), 0))
    with pytest.raises(runner.RunnerError):
        runner.run_agent(**setup_runner)
    assert not FakeMCP.instances[0].calls
    assert FakeMCP.instances[0].closed
    assert len(requests) == 1


@pytest.mark.parametrize("outputs,status", [([function_call()], "incomplete"),
                                            ([function_call(), function_call(call_id="call-2")], "completed")])
def test_partial_or_parallel_response_never_executes(setup_runner, response_server, outputs, status):
    response_server[0].append((200, response(outputs, status), 0))
    with pytest.raises(runner.RunnerError):
        runner.run_agent(**setup_runner)
    assert not FakeMCP.instances[0].calls


def test_step_budget_prevents_extra_browser_action(setup_runner, response_server):
    setup_runner["max_steps"] = 1
    response_server[0].append((200, response([function_call()]), 0))
    with pytest.raises(runner.RunnerError, match="budget exhausted"):
        runner.run_agent(**setup_runner)
    assert not FakeMCP.instances[0].calls


def test_repeated_call_id_never_replays_browser_action(setup_runner, response_server):
    response_server[0].extend([(200, response([function_call()]), 0)] * 2)
    with pytest.raises(runner.RunnerError, match="repeated tool-call"):
        runner.run_agent(**setup_runner)
    assert len(FakeMCP.instances[0].calls) == 1


@pytest.mark.parametrize("http_status,error,category", [
    (429, {"code": "credit_balance_exhausted"}, "provider_quota"),
    (429, {"code": "insufficient_quota"}, "provider_quota"),
    (429, {"code": "rate_limit_exceeded"}, "rate_limited"),
    (401, {"message": "synthetic-secret-not-a-real-key"}, "auth_required"),
    (503, {}, "transient_network"),
])
def test_provider_failures_do_not_retry_or_disclose_body(setup_runner, response_server, http_status, error, category):
    responses, requests = response_server
    responses.append((http_status, {"error": error}, 0))
    with pytest.raises(runner.RunnerError) as caught:
        runner.run_agent(**setup_runner)
    assert caught.value.category == category
    assert len(requests) == 1
    assert not FakeMCP.instances[0].calls
    assert FakeMCP.instances[0].closed
    assert "synthetic-secret-not-a-real-key" not in str(caught.value)
    assert "synthetic-secret-not-a-real-key" not in setup_runner["log_path"].read_text()


def test_http_wall_deadline_cancels_slow_response(response_server):
    response_server[0].append((200, response([final_result()]), 0.5))
    started = time.monotonic()
    with pytest.raises(runner.RunnerError):
        asyncio.run(runner._post_response({}, "synthetic", started + 0.1, None))
    assert time.monotonic() - started < 0.4


def test_stop_event_cancels_http_request(response_server):
    response_server[0].append((200, response([final_result()]), 1))
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        with pytest.raises(runner.RunnerError) as caught:
            asyncio.run(runner._post_response({}, "synthetic", time.monotonic() + 10, stop))
        assert caught.value.category == "cancelled"
    finally:
        timer.cancel()


def test_cleanup_transport_error_cannot_replace_sanitized_deadline(monkeypatch):
    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *_args, **_kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise httpx.ReadTimeout("sensitive transport detail") from None

    monkeypatch.setattr(runner.httpx, "AsyncClient", Client)
    with pytest.raises(runner.RunnerError, match="deadline exceeded"):
        asyncio.run(runner._post_response({}, "synthetic", time.monotonic() + 0.05, None))


def test_mcp_environment_drops_all_credentials(monkeypatch):
    for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY",
                "CAPSOLVER_API_KEY", "MY_COMPANY_PASSWORD", "NPM_TOKEN", "GITHUB_TOKEN"]:
        monkeypatch.setenv(key, "private-value")
    monkeypatch.setenv("npm_config_cache", "fixture-cache")
    environment = runner.child_environment()
    assert "private-value" not in environment.values()
    assert {key.lower(): value for key, value in environment.items()}["npm_config_cache"] == "fixture-cache"
    assert any(key.lower() == "path" for key in environment)


def test_screenshot_outputs_pass_images_without_resource_fetch():
    output = runner.tool_output({"content": [{"type": "text", "text": "Screenshot"},
                               {"type": "image", "mimeType": "image/png", "data": "ZmFrZQ=="},
                               {"type": "resource_link", "uri": "file:///private"}]})
    assert output == [{"type": "input_text", "text": "Screenshot"},
                      {"type": "input_image", "image_url": "data:image/png;base64,ZmFrZQ=="}]


@pytest.mark.browser
def test_real_pinned_mcp_client_reads_local_browser_and_closes(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<h1>OpenAI runner local fixture</h1><p>No submissions.</p>'
                             b'<label>Resume<input type="file"></label>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/job"
    policy = tmp_path / "policy.json"
    upload = tmp_path / "uploads" / "job-fixture" / "resume.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"%PDF-1.4 synthetic fixture only")
    policy.write_text(json.dumps({"urls": [url], "file_sha256": [hashlib.sha256(upload.read_bytes()).hexdigest()]}))
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "profiles")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = chrome.launch_chrome(711, port=port, headless=True)
    try:
        with runner.MCPClient(port, policy, time.monotonic() + 45, None, tmp_path / "mcp.log") as mcp:
            tools = runner.response_tools(mcp.list_tools())
            assert tools and all(tool["name"] in runner.ALLOWED for tool in tools)
            assert not any(tool["name"] == "browser_evaluate" for tool in tools)
            result = mcp.request("tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
            assert not result.get("isError")
            result = mcp.request("tools/call", {"name": "browser_snapshot", "arguments": {}})
            assert "OpenAI runner local fixture" in json.dumps(result)
            snapshot = "\n".join(item.get("text", "") for item in result["content"])
            file_ref = re.search(r'button "Resume" \[ref=(e\d+)\]', snapshot)
            assert file_ref, snapshot
            clicked = mcp.request("tools/call", {"name": "browser_click", "arguments": {
                "target": file_ref.group(1)}})
            assert not clicked.get("isError"), clicked
            uploaded = mcp.request("tools/call", {"name": "browser_file_upload", "arguments": {"paths": [str(upload)]}})
            assert not uploaded.get("isError"), uploaded
            rejected = mcp.request("tools/call", {"name": "browser_evaluate", "arguments": {"function": "() => 1"}})
            assert rejected["isError"] is True
            owned_pid = mcp.process.pid
        assert mcp.process.poll() is not None
        assert owned_pid > 0
    finally:
        chrome.cleanup_worker(711, process)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
