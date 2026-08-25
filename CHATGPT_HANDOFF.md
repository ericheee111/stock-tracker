# ChatGPT Handoff

> Updated: 2026-08-25
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

Status: `READY_FOR_GIT_DELIVERY`

- Reviewed the remaining dirty worktree after the Signal Ledger push.
- `AGENTS.md` contains a valid roadmap-state correction: Stage 1.1 is completed and Hybrid H1 is the current next engineering slice; it is safe to commit.
- `.gitignore` now excludes current reproducible local artifacts: `/build/`, `/stock-tracker-web*.zip`, and the generated QA screenshot families `01-dark-*`, `02-dark-*`, `_min.png`, `aurora-*`, and `v2-*`; existing tracked golden screenshots are not removed.
- Python bytecode is already covered by `__pycache__/` and `*.pyc`. Five historical `.pyc` files are still tracked by Git; the currently modified `stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc` must not be pushed. CodexPro blocked `git rm --cached` as a high-risk operation, so this session does not bypass the guard or delete local cache files.
- After the ignore update, the only remaining worktree paths are `.gitignore`, `AGENTS.md`, and the legacy tracked `.pyc`; generated build/ZIP/screenshot artifacts are correctly hidden from status.
- Final Git delivery and remote SHA verification remain to be completed below.
