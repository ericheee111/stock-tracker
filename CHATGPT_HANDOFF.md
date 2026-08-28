# ChatGPT Handoff

> Updated: 2026-08-28
> Workspace: `D:\Projects\stock-tracker`
> Branch: `main` tracking `origin/main`
> Purpose: preserve the exact continuation state for ChatGPT-led repository work.

## 1. Active user request

Reassess `docs/PRD-股票辅助判断与交易参考网站.md` under the constraint that a full cloud backend may be difficult or unnecessarily expensive. The accepted direction is to keep collection, computation, SQLite, research, portfolio facts and private APIs local while deploying only the web frontend to low-cost cloud static hosting. Oracle Cloud registration is unavailable and must not remain a dependency.

The user also requires this file to be updated after every completed logical task.

## 2. Task A — interrupted-session recovery and repository audit

Status: `COMPLETED`

Fresh evidence from the current working tree confirms that the interrupted session completed most of the documentation-level architecture revision, but did not finish consolidation or verification.

### Confirmed completed work

- PRD header and main body were upgraded to v1.1 and now define `HYBRID_PRIVATE` as the default deployment direction.
- Oracle Cloud was removed from the intended dependency chain.
- A detailed deployment candidate, `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`, was created.
- `docs/PRODUCT-GAP-MATRIX-v1.0.md` was renamed to `docs/PRODUCT-GAP-MATRIX-v1.1.md` and expanded with Hybrid H0–H5 gaps.
- `AGENTS.md`, `overview.md`, `docs/HANDOFF.md`, `docs/STAGE1-PARALLEL-EXECUTION-PLAN.md` and `render.yaml` were partially aligned.
- Existing code evidence matches the recorded Stage 1.5 gaps:
  - `config/app.toml` still defaults to `0.0.0.0`;
  - `web/js/api.js` and `web/js/sse.js` still use same-origin `/api/...` paths;
  - browser private access is not API-origin-scoped;
  - the backend has no formal CORS/`OPTIONS` implementation;
  - `/api/runtime/health` is not implemented;
  - Tailscale Serve and cloud static hosting have not been locally or cross-device accepted.

### Consolidation defects found

1. Two overlapping deployment documents currently act as competing specifications:
   - `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`;
   - `docs/DEPLOYMENT-HYBRID-LOCAL-CLOUD-ARCHITECTURE.md`.
2. The PRD already integrates the detailed H0–H5 design, but also contains a second appended deployment revision using a conflicting `LOCAL/HYBRID/SNAPSHOT/CLOUD` and D0–D4 vocabulary.
3. `overview.md` repeats the same conflicting second vocabulary and links the second document as though it were authoritative.
4. `docs/HANDOFF.md` still has v1.0/date wording in its header and incorrectly skips H0 when naming the next deployment slice.
5. Some test totals in historical handoff prose have not yet been freshly rerun for this documentation-only revision and must not be presented as current evidence without qualification.

## 3. Working-tree protection

Current pre-existing changes must be preserved. At audit time the working tree contained:

```text
M  AGENTS.md
M  docs/HANDOFF.md
M  docs/PRD-股票辅助判断与交易参考网站.md
R  docs/PRODUCT-GAP-MATRIX-v1.0.md -> docs/PRODUCT-GAP-MATRIX-v1.1.md
M  docs/STAGE1-PARALLEL-EXECUTION-PLAN.md
M  overview.md
M  render.yaml
?? docs/DEPLOYMENT-HYBRID-LOCAL-CLOUD-ARCHITECTURE.md
?? docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md
```

The rename is already staged; its content edits and the other changes are not fully staged. Do not reset, restore, clean, rebase or discard any of this work.

No production database, WAL, SHM, logs, screenshots or private portfolio data were read or modified during Task A.

## 4. Canonical direction for continuation

Use `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md` as the single normative deployment specification because it is already referenced by the main PRD section, Gap Matrix, primary Handoff, Overview reading order and Render warning, and because it contains the more complete H0–H5 contracts.

Preserve the useful optional signed/TTL snapshot concept from the alternate draft as a deferred non-default mode, then convert the alternate filename into a compatibility pointer rather than leaving two independent sources of truth.

## 5. Task B — deployment-spec consolidation

Status: `COMPLETED`

