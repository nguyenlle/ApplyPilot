# Validation checkpoints

## 2026-09-16: supervised local Codex preparation

This checkpoint adds a working preparation route without direct model API calls.
It does not qualify live employer uploads, submission, or unattended operation.
The optional API runner retains the quota limitation described in the historical
checkpoint below; API credit is not required for the three local handoff commands.

- A separate local Codex task authored an actual Archer resume/cover pair. An
  independent reviewer checked all 45 claims against the original resume and
  inspected every page of both final PDFs (one page each). Empty Projects heading
  removed. Both documents retain original employers, historical titles, dates,
  metrics, education and supported skills without adding thermal expertise.
- Strict v2 export/result/review checks passed. Import changed only seven
  preparation fields on the exact Archer job. All other jobs, application fields
  and attempt rows remained unchanged. Both artifacts verified against the
  committed receipt; a second import was rejected with no state changes.
- Archer's honest score remains 6/10, below the unchanged 7/10 threshold. The five
  stored jobs include no independently established >=7 match. A status counter
  that incorrectly counted below-threshold documents was reproduced on real data,
  fixed and independently reviewed. Prepared-candidate counts are not a claim
  that full eligibility, artifact or live-adapter gates have passed.
- Adverse tests cover strict schemas, secret-field projection, source/job drift,
  partial/incorrect evidence, reviewer separation, page/hash coverage, path/link
  escape, replay/concurrency, atomic rollback and process crashes. Two discovered
  defects were fixed and retested: shared resume/cover text cannot use job-only
  evidence; an approval file left by a crash cannot verify without a committed
  database receipt. Local PDFs cannot be silently recertified after regeneration.
- Native browser fixtures prove immutable verified file bytes are selected into
  the exact inputs. Attempted asynchronous POST uploads, delayed requests and
  beacons are blocked. Repeated previews preserve the complete jobs/attempts state.
- Two actual Archer page previews filled 14 known fields, replayed the cached city
  selection and selected both verified PDF buffers locally. Complete SQLite dumps
  matched before/after each run and across runs. Both screenshots were inspected.
  The site displayed attachment errors because upload initialization was blocked
  (`presigned_fields`); no real PDF-upload request arose. Thirteen requests were
  blocked per run. This verifies locked local selection, not employer upload or
  submission. No application was submitted.
- Updated editable installation with declared `pypdf` dependency and `pip check`
  passed in the second private virtual environment. Whole-source Ruff passed.
- Final full suite: **362 passed, 3 skipped in 68.66 seconds**, Windows/Python3.12
  in the second private environment, including native Chromium tests. Skips were
  only symlink fixtures denied by Windows permissions; hardlink/path-escape tests
  passed. Independent local-handoff/pipeline review also passed 124 tests with the
  same three skips before the final status regression was added.

Private evidence includes the v2 handoff's result, independent review, before/after
state snapshots and import receipt under `.private/state/local-handoffs/`, plus
test logs under `.private/`. These contain private applicant data and are not
published. See [LOCAL_HANDOFF.md](LOCAL_HANDOFF.md) for exact operating commands.

Remaining work: identify a qualifying current job, qualify its real upload and
submission transport, resolve required unknown answers/authentication, and verify
one controlled submission before any live batch/parallel/continuous validation.
The current milestone authorizes preparation and locked preview only.

## Historical checkpoint — 2026-09-16: OpenAI migration and Archer preview

The OpenAI key is configured and Git-ignored. A real Chat Completions scoring call
was attempted. The API returned HTTP 429 with `type=insufficient_quota` and
`code=credit_balance_exhausted`. No real model output or tailored document was
generated. The old retry loop waited 135 seconds; quota classification now stops
immediately, with regression coverage. A final real Archer scoring recheck still
failed for exhausted quota and returned in 2.22 seconds with no model output.
Available API credit is still required to
complete real scoring, résumé/cover generation and model-driven browser validation.

