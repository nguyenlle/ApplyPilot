# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — reliability hardening fork

- Support required LaTeX templates in supervised handoffs. Bind the template,
  editable source, compile metadata/log and PDFs to independent review and committed
  receipts. Preserve legacy receipts and reject generic resume rendering when a
  template is required. Guard imported output files before rendering, including
  explicit output paths and filesystem aliases.

- Add a separate, narrowly scoped Nuro Greenhouse transport validator: frozen
  PDF bytes, response-bound storage references, exact application JSON, and a
  one-use durable reservation callback. Reject ambiguous multipart, document
  overwrite, changed answers/consents and replay. This validator does not send
  requests or establish employer acceptance; dynamic live operation remains
  separately qualified and is not enabled in the generic application runner.

- Quarantine possible submissions at the result-persistence boundary even when a
  caller reports a transient failure; require reconciliation before another attempt.
- Add an exact Nuro question contract and network-isolated developer fixture for
  mapped answers, verified document bytes and synthetic confirmation handling.
  Missing employer-specific schedule answers and real location selection stay unresolved.

- Add independently reviewed score-only local imports without requiring document
  generation; preserve unknown eligibility and update only score fields.
- Allow reviewed employer requisitions to distinguish same-title jobs while
  deduplicating location variants of one requisition.

- Add supervised local Codex preparation with versioned snapshots, independently
  reviewed claims/PDFs, stale/replay rejection and preparation-only import receipts.
- Freeze verified PDF bytes before locked Archer form previews; distinguish local
  file selection from employer upload and preserve application state across previews.
- Omit empty Projects headings from rendered resumes.
- Apply configured score thresholds to preparation counts and label prepared
  candidates separately from full application readiness.

- Replace Claude Code with a bounded OpenAI Responses tool loop over guarded Playwright MCP.
- Stop immediately on exhausted API credit; preserve retry behavior for temporary rate limits.
- Repair LinkedIn/modern Greenhouse description extraction and reject signup URLs as application targets.
- Recognize explicit sponsorship exclusions in negative-requirements sections.
- Add a reviewed Archer form preview with exact question mapping and blocked network writes.
- Report verified totals from committed database outcomes, not the agent's claimed status.
- Record upstream baseline and inspect upstream fixes before integration.
- Add exact URL targeting, owned atomic claims, attempt ledger, conservative crash recovery,
  bounded delayed retries, daily/company budgets and terminal-state protection.
- Add network-isolated captured-form dry runs with no job/attempt state mutation.
- Require job-specific document manifests and hashes; prevent same-title overwrite and stale PDFs.
- Tighten truthful profile, résumé and cover validation; keep unknown answers unknown.
- Preserve dedicated browser sessions with exclusive locks and owned-process cleanup.
- Add restricted live native-form submission gate; unsupported dynamic ATS sites remain unvalidated.
- Add deterministic eligibility filters and structured scoring rejection reasons.
- Add meaningful local doctor failures, provider/browser checks, tests and automatic CI.
- Preserve AGPL-3.0 license and upstream history. No claim of production readiness or verified real submission.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
