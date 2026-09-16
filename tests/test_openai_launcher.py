"""OpenAI launcher boundary tests: no paid API or external browser operations."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from applypilot import config, database
from applypilot.apply import dryrun, launcher, openai_runner, state
from applypilot.policy import RuntimePolicy


class FakeGate:
    def __init__(self, job, proof=None):
        self.adapter = SimpleNamespace(application_url=job["url"], confirmation_urls=(),
                                       files={"resume": "synthetic-digest"})
        self.proof = proof or {
            "attempt_id": job.get("claim_token"), "job_url": job["url"],
            "submission_attempted": False, "request_validated": False,
            "response_received": False,
        }

    def evidence(self):
        return deepcopy(self.proof)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    logs = tmp_path / "logs"
    logs.mkdir()
    work = tmp_path / "worker"
    work.mkdir()
    resume = tmp_path / "resume.txt"
    resume.write_text("Synthetic verified candidate resume.", encoding="utf-8")
    policy = replace(RuntimePolicy(), apply_max_steps=17, apply_max_output_tokens=777,
                     apply_timeout_seconds=123, max_per_day=50, max_per_company=50)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(config, "load_blocked_sites", lambda: (set(), []))
    monkeypatch.setattr(config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(config, "load_search_config", dict)
    monkeypatch.setattr(config, "validate_profile_for_application", dict)
    monkeypatch.setattr(config, "apply_model", lambda: "synthetic-default-model")
    monkeypatch.setattr(config, "resolve_claude", lambda: pytest.fail("OpenAI must never query Claude"))
    monkeypatch.setattr(state, "load_policy", lambda: policy)
    monkeypatch.setattr(launcher, "load_policy", lambda: policy)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _: work)
    monkeypatch.setattr(launcher, "verify_job_artifacts", lambda _: {})
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda *_args, **_kw: "Synthetic instruction")
    monkeypatch.setattr(launcher, "launch_chrome", lambda *_args, **_kw: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *_args, **_kw: None)
    monkeypatch.setattr(launcher, "prepare_adapter", lambda _: object())
    monkeypatch.setattr(openai_runner, "run_agent", lambda *_args, **_kw: pytest.fail("Unexpected model call"))
    launcher._stop_event.clear()
    conn = database.init_db(path)
    conn.execute("INSERT INTO jobs(url,title,company,site,fit_score,tailored_resume_path) VALUES(?,?,?,?,?,?)",
                 ("https://synthetic.example.test/job/123", "Software Engineer", "Synthetic Employer",
                  "synthetic", 9, str(resume)))
    conn.commit()
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    job["claim_token"] = "synthetic-attempt"
    yield SimpleNamespace(conn=conn, path=path, logs=logs, work=work, job=job, policy=policy)
    launcher._stop_event.clear()
    database.close_connection(path)


def claim(setup):
    job = state.select_jobs(target_url=setup.job["url"])
    assert job is not None
    return job


def actual(setup):
    return dict(setup.conn.execute("SELECT * FROM jobs").fetchone())


def install_fake_runner(monkeypatch, result):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {"result": result, "usage": {"input_tokens": 100, "output_tokens": 10}, "steps": 1}

    monkeypatch.setattr(openai_runner, "run_agent", run)
    return calls


def test_runner_without_gate_never_calls_model_or_claims_success(setup):
    category, duration, evidence = launcher.run_job(setup.job, 9222)
    assert category == "form_changed"
    assert duration == 0
    assert "gate" in evidence["reason"]


@pytest.mark.parametrize("explicit,expected", [(None, "synthetic-default-model"), ("chosen-model", "chosen-model")])
def test_model_and_limits_forwarded_without_claude(setup, monkeypatch, explicit, expected):
    calls = install_fake_runner(monkeypatch, {"status": "needs_input", "missing_fields": ["authorization"]})
    gate = FakeGate(setup.job)
    category, duration, evidence = launcher.run_job(setup.job, 9321, worker_id=2, model=explicit, gate=gate)
    assert category == "missing_profile_data"
    assert duration >= 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ("Synthetic instruction", 9321)
    assert args[3:7] == (expected, 123, 17, 777)
    assert kwargs == {"stop_event": launcher._stop_event}
    browser_policy = json.loads(args[2].read_text(encoding="utf-8"))
    assert browser_policy == {"urls": [setup.job["url"]], "file_sha256": ["synthetic-digest"]}
    assert evidence["usage"] == {"input_tokens": 100, "output_tokens": 10}


@pytest.mark.parametrize("status", ["applied", "submission_verified"])
def test_model_success_claim_uses_independent_gate_evidence(setup, monkeypatch, status):
    malicious_proof = {"browser_corroborated": True, "attempt_id": "invented"}
    install_fake_runner(monkeypatch, {"status": status, "evidence": malicious_proof, "missing_fields": []})
    gate = FakeGate(setup.job)
    observations = []

    def inspect(port, job, result, proof):
        observations.append((port, job, result, proof))
        return {"browser_corroborated": False, "gate": proof}

    monkeypatch.setattr(launcher, "inspect_confirmation", inspect)
    category, _, evidence = launcher.run_job(setup.job, 9222, gate=gate)
    assert category == "submission_uncertain"
    assert evidence["browser_corroborated"] is False
    assert observations[0][3] == gate.evidence()
    assert observations[0][3] != malicious_proof


def test_claimed_browser_success_without_valid_gate_cannot_update_applied(setup, monkeypatch):
    job = claim(setup)
    install_fake_runner(monkeypatch, {"status": "applied", "missing_fields": []})
    monkeypatch.setattr(launcher, "inspect_confirmation", lambda *_args: {"browser_corroborated": True})
    category, duration, evidence = launcher.run_job(job, 9222, gate=FakeGate(job))
    state.finish(job, category, evidence, duration)
    assert actual(setup)["apply_status"] == "submission_uncertain"
    assert actual(setup)["applied_at"] is None


def test_worker_totals_match_durable_state_when_success_proof_is_rejected(setup, monkeypatch):
    install_fake_runner(monkeypatch, {"status": "applied", "missing_fields": []})
    monkeypatch.setattr(launcher, "inspect_confirmation", lambda *_args: {"browser_corroborated": True})
    monkeypatch.setattr(launcher, "SubmissionGate", lambda _port, job, _adapter, **_kwargs: FakeGate(job))
    totals = launcher.worker_loop(limit=1, target_url=setup.job["url"])
    assert actual(setup)["apply_status"] == "submission_uncertain"
    assert totals == (0, 1)


@pytest.mark.parametrize("error_category", ["provider_quota", "rate_limited", "auth_required", "transient_network"])
def test_runner_error_after_any_submission_is_uncertain(setup, monkeypatch, error_category):
    job = claim(setup)
    gate = FakeGate(job)
    gate.proof.update(submission_attempted=True, request_validated=True)

    def failed(*_args, **_kwargs):
        raise openai_runner.RunnerError(error_category, "Sanitized synthetic error")

    monkeypatch.setattr(openai_runner, "run_agent", failed)
    category, duration, evidence = launcher.run_job(job, 9222, gate=gate)
    assert category == "submission_uncertain"
    state.finish(job, category, evidence, duration)
    row = actual(setup)
    assert row["apply_status"] == "submission_uncertain"
    assert row["next_retry_at"] is None
    assert state.select_jobs(target_url=job["url"]) is None


def test_pre_submission_quota_is_needs_input_without_auto_retry(setup, monkeypatch):
    def quota(*_args, **_kwargs):
        raise openai_runner.RunnerError("provider_quota", "OpenAI API credit or quota is exhausted")

    monkeypatch.setattr(openai_runner, "run_agent", quota)
    monkeypatch.setattr(launcher, "SubmissionGate", lambda _port, job, _adapter, **_kwargs: FakeGate(job))
    assert launcher.worker_loop(limit=1, target_url=setup.job["url"]) == (0, 1)
    row = actual(setup)
    assert row["apply_status"] == "needs_input"
    assert row["error_category"] == "provider_quota"
    assert row["applied_at"] is None
    assert row["next_retry_at"] is None
    attempt = setup.conn.execute("SELECT * FROM application_attempts").fetchone()
    assert attempt["retryability"] == "after_input"
    assert attempt["verification_state"] == "not_submitted"
    assert state.reset_retryable() == 0


@pytest.mark.parametrize("status", ["expired", "navigation_failed", "needs_input", "failed"])
def test_post_submission_non_success_model_report_stays_uncertain(setup, monkeypatch, status):
    install_fake_runner(monkeypatch, {"status": status, "missing_fields": []})
    gate = FakeGate(setup.job)
    gate.proof["submission_attempted"] = True
    category, _, evidence = launcher.run_job(setup.job, 9222, gate=gate)
    assert category == "submission_uncertain"
    assert evidence["gate"]["submission_attempted"] is True


def test_direct_dry_run_does_not_touch_model_or_require_gate(setup, monkeypatch):
    monkeypatch.setattr(dryrun, "run_dry_run", lambda job, **_kwargs: {"status": "dry_run", "job_url": job["url"]})
    category, duration, evidence = launcher.run_job(setup.job, 9222, dry_run=True)
    assert (category, duration) == ("dry_run", 0)
    assert evidence["job_url"] == setup.job["url"]


def test_worker_dry_run_never_calls_model_and_preserves_database(setup, monkeypatch):
    monkeypatch.setattr(dryrun, "run_dry_run", lambda job, **_kwargs: {"status": "dry_run", "job_url": job["url"]})
    monkeypatch.setattr(launcher, "launch_chrome", lambda *_a, **_k: pytest.fail("Live Chrome must not launch"))
    before = actual(setup)
    assert launcher.worker_loop(limit=1, target_url=setup.job["url"], dry_run=True) == (0, 0)
    assert actual(setup) == before
    assert setup.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0


def test_unsupported_native_adapter_never_calls_openai(setup, monkeypatch):
    def unsupported(_job):
        raise launcher.UnsupportedAdapter("No reviewed adapter")

    monkeypatch.setattr(launcher, "prepare_adapter", unsupported)
    assert launcher.worker_loop(limit=1, target_url=setup.job["url"]) == (0, 1)
    assert actual(setup)["apply_status"] == "needs_input"
    assert actual(setup)["error_category"] == "form_changed"
