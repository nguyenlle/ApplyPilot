# Operating this fork

This branch preserves the upstream discovery → enrichment → scoring → résumé →
cover letter → application pipeline. It is **not yet qualified for unattended
high-volume submission to real ATS sites**. Read VALIDATION.md before enabling live work.

## Installation (Python 3.11 or 3.12)

```powershell
git clone https://github.com/nguyenlle/ApplyPilot.git
cd ApplyPilot
git checkout codex/reliability-hardening
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,discovery]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pip check
```

The discovery extra resolves JobSpy's actual NumPy/pandas/markdownify/regex
constraints. Do not install arbitrary newest dependency versions with `--no-deps`.
Python 3.13 is not a tested full-discovery environment because JobSpy pins NumPy 1.26.3.
On Linux install Playwright system dependencies with `playwright install --with-deps chromium`.

For this workspace, `. .\use-local.ps1` selects the existing venv, ignored state,
browser runtime and local executable paths. It does not contain credentials.
For a new checkout, set `APPLYPILOT_DIR` before importing/running ApplyPilot, create
that directory, and either run `applypilot init` or prepare the files below.

## Configuration

All private files belong under `APPLYPILOT_DIR` (this workspace uses `.private/state`).

- `profile.json`: factual personal answers, skills and immutable résumé facts.
  Omitted/null/empty/unknown fields remain unknown. Required unknown answers stop
  that application with `needs_input`. No default EEO/legal answers are supplied.
- `resume.txt`, `resume.pdf`: original verified résumé. Keep original employers,
  dates, metrics and technologies; never insert fictional experience.
- `searches.yaml`: searches and preferences. `queries`/`locations` and the older
  `searches` list are supported. `preferences` can specify `target_roles`,
  `locations`, `work_modes`, `salary_min`, `salary_currency`,
  `requires_sponsorship`, `max_job_age_days`, and `exclude_companies`,
  `exclude_titles`, `exclude_locations`, `exclude_domains`.
- `runtime.yaml`: copy `runtime.example.yaml`; contains score, volume, worker,
  retry, lease and timeout controls. Defaults are small; daily limits count
  attempted live applications (including failures) and reset at midnight UTC.
  Per-company limits count attempts in a rolling period. Unknown companies must
  be resolved by the submission adapter before live work.
- `.env`: `OPENAI_API_KEY` and optionally `LLM_MODEL` (default `gpt-4o-mini`).
  Gemini and OpenAI-compatible local endpoints remain supported. OpenAI powers
  scoring/writing and browser automation. The browser uses the OpenAI Responses API
  directly and requires Chrome, Node.js and npx. No Claude installation/login is needed.
  `APPLY_MODEL` overrides the browser model (otherwise the OpenAI `LLM_MODEL` or
  `gpt-4o-mini`). `apply_max_steps` and `apply_max_output_tokens` in runtime.yaml bound
  the tool loop and each response; `apply_timeout_seconds` bounds the entire attempt.
- `application_adapters.json`: explicit reviewed native-form mappings. Unsupported
  ATS workflows are held for input; see the adapter contract in `apply/gate.py`.

Salary filtering compares annual amounts in the configured currency; it does not
invent an hourly conversion. Missing salary/geographic data is held at apply time
by default (`require_salary_for_apply` / `require_location_for_apply` may be
configured under preferences). Explicit sponsorship denial is a disqualifier when
sponsorship is required. A missing posted date does not prove a job is fresh.

## Commands

```powershell
. .\use-local.ps1
applypilot doctor --tier 1          # local discovery readiness
applypilot doctor --tier 2          # also checks AI configuration
applypilot doctor                   # full local configuration; nonzero on missing requirements
applypilot run --dry-run            # pipeline plan, no AI call
applypilot run discover enrich -w 4
applypilot run score tailor cover pdf --min-score 7
applypilot status
applypilot dashboard
applypilot apply --dry-run --limit 3 --headless
```

After a site adapter and live end-to-end submission have been validated:

```powershell
applypilot apply --url "EXACT_QUEUED_JOB_URL" --limit 1
applypilot apply --limit 3 -w 3
applypilot apply --continuous -w 3
applypilot watch -w 4                 # repeatedly discover and prepare
applypilot watch -w 4 --apply-jobs    # also consume through validated live adapters
```

