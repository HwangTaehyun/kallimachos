# 3D Graph Plugin — Risk, Sequencing & Verification Plan

> **Inherited, unedited, from upstream.**  This is one of [galaxy-view](https://github.com/Longwind1984/galaxy-view)'s pre-implementation design
> documents by Rick, written for that project on that author's machine and published by them
> at `docs/design/` in that repository.  It describes the plugin **before** it existed and
> before this fork —— the absolute paths in it (`/Users/rick/…`) were build instructions there,
> not prose, and the vault figures are that author's, not this project's.  It is kept as
> written rather than rewritten: the reasoning is the value, and diverging from upstream's own
> published copy would gain nothing.  For what this fork actually ships, read
> [`../../README.md`](../../README.md) and [`../../WORKLOG.md`](../../WORKLOG.md).

(Perspective: the skeptic who watched 4 predecessor plugins die on the same wall)

## 0. New measured facts (from Rick's actual vault, 2026-06-12)

I scanned the live vault before planning. These numbers override the assumptions in the brief:

| Metric | Value |
|---|---|
| Markdown files (nodes) | 3,225 |
| Raw `[[wikilink]]` occurrences | 40,676 |
| Unique (src → dst) pairs after dedupe | 27,811 |
| **Resolved unique edges** (basename match) | **~19,482** |
| Unresolved unique edges | ~8,313 → **6,548 phantom targets** |
| Orphan files (zero resolved links) | 523 |
| Top hub | the concept dictionary, degree 689 (then 574, 500, 399, 390) |
| Degree concentration | top 500 nodes = 66% of degree mass, top 1,000 = 85%, top 1,500 = 94% |

Three consequences:

1. **The "3.2k node" framing undersells the problem.** Rick's `graph.json` has `hideUnresolved: false` and `showOrphans: true` — the core 2D graph he sees today renders **~9,773 nodes (3,225 files + 6,548 unresolved) and ~27,800 edges**. If the 3D view naively mirrors his config, we triple the node budget on day one. Decision: V1 defaults to **resolved-links-only** (3,225 n / ~19.5k e); "show unresolved" ships as a perf-gated toggle only if M0 numbers allow.
2. **Edges, not nodes, are the first wall.** 3d-force-graph's default pipeline creates ~1 Object3D per node AND per link → ~22.7k draw calls at resolved-only scale, ~37k with unresolved. Predecessor plugins died below this. The mitigation (single `LineSegments` geometry for all links = 1 draw call) is known and also *is* the NASA "thin desaturated trails" look — performance and aesthetics converge here.
3. **Mobile node cap is cheap.** Capping at 1,000–1,500 highest-degree nodes preserves 85–94% of link mass — the graph will still *look like* Rick's brain on an iPhone.

Also verified: **no installed plugin bundles three.js or any WebGLRenderer** (grepped all 29 `main.js` bundles); `hot-reload` is not yet installed; colorGroups queries contain trailing spaces (`"path:01学习  "`) and colors as decimal ints (`14048348`) — the graph.json parser must trim and convert defensively.

---

## 1. Ranked kill-risks

### R1 — Render performance wall: per-object pipeline + bloom (HIGH, the proven killer)
**What kills you:** 3d-force-graph defaults = 3,225 sphere Meshes + 19,482 Line objects ≈ 22.7k draw calls/frame, then UnrealBloomPass adds 2–4 full-screen passes on top. This is exactly where "3D Graph New" and the other wrappers died. Bloom itself is cheap-ish (fixed full-screen cost); the object count is the killer, and hub nodes (degree 689) create locally dense line bundles.
**Mitigation (staged, decided by M0 data):**
- Stage 1: stock pipeline, measure honestly.
- Stage 2: replace links with one `LineSegments` buffer geometry (positions updated from sim each frame) — eliminates ~19.5k draw calls; nodes stay as 3d-force-graph objects for picking.
- Stage 3 (if needed): nodes as `InstancedMesh` (one draw call, per-instance color) + GPU picking or raycast against instances; 3d-force-graph retained only as layout/interaction driver behind the `RenderPipeline` interface.
- Bloom strength is a user slider (NASA recipe) and the degraded-tier off-switch.
**Early-warning test:** M0 scenario S1 (below) with hard pass/fail numbers. Do not write a single line of product UI before this number exists.

### R2 — Main-thread d3 sim jank: the "spinning fan" first 10 seconds (HIGH)
**What kills you:** d3-force-3d cold-start on 3,225 nodes / 19.5k links runs Barnes–Hut + link forces for ~300 ticks. On the main thread this freezes **all of Obsidian** (editor included) for seconds — instant uninstall material. Also: `metadataCache.on('resolved')` fires on every file save; naive full-rebuild + sim restart means jank on every keystroke-save.
**Mitigation:**
- Web Worker running d3-force-3d (inlined Blob URL — confirmed working in Obsidian plugins; d3-force-3d also runs in Node, so the worker protocol is unit-testable). Positions posted as transferable `Float32Array`.
- **Warm start:** persist settled positions in plugin data; seed next open from cache → near-zero cold start for an unchanged vault.
- Progressive reveal: starfield + fade-in while the sim settles off-thread (turns the wait into theater).
- Debounced (≥2s) diff-based graph updates; local alpha reheat instead of full restart; suspend updates while the view is hidden.
**Early-warning test:** M0 scenario S2 — `PerformanceObserver` longtask log during initial layout. Any main-thread block >1s or settle >8s ⇒ Worker becomes M3 item #1 (expected outcome; the architecture assumes it).

### R3 — Memory leaks on repeated view open/close (HIGH likelihood, predecessors' top bug)
**What kills you:** WebGL contexts and GPU buffers are not garbage-collected with the DOM. Reopening the view 8–16 times exhausts Chromium's WebGL context pool ("WARNING: Too many active WebGL contexts") and the graph silently dies; heap grows by tens of MB per cycle.
**Mitigation — a written disposal contract in `Graph3DView.onClose()`:** `ForceGraph3D._destructor()`; traverse scene disposing geometries/materials/textures; composer pass disposal; `renderer.dispose()` + `renderer.forceContextLoss()`; `cancelAnimationFrame`; `worker.terminate()`; `ResizeObserver.disconnect()`; DOM overlay removal; all Obsidian events via `registerEvent()` (auto-cleanup). Handle `leaf.isDeferred` (minAppVersion ≥1.7.2): never instantiate WebGL until the view is revealed. Pause the RAF loop when the leaf is hidden.
**Early-warning test:** debug command "Leak check: cycle view ×10" — reports `performance.memory` delta and live-renderer count. Pass: heap delta <20MB post-GC, zero context warnings. Run at **every** milestone, not just at the end.

### R4 — Mobile iOS: WKWebView memory + GPU limits (MEDIUM-HIGH)
**What kills you:** iOS jetsam kills the whole Obsidian app, not just the view. Bloom's HDR render-target chain at iPhone devicePixelRatio 3 alone can eat 100MB+ of GPU memory; full vault geometry on top risks `webglcontextlost` or app death.
**Mitigation (degraded tier behind `Platform.isMobile`):** pixelRatio cap ≤1.5; bloom off (or quarter-res); node cap 1,000–1,500 by degree (justified by measured 85–94% mass retention) with a "showing top N of 3,225" notice; sprite-only nodes; antialias off; mandatory `webglcontextlost`/`restored` handlers with a graceful reload card.
**Early-warning test:** smoke test on Rick's actual iPhone right after M2 (plugin folder syncs via iCloud — no extra install path needed), not waiting for M4. Pass floor: opens, orbits at ≥25fps, survives 5 open/close cycles, no app kill.
**Rollback:** ship first community release `isDesktopOnly: true`; mobile becomes a point release. This is an explicit, acceptable pivot.

### R5 — Community review requirements that bite late (MEDIUM)
**What kills you:** weeks of rework at submission time: `innerHTML`/`outerHTML` bans (preview cards! the most tempting place to cheat), styles injected from JS instead of `styles.css`, missing unload cleanup, plugin id containing "obsidian", private-API usage flags.
**Mitigation:** `eslint-plugin obsidianmd` in CI from the first commit; preview cards built with `createEl()` only; reading `.obsidian/graph.json` via `vault.adapter.read(normalizePath(...))` wrapped in try/catch with graceful fallback (proven pattern from sync-graph-settings — note it in the PR description for reviewers); zero network calls/telemetry (nothing to remove later); release artifacts main.js + manifest.json + styles.css via GitHub Action from day 1.
**Early-warning test:** CI fails on lint; M5 includes a line-by-line self-audit against the official plugin review checklist.

### R6 — iCloud dev-loop churn (MEDIUM annoyance, LOW project risk)
**What hurts:** esbuild watch rewriting `main.js` into `~/Library/Mobile Documents/...` every save → fileproviderd churn, sync latency, possible conflicted copies pushed to the iPhone, and a buggy dev build freezing the vault Rick actually works in.
**Mitigation (primary, verified-safe):** create a **local clone dev vault** outside iCloud — one-time rsync of `*.md` only (no attachments); identical link structure ⇒ identical graph and perf numbers. esbuild outputs there + `.hotreload` marker (install pjeby/hot-reload in the clone only). Real iCloud vault is touched only at milestone verification and for mobile testing.
**Alternative (UNVERIFIED, research-level):** symlink `plugins/<id>` in the real vault to a local build dir — iCloud syncs the symlink not the content, Obsidian follows it locally. Test before relying on it; the `.nosync` rename trick is **not** viable because the plugin folder must be named exactly the plugin id.

### R7 — three.js version conflicts with other plugins (LOW — verified, close it)
Each Obsidian plugin is an independently bundled CommonJS module evaluated in its own scope; bundled three.js does not attach `window` globals, so two plugins with different three versions cannot collide at the module level. I additionally verified **no installed plugin bundles three.js or creates a WebGLRenderer**. The only genuinely shared resource is Chromium's WebGL context pool (~8–16 per renderer process) — which loops back to R3: hold exactly one context, release it on close. No further action needed.

### R8 — "Stunning" is subjective and Rick is the judge (MEDIUM, schedule risk)
**What hurts:** endless aesthetic churn with no convergence criterion.
**Mitigation:** the NASA recipe is the spec ("premium feel = restraint" — no fog, no grain, no gimmick layouts); two concrete presets at gate G2 force a decision by *looking*, not describing; every milestone ends with a 2-minute demo checkpoint so taste corrections happen early and small.

---

## 2. Milestones with decision gates

Repo: new project at `/Users/rick/Claude_Code/<Name>/` (working name below: **Nebula_Graph**, plugin id `nebula-graph` — final name is Rick's pick at M1; must not contain "obsidian"). Git from day 1; `README.zh.md` + `WORKLOG.md` in Chinese per protocol.

### M0 — Perf spike on real data (2–3 days) → GATE G0
**Deliverable:** throwaway-quality but real-data spike: ItemView hosting stock `3d-force-graph`, graph built from `app.metadataCache.resolvedLinks`, Stats.js overlay, bloom toggle, a "run benchmark" command. Numbers written into `WORKLOG.md` (Chinese).
**Benchmark scenarios (fixed, repeatable — deterministic initial positions seeded from a hash of node path):**
- **S1:** resolved-only (3,225 n / ~19.5k e), bloom ON, layout settled, 20s scripted orbit → avg + p95 FPS.
- **S2:** cold-start layout: time to alpha<0.01 + longest main-thread block (longtask observer).
- **S3:** unresolved shown (~9.8k n / ~27.8k e) — informational, decides the unresolved toggle's fate.
- **S4:** open/close ×10 leak canary.
**GATE G0 — pass/fail and what each outcome means:**
- S1 ≥45fps → green: stock pipeline survives through M2; batched links deferred to M3.
- S1 30–45fps → yellow: proceed, but `LineSegments` batched links are committed M3 scope.
- S1 <30fps → red: build batched links + sprite nodes **immediately in M1**; 3d-force-graph demoted to layout/interaction shell behind the interfaces. (This is Plan B, pre-decided, not a crisis.)
- S2 block >1s → Worker confirmed as M3 first item (assume yes).
- S3 <20fps → unresolved-nodes toggle is cut from V1 desktop (documented deviation from core graph parity).
**Rollback:** if even Plan B projections look hopeless (they won't — 1-draw-call links + instanced nodes at 3.2k is comfortably within WebGL budget), the fallback stack is regl/custom points pipeline; the spike costs 3 days, not 3 months.

### M1 — Walking skeleton + first "wow" (≈1 week) → GATE G1 (morale demo)
**Deliverable:** proper scaffold from obsidian-sample-plugin (esbuild 0.25.x, TS 5.8, eslint obsidianmd, vitest); ribbon icon + command opening a center-pane view; correct lifecycle (deferred leaf, full disposal contract from R3); **T1 visuals**: #000003 bg + `THREE.Points` starfield, bloom slider, click → eased `cameraPosition()` fly-to, idle auto-orbit (pause on interact, resume ~10s). The interfaces `LayoutEngine` and `RenderPipeline` exist from this milestone (justified pre-abstraction: the swap is the known death-wall, not speculation).
**Verification:** manual checklist — open via ribbon; orbit; click node flies camera; idle 10s starts cruise; interact pauses it; close view; leak canary ×10 passes; `npm run lint` + `vitest` green in CI.
**GATE G1:** Rick looks at it in (a copy of) his vault: "does this already feel like NASA Eyes?" If no — fix T1 before adding features. Morale is a managed resource.
**Rollback:** none needed; this is foundation.

### M2 — Visual direction previews + the 4 playability features (1–1.5 weeks) → GATE G2 (THE design gate)
**Deliverable:** both visual directions as runtime-switchable presets, each fully wearing the must-have features:
- **Preset A "Deep Space":** always-dark cinematic, ignores Obsidian theme (#000003 everywhere, bloom-forward).
- **Preset B "Theme-Adaptive":** dark theme = cinematic; light theme = clean bright mode driven by `--graph-*` CSS vars, re-themed live on `css-change`.
- Features in both: folder color groups parsed from `graph.json` (trim trailing spaces, decimal-int rgb → hex; fallback palette if parse fails); search box + fly-to; DOM-overlay hover labels + selection preview card (createEl only, themed via Obsidian CSS vars); idle cruise polish.
**Verification:** manual checklist per preset × {Minimal-dark, Minimal-light}: 9 color groups visibly match 2D graph colors; searching the top hub's name flies to it; hover shows label <100ms; selection card shows title/path/link-count; theme switch live-retints Preset B without reload. Leak canary. Unit tests for graph.json mapping green.
**GATE G2:** Rick runs both presets in his vault and **picks the direction by looking** (or keeps both as a setting if both delight — acceptable, they share 90% of code). Per his protocol: previewable drafts, 2 directions, decision by eyes not text.
**Rollback:** presets are settings profiles, not branches — no code is thrown away either way.

### M3 — Perf hardening (1–2 weeks) ⚠ RISKIEST MILESTONE → GATE G3
**Deliverable:** Worker layout (Blob-URL d3-force-3d, transferable position arrays, warm-start position cache); batched `LineSegments` links (+ instanced nodes if G0 was yellow/red); LOD (far = sprite dots, label fade by distance); debounced diff-based updates on `metadataCache` 'resolved'/'changed'/'deleted' + `vault` 'rename'/'delete' (rename is NOT covered by 'changed' — explicit handler); selection halo (additive sprite) + transient link highlight.
**Verification:** re-run the full M0 benchmark suite, publish before/after table in WORKLOG. **Pass:** S1 ≥45fps sustained; zero main-thread blocks >200ms during cold layout; editing a note while the graph is open causes no visible editor jank; rename/delete reflected in graph <3s; warm reopen shows settled graph <1.5s.
**GATE G3:** numbers decide mobile go/no-go and whether the unresolved-nodes toggle ships on desktop.
**Rollback:** each optimization lands behind its interface independently — if instancing fights 3d-force-graph picking, ship batched-links-only (the bigger win) and keep mesh nodes.

### M4 — Mobile degraded tier (≈1 week) → GATE G4
**Deliverable:** `Platform.isMobile` tier per R4 (pixelRatio cap, no/low bloom, 1,000–1,500 node cap by degree with notice, sprite nodes, context-lost handler); `isDesktopOnly: false`; touch controls verified (orbit/pinch/tap-select).
**Verification (on Rick's actual iPhone, vault via iCloud):** opens without crash; ≥25–30fps orbit; tap selects + card readable; 5× open/close survives; Obsidian remains responsive after backgrounding/foregrounding.
**GATE G4 / Rollback:** if the floor isn't met in a week of tuning, flip `isDesktopOnly: true` and ship desktop-first; mobile moves to the V2 parking lot with the measurements documented. Pre-agreed, not a failure.

### M5 — Release prep + beta soak (≈1 week)
**Deliverable:** settings polish (Chinese + English strings); README.md (English, store-facing) + README.zh.md + screenshots/GIF; GitHub Action releasing main.js/manifest.json/styles.css; BRAT beta; line-by-line self-audit vs official review checklist; ≥1 week of Rick using it daily on the real iCloud vault as soak test.
**Verification:** CI green (build/lint/vitest); BRAT install from a clean test vault succeeds; soak week with zero crashes/leak warnings; then community store submission.
**Rollback:** BRAT *is* the rollback — distribution works indefinitely while store review iterates.

**Total: ~6–8 calendar weeks at sustainable pace. Something visually impressive exists at end of week ~1.5 (M1), per the morale requirement.**

---

## 3. Test strategy (honestly scoped)

**Unit-testable (vitest, node, no Obsidian runtime — keep these modules pure, taking plain records as input so the `obsidian` import never leaks in):**
- `GraphModel` builder: `resolvedLinks`-shaped record → nodes/edges arrays; dedupe; orphan/unresolved/attachment toggles; degree computation; top-N cap selection (fixture: a 50-node synthetic vault + a frozen snapshot of real-vault stats).
- `graph.json` mapping: decimal rgb → hex; `path:`/`tag:` query parsing **including trailing-space trimming** (real config has `"path:01学习  "`); missing/corrupt file → default palette (the format is undocumented — defensive tests are the spec).
- Worker layout protocol: d3-force-3d runs in Node — test tick/position message round-trip and warm-start seeding deterministically.
- Settings serialization, color precedence (group vs selection vs hover).

**Manually verifiable only (visuals) — fixed checklists per milestone, written in the repo (`docs/checklists/`):** per-milestone steps as specified above; always run in both Minimal-dark and Minimal-light; always end with the leak canary.

**Perf harness (in-plugin, repeatable):** debug command "Benchmark: camera sweep" — deterministic seeded layout, 20s scripted orbital path, RAF-delta frame times → avg/p95/worst + longtask list dumped to console as JSON; Stats.js overlay behind a dev flag. Same command on desktop and mobile. Results table maintained in WORKLOG.md so regressions are visible commit-to-commit.

**Explicitly not automated (and that's fine):** screenshot-diff testing of WebGL output (flaky, low value at this team size), e2e driving of Obsidian itself.

---

## 4. YAGNI — not in V1

- Local graph view (V2 headline, per Rick's decision)
- Time scrubber / vault-growth replay and link particles (T3 — parking lot; only if M3 lands early and green)
- Tag nodes and attachment nodes (Rick's own config has both off)
- Shaped layouts (brain/galaxy), fog, vignette, film grain (NASA recipe explicitly skips)
- WebGPU path (unsafe baseline), VR/AR
- Full core-search query parity for filters (only `path:`/`tag:` color-group queries + simple name search)
- Writing back to `graph.json` (read-only inherit, best-effort)
- Custom per-node shader materials beyond the selection halo sprite
- Multi-window/popout support; i18n framework beyond a zh/en strings table

**V2 parking lot:** local graph mode; time scrubber; link particles; unresolved-nodes on mobile; tag/attachment nodes; saved camera bookmarks; deep-link `obsidian://` fly-to a note.

## 5. Riskiest milestone

**M3 (perf hardening).** It is where every predecessor died, and it is the only milestone doing genuinely novel integration: driving 3d-force-graph's scene from an off-thread sim while replacing its per-object rendering with batched geometry — fighting the library's assumptions rather than using its happy path. M0 exists precisely to buy this milestone's information early (worst case is discovered in week 1, not week 5), the M1 interfaces exist to contain its blast radius, and its scope items are independently land-able so a partial win (worker + batched links, no instancing) still clears the bar. M4 (mobile) is the second-riskiest but has a pre-agreed, honorable exit (`isDesktopOnly` first release); M3 does not — if M3 fails, the product is another abandoned wrapper.

---

### Critical Files for Implementation
- /Users/rick/Claude_Code/Nebula_Graph/src/graph/GraphModel.ts — pure graph builder (metadataCache record → nodes/edges, dedupe, caps, toggles); the most unit-tested file
- /Users/rick/Claude_Code/Nebula_Graph/src/engine/LayoutEngine.ts — layout interface + d3-force-3d main-thread and Worker(Blob) implementations; the R2 seam
- /Users/rick/Claude_Code/Nebula_Graph/src/engine/RenderPipeline.ts — 3d-force-graph wrapper vs batched LineSegments/InstancedMesh implementations; the R1 seam
- /Users/rick/Claude_Code/Nebula_Graph/src/view/Graph3DView.ts — ItemView lifecycle, deferred-leaf handling, the R3 disposal contract
- /Users/rick/Library/Mobile Documents/iCloud~md~obsidian/Documents/Rick's Second Brain/.obsidian/graph.json — source config to inherit (trailing-space queries, decimal-int colors verified)

## KEY DECISIONS
- V1 defaults to resolved-links-only (3,225 nodes / ~19.5k edges); 'show unresolved' becomes a perf-gated toggle instead of mirroring graph.json parity — Measured the live vault: Rick's current graph.json (hideUnresolved:false) makes the core graph render ~9,773 nodes and ~27.8k edges — 3x the advertised node budget. Predecessor plugins died at smaller scales; naive parity would recreate their death on day one.
- M0 is a 2-3 day perf spike on real vault data with numeric pass/fail gates (45/30 fps thresholds, 1s longtask threshold) before any product code — The single question that killed all four predecessors — does the per-object 3d-force-graph pipeline survive ~22.7k draw calls plus bloom — is answerable in days. Every architectural fork (batched links, instancing, worker) is pre-decided by these numbers, so a bad result is a planned branch, not a crisis.
- LayoutEngine and RenderPipeline interfaces exist from M1, with Web Worker layout and batched LineSegments/InstancedMesh rendering as first-class M3 scope — Justified exception to the no-premature-abstraction rule: the swap-out is not speculative — it is the documented death-wall of every predecessor ('performance issue that I don't know how to fix'). The second use case is already known. Batched links double as the NASA thin-trails aesthetic, so perf and beauty converge.
- Develop against a local clone vault (rsync of .md files only, outside iCloud); the real iCloud vault is used only at milestone verification and mobile testing — esbuild watch writing into ~/Library/Mobile Documents causes fileproviderd sync churn, conflict risk pushed to the iPhone, and exposes Rick's working vault to dev-build freezes. A clone with identical link structure yields identical graph and perf numbers. (Symlinking the plugin folder is a possible alternative but unverified; the .nosync rename trick is non-viable since the plugin folder must be named exactly the plugin id.)
- Visual direction is decided at gate G2 by Rick running two switchable presets (Deep Space always-dark vs Theme-Adaptive dark/light) in his own vault — Rick's protocol requires previewable drafts with 2 directions for user-visible design, decided by looking at running software. Presets are settings profiles sharing ~90% code, so the unchosen direction costs nothing and can even ship as an option.
- Mobile ships as a degraded tier (pixelRatio ≤1.5, no/low bloom, sprite nodes, 1,000–1,500 node cap by degree) with a pre-agreed rollback to isDesktopOnly for the first release — Measured degree distribution shows the top 1,000–1,500 nodes carry 85–94% of link mass, so the cap preserves the visual identity of the graph. iOS WKWebView memory limits make full-fat bloom + full geometry a jetsam risk; an honorable desktop-first exit prevents mobile from blocking release.
- Community-store compliance is built in from the first commit: obsidianmd eslint in CI, createEl-only DOM (no innerHTML anywhere, especially preview cards), styles.css-only styling, full disposal contract, zero network/telemetry — Review requirements bite late and expensively; predecessors' top bug (leaked WebGL contexts on view close) is also a review concern. A leak canary command run at every milestone converts the predecessors' chronic bug into a continuously verified invariant.

## RISKS
- R1 Render perf wall (HIGH, the proven killer): ~22.7k draw calls from per-object nodes+links before bloom. Mitigation: staged plan decided by M0 numbers — stock → single LineSegments for all links (1 draw call, also the NASA look) → InstancedMesh nodes with 3d-force-graph demoted to layout shell. Early warning: M0 S1 benchmark, hard thresholds 45/30 fps.
- R2 Main-thread sim jank (HIGH): d3-force-3d cold start on 19.5k links freezes all of Obsidian for seconds; metadataCache 'resolved' fires on every save. Mitigation: Blob-URL Web Worker layout with transferable position arrays, warm-start position cache, debounced diff updates, progressive starfield reveal. Early warning: M0 S2 longtask measurement (>1s block ⇒ worker is M3 item #1).
- R3 Memory leaks on view open/close (HIGH likelihood — predecessors' top bug): WebGL context pool exhaustion after ~8–16 reopens. Mitigation: written disposal contract in onClose (destructor, dispose, forceContextLoss, worker.terminate, registerEvent) + deferred-leaf handling; 'leak canary ×10' debug command run at every milestone (pass: <20MB heap delta, no context warnings).
- R4 Mobile iOS memory/context limits (MEDIUM-HIGH): bloom render targets at dpr 3 + full geometry risks jetsam-killing the whole app. Mitigation: degraded tier (dpr cap, bloom off, 1,000–1,500 node cap = 85–94% of measured link mass, contextlost handler); iPhone smoke test pulled forward to right after M2; pre-agreed rollback to isDesktopOnly first release.
- R5 Community review rework biting late (MEDIUM): innerHTML bans (preview cards are the temptation), JS-injected styles, unload hygiene, id naming. Mitigation: obsidianmd eslint in CI from first commit, createEl-only, styles.css-only, graph.json read wrapped in try/catch and documented for reviewers, M5 line-by-line checklist audit.
- R6 iCloud dev-loop churn (MEDIUM annoyance): esbuild watch into Mobile Documents causes sync churn and conflict copies. Mitigation: local clone dev vault (md-only rsync, identical graph), hot-reload only there; symlink alternative flagged as unverified.
- R8 Aesthetic churn with a PM as judge (MEDIUM schedule risk): 'stunning' has no convergence criterion. Mitigation: NASA recipe as the written spec (restraint list: no fog/grain/gimmicks), two-preset decision gate G2 decided by eyes, 2-minute demo checkpoint closing every milestone. (R7 three.js conflicts evaluated and closed: verified no installed plugin bundles three/WebGL; module-scoped bundles cannot collide.)
- Riskiest milestone: M3 perf hardening — the exact wall where all predecessors died and the only genuinely novel integration (off-thread sim driving batched geometry against 3d-force-graph's assumptions); de-risked by M0's early numbers, M1's interface seams, and independently landable scope items so a partial win (worker + batched links only) still clears the 45fps bar.