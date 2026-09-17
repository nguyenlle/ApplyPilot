"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")
STAGES_ARGUMENT = typer.Argument(None, help="Pipeline stages: discover, enrich, score, tailor, cover, pdf, all")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _local_action(action: Callable, *args: str) -> None:
    """Run preparation without loading provider credentials or dispatching apply."""
    try:
        output = action(*args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(f"Local preparation rejected: {exc}", style="red", markup=False)
        raise typer.Exit(code=1) from exc
    console.print(str(output), markup=False)


@app.command()
def local_export(url: str = typer.Option(..., "--url")) -> None:
    """Export one private handoff for a local Codex task (no API calls)."""
    from applypilot.local_handoff import export_job

    _local_action(export_job, url)


@app.command()
def local_render(handoff: str, result: str) -> None:
    """Validate and render a local result without importing or approving."""
    from applypilot.local_handoff import render_result

    _local_action(render_result, handoff, result)


@app.command()
def local_import(handoff: str, result: str, review: str = typer.Option(..., "--review")) -> None:
    """Import reviewed preparation only; never mark an application submitted."""
    from applypilot.local_handoff import import_result

    _local_action(import_result, handoff, result, review)


@app.command()
def local_score_export(url: str = typer.Option(..., "--url")) -> None:
    """Export one job for local scoring without generating documents."""
    from applypilot.local_score import export_job

    _local_action(export_job, url)


@app.command()
def local_score_import(handoff: str, result: str, review: str = typer.Option(..., "--review")) -> None:
    """Import an independently reviewed score; never approve documents or apply."""
    from applypilot.local_score import import_result

    _local_action(import_result, handoff, result, review)


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: list[str] | None = STAGES_ARGUMENT,
    min_score: int | None = typer.Option(None, "--min-score", min=1, max=10, help="Minimum fit score; defaults to runtime.yaml."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default). "
            "lenient: banned words ignored. Factual checks and the judge remain required in every mode."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline
    from applypilot.policy import load_policy
    min_score = min_score if min_score is not None else load_policy().min_score

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if not dry_run and (any(s in stage_list for s in llm_stages) or "all" in stage_list):
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: int | None = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int | None = typer.Option(None, "--workers", "-w", min=1, help="Number of parallel browser workers."),
    min_score: int | None = typer.Option(None, "--min-score", min=1, max=10, help="Minimum fit score for job selection."),
    model: str | None = typer.Option(None, "--model", "-m", help="OpenAI browser model; defaults to APPLY_MODEL or LLM_MODEL."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: str | None = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: str | None = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: str | None = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: str | None = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import PROFILE_PATH as _profile_path
    from applypilot.config import check_tier
    from applypilot.database import get_connection
    from applypilot.policy import load_policy
    policy = load_policy()
    from applypilot.config import apply_model
    model = model or apply_model()
    workers = workers if workers is not None else policy.workers
    min_score = min_score if min_score is not None else policy.min_score
    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be positive; use --continuous for continuous mode")
    if continuous and dry_run:
        raise typer.BadParameter("--dry-run requires a bounded batch")
    if url and workers != 1:
        raise typer.BadParameter("--url requires exactly one worker")

    # --- Utility modes (no browser or model needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[yellow]Recorded manual submission as unverified:[/yellow] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: OpenAI browser runtime and reviewed adapters required.
    if not dry_run and not gen:
        check_tier(3, "auto-apply")
        from applypilot.diagnostics import run_diagnostics
        if not run_diagnostics()["ok"]:
            console.print("[red]Full-pipeline checks failed. Run applypilot doctor.[/red]")
            raise typer.Exit(code=1)

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("Debugging artifact only. Use apply --dry-run for the isolated browser preview.")
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else policy.max_per_run)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    results = apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        poll_interval=policy.poll_interval_seconds,
    )
    if results["failed"]:
        raise typer.Exit(code=1)


@app.command()
def preview_form(url: str = typer.Option(..., "--url", help="Exact queued job URL.")) -> None:
    """Validate a supported employer form without uploading or submitting."""
    from applypilot.apply import archer, state
    from applypilot.database import get_connection
    _bootstrap()
    conn = get_connection()
    canonical = state.resolve_target(conn, url)
    job = dict(conn.execute("SELECT * FROM jobs WHERE url=?", (canonical,)).fetchone())
    if job.get("application_url") != archer.APPLICATION_URL:
        raise typer.BadParameter("No reviewed employer preview adapter matches this job")
    result = archer.preview(job)
    console.print(f"Form preview: {result['status']}; submitted: {result['submitted']}")
    console.print(f"Evidence: {result['evidence_path']}")
    for item in result.get("missing_fields", []):
        console.print(f"Required: {item.get('question') or item.get('field')}: {item.get('profile_path') or item.get('reason', '')}")
    for item in result.get("unresolved_controls", []):
        console.print(f"Unresolved control: {item['field']}: {item['reason']}")
    if result["status"] != "preview_only":
        raise typer.Exit(code=1)


@app.command()
def watch(
    cycles: int = typer.Option(0, "--cycles", min=0, help="Zero repeats until Ctrl+C."),
    workers: int = typer.Option(1, "--workers", "-w", min=1),
    apply_jobs: bool = typer.Option(False, "--apply-jobs", help="Also consume qualified jobs through validated adapters."),
) -> None:
    """Repeat discovery and preparation; optionally submit through configured adapters."""
    import time

    from applypilot.config import check_tier
    from applypilot.pipeline import run_pipeline
    from applypilot.policy import load_policy
    _bootstrap()
    check_tier(3 if apply_jobs else 2, "continuous pipeline")
    iteration = 0
    try:
        while cycles == 0 or iteration < cycles:
            policy = load_policy()
            result = run_pipeline(stages=["all"], min_score=policy.min_score, workers=workers)
            if result.get("errors"):
                console.print("[yellow]Pipeline cycle has errors; inspect logs. Queue state is retained.[/yellow]")
            if apply_jobs:
                from applypilot.apply.launcher import main as apply_main
                apply_main(limit=policy.max_per_run, workers=policy.workers, min_score=policy.min_score,
                           poll_interval=policy.poll_interval_seconds)
            iteration += 1
            if cycles == 0 or iteration < cycles:
                time.sleep(policy.discovery_interval_seconds)
    except KeyboardInterrupt:
        console.print("Stopped continuous pipeline.")


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored or filtered", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row(f"Pending tailoring ({stats['min_score']}+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row(f"Prepared candidates ({stats['min_score']}+)", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)
    for label, counts in (("Application states", stats["application_states"]),
                          ("Failure categories", stats["failure_categories"])):
        breakdown = Table(title=label)
        breakdown.add_column("State / category")
        breakdown.add_column("Count")
        for name, count in counts.items():
            breakdown.add_row(name, str(count))
        console.print(breakdown)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor(tier: int = typer.Option(3, "--tier", min=1, max=3)) -> None:
    """Verify configuration and credentials; exit nonzero when required checks fail."""
    from applypilot.diagnostics import run_diagnostics
    report = run_diagnostics(required_tier=tier)
    for check in report["checks"]:
        label = "OK" if check["ok"] else ("MISSING" if check["required"] else "OPTIONAL")
        console.print(f"{label:8} {check['name']}: {check['detail']}")
    if not report["ok"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
