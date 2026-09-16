"""OpenAI/Playwright orchestration with owned attempts and isolated previews."""

import json
import logging
import re
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import state
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.apply.gate import SubmissionGate, UnsupportedAdapter, prepare_adapter
from applypilot.artifacts import verify_job_artifacts
from applypilot.database import get_connection
from applypilot.identity import normalized_text
from applypilot.policy import load_policy

logger = logging.getLogger(__name__)
_stop_event = threading.Event()
POLL_INTERVAL = 60
ALLOWED_BROWSER_TOOLS = (
    "browser_navigate", "browser_snapshot", "browser_click", "browser_fill_form",
    "browser_type", "browser_select_option", "browser_file_upload", "browser_press_key",
    "browser_wait_for", "browser_take_screenshot", "browser_tabs", "browser_navigate_back",
)


def _make_mcp_config(cdp_port: int, policy_path: Path | None = None) -> dict:
    """No email, arbitrary code, shell or inherited MCP servers in application sessions."""
    if policy_path is None:
        policy_path = config.APP_DIR / "unconfigured-policy.json"
    return {"mcpServers": {"playwright": {
        "command": sys.executable,
        "args": ["-m", "applypilot.apply.mcp_proxy", "--port", str(cdp_port),
                 "--policy", str(policy_path)]}}}