- `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md` is now the single normative deployment specification and is marked `Design Freeze`; H0–H5 implementation and acceptance remain pending.
- The duplicate `docs/DEPLOYMENT-HYBRID-LOCAL-CLOUD-ARCHITECTURE.md` was converted into a compatibility pointer with an explicit old-to-new terminology map.
- The PRD's duplicated `LOCAL/HYBRID/SNAPSHOT/CLOUD` and D0–D4 draft is now a collapsed, explicitly non-normative historical appendix.
- `overview.md` and `AGENTS.md` now use the canonical mode names: `LOCAL_ONLY`, `HYBRID_PRIVATE`, `HYBRID_PUBLIC_AUTH`, optional deferred `HYBRID_SNAPSHOT`, and `PURE_CLOUD_EXPERIMENTAL`.
- The useful signed, short-TTL, read-only Snapshot concept was preserved in the canonical spec but removed from the Stage 1.5 H0–H5 critical path.
- A real stage-assignment defect was corrected: loopback-by-default belongs to H0; H3 only revalidates and hardens that completed contract.

## 6. Task C — v1.1 metadata and execution-order correction

Status: `COMPLETED`

- `docs/HANDOFF.md` now identifies PRD v1.1 and the 2026-08-24 alignment date.
- The next engineering slice is consistently H0 first, followed by H1/H2; historical text that skipped H0 was corrected.
- PRD wording that still called implemented Stage 1 the “next product slice” was corrected; only Portfolio editing UI remains parallel follow-up work.
- Previously recorded test totals were labeled historical and have now been superseded by the fresh Task E results below.

## 7. Task D — official platform-fact validation

Status: `COMPLETED`

The canonical deployment specification now carries direct primary-documentation references for Cloudflare Pages, GitHub Pages, Tailscale Personal/Serve/Funnel, Cloudflare Tunnel/Quick Tunnels, Render Free and Cloudflare Workers. Unsupported or overly broad claims were corrected; in particular, the document no longer relies on a generic Serve identity-header claim and instead records the verified tailnet-only/loopback-target/background-service behavior.

## 8. Task E — consistency checks, regression and final review

Status: `COMPLETED`

### Fresh verification results

- `python -m compileall -q stock_tracker scripts tests tests_quant`: exit 0.
- `python -m unittest discover -s tests -p "test_*.py"`: 364 tests passed, 1 live probe skipped.
- `python -m unittest discover -s tests_quant -p "test_*.py"`: 560 tests passed. Existing negative-path migration tests emitted SQLite `ResourceWarning` messages from `migrations.py:304`; the suite still exited 0. Do not describe this run as warning-free.
- `python -m pip check`: no broken requirements.
- `python scripts/run_quant_contract_smoke.py`: `passed=true`, `synthetic_fixture_only=true`, `production_database_modified=false`.
- `python scripts/run_quant_fixture_benchmark.py`: exit 0, synthetic fixture only; the challenger was correctly not promoted because of ECE regression and time instability. LightGBM was unavailable and not evaluated.
- `npm run today:qa` under `qa/`: 17/17 passed.
- `python scripts/run_stage1_today_integration.py`: real Python API/Web Today 17/17 and Portfolio CRUD 13/13 passed using temporary integration data.
- `python scripts/quant_migrate.py --database data/stock_tracker.db`: read-only dry-run succeeded, reported four pending Quant migrations and `database_modified=false`.
- Production database SHA-256 before and after the dry-run remained `1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1`.
- Required-document existence check returned `required_docs_ok`.
- `git diff --check`: exit 0 after removing three trailing blank lines. Git still prints normal Windows LF-to-CRLF warnings.
- Repository searches found no active `Design Freeze Candidate`, stale H1/H2-first next-step wording, H3-owned loopback contract, or active reference to the alternate deployment document. Old terminology remains only where explicitly preserved for audit/compatibility history.

### Review result

The interrupted PRD/deployment reassessment is complete at the architecture-and-documentation layer. It does **not** implement or claim acceptance of Hybrid H0–H5. Current runtime evidence still shows same-origin frontend paths, `0.0.0.0` default bind, no formal CORS/`OPTIONS`, no `/api/runtime/health`, and no Tailscale Serve or Cloudflare/GitHub Pages cross-device acceptance. The next engineering slice is Hybrid H0.

No live market/provider data was fetched for this documentation task. Quant smoke/benchmark evidence is synthetic and must not be presented as investment-performance validation.

## 9. Parallel-work and generated-artifact isolation

The initial audit did not list UI/build work. During this continuation, additional changes appeared in `web/css/*.css`, `web/index.html`, `web/js/app.js`, `build/stock-tracker-web/**` and `stock-tracker-web.zip`. Treat them as external/concurrent work unless independently proven otherwise; they were not edited, reviewed or claimed by this task and must not be swept into a deployment-document commit.

The compileall verification also updated the tracked cache file `stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc`. It is a verification artifact, not a source change. It was left untouched because repository rules prohibit unauthorized checkout/restore and because the working tree is concurrently active.

