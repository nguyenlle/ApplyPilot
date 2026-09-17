# ApplyPilot — reliability development fork

ApplyPilot finds job postings, enriches descriptions, scores them against your
profile, tailors a résumé, writes a cover letter, and uses OpenAI Responses with
guarded Playwright MCP tools to operate application forms. SQLite tracks jobs and attempts.
Scoring and document preparation can also use a supervised local Codex task through
the [private handoff workflow](LOCAL_HANDOFF.md), without direct model API calls.

This fork of [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
preserves the six-stage pipeline and upstream AGPL-3.0 license and history.
The original project was created by Pickle-Pixel. This repository is not affiliated
with similarly named commercial products.

**Validation is in progress. This branch is not ready for unattended high-volume
applications.** Live submission currently supports only explicitly configured
native HTML forms. Workday, Greenhouse, Lever, iCIMS, Ashby and SmartRecruiters
application flows have not been qualified. Local browser tests do not establish
real employer compatibility, and no real employer submission has been verified.

## What changed

- Exact job targeting and canonical deduplication retain requisition query parameters.
- Atomic, leased application claims prevent workers from claiming the same job;
  uncertain outcomes require review, with bounded retries for known transient failures.
- Unknown personal, legal and demographic answers stay unknown. Required missing
  information pauses that job while the batch continues.
- Factual document validation, job-bound manifests and checksums reject mixed-up artifacts.
- Captured-form dry runs block browser network writes and leave application state unchanged.
- Restricted browser tools and a native-form gate check identity, answers, consent,
  upload bytes and confirmation evidence before recording verified success.
- Configurable thresholds, volume controls and status/failure reporting support bounded runs.
- Local score-only handoffs require independent, hash-bound assessment reviews and
  update scores without generating documents or changing application state.
- OpenAI now runs the browser tool loop directly; no Claude installation or login is required.
- A specific Archer/Greenhouse preview adapter validates the observed form schema and
  fills explicit known answers after blocking network. Live Archer submission is still disabled.

Read [UPSTREAM_AUDIT.md](UPSTREAM_AUDIT.md) for reviewed issues, PRs and forks,
[BASELINE.md](BASELINE.md) for original failures, and
[VALIDATION.md](VALIDATION.md) for actual test results and outstanding work.

## Install

Use Python 3.11 or 3.12. The discovery extra installs JobSpy with its declared dependencies.

```powershell
git clone https://github.com/nguyenlle/ApplyPilot.git
cd ApplyPilot
git checkout codex/reliability-hardening
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,discovery]"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.private\browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
. .\use-local.ps1
applypilot init
```

On Linux, activate the venv and install browser system dependencies with
`python -m playwright install --with-deps chromium`; set `APPLYPILOT_DIR` to a
private state directory. `use-local.ps1` is the Windows workspace helper.

## Configure and run

For supervised preparation using your local Codex task, follow
[LOCAL_HANDOFF.md](LOCAL_HANDOFF.md). With private profile, resume and search files
configured and an existing discovered job, you can score one exact posting locally:

```powershell
applypilot local-score-export --url "EXACT_STORED_JOB_URL"
# Author score-result.json, then obtain an independent score-review.json.
applypilot local-score-import "HANDOFF_JSON" "SCORE_RESULT_JSON" --review "SCORE_REVIEW_JSON"
```

The export binds the job and current input snapshots. The author supplies an honest
score, strengths and gaps with exact source quotes; a distinct reviewer checks the
score and every assessment. Import validates their hashes, rejects stale or reused
handoffs, and updates only `fit_score`, `score_reasoning`, `scored_at` and the receipt.
These commands make no model API calls, generate no PDFs and do not change
application fields, artifacts or attempts. Eligibility remains `not_assessed`;
unknown salary, location, sponsorship and open status remain visible where applicable.
A fit score is not permission or eligibility to apply. Export a fresh handoff if
you later prepare documents.

For reviewed discovery ingestion, `verified_requisition_id` can identify an official
employer requisition. Combined with the employer name, it preserves distinct
same-title jobs while deduplicating location variants of one requisition. Retain
the official evidence and centrally review cross-source aliases and duplicate
posting templates before importing. An unverified board ID or a different posting
ID alone does not establish a distinct job; this field is not an automatic
deduplication guarantee.

The commands below use the optional API pipeline.

Put `profile.json`, verified `resume.txt`/`resume.pdf`, `searches.yaml`, and `.env`
in `APPLYPILOT_DIR`. The Windows helper selects `.private/state` inside this checkout.
Set `OPENAI_API_KEY` privately and optionally `LLM_MODEL`; the example uses
`gpt-4o-mini`. Provider/model access must be checked with your own API project.
Copy `runtime.example.yaml` there as `runtime.yaml` to adjust execution limits.
Never commit personal files, credentials, browser sessions or generated applications.

```powershell
applypilot doctor --tier 1
applypilot run --dry-run
applypilot run discover enrich -w 4
applypilot doctor --tier 2
applypilot run score tailor cover pdf --min-score 7
applypilot status
applypilot apply --dry-run --limit 3 --headless
```

OpenAI is used for scoring, writing and browser automation. Set `APPLY_MODEL` to
override the browser model independently. Browser execution requires Chrome,
Node.js/npx and a reviewed live per-job `application_adapters.json`. The full `applypilot doctor`
exits unsuccessfully until those requirements are met. It checks local readiness,
not API credit, remote model access or ATS compatibility. Exhausted API credit is
reported immediately instead of being retried as a temporary rate limit.

The specific Archer form can be previewed before documents are ready:

```powershell
applypilot preview-form --url "EXACT_QUEUED_ARCHER_JOB_URL"
```

This command never uploads or submits. It reports missing documents and unresolved
controls. The implementation follows OpenAI's documented
[function-calling loop](https://developers.openai.com/api/docs/guides/function-calling);
the application gate remains responsible for validation and confirmation.

After a real site adapter and controlled submission are validated:

```powershell
applypilot apply --url "EXACT_QUEUED_JOB_URL" --limit 1
applypilot apply --limit 3 -w 3
applypilot apply --continuous -w 3
applypilot watch -w 4
applypilot watch -w 4 --apply-jobs
```

See [OPERATIONS.md](OPERATIONS.md) for the full configuration contract, dry-run
boundaries, continuous operation, recovery rules and evidence interpretation.

## Development

```powershell
python -m pip check
pytest tests/ -v
ruff check src/
```

Browser tests start isolated local servers and Chromium with synthetic candidate
data. They do not submit to employer sites. See [CONTRIBUTING.md](CONTRIBUTING.md)
for contribution conventions and [CHANGELOG.md](CHANGELOG.md) for changes.