def acquire_job(target_url: str | None = None, min_score: int = 7, worker_id: int = 0) -> dict | None:
    """Compatibility entry point backed by exact targets and atomic owned leases."""
    return state.select_jobs(target_url, min_score, worker_id)


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manual success remains unverified and cannot automatically retry."""
    conn = get_connection()
    target = state.resolve_target(conn, url)
    if status not in {"applied", "failed"}:
        raise ValueError("Expected applied or failed")
    result = "submission_uncertain" if status == "applied" else "failed"
    with conn:
        changed = conn.execute("UPDATE jobs SET apply_status=?, apply_error=?, error_category=?, "
                               "verification_state=? WHERE url=? AND COALESCE(apply_status,'') != 'in_progress'",
                               (result, reason or "manual report", "manual_report",
                                "submitted_but_unverified" if status == "applied" else "not_submitted", target))
        if changed.rowcount != 1:
            raise ValueError("Job is owned by an active worker")


def reset_failed() -> int:
    """Never reset permanent, unknown, or potentially submitted outcomes."""
    return state.reset_retryable()


def gen_prompt(target_url: str, min_score: int = 7, model: str | None = None, worker_id: int = 0) -> Path | None:
    """Generate debugging instructions without acquiring or changing a job."""
    job = state.select_jobs(target_url, min_score, dry_run=True)
    if not job:
        return None
    verify_job_artifacts(job)
    text = Path(job["tailored_resume_path"]).with_suffix(".txt").read_text(encoding="utf-8")
    prompt = prompt_mod.build_prompt(job, text, dry_run=True, worker_id=worker_id)
    config.ensure_dirs()
    path = config.LOG_DIR / f"prompt-{uuid.uuid4().hex}.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def parse_result(output: str) -> dict:
    """Require one unambiguous final result; malformed output is uncertain."""
    matches = []
    for line in output.splitlines():
        if line.strip().startswith("RESULT_JSON:"):
            try:
                result = json.loads(line.split("RESULT_JSON:", 1)[1].strip())
                if isinstance(result, dict):
                    matches.append(result)
            except json.JSONDecodeError:
                pass
    if not matches or any(item != matches[-1] for item in matches):
        return {"status": "submission_uncertain", "reason": "No unambiguous structured agent result"}
    result = matches[-1]
    if result.get("status") not in state.RETRYABLE | state.NEEDS_INPUT | state.PERMANENT | state.UNCERTAIN | {
        "applied", "submission_verified", "needs_input", "failed"
    }:
        result["status"] = "submission_uncertain"
    return result


def inspect_confirmation(port: int, job: dict, result: dict, gate_evidence: dict | None = None) -> dict:
    """Collect browser evidence independently of the agent's success assertion."""
    from playwright.sync_api import sync_playwright
    evidence = {"agent_result": result, "browser_corroborated": False, "gate": gate_evidence}
    if (not gate_evidence or gate_evidence.get("attempt_id") != job.get("claim_token")
            or gate_evidence.get("job_url") != job["url"]
            or gate_evidence.get("submission_attempted") is not True
            or gate_evidence.get("request_validated") is not True
            or gate_evidence.get("response_received") is not True
            or not 200 <= gate_evidence.get("response_status", 0) < 400
            or not 200 <= gate_evidence.get("confirmation_response_status", 0) < 300
            or gate_evidence.get("gate_error")):
        return evidence
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        candidates = []
        expected_host = urlsplit(job.get("application_url") or job["url"]).hostname
        for context in browser.contexts:
            for page in context.pages:
                body = page.locator("body").inner_text(timeout=5000)
                if urlsplit(page.url).hostname != expected_host:
                    continue
                if page.url != gate_evidence.get("confirmation_url"):
                    continue
                normalized = normalized_text(body)
                title = normalized_text(job.get("title"))
                company = normalized_text(job.get("company"))
                confirmed = bool(re.search(r"application (?:has been |was )?(?:submitted|received)|thank you for applying", body, re.IGNORECASE))
                identity = bool(title and company and title in normalized and company in normalized)
                if confirmed and identity and not result.get("missing_fields"):
                    screenshot = config.LOG_DIR / f"confirmation-{job['claim_token']}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    candidates.append({"url": page.url, "text": body[:20000], "screenshot": str(screenshot)})
        if len(candidates) == 1:
            evidence.update(browser_corroborated=True, confirmation=candidates[0])
    return evidence


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str | None = None, dry_run: bool = False,
            gate: SubmissionGate | None = None) -> tuple[str, int, dict]:
    """Run OpenAI's bounded tool loop; only independent evidence proves success."""
    start = time.monotonic()
    if dry_run:
        from applypilot.apply.dryrun import run_dry_run
        return "dry_run", 0, run_dry_run(job, headless=True)
    if gate is None:
        return "form_changed", 0, {"reason": "A validated submission gate is required"}
    verify_job_artifacts(job)
    config.validate_profile_for_application()
    worker_dir = reset_worker_dir(worker_id)
    policy_path = worker_dir / "browser-policy.json"
    policy_path.write_text(json.dumps({
        "urls": [gate.adapter.application_url],
        "file_sha256": list(gate.adapter.files.values()),
    }), encoding="utf-8")
    text = Path(job["tailored_resume_path"]).with_suffix(".txt").read_text(encoding="utf-8")
    prompt = prompt_mod.build_prompt(job, text, worker_id=worker_id)
    log_path = config.LOG_DIR / f"attempt-{job['claim_token']}.json"
    policy = load_policy()
    from applypilot.apply.openai_runner import RunnerError, run_agent
    try:
        run = run_agent(prompt, port, policy_path, model or config.apply_model(),
                        policy.apply_timeout_seconds, policy.apply_max_steps,
                        policy.apply_max_output_tokens, log_path, stop_event=_stop_event)
        result = run.get("result", {})
        # Reuse the strict result parser; unexpected model status never authorizes success.
        result = parse_result("RESULT_JSON:" + json.dumps(result))
        status = result.get("status", "submission_uncertain")
        elapsed = int((time.monotonic() - start) * 1000)
        if status in {"applied", "submission_verified"}:
            evidence = inspect_confirmation(port, job, result, gate.evidence())
            evidence.update(log=str(log_path), usage=run.get("usage", {}))
            return ("submission_verified" if evidence["browser_corroborated"] else "submission_uncertain"), elapsed, evidence
        if status == "needs_input":
            status = "missing_profile_data"
        gate_proof = gate.evidence()
        if gate_proof.get("submission_attempted"):
            status = "submission_uncertain"
        return status, elapsed, {"agent_result": result, "gate": gate_proof,
                                 "log": str(log_path), "usage": run.get("usage", {})}
    except RunnerError as exc:
        proof = gate.evidence()
        category = "submission_uncertain" if proof.get("submission_attempted") else exc.category
        return category, int((time.monotonic() - start) * 1000), {
            "reason": str(exc), "gate": proof, "log": str(log_path),
        }