The already staged rename `docs/PRODUCT-GAP-MATRIX-v1.0.md -> docs/PRODUCT-GAP-MATRIX-v1.1.md` was preserved. No reset, clean, broad restore, rebase or deletion was performed.

## 10. Git actions before H0

No commit, merge or push had been performed during the PRD-only continuation. The next user instruction now explicitly authorizes designing, implementing, reviewing and pushing Hybrid H0. All Git actions must still stage only deployment/H0 files and exclude concurrently changing UI/build/cache artifacts.

## 11. Task F — Hybrid H0 baseline, design and implementation plan

Status: `COMPLETED`

- Fresh worktree inspection reconfirmed that the prior deployment/PRD changes are still uncommitted and that unrelated UI/build work is concurrently present.
- The execution host does not currently provide a Tailscale CLI (`tailscale version` and `where.exe tailscale.exe` both failed), so this session cannot fabricate a real Tailnet or physical two-device acceptance result.
- Official Tailscale documentation was revalidated for the current Serve CLI: the supported Bootstrap command is `tailscale serve --bg http://127.0.0.1:<port>`; Serve is tailnet-only, applies tailnet access controls, and background Serve resumes after daemon/device restart.
- `docs/STAGE1.5-HYBRID-H0-DESIGN-PLAN.md` freezes the implementation into four slices:
  1. loopback-by-default plus explicit non-loopback acknowledgement;
  2. token-safe Tailscale Serve orchestration;
  3. temporary-DB local/server/client acceptance tooling with a write-safety fixture marker;
  4. full regression, adversarial review, scoped commits and GitHub push.
- Review will report engineering readiness and physical-device operational acceptance separately. Engineering code may be pushed after full repository gates pass, but `OPERATIONAL_DEVICE_ACCEPTANCE` must remain `PENDING` until a Tailscale-installed server and a distinct Tailnet client run the acceptance command.

## 12. Task G — Hybrid H0 implementation and focused verification

Status: `COMPLETED`

Implemented:

- four-layer loopback enforcement in config, CLI/build context, local start script and `APIServer` itself;
- explicit `--allow-non-loopback` acknowledgement for reviewed `PURE_CLOUD_EXPERIMENTAL` use;
- `.dockerignore` protection for runtime databases, logs, caches, build artifacts, archives, screenshots and Agent files;
- shared strict private-access validation;
- Tailscale Serve `preflight/enable/status/disable` adapter locked to `http://127.0.0.1:<port>`;
- existing Serve configuration ownership/conflict checks, idempotent exact-target enable and no global `serve reset`;
- temporary-DB H0 local/server/client acceptance tooling with strict marker gating before any Portfolio write;
- physical-device evidence strengthened from hostname comparison to distinct stable Tailscale node IDs from each device's `tailscale status --json`;
- synchronized PRD, architecture, deployment spec, Gap Matrix, Overview and implementation handoff.

Focused evidence:

```text
H0 focused tests = 16 passed
related existing config/private-API/server tests = 22 passed
local remote-style static/REST/SSE/Portfolio acceptance = passed
focused compileall = passed
ruff check = passed
production database SHA before/after =
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

`ruff format --check` was blocked by the CodexPro high-risk command guard and was not run; it must not be claimed as passing. The current host still lacks a Tailscale CLI and a controllable second Tailnet device, so `REAL_TAILSCALE_SERVE` and `TWO_DISTINCT_TAILNET_NODES` remain `PENDING`.

## 13. Task H — full gates and independent adversarial review

Status: `COMPLETED`

Independent review findings and fixes:

1. `IMPORTANT`: direct `APIServer("0.0.0.0", ...)` could bypass the CLI/build-context guard. Fixed by enforcing `require_safe_bind()` inside `APIServer` itself and adding a regression test.
2. `IMPORTANT`: hostname-only evidence could not prove two different Tailnet devices. Fixed by requiring distinct stable Tailscale node IDs from each device's `tailscale status --json` before any acceptance CRUD write.
3. `CRITICAL`: a matching Proxy string could coexist with Funnel, malformed pseudo-disabled Funnel values, extra mounts, Services, Foreground or extra listeners, causing false ownership and unsafe `serve off`. Fixed by structural exact Serve ownership: Funnel absent/empty or strict boolean false only, exactly one HTTPS listener, exactly one `/` Proxy, no extra handlers/sections. Funnel, string `"false"` and extra-mount negative tests confirm no mutation.
4. `IMPORTANT`: Docker `COPY .` could include local database/build/archive/cache/screenshot/Agent artifacts. Fixed with `.dockerignore` and a repository contract test.
5. `IMPORTANT`: server and operator Token validators could drift. Fixed by one strict shared security helper and environment-only access.
6. `CRITICAL`: two-device Portfolio CRUD must never touch production holdings. Fixed with a temporary SQLite fixture, exact marker/fixture identity, fail-before-write device checks and production SHA verification.
7. `CRITICAL`: the real-device client previously accepted an arbitrary `--base-url`, risking Bearer disclosure to a wrong Origin. Fixed by requiring normalized `https://*.ts.net` on port 443 with no userinfo, path, query or fragment before the Bearer is read or sent.

