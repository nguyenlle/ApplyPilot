"""Application-state regression tests using synthetic isolated SQLite databases."""

import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from applypilot import config, database
from applypilot.apply import state
from applypilot.identity import normalize_url
from applypilot.policy import RuntimePolicy


def _policy(**overrides):
    return replace(RuntimePolicy(), max_per_day=100, max_per_company=100, **overrides)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    policy = _policy()
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(state, "load_policy", lambda: policy)
    monkeypatch.setattr(config, "load_blocked_sites", lambda: (set(), []))
    monkeypatch.setattr(config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(config, "load_search_config", dict)
    conn = database.init_db(path)
    yield SimpleNamespace(path=path, conn=conn, policy=policy)
    database.close_connection(path)


def seed(queue, number=1, **overrides):
    row = {
        "url": f"https://jobs.example.test/job/{number}",
        "title": "Synthetic role",
        "company": f"Synthetic company {number}",
        "site": "synthetic",
        "location": "Synthetic location",
        "tailored_resume_path": "synthetic-resume.txt",
        "fit_score": 9,
        "apply_status": None,
        "apply_attempts": 0,
    }
    row.update(overrides)
    fields = ",".join(row)
    marks = ",".join("?" for _ in row)
    queue.conn.execute(f"INSERT INTO jobs ({fields}) VALUES ({marks})", tuple(row.values()))
    queue.conn.commit()
    return row["url"]


def row_for(queue, url):
    return dict(queue.conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone())


def verified_evidence(job):
    """Synthetic evidence for the final state boundary, never a live ATS claim."""
    return {
        "browser_corroborated": True,
        "gate": {
            "adapter": "native_html_v1", "attempt_id": job["claim_token"], "job_url": job["url"],
            "submission_attempted": True, "request_validated": True,
            "response_received": True, "response_status": 200,
            "confirmation_url": job.get("application_url") or job["url"],
            "confirmation_response_status": 200,
        },
        "confirmation": {
            "url": job.get("application_url") or job["url"],
            "text": f"Application received for {job['company']} {job['title']}",
            "screenshot": "synthetic-confirmation.png",
        },
    }


def snapshot_rows(queue):
    return {
        table: [tuple(row) for row in queue.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("jobs", "application_attempts", "job_aliases")
    }


def file_bytes(path):
    return {suffix: p.read_bytes() for suffix in ("", "-wal")
            if (p := path.with_name(path.name + suffix)).exists()}


@pytest.mark.parametrize("urls", [
    ("https://www.indeed.com/viewjob?jk=AAAA", "https://www.indeed.com/viewjob?jk=BBBB"),
    ("https://www.linkedin.com/jobs/view?currentJobId=123",
     "https://www.linkedin.com/jobs/view?currentJobId=1234"),
])
def test_target_query_identity_is_exact(queue, urls):
    seed(queue, 1, url=urls[0])
    seed(queue, 2, url=urls[1])
    job = state.select_jobs(target_url=urls[1])
    assert job["url"] == urls[1]
    assert row_for(queue, urls[0])["apply_status"] is None


@pytest.mark.parametrize("target", [
    "https://jobs.example.test/job/1",
    "https://jobs.example.test/job/%",
    "https://jobs.example.test/job/_",
    "https://jobs.example.test/job/999",
])
def test_unknown_prefix_or_wildcard_never_falls_back_to_queue(queue, target):
    seed(queue, 1234)
    before = snapshot_rows(queue)
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        state.select_jobs(target_url=target)
    assert snapshot_rows(queue) == before


def test_target_normalization_retains_identity_and_removes_tracking(queue):
    url = seed(queue, url="https://JOBS.example.test/job/123/?jk=ABC&utm_source=board")
    job = state.select_jobs(target_url="https://jobs.example.test/job/123?jk=ABC&utm_source=other#fragment")
    assert job["url"] == url
    assert normalize_url("https://jobs.example.test?jk=A") != normalize_url("https://jobs.example.test?jk=B")


def test_generic_ref_parameter_may_be_job_identity_and_must_not_be_dropped(queue):
    seed(queue, url="https://ats.example.test/job?ref=requisition-A")
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        state.select_jobs(target_url="https://ats.example.test/job?ref=requisition-B")
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0


def test_shared_application_url_is_rejected(queue):
    application = "https://ats.example.test/apply/shared"
    seed(queue, 1, application_url=application)
    seed(queue, 2, application_url=application)
    with pytest.raises(ValueError, match="ambiguous"):
        state.select_jobs(target_url=application)
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0


def test_alias_resolves_to_canonical_job(queue):
    url = seed(queue)
    alias = "https://other-board.example.test/job/abc"
    queue.conn.execute("INSERT INTO job_aliases(alias_url,job_url) VALUES(?,?)", (alias, url))
    queue.conn.commit()
    assert state.select_jobs(target_url=alias)["url"] == url


@pytest.mark.parametrize("status", ["applied", "expired", "duplicate", "submission_uncertain", "needs_input"])
def test_terminal_and_intervention_jobs_never_targeted(queue, status):
    url = seed(queue, apply_status=status)
    assert state.select_jobs(target_url=url) is None
    assert row_for(queue, url)["apply_status"] == status


def test_applied_timestamp_prevents_replay_even_if_status_reset(queue):
    url = seed(queue, apply_status=None, applied_at=state.now_iso())
    assert state.select_jobs(target_url=url) is None


def test_null_status_job_is_claimed_and_attempt_recorded(queue):
    url = seed(queue)
    job = state.select_jobs(target_url=url, worker_id=7)
    actual = row_for(queue, url)
    assert actual["apply_status"] == "in_progress"
    assert actual["claim_token"] == job["claim_token"]
    assert actual["apply_attempts"] == 1
    attempt = queue.conn.execute("SELECT * FROM application_attempts").fetchone()
    assert attempt["attempt_id"] == actual["claim_token"]
    assert attempt["worker_id"] == "7"


def test_target_respects_score_retry_and_delay(queue):
    for number, fields in enumerate((
        {"fit_score": 6}, {"apply_attempts": 3},
        {"next_retry_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )):
        url = seed(queue, number, **fields)
        assert state.select_jobs(target_url=url) is None


def test_manual_first_job_does_not_stall_queue(queue, monkeypatch):
    manual = seed(queue, 1, url="https://manual.example.test/1", fit_score=10)
    eligible = seed(queue, 2)
    monkeypatch.setattr(config, "is_manual_ats", lambda url: "manual.example.test" in url)
    assert state.select_jobs()["url"] == eligible
    assert row_for(queue, manual)["apply_status"] == "needs_input"
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1


def test_configured_sql_style_blocked_url_patterns_are_enforced(queue, monkeypatch):
    blocked = seed(queue, 1, application_url="https://blocked.example.test/apply/1", fit_score=10)
    eligible = seed(queue, 2)
    monkeypatch.setattr(config, "load_blocked_sites", lambda: (set(), ["%blocked.example.test%"]))
    assert state.select_jobs()["url"] == eligible
    assert row_for(queue, blocked)["apply_status"] is None


def test_dry_run_is_read_only_including_expired_and_manual_jobs(queue, monkeypatch):
    expired = seed(queue, 1, apply_status="in_progress", claim_token="old-owner",
                   lease_expires_at="2000-01-01T00:00:00+00:00", fit_score=10)
    manual = seed(queue, 2, url="https://manual.example.test/2", fit_score=10)
    chosen = seed(queue, 3, apply_status="retryable", apply_attempts=1,
                  last_attempted_at="2000-01-01T00:00:00+00:00")
    monkeypatch.setattr(config, "is_manual_ats", lambda url: "manual.example.test" in url)
    queue.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rows_before, files_before = snapshot_rows(queue), file_bytes(queue.path)
    assert state.select_jobs(dry_run=True)["url"] == chosen
    assert state.select_jobs(target_url=chosen, dry_run=True)["url"] == chosen
    assert state.select_jobs(target_url=manual, dry_run=True) is None
    assert state.select_jobs(target_url=expired, dry_run=True) is None
    with pytest.raises(ValueError):
        state.select_jobs(target_url="https://unknown.example.test/1", dry_run=True)
    assert snapshot_rows(queue) == rows_before
    assert file_bytes(queue.path) == files_before


def threaded_claims(queue, count=20):
    start = threading.Barrier(count)

    def claim(index):
        try:
            start.wait(timeout=10)
            return state.select_jobs(worker_id=index)
        finally:
            database.close_connection(queue.path)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(claim, range(count)))


def test_twenty_concurrent_claims_have_unique_jobs_and_tokens(queue):
    for number in range(20):
        seed(queue, number)
    claims = threaded_claims(queue)
    assert all(claims)
    assert len({job["url"] for job in claims}) == 20
    assert len({job["claim_token"] for job in claims}) == 20
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 20


def test_daily_limit_is_reserved_atomically_across_workers(queue, monkeypatch):
    monkeypatch.setattr(state, "load_policy", lambda: replace(queue.policy, max_per_day=4))
    for number in range(20):
        seed(queue, number)
    assert sum(job is not None for job in threaded_claims(queue)) == 4
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 4


def test_company_limit_is_reserved_atomically_case_insensitive(queue, monkeypatch):
    monkeypatch.setattr(state, "load_policy", lambda: replace(queue.policy, max_per_company=2))
    for number in range(20):
        seed(queue, number, company="SYNTHETIC" if number % 2 else "Synthetic")
    assert sum(job is not None for job in threaded_claims(queue)) == 2


def test_expired_claim_is_quarantined_never_replayed_and_stale_owner_rejected(queue):
    url = seed(queue)
    stale = state.select_jobs()
    queue.conn.execute("UPDATE jobs SET lease_expires_at=? WHERE url=?", ("2000-01-01T00:00:00+00:00", url))
    queue.conn.commit()
    assert state.select_jobs() is None
    actual = row_for(queue, url)
    assert actual["apply_status"] == "submission_uncertain"
    assert actual["claim_token"] is None
    assert state.select_jobs(target_url=url) is None
    with pytest.raises(ValueError, match="lease lost"):
        state.finish(stale, "submission_verified", {"browser_corroborated": True})
    attempt = queue.conn.execute("SELECT * FROM application_attempts").fetchone()
    assert attempt["ended_at"]
    assert attempt["retryability"] == "manual_review"


def test_expired_owner_cannot_finish_even_before_recovery_runs(queue):
    url = seed(queue)
    stale = state.select_jobs()
    queue.conn.execute("UPDATE jobs SET lease_expires_at=? WHERE url=?", ("2000-01-01T00:00:00+00:00", url))
    queue.conn.commit()
    with pytest.raises(ValueError, match="lease lost"):
        state.finish(stale, "submission_verified", {"browser_corroborated": True})
    assert row_for(queue, url)["apply_status"] != "applied"


def test_forged_owner_cannot_finish_another_claim(queue):
    url = seed(queue)
    job = state.select_jobs()
    wrong = dict(job, claim_token="forged")
    with pytest.raises(ValueError, match="lease lost"):
        state.finish(wrong, "expired")
    assert row_for(queue, url)["claim_token"] == job["claim_token"]


def test_unverified_success_never_marks_applied(queue):
    url = seed(queue)
    job = state.select_jobs()
    state.finish(job, "submission_verified", {"agent_said_applied": True})
    actual = row_for(queue, url)
    assert actual["apply_status"] == "submission_uncertain"
    assert actual["applied_at"] is None
    assert state.select_jobs() is None


@pytest.mark.parametrize("corroboration", ["false", "true", 1, [True]])
def test_corroboration_requires_explicit_boolean_true(queue, corroboration):
    url = seed(queue)
    job = state.select_jobs()
    evidence = verified_evidence(job)
    evidence["browser_corroborated"] = corroboration
    state.finish(job, "submission_verified", evidence)
    assert row_for(queue, url)["apply_status"] == "submission_uncertain"


@pytest.mark.parametrize("section,key,value", [
    (None, "gate", None),
    ("gate", "attempt_id", "another-attempt"),
    ("gate", "job_url", "https://other-job.example.test/123"),
    ("gate", "submission_attempted", False),
    ("gate", "request_validated", False),
    ("gate", "request_validated", "true"),
    ("gate", "response_received", False),
    ("gate", "response_status", 500),
    ("gate", "confirmation_url", "https://unrelated.example.test/receipt"),
    ("gate", "confirmation_response_status", 500),
    ("gate", "confirmation_response_status", None),
    ("gate", "gate_error", "browser interception failed"),
    (None, "confirmation", None),
])
def test_verified_state_requires_attempt_bound_validated_request_and_receipt(queue, section, key, value):
    url = seed(queue)
    job = state.select_jobs()
    evidence = deepcopy(verified_evidence(job))
    container = evidence if section is None else evidence[section]
    container[key] = value
    state.finish(job, "submission_verified", evidence)
    actual = row_for(queue, url)
    assert actual["apply_status"] == "submission_uncertain"
    assert actual["applied_at"] is None
    assert state.select_jobs(target_url=url) is None


def test_bare_browser_boolean_cannot_authorize_verified_state(queue):
    url = seed(queue)
    job = state.select_jobs()
    state.finish(job, "submission_verified", {"browser_corroborated": True})
    assert row_for(queue, url)["apply_status"] == "submission_uncertain"


def test_corroborated_success_is_final_with_immutable_attempt(queue):
    url = seed(queue)
    job = state.select_jobs()
    state.finish(job, "submission_verified", verified_evidence(job), duration_ms=123)
    assert row_for(queue, url)["apply_status"] == "applied"
    before = snapshot_rows(queue)
    with pytest.raises(ValueError, match="lease lost"):
        state.finish(job, "expired")
    assert snapshot_rows(queue) == before


def test_retry_backoff_is_bounded_and_attempt_budget_exhausts(queue):
    url = seed(queue)
    for expected_attempt in range(1, queue.policy.max_attempts + 1):
        job = state.select_jobs()
        assert job["apply_attempts"] == expected_attempt
        before = datetime.now(UTC)
        state.finish(job, "transient_network")
        actual = row_for(queue, url)
        if expected_attempt < queue.policy.max_attempts:
            retry_time = datetime.fromisoformat(actual["next_retry_at"])
            delay = (retry_time - before).total_seconds()
            expected_delay = min(queue.policy.max_retry_delay_seconds,
                                 queue.policy.retry_delay_seconds * 2 ** (expected_attempt - 1))
            assert expected_delay <= delay < expected_delay + 5
            assert state.select_jobs() is None
            queue.conn.execute("UPDATE jobs SET next_retry_at=? WHERE url=?", ("2000-01-01T00:00:00+00:00", url))
            queue.conn.commit()
        else:
            assert actual["apply_status"] == "failed"
            assert actual["next_retry_at"] is None
            assert state.select_jobs() is None
    assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 3


def test_reset_only_resets_exhausted_transient_failures(queue):
    reset = seed(queue, 0, apply_status="failed", error_category="transient_network", apply_attempts=3)
    keep = [seed(queue, number + 1, apply_status=status, error_category=error, apply_attempts=3)
            for number, (status, error) in enumerate([
                ("submission_uncertain", "worker_lost"), ("expired", "expired"),
                ("failed", "unknown_error"), ("needs_input", "auth_required"),
                ("applied", None), ("duplicate", "duplicate"),
            ])]
    before = {url: row_for(queue, url) for url in keep}
    assert state.reset_retryable() == 1
    assert row_for(queue, reset)["apply_status"] == "queued"
    assert row_for(queue, reset)["apply_attempts"] == 0
    assert {url: row_for(queue, url) for url in keep} == before


def _claim_and_crash(database_path, pipe):
    """Spawn-safe worker: commit a synthetic claim, report it, then die abruptly."""
    from pathlib import Path

    from applypilot import config as child_config
    from applypilot import database as child_database
    from applypilot.apply import state as child_state

    child_database.DB_PATH = Path(database_path)
    child_state.load_policy = lambda: _policy()
    child_config.load_blocked_sites = lambda: (set(), [])
    child_config.is_manual_ats = lambda _: False
    child_config.load_search_config = dict
    job = child_state.select_jobs(worker_id=99)
    pipe.send(job)
    pipe.close()
    os._exit(23)


def test_process_crash_keeps_durable_attempt_and_recovery_quarantines(queue):
    url = seed(queue)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_claim_and_crash, args=(str(queue.path), sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(15), "Synthetic child never reported its committed claim"
        job = receiver.recv()
        process.join(timeout=15)
        assert process.exitcode == 23
        assert row_for(queue, url)["claim_token"] == job["claim_token"]
        assert queue.conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1
        queue.conn.execute("UPDATE jobs SET lease_expires_at=? WHERE url=?", ("2000-01-01T00:00:00+00:00", url))
        queue.conn.commit()
        assert state.select_jobs() is None
        assert row_for(queue, url)["apply_status"] == "submission_uncertain"
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