def worker_loop(worker_id: int = 0, limit: int = 1, target_url: str | None = None,
                min_score: int = 7, headless: bool = False, model: str | None = None,
                dry_run: bool = False) -> tuple[int, int]:
    """Isolate failures and never automatically recycle uncertain submissions."""
    applied = failed = done = 0
    previewed: set[str] = set()
    while not _stop_event.is_set() and (limit == 0 or done < limit):
        job = state.select_jobs(target_url, min_score, worker_id, dry_run, previewed)
        if not job:
            if limit or target_url or dry_run:
                break
            _stop_event.wait(POLL_INTERVAL)
            continue
        browser = None
        entered_agent = False
        try:
            if dry_run:
                from applypilot.apply.dryrun import run_dry_run
                evidence = run_dry_run(job, headless=headless)
                Console().print(f"Preview {job['title']}: {evidence.get('evidence_path', evidence.get('status'))}")
                previewed.add(job["url"])
            else:
                try:
                    verify_job_artifacts(job)
                except ValueError as exc:
                    state.finish(job, "artifact_invalid", {"reason": str(exc)})
                    failed += 1
                    done += 1
                    continue
                try:
                    config.validate_profile_for_application()
                    adapter = prepare_adapter(job)
                except UnsupportedAdapter as exc:
                    state.finish(job, "form_changed", {"reason": str(exc)})
                    failed += 1
                    done += 1
                    continue
                except ValueError as exc:
                    state.finish(job, "missing_profile_data", {"reason": str(exc)})
                    failed += 1
                    done += 1
                    continue
                browser = launch_chrome(worker_id, port=BASE_CDP_PORT + worker_id, headless=headless)
                with SubmissionGate(BASE_CDP_PORT + worker_id, job, adapter,
                                    evidence_path=config.LOG_DIR / f"attempt-{job['claim_token']}-gate.json") as gate:
                    entered_agent = True
                    category, duration, evidence = run_job(job, BASE_CDP_PORT + worker_id, worker_id, model, gate=gate)
                persisted_status = state.finish(job, category, evidence, duration)
                applied += int(persisted_status == "applied")
                failed += int(persisted_status != "applied")
        except Exception as exc:
            logger.exception("Application attempt failed for worker %d", worker_id)
            if not dry_run:
                try:
                    state.finish(job, "submission_uncertain" if entered_agent else "browser_crashed", {"reason": str(exc)})
                except ValueError:
                    logger.exception("Worker lost its lease; durable state retained for review")
            else:
                previewed.add(job["url"])
            failed += 1
        finally:
            if browser:
                cleanup_worker(worker_id, browser)
        done += 1
        if target_url:
            break
    return applied, failed


def main(limit: int = 1, target_url: str | None = None, min_score: int = 7,
         headless: bool = False, model: str | None = None, dry_run: bool = False,
         continuous: bool = False, poll_interval: int = 60, workers: int = 1) -> dict:
    """Bound total work across workers; zero-allocation workers never run forever."""
    global POLL_INTERVAL
    if workers < 1 or limit < 0 or not 1 <= min_score <= 10:
        raise ValueError("workers must be positive, limit nonnegative, and score between 1 and 10")
    if dry_run and continuous:
        raise ValueError("Use bounded dry-run batches; continuous preview is unsupported")
    if target_url and workers != 1:
        raise ValueError("A specific target must use one worker")
    _stop_event.clear()
    POLL_INTERVAL = poll_interval
    config.ensure_dirs()
    if not continuous and limit == 0:
        return {"verified": 0, "failed": 0}
    if dry_run:
        workers = 1
    workers = workers if continuous else min(workers, limit)
    limits = [0] * workers if continuous else [limit // workers + int(i < limit % workers) for i in range(workers)]
    previous_handler = signal.getsignal(signal.SIGINT)

    def stop(signum, frame):
        _stop_event.set()

    signal.signal(signal.SIGINT, stop)
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker_loop, i, limits[i], target_url, min_score, headless, model, dry_run)
                       for i in range(workers)]
            results = [future.result() for future in futures]
        Console().print(f"Finished: {sum(r[0] for r in results)} verified applications; {sum(r[1] for r in results)} failures.")
        return {"verified": sum(r[0] for r in results), "failed": sum(r[1] for r in results)}
    finally:
        _stop_event.set()
        kill_all_chrome()
        signal.signal(signal.SIGINT, previous_handler)