`--url` never falls back to another job. Meaningful query parameters are retained.
`--limit` is a total batch attempt limit, not per worker. More workers than jobs
cannot accidentally enable continuous mode. Continuous apply consumes the existing
queue; it does not itself rerun discovery. Run discovery/preparation again to add work.
`watch` repeats the full pipeline using `discovery_interval_seconds`; `--cycles 2`
bounds a soak test. `--apply-jobs` also runs a guarded application batch per cycle.

## Dry-run boundaries

Application dry-run performs an unauthenticated HTML GET, captures the document,
then fills exact known profile fields in a fresh browser with network, form
submissions, workers, frames and popups blocked. It does not invoke an unrestricted
AI agent, create accounts, upload documents to a server, or update job/attempt
rows. Debug screenshots, captured HTML and evidence JSON are written privately.

This is a captured single-page form integration check. It cannot validate login,
dynamic JavaScript applications, file-upload endpoints, or complete multi-page
Workday/Greenhouse/Lever/iCIMS/Ashby/SmartRecruiters flows. A passing preview is not
evidence that a live application will succeed.

`applypilot preview-form --url "EXACT_QUEUED_ARCHER_JOB_URL"` uses the reviewed
Archer mechanical-design form adapter. It first loads the public page and observed
static resources without candidate data, then blocks all browser network before
filling known text and dropdown values. It verifies the complete field/label schema;
unexpected questions fail closed. Optional unknown EEO/SMS answers remain blank.
The report includes unresolved dynamic controls and missing job-specific documents.
For the reviewed city widget, `archer-location-cache.json` can contain an observed
public geocoder response. The preview replays it locally only when its query,
city, state, country and exact option match the explicit profile. This enables
testing the real dropdown without sending candidate fields to the geocoder.
The preview never changes the job or attempt ledger and never uploads/submits.
Archer's live submission transport and CAPTCHA behavior remain unqualified.

Archer-specific answers belong in `profile.json` under `employer_answers.archer`:
`base_salary_usd`, `relocate_san_jose`, `referred`, optional `referrer_name`, and
optional `sms_consent`. Legal given/family names belong in `personal.first_name`
and `personal.last_name`. Do not infer a name split or consent from the résumé.

OpenAI model output remains untrusted. The runner forwards only allowlisted MCP
functions, serializes actions, preserves function-call history, imposes deadlines
and writes a private trace. Provider credentials never enter the MCP subprocess
environment. Model success text cannot bypass independent gate/browser evidence.
An exhausted credit/quota error needs billing correction; it is not automatically
retried. No real model response can be validated until the API project has credit.

## Live submission boundary

The native-form gate requires exact per-job URLs, employer, title, requisition ID,
an exhaustive field mapping to explicit profile facts, consent values, and verified
document bytes. Site JavaScript is disabled; only one validated request is allowed.
The agent has no shell, arbitrary JavaScript or email tools. Unknown endpoint/form
requirements stop the application rather than bypassing checks.

Dedicated Chrome profiles preserve authorized sessions between runs. The program
does not copy personal Chrome cookies or kill processes merely because a port is in
use. Workday account creation, verification email and CAPTCHA gates require
manual handling; persistent profiles alone do not prove Workday compatibility.

## Results and recovery

`application_attempts` records owned claim tokens, times, worker/session identity,
artifacts, category, retryability and evidence. A success requires an attempt-bound
validated submission plus independent confirmation evidence. Model text alone is
insufficient. Unverified submissions and expired/crashed active leases are held as
`submission_uncertain`; they never automatically resubmit.

Only known pre-submission transient failures get bounded exponential retry. A
stale worker cannot finalize a lost/expired claim. `--reset-failed` only requeues
exhausted known transient failures. `--mark-applied` records a manual unverified
submission, not independently confirmed success. Do not manually clear uncertain
states until the employer's application history establishes the actual outcome.

## Validation and privacy

```powershell
python -m pytest tests/ -v
python -m ruff check src/
python -m pip check
```

Browser tests launch real local Chromium against synthetic controlled endpoints.
They must pass, not be mistaken for external ATS validation. Some managed Windows
sandboxes require a workspace test directory and native process access:
`pytest tests/ -v --basetemp=.private/tests-validation`.

Private state, credentials, résumé files, cookies, generated PDFs, screenshots and
logs are ignored by git. Logs can contain private answers; retain them locally.
Review staged files before publishing. Upstream attribution/history and AGPL-3.0
license are preserved. No affiliation with unrelated ApplyPilot-branded services.
