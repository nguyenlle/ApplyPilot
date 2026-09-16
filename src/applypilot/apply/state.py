"""Owned application leases, append-only attempts, bounded retries and recovery."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from applypilot import config
from applypilot.database import get_connection
from applypilot.eligibility import eligibility_reason
from applypilot.identity import normalize_url
from applypilot.policy import RuntimePolicy, load_policy

RETRYABLE = {"transient_network", "rate_limited", "navigation_failed", "browser_crashed"}
NEEDS_INPUT = {"auth_required", "account_creation_failed", "email_verification_required",
               "captcha_blocked", "missing_profile_data", "artifact_invalid", "form_changed"}
PERMANENT = {"expired", "duplicate", "not_eligible", "location_mismatch", "blocked_domain"}
UNCERTAIN = {"submission_uncertain", "submitted_but_unverified"}


def now_iso() -> str:
    """Return a sortable aware UTC timestamp."""
    return datetime.now(UTC).isoformat()


def blocked_pattern_matches(url: str, pattern: str) -> bool:
    """Honor shipped SQL-LIKE patterns without interpolating them into SQL."""
    expression = "".join(".*" if char == "%" else "." if char == "_" else re.escape(char)
                         for char in pattern)
    return re.search(expression, url, flags=re.IGNORECASE) is not None


def resolve_target(conn, target: str) -> str:
    """Resolve only exact canonical URLs; never fall back to queue or substring."""
    canonical = normalize_url(target)
    matches = set()
    for row in conn.execute("SELECT url, application_url FROM jobs"):
        for value in (row["url"], row["application_url"]):
            if value:
                try:
                    if normalize_url(value) == canonical:
                        matches.add(row["url"])
                except ValueError:
                    continue
    alias = conn.execute("SELECT job_url FROM job_aliases WHERE alias_url = ?", (canonical,)).fetchone()
    if alias:
        matches.add(alias[0])
    if len(matches) != 1:
        raise ValueError("Target URL is unknown or ambiguous; no application selected")
    return matches.pop()


def recover_expired(conn, now: str) -> int:
    """Quarantine expired leases; a killed worker may already have clicked submit.

    Recovery never makes a potentially submitted job automatically eligible again.
    A live worker must renew its lease and cannot commit after losing ownership.
    """
    rows = conn.execute("SELECT url, claim_token FROM jobs WHERE apply_status = 'in_progress' "
                        "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?", (now,)).fetchall()
    for row in rows:
        conn.execute("UPDATE application_attempts SET ended_at = ?, status = 'submission_uncertain', "
                     "error_category = 'worker_lost', retryability = 'manual_review', "
                     "verification_state = 'submitted_but_unverified' WHERE attempt_id = ? AND ended_at IS NULL",
                     (now, row["claim_token"]))
        conn.execute("UPDATE jobs SET apply_status = 'submission_uncertain', error_category = 'worker_lost', "
                     "verification_state = 'submitted_but_unverified', claim_token = NULL, agent_id = NULL, "
                     "lease_expires_at = NULL WHERE url = ?", (row["url"],))
    return len(rows)


def _eligible(row: dict, policy: RuntimePolicy, now: str) -> bool:
    return (row["apply_status"] in (None, "queued", "retryable") and not row["applied_at"]
            and bool(row["tailored_resume_path"]) and (row["fit_score"] or 0) >= policy.min_score
            and (row["apply_attempts"] or 0) < policy.max_attempts
            and (not row["next_retry_at"] or row["next_retry_at"] <= now))


def select_jobs(target_url: str | None = None, min_score: int | None = None,
                worker_id: int = 0, dry_run: bool = False, exclude: set[str] | None = None) -> dict | None:
    """Claim one job atomically, or return a preview without any DB mutation."""
    from dataclasses import replace
    policy = load_policy()
    if min_score is not None:
        policy = replace(policy, min_score=min_score)
    conn = get_connection()
    now = now_iso()
    try:
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
            recover_expired(conn, now)
        target = resolve_target(conn, target_url) if target_url else None
        rows = conn.execute("SELECT * FROM jobs ORDER BY fit_score DESC, url").fetchall()
        blocked_sites, blocked_patterns = config.load_blocked_sites()
        for raw in rows:
            row = dict(raw)
            if (target and row["url"] != target) or row["url"] in (exclude or set()):
                continue
            if not _eligible(row, policy, now):
                continue
            reason = eligibility_reason(row, phase="apply")
            if reason:
                if not dry_run:
                    status = "needs_input" if reason.startswith("missing_info:") else "not_eligible"
                    conn.execute("UPDATE jobs SET apply_status=?, apply_error=?, error_category=? WHERE url=?",
                                 (status, reason, "missing_profile_data" if status == "needs_input" else "not_eligible",
                                  row["url"]))
                continue
            url = row["application_url"] or row["url"]
            try:
                normalize_url(url)
            except ValueError:
                continue
            if row["site"] in blocked_sites or any(blocked_pattern_matches(url, pattern) for pattern in blocked_patterns):
                continue
            if config.is_manual_ats(url):
                if not dry_run:
                    conn.execute("UPDATE jobs SET apply_status = 'needs_input', error_category = 'auth_required' "
                                 "WHERE url = ?", (row["url"],))
                continue
            # Reserve capacity at claim-time, counting in-flight and uncertain attempts.
            day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            daily = conn.execute("SELECT COUNT(*) FROM application_attempts WHERE dry_run = 0 AND started_at >= ?",
                                 (day,)).fetchone()[0]
            period = (datetime.now(UTC) - timedelta(days=policy.company_period_days)).isoformat()
            company_count = 0
            if row.get("company"):
                company_count = conn.execute("SELECT COUNT(*) FROM application_attempts WHERE dry_run = 0 "
                                             "AND lower(company) = lower(?) AND started_at >= ?",
                                             (row["company"], period)).fetchone()[0]
            if not dry_run and (daily >= policy.max_per_day or company_count >= policy.max_per_company):
                continue
            if dry_run:
                return row
            token = uuid.uuid4().hex
            lease = (datetime.now(UTC) + timedelta(seconds=policy.lease_seconds)).isoformat()
            conn.execute("UPDATE jobs SET apply_status = 'in_progress', agent_id = ?, claim_token = ?, "
                         "lease_expires_at = ?, last_attempted_at = ?, apply_attempts = COALESCE(apply_attempts,0)+1 "
                         "WHERE url = ?", (str(worker_id), token, lease, now, row["url"]))
            paths = json.dumps({"resume": row["tailored_resume_path"], "cover": row["cover_letter_path"]})
            conn.execute("INSERT INTO application_attempts "
                         "(attempt_id, job_url, company, title, worker_id, session_id, started_at, status, artifact_paths) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)",
                         (token, row["url"], row.get("company"), row["title"], str(worker_id),
                          f"worker-{worker_id}", now, paths))
            conn.commit()
            row.update(claim_token=token, apply_attempts=(row["apply_attempts"] or 0) + 1)
            return row
        if not dry_run:
            conn.commit()
        return None
    except Exception:
        if not dry_run:
            conn.rollback()
        raise


def finish(job: dict, category: str, evidence: dict | None = None, duration_ms: int = 0) -> None:
    """Commit only the current owner's result; uncertain submissions never retry."""
    conn = get_connection()
    token = job.get("claim_token")
    if not token:
        raise ValueError("Cannot record live result without an owned attempt")
    policy = load_policy()
    now = now_iso()
    evidence = evidence or {}
    if category == "submission_verified":
        gate = evidence.get("gate") or {}
        confirmation = evidence.get("confirmation") or {}
        valid = (evidence.get("browser_corroborated") is True and isinstance(gate, dict)
                 and gate.get("attempt_id") == token and gate.get("job_url") == job["url"]
                 and gate.get("submission_attempted") is True and gate.get("request_validated") is True
                 and gate.get("response_received") is True and not gate.get("gate_error")
                 and isinstance(gate.get("response_status"), int) and 200 <= gate["response_status"] < 400
                 and isinstance(confirmation, dict) and all(confirmation.get(k) for k in ("url", "text", "screenshot"))
                 and confirmation["url"] == gate.get("confirmation_url")
                 and isinstance(gate.get("confirmation_response_status"), int)
                 and 200 <= gate["confirmation_response_status"] < 300)
        if not valid:
            category = "submission_uncertain"
    if category == "submission_verified":
        status, retry = "applied", "never"
        verification = "submitted_and_verified"
    elif category in UNCERTAIN:
        status, retry, verification = "submission_uncertain", "manual_review", "submitted_but_unverified"
    elif category in NEEDS_INPUT:
        status, retry, verification = "needs_input", "after_input", "not_submitted"
    elif category in PERMANENT:
        status, retry, verification = category, "never", "not_submitted"
    elif category in RETRYABLE and job.get("apply_attempts", 1) < policy.max_attempts:
        status, retry, verification = "retryable", "after_delay", "not_submitted"
    else:
        status, retry, verification = "failed", "manual_review", "not_submitted"
    delay = min(policy.max_retry_delay_seconds,
                policy.retry_delay_seconds * 2 ** max(0, job.get("apply_attempts", 1) - 1))
    next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat() if status == "retryable" else None
    payload = json.dumps(evidence, ensure_ascii=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("UPDATE jobs SET apply_status=?, apply_error=?, error_category=?, "
            "applied_at=?, verification_state=?, verification_evidence=?, verification_confidence=?, "
            "next_retry_at=?, claim_token=NULL, lease_expires_at=NULL, agent_id=NULL, apply_duration_ms=? "
            "WHERE url=? AND claim_token=? AND apply_status='in_progress' AND lease_expires_at > ?",
            (status, None if status == "applied" else category, None if status == "applied" else category,
             now if status == "applied" else None, verification, payload,
             "corroborated" if status == "applied" else "unverified", next_retry, duration_ms, job["url"], token, now))
        if changed.rowcount != 1:
            raise ValueError("Application lease lost; refusing stale worker result")
        conn.execute("UPDATE application_attempts SET ended_at=?, duration_ms=?, status=?, error_category=?, "
                     "retryability=?, evidence_json=?, verification_state=? WHERE attempt_id=? AND ended_at IS NULL",
                     (now, duration_ms, status, category, retry, payload, verification, token))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reset_retryable() -> int:
    """Only reset exhausted transient failures; preserve uncertain and permanent states."""
    conn = get_connection()
    placeholders = ",".join("?" for _ in RETRYABLE)
    cursor = conn.execute(f"UPDATE jobs SET apply_status='queued', apply_attempts=0, next_retry_at=NULL "
                          f"WHERE apply_status='failed' AND error_category IN ({placeholders})",
                          tuple(RETRYABLE))
    conn.commit()
    return cursor.rowcount