The browser runner now uses OpenAI Responses function calling over the existing
restricted Playwright MCP relay. Claude Code installation/authentication is no
longer required. Actual MCP schemas, sequential tool calls, deadlines, tool budgets,
credential isolation and independent submission verification are preserved.
An integration review also fixed application totals counting a model-claimed
success that the database had correctly downgraded to uncertain.

Current live findings:

- LinkedIn and modern Greenhouse description selectors were corrected. Both stored
  enrichment errors are resolved; all five stored jobs have descriptions.
- CoffeeSpace explicitly excludes sponsorship. It is now `not_eligible`, with
  deterministic score 1. This is a rule-based rejection, not a successful AI score.
- Archer's signup URL was replaced with its verified official Greenhouse application
  URL. The LinkedIn and employer descriptions match after whitespace normalization.
- The specific Archer mechanical-design form was loaded with HTTP 200. Its complete
  field/label schema was reviewed. After browser network access was blocked, 14
  fields were filled locally using explicit profile answers, including dropdowns.
  The city dropdown used a cached public geocoder response replayed offline.
- Required résumé documents remain missing. Optional demographic/SMS consent answers
  were left blank. No live uploads, CAPTCHA response or final submission occurred.
  The configured Archer adapter is explicitly preview-only and cannot unlock live
  application readiness. `preview-form` returns `needs_input` and a nonzero exit.
- Full local doctor now fails only its live-adapter requirement; its key-presence
  checks do not establish billing availability. No separate Claude login is needed.

