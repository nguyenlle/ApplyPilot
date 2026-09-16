"""Orchestration boundaries that previously caused accidental submissions."""

import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from applypilot import cli
from applypilot.apply import launcher


@pytest.mark.parametrize("text", ["RESULT:APPLIED", "RESULT_JSON:bad json", "",
    'RESULT_JSON:{"status":"applied"}\nRESULT_JSON:{"status":"failed"}'])
def test_legacy_or_ambiguous_agent_output_is_never_success(text):
    assert launcher.parse_result(text)["status"] == "submission_uncertain"


def test_structured_result_preserves_missing_questions():
    result = {"status": "needs_input", "missing_fields": ["consent"]}
    assert launcher.parse_result("RESULT_JSON:" + json.dumps(result)) == result


def test_small_batch_does_not_allocate_zero_continuous_workers(monkeypatch):
    worker = Mock(return_value=(0, 0))
    monkeypatch.setattr(launcher, "worker_loop", worker)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(launcher, "kill_all_chrome", lambda: None)
    launcher.main(limit=1, workers=3)
    assert worker.call_count == 1
    assert worker.call_args.args[1] == 1


def test_zero_bounded_batch_does_not_start_worker(monkeypatch):
    worker = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(launcher, "worker_loop", worker)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda: None)
    launcher.main(limit=0, workers=3)
    worker.assert_not_called()


def test_target_cannot_spawn_parallel_workers():
    with pytest.raises(ValueError, match="one worker"):
        launcher.main(target_url="https://example.test/1", workers=2)


def test_doctor_reports_failure_exit_code(monkeypatch):
    from applypilot import diagnostics
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda **_: {
        "ok": False, "checks": [{"name": "Claude authentication", "ok": False,
                                 "required": True, "detail": "Sign in required"}]})
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "Sign in required" in result.stdout


def test_generated_mcp_config_does_not_include_email():
    servers = launcher._make_mcp_config(9222)["mcpServers"]
    assert set(servers) == {"playwright"}
    assert "@latest" not in json.dumps(servers)


def test_unsafe_code_tools_not_in_agent_allowlist():
    assert "browser_evaluate" not in launcher.ALLOWED_BROWSER_TOOLS
    assert "browser_run_code" not in launcher.ALLOWED_BROWSER_TOOLS


def test_continuous_preparation_bounded_cycles(monkeypatch):
    import time

    from applypilot import config, pipeline
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(config, "check_tier", lambda *args: None)
    run = Mock(return_value={"errors": []})
    pause = Mock()
    monkeypatch.setattr(pipeline, "run_pipeline", run)
    monkeypatch.setattr(time, "sleep", pause)
    result = CliRunner().invoke(cli.app, ["watch", "--cycles", "2"])
    assert result.exit_code == 0, result.stdout
    assert run.call_count == 2
    assert pause.call_count == 1


def test_browser_confirmation_requires_causal_submission_before_connecting():
    evidence = launcher.inspect_confirmation(1, {"url": "https://example.test/job", "claim_token": "new"},
                                            {"status": "applied"}, {"attempt_id": "old"})
    assert evidence["browser_corroborated"] is False
