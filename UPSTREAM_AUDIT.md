# Upstream audit

Reviewed 2026-09-15 (America/Los_Angeles). Baseline: `Pickle-Pixel/ApplyPilot` commit `4a8d521f67f5139811c0a910ef37410f8e6d836a` (package 0.3.0).

This review informed the local hardening work. Upstream patches were not blindly merged or cherry-picked; the principles selected below were independently implemented. Their authors' test claims were not independently reproduced, and no employer application was submitted during this audit.

## Implementation follow-through

The development branch implements exact identity selection (65/101), explicit NULL-safe
queue states and score thresholds (102), non-mutating previews (104/93), factual
answers and restricted browser capabilities (59), hashed artifact identities and
owned leases (61), and owned Chrome lifecycle/loopback addressing (70). Broad URL
fallbacks, unconditional stale-lock resets, guessed legal answers and personal
presets were rejected. Tests in `test_application_state.py`, `test_launcher_cli.py`,
`test_browser_safety.py`, `test_submission_gate.py` and `test_pipeline_integrity.py`
exercise these independently implemented behaviors. See VALIDATION.md for executed
results and the distinction between local fixtures and real external validation.

## Method and evidence limits

- Read the public issue list, all 58 PR metadata records returned by GitHub's all-state collection, the two release records, local CHANGELOG.md, and baseline apply launcher/Chrome/database code.
- Retrieved actual patches for PRs 34, 59, 61, 65, 70, 73, 93, 99, 101, 102, 104, and 108. Focused source review on target selection, dry-run persistence, recovery, Windows spawning, browser ownership, and screening instructions. Large unrelated changes were not exhaustively audited.
- Checked selected community repositories using repository metadata, README content, and latest five commits. This is a meaningful sample, not an exhaustive audit of hundreds of forks.
- Ran four small SQLite in-memory assertions locally using `.venv/Scripts/python.exe`. These reproduce SQL semantics only; they do not constitute end-to-end upstream PR tests.
- Did not run upstream browser agents, send email, log into employer sites, upload a resume, or submit applications. Author-reported live runs and test totals below remain third-party claims.
- GitHub API state was preferred over cached web lists: cached pages still showed PR 73 as open, whereas the current API showed it closed and unmerged. State can change after this audit.

## Priority findings and patch decisions