The complete suite passes in the clean Python 3.12 installation: **254 tests in
45.38 seconds**. `ruff check src/` and `pip check` also pass. Linux CI passed on
Python 3.11 and 3.12 for implementation commit `1343822`:
[run 35109164190](https://github.com/nguyenlle/ApplyPilot/actions/runs/35109164190).

The OpenAI runner's focused 23 tests pass, including actual pinned MCP/Chrome
navigation, inline snapshots, SHA-approved synthetic PDF selection, denial of
arbitrary JavaScript and process shutdown. Provider behavior is tested using a
local HTTP server, not real OpenAI completions. Archer schema/offline-preview and
launcher/database integration tests cover the migrated boundaries.

Private evidence is in `.private/validation/enrichment/`,
`.private/state/logs/archer-previews/` and `.private/openai-final-clean.txt`.
No candidate profile, résumé, API key, filled screenshot or operational trace is published.

Next required work: fund the API project; generate and fact-check one actual
document pair; validate Archer's upload/CAPTCHA/submission transport with those
documents; only then attempt one controlled employer submission. Existing local
tests do not satisfy these remaining live acceptance criteria.

## Historical checkpoint — 2026-09-15

This is an unfinished reliability development branch, not a production-readiness
certificate. No real employer application has been submitted or verified.

### Executed checks

| Check | Observed result | Scope |
| --- | --- | --- |
| Clean Python 3.12 virtual environment, `pip install -e ".[dev,discovery]" pypdf` | Passed | A second venv without inherited site packages |
| `pip check` | Passed, no broken requirements | Both development and clean-install environments |
| `applypilot --version` | 0.3.0 | Preserved upstream package version |
| `ruff check src/` | Passed | Whole source tree |
| `pytest tests/ -v -p no:cacheprovider` | 180 passed in 20.55 seconds | Whole suite in clean venv; actual local Chromium tests included |
| GitHub Actions Linux CI | Passed on Python 3.11 and 3.12 | [Run 35063679464](https://github.com/nguyenlle/ApplyPilot/actions/runs/35063679464), implementation commit `55749f1` |
| `applypilot doctor --tier 1` | Passed | Discovery configuration/imports/browser |
| Full `applypilot doctor` | Failed as intended, exit 1 | Missing OpenAI key, Claude authentication and reviewed site adapters |
| Live LinkedIn discovery | Five postings returned and stored | One bounded mechanical-design search in the SF Bay Area |
| Live Workday discovery API | Three postings returned from a 280-result search | NVIDIA API response; off-target locations were not imported |
| Live LinkedIn enrichment | Initial two-page run: one partial, one error | Not proof of reliable enrichment; existing useful descriptions must survive failures |
| Final read-only LinkedIn enrichment recheck | Partial; zero extracted description characters | LLM fallback unavailable without credentials; no auth-wall detection on this sample; database hash unchanged |
| Synthetic document pipeline | Passed | Mocked model outputs; job mapping, factual checks and file checksums |
| Actual PDF rendering | Passed and visually inspected | Synthetic résumé and cover letter; no claim about live model-generated documents |
| Browser dry run | Passed on local synthetic forms | Network writes blocked; application/attempt rows unchanged |
| Native-form live gate | Passed against local synthetic HTTP server | One validated POST, employer/title checks, exact payload/files, response-bound receipt |
| Playwright MCP 0.0.81 | Actual initialize/tools-list/forbidden-tool/EOF smoke passed | Cached official subprocess; 26 tools reported before filtering |
| Real AI scoring/tailoring | Not run | No configured OpenAI key at checkpoint |
| Real ATS submission | Not run | Authentication, reviewed dynamic-site adapters and full workflow validation remain outstanding |

The suite includes exact-target/wildcard/query-ID regressions, SQL NULL handling,
deduplication, atomic claims, thread/process concurrency, forced worker exit and
lease recovery, bounded retries, provider failures, immutable document facts,
cross-job artifact checks, isolated dry runs, missing fields, upload bytes,
one-use submission tokens, misleading confirmations, browser cleanup and bounded
continuous-cycle behavior. These are local tests, not employer-site success claims.

## Acceptance criteria status

| Requested criteria | Status and evidence |
| --- | --- |
| 1, 3, 4: install, full suite, lint | Passed locally on Windows/Python 3.12 and in Linux CI on Python 3.11/3.12. |
| 2: full environment doctor | Blocked by local credentials and configured site adapters. Discovery-only doctor passes. |
| 5: valid discovery | Demonstrated with a small live LinkedIn sample and a read-only Workday API request. Not all supported boards validated. |
| 6: reliable enrichment | Partial. Failure preservation/auth-wall tests pass; live extraction still has failures. |
| 7–9: scoring, factual résumé, cover letters | Mocked integration and actual synthetic PDF rendering pass. Real model outputs remain untested. |
| 10, 13, 14: duplicate/target/artifact safety | Local unit, state, concurrency and browser-gate tests pass. |
| 11–12: representative dry runs, no false applied state | State and static-form browser tests pass. Multi-page/dynamic employer flows remain unverified. |
| 15: controlled real verified submission | Not performed. Local synthetic server submission does not satisfy this criterion. |
| 16–20: batch failures, retries, crash recovery, parallel safety | Local regressions pass. An expired live lease is quarantined for reconciliation, not blindly retried. |
| 21: continuous operation | Bounded-cycle tests pass; sustained external soak test remains outstanding. |
| 22: diagnostic logs | Structured attempts, categories, manifests and confirmation evidence implemented and locally tested. |
| 23: private data excluded | Private state, résumé, credentials, cookies and generated artifacts are ignored. Synthetic fixtures only in tests. |
| 24–25: operating instructions and limits | README and OPERATIONS.md describe this branch's actual supported scope and commands. |

## Remaining engineering and validation

1. Supply an OpenAI API key locally and test one real scoring/document chain.
   Review actual outputs against the original résumé before broader preparation.
2. Authenticate Claude Code separately. OpenAI credentials do not authenticate the
   current Claude browser runner.
3. Implement and qualify dynamic ATS adapters, starting with a concrete eligible
   job. Current native HTML adapters deliberately disable site JavaScript and do
   not support Workday account creation, multi-page forms or custom controls.
4. Exercise representative real forms in dry-run mode, then one controlled live
   submission with stored confirmation evidence. An application needing unknown
   facts/consent/login/verification must pause for input.
5. Only after that, validate a small live batch, parallel execution, recovery and
   a sustained continuous run. Do not extrapolate from local fixture tests.

Raw logs and screenshots are retained privately under `.private/`; they are not
published because operational runs can contain candidate information.