Fresh final gates:

```text
focused H0 tests = 16 passed
full runtime = 380 passed, 1 skipped
full quant = 560 passed (existing migration-path SQLite ResourceWarnings)
Mock Today UI = 17/17
real Python API/Web Today = 17/17
Portfolio CRUD integration = 13/13
H0 local remote-style acceptance = 12/12
compileall = passed
ruff check = passed
pip check = passed
quant contract smoke = passed, synthetic only
quant fixture benchmark = passed, synthetic only, challenger not promoted
migration dry-run = passed, database_modified=false
git diff --check = passed
production database SHA before/after =
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

Review document:
`docs/STAGE1.5-HYBRID-H0-INDEPENDENT-REVIEW.md`

Final review verdict:

```text
ENGINEERING_READY_FOR_MERGE
OPERATIONAL_DEVICE_ACCEPTANCE_PENDING
```

The operational verdict remains pending because this host has no Tailscale CLI and the session has no second controllable Tailnet node. This does not block engineering merge/push, but it blocks any claim that real Serve or two-device acceptance passed.

## 14. Task I — scoped Git delivery

Status: `COMPLETED`

- Created implementation commit `cf5b5f8eeae93c4147d6b607b30c3c569247a2b1` with message `feat: implement hybrid H0 private bootstrap`.
- Pushed `main` to `origin` successfully.
- Verified exact SHA equality across local `HEAD`, local remote-tracking `origin/main`, and `git ls-remote --heads origin main`:

```text
cf5b5f8eeae93c4147d6b607b30c3c569247a2b1
```

- The pushed commit contains only the PRD/hybrid architecture/H0 implementation, tests and review documents. Concurrent UI changes, `build/`, ZIP packages, QA screenshots, runtime data and the tracked Python cache artifact were not included.
- The worktree intentionally remains dirty because those unrelated concurrent changes are preserved rather than reset, cleaned or swept into H0.
- `OPERATIONAL_DEVICE_ACCEPTANCE` remains `PENDING` until Tailscale is installed and signed in on the server and a distinct Tailnet node runs the real `server/client` acceptance.

## 15. Next continuation point

- Complete real Tailscale Serve and two-distinct-node operational acceptance when the devices are available.
- Continue the next code slice with Hybrid H1/H2: Runtime Config/API Base separation, exact CORS/`OPTIONS`, Runtime Health, version handshake and offline/auth state separation.

## 16. Task J — Signal Ledger UI redesign

Status: `COMPLETED`

- Redesigned the current static frontend from the previous aurora/glass treatment into a restrained `Signal Ledger` decision terminal.
- The UI source set for this release is `web/index.html`, `web/css/base.css`, `web/css/cockpit.css`, `web/css/today.css`, `web/css/portfolio.css`, new `web/css/terminal.css`, and `web/js/app.js`; existing DOM target IDs, API paths and financial-decision semantics remain unchanged.
- The visual hierarchy now prioritizes the daily action summary, blockers, position sizing, market posture and data status; scores and decorative effects are secondary.
- The design uses matte ledger panels, a restrained grid background, tabular numerics, compact system chrome, desktop side navigation and mobile bottom navigation.
- Existing runtime/cache and unrelated repository changes are preserved and will not be reset or swept into this UI commit.
- Fresh verification before Git delivery: Mock Today QA `17/17`; real Python API/Web Today `17/17`; Portfolio CRUD `13/13`; 390 / 768 / 1280 screenshot checks all report `overflowPx=0`; overview/watch/radar/research routes all activate successfully; `git diff --check` passes for the redesign source set.
- No remote fonts, font binaries, analytics or third-party visual scripts were added; generated screenshots and local ZIP/build artifacts remain outside the source commit unless explicitly requested.
- UI implementation commit `370317b` (`feat(web): redesign decision terminal UI`) was pushed successfully to `origin/main`; this handoff update records the completed delivery.

## 17. Task K — local repository hygiene review

Status: `COMPLETED`

- Reviewed the remaining dirty worktree after the Signal Ledger push.
- `AGENTS.md` contains a valid roadmap-state correction: Stage 1.1 is completed and Hybrid H1 is the current next engineering slice; it is safe to commit.
- `.gitignore` now excludes current reproducible local artifacts: `/build/`, `/stock-tracker-web*.zip`, and the generated QA screenshot families `01-dark-*`, `02-dark-*`, `_min.png`, `aurora-*`, and `v2-*`; existing tracked golden screenshots are not removed.
- Python bytecode is already covered by `__pycache__/` and `*.pyc`. Five historical `.pyc` files are still tracked by Git; the currently modified `stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc` must not be pushed. CodexPro blocked `git rm --cached` as a high-risk operation, so this session does not bypass the guard or delete local cache files.
- After the hygiene commit, generated build/ZIP/screenshot artifacts are correctly hidden from status; after this handoff-only completion commit, the only expected dirty path is the legacy tracked `.pyc` noted above.
- Hygiene/roadmap commit `2dca0fd809127678c4767bf723eff44a498ea2fc` (`chore: ignore local generated artifacts`) was pushed to `origin/main`; local `HEAD`, local `origin/main`, and remote `refs/heads/main` were verified equal at that SHA before this handoff-only completion update.

## 18. Task L — Hybrid H1/H2 design freeze

Status: `COMPLETED`

- Revalidated the canonical H1/H2 contracts in `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md` and froze the implementation plan in `docs/STAGE1.5-HYBRID-H1-H2-DESIGN-PLAN.md`.
- H1 scope is limited to no-secret Runtime Config, one REST/SSE/Health URL Builder, normalized allowed API origins, origin-scoped session access, API Major/Engine/Commit handshake, and explicit runtime-state presentation.
- H2 scope is limited to exact CORS allowlisting, strict OPTIONS preflight, cross-origin Authorization support, public metadata-only `/api/runtime/health`, SSE CORS, and security/regression gates.
- The design requires private requests to omit credentials and referrers and to reject redirects; browser cookies or private headers must not follow an unexpected redirect.
- Hard runtime/config/version/engine/network/tunnel/CORS failures must clear current private decision state so stale `EXECUTABLE` actions are not left on screen.
- H3/H4/H5, real Tailnet operational acceptance, cloud static deployment, Quant and trading semantics remain outside this slice.
- The tracked Python cache modification remains isolated and must not enter H1/H2 commits.

## 19. Task M — Hybrid H1/H2 implementation

Status: `COMPLETED`

- Implemented strict no-secret Runtime Config, one REST/SSE/Health URL layer, origin-scoped `sessionStorage` access, exact API Major/Engine/Build handshake, explicit Runtime UI states and fail-closed stale/hard-failure behavior.
- Implemented strict backend Runtime config, exact CORS/OPTIONS, metadata-only `/api/runtime/health`, SSE CORS and scheduler/provider/database health metadata without upstream Provider calls.
- Remote HTTP origins are rejected; loopback HTTP remains available for local recovery and dual-origin acceptance. Private fetch security options are pinned after caller options are merged.
- Runtime Health recomputes quote freshness at request time and normalizes naive source timestamps by market timezone, preventing old A/HK/US local timestamps from being treated as fresh UTC data.
- Added temporary-SQLite real-browser acceptance for cross-origin REST, Portfolio CRUD, fetch-stream SSE, Origin token cleanup, invalid config/health, API/Build mismatch and STALE decision blocking.

## 20. Task N — Hybrid H1/H2 adversarial review

Status: `COMPLETED`

- Review report: `docs/STAGE1.5-HYBRID-H1-H2-INDEPENDENT-REVIEW.md`.
- Resolved all blocking findings: remote HTTP, overridable secure fetch options, soft Build mismatch, weak Health validation, stale stored LIVE state, market-local time misinterpretation, orphan/legacy tokens, unbounded health probes, SSE auth hot retry, malformed Host same-origin handling and missing Provider degradation.
- Final engineering verdict is `ENGINEERING_READY_FOR_MERGE`; real Tailscale, two Tailnet devices, H3 recovery and H4 Pages deployment remain explicit operational gates.
- Latest verified gates before Git staging: H1/H2 Python 14/14; browser main 28/28 plus 11/11 negative scenarios; runtime unittest 394/1; Quant 560 + 244 subtests; Mock Today 17/17; real Today 17/17; Portfolio 13/13; production database SHA unchanged.
- Full-repository Ruff still reports pre-existing unrelated lint debt; only H1/H2 new/related surfaces are claimed as passing targeted Ruff. `ruff format --check` is not claimed.

## 21. Task O — Hybrid H1/H2 GitHub delivery

Status: `COMPLETED`

- H1/H2 implementation/review commit `51d9d907d9c2773eead1dfcd48ff58c2a9474d8e` (`feat: implement hybrid H1 H2 runtime boundary`) was pushed to `origin/main`.
- The staged Git index independently passed runtime unittest 394/1, H1/H2 browser 28/28 + 11/11 negative scenarios, Mock Today 17/17, real Today 17/17, Portfolio 13/13, targeted Ruff and compileall. Its Quant export passed 559 with one expected no-`.git` source-distribution skip; the same source-distribution gate passed 2 tests and 45 subtests in the real checkout, where the complete Quant run passed 560 tests and 244 subtests.
- After the implementation push, local `HEAD`, local `origin/main`, and remote `refs/heads/main` were verified equal at `51d9d907d9c2773eead1dfcd48ff58c2a9474d8e` before this handoff-only completion commit.
- The tracked Python cache modification remained outside all H1/H2 staged files and commits.
- Real Tailscale Serve/two-device acceptance and H3/H4 operational deployment remain pending and are not represented as completed.

## 22. Task P — HiThink Financial-API gated integration

Status: `IMPLEMENTED / LIVE_KEY_ACCEPTANCE_PENDING`

- Evaluated the official `HiThink-Tech/Financial-API` repository and integrated only the documented A-share historical daily REST boundary; the upstream SDK/CLI/DuckDB project was not vendored and no runtime dependency was added.
- Added `HithinkFinanceProvider` as a default-disabled, read-only `T1_BEST_EFFORT` exact-raw capture adapter. It uses the fixed official HTTPS origin, verified system TLS, no inherited proxy, no redirects, bounded responses, strict JSON/envelope/OHLC checks, and a process-environment credential that is never written to TOML, Git, logs, artifacts, or CLI output.
- Added `scripts/capture_hithink_bars.py`. The explicit CLI temporarily activates the disabled adapter only for its own process, writes exact response bytes plus a content-addressed descriptor, and never modifies production SQLite or joins the Runtime Quote/Snapshot/BAR Router.
- Runtime routing gained a backward-compatible `supports_quotes()` capability; free-stockdb and HiThink both refuse quote routing, while HiThink also refuses runtime BAR routing and exposes only explicit raw-bar capture.
- Updated `docs/HITHINK-FINANCE-INTEGRATION.md`, the A-share authoritative-source audit, AGENTS boundaries, HANDOFF, overview, product gap matrix, and source-distribution tracking gates.
- Full regression discovered and repaired four pre-existing H3 blockers: remote same-origin proxy requests are no longer misclassified as loopback, no-Origin remote-host requests remain remote, and Serve recovery test fixtures now match the two preflight boundaries and partial-mutation semantics.
- Final verified gates: HiThink/provider/CLI/router/config tests 37/37; H3 20/20; complete runtime unittest 436 passed with one expected skip; runtime pytest excluding the duplicate H3 module 416 passed with one skip; H0/H1-H2/H4 browser-style acceptance passed; full staged Quant/source-distribution 563 passed plus 248 subtests; targeted Ruff, compileall, `pip check`, and cached-diff checks passed.
- No live HiThink request was made because the user credential was not supplied to the session. The exact environment variable and a bounded one-month acceptance command are documented; the key must not be pasted into chat or committed.
- Implementation/review commit `2a736f09434acf4cd0e5f44d5b7bae85fbc062f0` (`feat: add gated HiThink financial API capture`) was pushed to `origin/main`; local `HEAD`, local `origin/main`, and remote `refs/heads/main` were verified equal before this handoff-only completion commit.

## 23. Task Q — Stage 3D–5C XTP, event store and Monitor delivery

Status: `COMPLETED / LIVE_XTP_ACCEPTANCE_PENDING`

- Recorded the user's stock-type and algorithm-type XTP test-account categories without storing identifiers or secrets. The engineering implementation uses neither account; algorithm access, Trader/Order/Algo APIs, account/position sync and automatic trading remain absent.
- Added a CPython 3.9-compatible read-only XTP Quote Sidecar contract, loopback HTTP/JSON IPC, Simulator, strict event/cursor/session/metric validation, a fail-closed official Quote-module/environment probe and a CPython 3.14 main-process client. Real XTP login/subscription remains an operational gate.
- Added a separate append-only Market Event Store with immutable canonical records, partition SHA-256 chain/manifest, duplicate/gap/out-of-order findings, metadata-only quarantine, atomic minute aggregation, targeted/full integrity verification and Python/optional DuckDB replay. It never opens production `data/stock_tracker.db`.
- Added a non-eval Signal Monitor Engine, bounded scopes, cooldown/duplicate suppression, transactional Inbox/Outbox lifecycle, Browser SSE, optional allowlisted HTTPS webhook and private `/api/monitor/*` REST endpoints.
- Added the Monitor Workspace with Signal Inbox, Rule Center, Data Link and Replay, CSP-safe native SVG, strict Runtime query construction, HTML escaping, Auth/Offline states and no external asset dependency.
- Added deterministic Stage 5C synthetic Shadow for 64 A-share symbols, four boards, 16 scenarios and 256 comparisons. Conflicts and unavailable/frequency-mismatched sources remain explicit; no source is promoted.
- Adversarial Review repaired coordinated raw/derived storage, nested Payload mutation and identity drift, A-share trading-day semantics, Sidecar exact URL/metadata/session snapshots, production-SQLite path isolation, concurrent first-trigger dedup, immutable Rule snapshots, missing-fact `NE` false positives, synchronous EventBus isolation through a bounded runtime-event worker, Outbox leases and the independent Notification Worker, bounded SSE backpressure, per-poll full integrity scans, SQLite parameter scaling, Replay GET side effects/time windows, query-field bounds, static UTF-8 and Monitor fact/UI evidence defects.
- Latest verified pre-staging gates: targeted Stage tests `83 passed, 1 skipped + 19 subtests`; runtime `512 passed, 1 skipped + 316 subtests`; Quant `563 passed + 248 subtests`; Monitor browser `49/49`; H0 `12/12`; H1/H2 `28/28 + 11/11`; H4 `18/18`; Mock/real Today `17/17`; Portfolio `13/13`; source distribution/no tracked bytecode `3 passed + 49 subtests`; targeted Ruff, six-file JS syntax, six-file CPython 3.9 grammar parse, compileall, pip check, Quant smoke/benchmark, secret scan and production migration dry-run passed. Production DB SHA remained `1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1`.
- Full-repository Ruff is not claimed; only the complete Stage 3D–5C changed/new Python surface is claimed as passing targeted Ruff. `ruff format --check` is not claimed.
- `LIVE_XTP_ACCEPTANCE`, real CPython 3.9 + official binary execution, Login/Subscribe, Level 1/2 proof, sustained throughput, live 50–100-symbol Shadow and data storage/training/redistribution rights remain `PENDING`.

## 24. Task R — Stage 3D–5C GitHub delivery

Status: `COMPLETED`

- Implementation/review commit `4a1accfffd8c5e8b9aaffdb2fd4d872f78cbe39b` (`feat: add XTP market event monitoring lane`) was pushed to `origin/main`.
- The exact Git index tree `7b519054bd72cb9870fdb8f16d4274213159dc6a` independently passed targeted Stage tests `83 passed, 1 skipped + 19 subtests`, runtime `512 passed, 1 skipped + 316 subtests`, Monitor Workspace `49/49`, Hybrid H0 `12/12`, H1/H2 `28/28 + 11/11`, H4 `18/18`, Mock Today `17/17`, real Today `17/17`, Portfolio CRUD `13/13`, targeted Ruff, JavaScript syntax, CPython 3.9 grammar parse and `git diff --cached --check`.
- Its source-distribution export passed Quant `561 passed, 2 skipped + 199 subtests`; both skips were expected because the archive has no `.git`. The same source-distribution/no-tracked-bytecode gates passed `3 tests + 49 subtests` in the real checkout, where full Quant passed `563 tests + 248 subtests`.
- Git-index secret review covered 562 tracked files and found no tracked database, bytecode, archive, `.env`, private-key file or XTP credential assignment. The only private-key marker strings were the expected scanner implementation and its negative fixture.
- Production migration remained dry-run only and the production database SHA-256 remained `1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1`.
- After the implementation push, local `HEAD`, local `origin/main`, and GitHub `refs/heads/main` were verified equal at `4a1accfffd8c5e8b9aaffdb2fd4d872f78cbe39b` before this handoff-only completion commit.
- Real CPython 3.9 plus the official XTP binary, Quote Login/Subscribe, Level 1/2 permission proof, sustained throughput, 50–100-symbol Live Shadow and data storage/training/redistribution rights remain explicit operational gates. The algorithm account remains unused and no Trader/Order/Algo or automatic-trading surface was shipped.

## 25. Task S — Stage 2G Golden Raw and market-bar reconciliation

Status: `ENGINEERING_COMPLETE / REAL_SOURCE_RECONCILIATION_PENDING`

- Added an exact-raw research HTTP boundary with system CA/hostname verification, no inherited proxy, no redirect, no Host override, exact final URL, strict content type, declared/actual length and bounded-body checks. The public channel rejects authority/credential headers, duplicate case-insensitive header names and header injection. The legacy Runtime Quote request path was not relabelled as secure.
- Split Tencent QFQ raw fetch/strict parse and prohibited fallback from `qfqday` to unadjusted `day`; hardened Eastmoney/Tencent strict JSON/OHLC/date parsing, strict chronological uniqueness and canonical Symbol/Market identity before network or parsing.
- Added in-memory `CapturedBarArtifact` identity revalidation, immutable `MarketBarPoint`, A/HK/US synthetic Golden Pack v2（v1 identity preserved）, exact Parser/Schema binding, cross-source field comparison, Calendar Session coverage, content-addressed reports and fail-closed Trust/License/T3 blockers.
- Extended `capture_quant_bars.py` to explicit Eastmoney or Tencent exact-raw capture while retaining the `BEST_EFFORT` ceiling; added offline `report_stage2g_market_bars.py` for committed synthetic cases.
- Current checkout gates: focused Stage/Provider `93 passed + 54 subtests`, Runtime `520 passed, 1 skipped`, Quant `604 passed + 290 subtests`, source-distribution/no-bytecode `3 passed + 70 subtests`; targeted Ruff, compileall, pip check, Quant smoke/benchmark and SQLite-backup migration dry-run passed.
- The CLI passed a subprocess sandbox where both `sqlite3.connect` and `sqlite3.dbapi2.connect` were forced to fail, proving the three-market report path has no SQLite dependency.
- The committed fixtures are synthetic vendor-shaped envelopes. Every case remains `STRUCTURALLY_CONSTRUCTIBLE` only, with 0 hard blocks and 11 Trust findings; `LICENSE_PENDING / T3_NOT_REACHED` plus source/calendar/unit/adjustment/policy blockers remain open.
- Two existing Engines on ports 8080/8090 predated this task and continuously write the production DB/WAL. Stage 2G has no production write path, but global task-window hash stability is not provable; a read-only SQLite backup snapshot retained SHA `3de90a42057cca61479278131b53e2359bab83bdf325c210977b5b9ad3dd857f` across migration dry-run with `database_modified=false`.

## 26. Task T — Stage 2G initial GitHub delivery

Status: `COMPLETED`

- Initial implementation/review commit `4a9b04eccf182e4545ab6d70fc3eee9cf8afbf48` (`feat: add Stage 2G market bar reconciliation`) and delivery-handoff commit `2d7d96e52fb18c58c8af4440cfd5ea13f30c157b` are on `origin/main`.
- The initial implementation tree was `f56e9965534dbebe6fbff26a3e41c499ff3f0573`.
- This record covers the initial Stage 2G delivery only; the later Task U post-review hardening still requires a separate final Index review and push.
- Parallel UI work under `web/**`, `qa/shots/live-*.png`, and `qa/ui-fix-report-2026-08-28.md` remains outside the Stage 2G lane.

## 27. Task U — Stage 2G post-review hardening

Status: `COMPLETED / REAL_SOURCE_RECONCILIATION_PENDING`

- Added UTF-8-BOM-aware body HTML rejection, canonical URL and authority/credential Header refusal, duplicate header-name rejection, Eastmoney duplicate/chronology validation, Symbol/Market identity validation, pinned Golden Pack identities, future-artifact exclusion, capture-local non-final Daily Session exclusion, symlink/junction-safe report writes, and exact raw-bytes/parser revalidation with detached canonical Bar copies.
- Preserved the published v1 Golden Pack and its legacy Eastmoney parser identity; added default v2 bound to `eastmoney-bars-v3-strict-research`.
- Exact Git Index export passed focused Stage/Provider `93 + 54 subtests`, Runtime `520/1`, Quant `602/2 + 220 subtests`, H0 `12/12`, H1/H2 `28/28 + 11/11`, H4 `18/18`, Monitor `49/49`, Mock/real Today `17/17`, Portfolio `13/13`, targeted Ruff, compileall and cached-diff.
- Index boundary contained 28 Stage 2G hardening files and no `web/**`, generated artifact, database, bytecode, archive or credential finding.
- Hardening commit `57b06e1ac230e6b7b770ffc876f40b07942979b2` (`fix: harden Stage 2G evidence boundaries`) has verified tree `fb09a33987b1743ed540bb94a7973d189c724cc9` and was pushed to `origin/main`.
- After push, local `HEAD`, local `origin/main`, and GitHub `refs/heads/main` were verified equal at `57b06e1ac230e6b7b770ffc876f40b07942979b2`. Real-source reconciliation, licence clearance, authoritative auxiliary-data binding and T3 remain pending.