| Source / inspected revision | Finding and applicability | Decision |
| --- | --- | --- |
| [PR 65: exact job targeting](https://github.com/Pickle-Pixel/ApplyPilot/pull/65), `be6a515` (open) | Baseline strips the query string for a substring LIKE fallback. Indeed `?jk=` and LinkedIn job parameters can carry identity, so a target can select another employer's job. Patch tries exact matching first but retains a broad substring fallback. | Adopt the exact-target principle, **not** the fallback. Require exact identity or narrowly specified canonical normalization; reject unknown and ambiguous application URLs. Add query-ID, prefix-ID, `%`/`_`, and duplicate-application-URL cases. |
| [PR 102: SQL/null/min-score fixes](https://github.com/Pickle-Pixel/ApplyPilot/pull/102), `9aedb80` (open) | `apply_status != 'in_progress'` excludes NULL fresh rows. Patch makes it NULL-safe, but still permits applied/manual/expired rows. Separately, `database.get_jobs_by_stage` suppresses a supplied minimum for `scored` because its predicate already contains the text `fit_score`. | Adopt explicit allowed states and the stage score correction; apply score/retry/site rules consistently to target and queue modes. Do not copy the broad status inequality. Queue parameter order itself is correct in baseline. |
| [PR 104: dry-run applied state](https://github.com/Pickle-Pixel/ApplyPilot/pull/104), `65dac84` (open) | Baseline dry-run prompt requests `RESULT:APPLIED`; worker persists it as applied. Patch bypasses marking and calls `release_lock`. | Adopt a mode-level prohibition on persisting application outcomes. Patch alone is incomplete: baseline release changes failed state to NULL and preserves the newly written attempt timestamp. True no-mutation preview should select without claiming, or restore the full prior state under ownership checks. |
| [PR 93: Windows apply and dry-run](https://github.com/Pickle-Pixel/ApplyPilot/pull/93), `8f72f5a` (open) | Resolves Claude with `shutil.which`; adds an observe-only dry-run prompt and a separate result token. Also addresses NULL selection. | Adopt explicit executable resolution, Windows startup tests, and observe-only wording. Do not assume prose alone prevents browser writes, and do not rely on the agent choosing the right token to protect the database. |
| [PR 59: apply safety](https://github.com/Pickle-Pixel/ApplyPilot/pull/59), `7a5d504` (open) | Adds dedicated dry-run token, blocks Gmail send in dry-run, denies several built-in tools, adds prompt-injection warnings, and replaces several hardcoded screening answers with profile values. Still uses permission bypass, leaves broad Read available, and retains Gmail outside dry-run. | Reuse factual screening and untrusted-page boundaries. Remove unnecessary Gmail capability entirely for this workflow and restrict child capabilities/configuration. POSIX chmod additions do not establish Windows ACL protection. Author reports 15 tests; not rerun here. |
| [PR 61: artifact collisions and recovery](https://github.com/Pickle-Pixel/ApplyPilot/pull/61), `a66508e` (open) | URL-hashed filenames prevent same-title document overwrite. Per-worker upload folders remove one race. `reset_stale_locks()` clears **all** in-progress jobs at startup, assuming no second process. | Adopt collision-resistant filenames and per-attempt upload isolation. Reject unconditional lock clearing; another process can own an active claim, and a crashed submit may have succeeded remotely. Use ownership/lease checks and treat uncertain submissions as requiring reconciliation. Per-worker IDs alone still collide across processes. Author reports 16 tests; not rerun here. |
| [PR 70: CDP/lifecycle/null strings](https://github.com/Pickle-Pixel/ApplyPilot/pull/70), `0627d27` (open) | Changes CDP localhost to 127.0.0.1, normalizes missing application URL strings, catches browser cleanup failure. Port selection hashes only an instance directory basename into 20 windows, while broad port-based killing remains. | Adopt loopback consistency and URL validation. Reject the port-allocation guarantee: collisions remain possible, and basename identity is insufficient. Track and close only owned processes; use a dedicated profile rather than cloning the user's default profile. |
| [PR 108: broad pipeline/safety patch](https://github.com/Pickle-Pixel/ApplyPilot/pull/108), `973438f` (open at review) | Exact-only targets, timed stale recovery, retryable score errors, Gmail removal, login/account-creation stops, browser watchdog, and PDF preservation changes. Also rewrites scoring/tailoring extensively and proposes defaulting high-risk screening answers to No. | Treat as a collection of ideas, not a patch to import wholesale. Adopt no-email/no-account-creation defaults and verify generated PDF content. Reject guessed legal answers: unknown must stay unknown. Age-only recovery cannot establish that a remote submission failed. Its dry-run branch covers applied outcomes but is not a complete state-preservation model. Live verification claims belong to the author. |

Additional concern in PR 102: `extract_json` changes its failure contract from raising JSONDecodeError to returning None. That can move failures into downstream attribute errors and is unrelated to the targeted queue fix. Keep a clear parser contract and test malformed/truncated/model-commentary inputs before adopting any JSON recovery changes.

### Independently reproduced SQL behavior

A scratch `:memory:` SQLite database with two failed Indeed rows A/B and one NULL-state row C confirmed all four assertions:

1. Baseline `(url = target OR url LIKE '%.../viewjob%') ... LIMIT 1` can return A when B is requested.
2. `NULL != 'in_progress'` does not select a fresh job.
3. PR 65's fallback `%boards.greenhouse.io/acme/jobs/123%` matches `/jobs/1234`.
4. PR 102's `(apply_status IS NULL OR apply_status != 'in_progress')` permits an applied row.

These are reproducible query semantics, not speculative browser behavior. No production database was read or modified by these assertions.

## Open issues reviewed

| Issue | Relevance and response |
| --- | --- |
| [40: prompt injection and Chrome cloning](https://github.com/Pickle-Pixel/ApplyPilot/issues/40) | Baseline source confirms personal-profile cloning and powerful child-agent capabilities. Use a dedicated automation profile and narrow capabilities; prompt wording alone is not isolation. |
| [44: ready jobs but empty apply queue](https://github.com/Pickle-Pixel/ApplyPilot/issues/44) | Consistent with the confirmed targeted NULL predicate and inconsistent queue/readiness definitions. Readiness counts should share eligibility rules with selection. The report alone does not prove every reported case has this cause. |
| [57: PDF-only resume cannot tailor](https://github.com/Pickle-Pixel/ApplyPilot/issues/57) | Directly applicable to the user's supplied PDF: extract and verify text before scoring/tailoring, retaining the original PDF. |
| [68: scoring progress lost on interruption](https://github.com/Pickle-Pixel/ApplyPilot/issues/68) | Reports batch-at-end writes and zero scores on provider errors. Durable incremental writes and retryable provider failures deserve coverage; PRs [90](https://github.com/Pickle-Pixel/ApplyPilot/pull/90) and [100](https://github.com/Pickle-Pixel/ApplyPilot/pull/100) are candidates, but their patches were not inspected in depth here. |
| [11](https://github.com/Pickle-Pixel/ApplyPilot/issues/11), [17](https://github.com/Pickle-Pixel/ApplyPilot/issues/17): document overwrites | Supports per-job artifact names and per-attempt uploads. Do not let a shared title identify a resume. |
| [22: field report](https://github.com/Pickle-Pixel/ApplyPilot/issues/22) | Describes real-world Workday login friction, MCP configuration interference, misleading output, and artifact problems. Its application counts/success rates are author-reported and do not validate this installation. Strict child MCP config and explicit intervention states are useful ideas. |
| [91: Workday locale URLs](https://github.com/Pickle-Pixel/ApplyPilot/issues/91) | Some portals need locale segments. Do not blindly add en-US to every tenant; validate configured portal URLs and distinguish discovery URL repair from permission to create accounts. |
| [105: PostgreSQL complaint](https://github.com/Pickle-Pixel/ApplyPilot/issues/105) | The body describes `skyvern init --no-postgres`; upstream baseline uses SQLite. Not evidence that this repository requires PostgreSQL. |
| [107: Gemini quota](https://github.com/Pickle-Pixel/ApplyPilot/issues/107) | A recent user report that free quota is insufficient. Do not promise free-tier throughput based on old README text; measure the chosen provider and honor limits. |

## Recently closed and other closed PRs

Closed does not mean merged. The records below were unmerged at review.

| PR | Inspected material | Recommendation |
| --- | --- | --- |
| [101: manual status URLs](https://github.com/Pickle-Pixel/ApplyPilot/pull/101), `ae1c45a`, closed 2026-08-30 | Actual patch resolves canonical discovery URL first, otherwise unique exact application_url; errors on zero/ambiguous matches; CLI exits nonzero. Includes six regression tests. | Adopt the behavior with concurrency/ownership checks appropriate to the local claim design. Useful for accurately recording manual submissions. Tests were read, not run here. |
| [73: searches config format](https://github.com/Pickle-Pixel/ApplyPilot/pull/73), `f5dd6be`, closed 2026-09-10 | Actual patch adds flat `searches:` handling but hardcodes Switzerland and Swiss city accept lists. | Reject wholesale. Normalize config generically and honor user locations. A patch title and claimed Windows result are insufficient evidence of suitability. |
| [99](https://github.com/Pickle-Pixel/ApplyPilot/pull/99), `4731753`, and [98](https://github.com/Pickle-Pixel/ApplyPilot/pull/98), closed 2026-08-10 | PR 99 patch and both descriptions reviewed. Claude CLI call budget, subprocess timeout, fail-closed discovery-noise filtering; overlap with provider PR 96. | Defer provider migration pending chosen backend. Retain hard budgets and fail-closed invalid-job handling as design requirements. Author live-budget checks not independently reproduced. |
| [89: PostgreSQL runtime](https://github.com/Pickle-Pixel/ApplyPilot/pull/89), closed 2026-07-20 | Metadata/body only; a much larger architecture migration with claimed extensive CI. | Reject for this focused Windows setup. No need to replace SQLite to fix current queue predicates. Did not audit its migration or validate CI claims. |
| [34: personal preset and approval gate](https://github.com/Pickle-Pixel/ApplyPilot/pull/34), `35635e3`, closed 2026-03-11 | Patch adds approval concepts alongside another person's presets and changed ignore rules. | Do not import personal data/config. A local review gate can be designed independently when required by this user's workflow. |

## Meaningful forks and derivatives

| Repository / reviewed head | Evidence of divergence | Decision |
| --- | --- | --- |
| [spencerthayer/ApplyPilot](https://github.com/spencerthayer/ApplyPilot), `4b8dbd2` (latest push 2026-08-27) | Metadata confirms an upstream fork. README adds canonical resume.json, themed renderer, Codex/Claude/OpenCode backends. Recent commits merge ibarrajo-main and add a future methodology plan. | Potential later reference for backend abstraction; do not switch this installation wholesale. Extensive schema/runtime differences increase review scope. README/latest commits inspected, full code and CI not audited. |
| [ibarrajo/ApplyPilot](https://github.com/ibarrajo/ApplyPilot), `e77ec11` (latest push 2026-06-22) | README explicitly attributes upstream; GitHub metadata says `fork: false` (a derivative rather than a current fork-network member). Adds human intervention, Gmail tracking, ATS sessions, Q&A knowledge base and provider fallbacks. Recent commits repair structured education, hardcoded personal email, and dependency floors. | Good design reference for intervention and session continuity. Defer broad Gmail/tracking integration and migration. No claim that this branch is safe merely because it is more featureful. |
| [bincat233/ApplyPilot-Plus](https://github.com/bincat233/ApplyPilot-Plus), `44e3cb3` (latest push 2026-03-13) | Metadata confirms upstream fork. README advertises collecting community fixes, LiteLLM and Greenhouse. Latest five commits are documentation. | Do not treat the README's maintenance claim as proof of current freshness. Candidate reference for specific discovery/provider code only; not a replacement baseline. |
| [sebastianmukuria/ApplyPilot](https://github.com/sebastianmukuria/ApplyPilot) | Four focused upstream proposals; actual patches reviewed for safety, output, and URL identity. | Best narrow reference for several confirmed bugs, with the wildcard/recovery/cross-process caveats above. |
| [ArchieChelle/ApplyPilot](https://github.com/ArchieChelle/ApplyPilot) | Windows-specific proposal 93 plus resume/config/URL proposals. | Windows spawn and dry-run ideas are relevant; country registry replacement or broad resume rewrites need separate review. |

No fork was cloned over the working repository or merged during this audit. No third-party personal preset was used.

## Release notes and baseline

The API returned [v0.3.0](https://github.com/Pickle-Pixel/ApplyPilot/releases/tag/v0.3.0), published 2026-02-21, and [v0.1.0](https://github.com/Pickle-Pixel/ApplyPilot/releases/tag/v0.1.0). The local [CHANGELOG.md](CHANGELOG.md) stops at 0.2.0 despite package 0.3.0 and should not be treated as a complete release history.

The 0.3.0 release describes schema alignment, Gemini compatibility/rate-limit handling, parameterized queue filters, doctor, and validation modes. Those changes are already in this baseline. Parameterized SQL prevents injection through values; it does not fix identity ambiguity, NULL logic, or inconsistent eligibility. Reapplying release changes is unnecessary.

## Local implementation acceptance targets

1. Exact target identity, unambiguous application URL lookup, consistent score/retry/site rules, and no duplicate terminal-state application.
2. Preview leaves application rows unchanged on success, failure, interruption, and misleading agent output; no email/account creation/upload/submit path is available as a preview side effect.
3. Atomic claims with owner identity, safe completion/release, and conservative recovery. Uncertain remote submission must not become an automatic duplicate retry.
4. Dedicated Chrome profiles; no copying personal sessions; no killing arbitrary CDP listeners; per-run/attempt upload paths and owned cleanup.
5. Unknown screening facts remain unresolved, documents retain verified resume facts, and generated PDFs are checked before use.
6. Tests report local results separately from upstream authors' claims. No live success rate is inferred from unit tests or mocked browser sessions.
