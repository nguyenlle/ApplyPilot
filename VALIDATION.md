# Validation checkpoint — 2026-09-15

This is an unfinished reliability development branch, not a production-readiness
certificate. No real employer application has been submitted or verified.

## Executed checks

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
