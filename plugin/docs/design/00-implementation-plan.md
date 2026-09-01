# Galaxy View — a cinematic 3D graph plugin for Obsidian · the implementation plan

## Context (background and goal)

The measured size of Rick's vault "Rick's Second Brain": **3,225 notes, about 19,500 deduplicated effective edges** (note: the current 2D graph has "show unresolved links" on, so it really renders about 9,773 nodes / 27,800 edges).  At that size the built-in 2D Graph View is neither impressive nor much use for discovering patterns.

The goal: a new Obsidian plugin, **Galaxy View** (id: `galaxy-view`, Chinese name 星系视图), giving a cinematic 3D graph in the style of NASA's "Eyes on Asteroids" —— for PKM, looks and playability are productivity in themselves.

### Confirmed product decisions (Rick's call, 2026-06-12)
- **V1 does the global graph only**, polished until it impresses; a local graph is V2
- **A community-store listing later** → developed to the official rules from day one
- **Desktop + mobile** (the vault syncs to iOS through iCloud), with a downgraded mobile tier; the exit is agreed in advance: if a week of tuning cannot reach the floor, ship with `isDesktopOnly: true` and let mobile land in a point release
- **All four playability pieces**: search + camera flight, folder/tag colouring, a node preview card, and an idle cruise

## Research summary (5 parallel research agents, verified, 2026-06-12)

- **The competition is entirely dead**: all 4 published 3D plugins are thin wrappers over `3d-force-graph`, all MIT, all unmaintained; the cause of death is the performance wall of per-object rendering ("3D Graph New"'s author, verbatim: "performance issue that I don't know how to fix").  Not one did bloom, cinematic camera work or aggregate rendering.  **No fork; build from scratch**; the MIT competitors serve as references (Apoo711 for integration, Hananoshika for search focus, TagsRoutes for particles).
- **The NASA Eyes recipe (verified by reading its production bundle directly)**: three.js r167 + UnrealBloomPass (strength = a user slider) + a custom starfield shader + **DOM overlay labels** + tweened camera easing + thin desaturated trajectory lines; no fog, no vignette, no grain.  **The premium feel is restraint.**
- **The three Plan agents' design perspectives** (architecture / visual / risk) are complete and stored at `/tmp/galaxy-design/{architecture,visual,risk}.md` —— **the first thing M0 does is copy them into the repository's `docs/design/`** (/tmp is lost on a restart; the backup source is `/private/tmp/claude-501/-Users-rick-Claude-Code-Obsidian-PKM/2cf2fa95-a0a1-4de2-b146-9705849eef55/tasks/wzhwurhzm.output`).

## Technology choices (internal engineering, decided independently)

| Component | Choice | Reason |
|---|---|---|
| Rendering | three@0.184.0, on a WebGL baseline | WebGPU cannot be relied on (the user's Electron is behind) |
| Graph framework | 3d-force-graph@1.80.0 | Actively maintained; postProcessingComposer() / cameraPosition() / degradable to a pure layout shell |
| Physics | d3-force-3d@3.0.6 (ngraph as a backup) | Pure JS and able to run in a Web Worker (inlined through a Blob URL, verified feasible) |
| Labels | A DOM overlay (the NASA pattern), with three-spritetext only as a backup | Sharp CJK, and it can be re-skinned through Obsidian's CSS variables |
| Build | The obsidian-sample-plugin template (esbuild 0.25 / TS 5.8) + eslint-plugin-obsidianmd + vitest | The official standard, so store compliance starts at the first commit |
| Distribution | pjeby/hot-reload (development) → BRAT (beta) → the store portal | The standard path |

The bundle is about 1.3MB minified (< Sync's 5MB limit).  Rejected: cosmos.gl (2D only), Babylon.js (no graph ecosystem), forking an existing plugin (inheriting its performance ceiling).

## Architecture

### The repository and its modules (a new repository at `/Users/rick/Claude_Code/Galaxy_View/`, git init)

```
manifest.json        id: galaxy-view, minAppVersion: 1.7.2 (the floor for the deferred view API), isDesktopOnly: false
styles.css           every overlay style (labels/cards/HUD), using the --graph-* and --background-* CSS variables
src/
  main.ts            the Plugin subclass: registerView + addRibbonIcon + 3 commands + SettingTab, and nothing else
  constants.ts / types.ts    VIEW_TYPE, TIER_PRESETS, pure types (zero dependencies)
  data/GraphStore.ts         the only module that reads metadataCache; vault → GraphData; emits 'data-changed'
  data/queries.ts            colorGroup query parsing (the path:/tag:/file: subset) + search matching, pure functions and unit-testable
  settings/                  the settings schema + SettingsTab + graphJsonImport.ts (a one-off graph.json import)
  view/GraphItemView.ts      the ItemView, owning the lifecycle alone (deferred/resize/visibility/disposal)
  view/GraphController.ts    the single assembly point: Store→Layout→Renderer→Interactions→Overlay
  layout/LayoutEngine.ts     the interface: init/start/stop/onPositions/updateParams/pinNode/dispose
  layout/  BuiltinForceLayout (M1: configuring 3d-force-graph's internal d3 forces)
           WorkerForceLayout (M3: d3-force-3d in a Blob URL Worker, Float32Array returned transferable,
           the main thread writing node.fx/fy/fz with the internal forces emptied —— the only mechanism for an
           external layout without forking the library)
  render/GraphRenderer.ts    the interface: mount/setData/applyTheme/applyTier/flyToNode/setSelection/
                             setHover/projectNode/resize/pause/resume/dispose
  render/  ForceGraphRenderer (M0-M2: the library's default objects, for the early milestones and as a control)
           AggregateRenderer (the shipping target, see the visual spec: 1×Points for nodes + 1×LineSegments for links)
  render/presets.ts          the single source of both A/B visual token sets; effects.ts (the starfield/halo/bloom factories)
  interactions/              CameraDirector (flight + cruise), Picker (hover/click + per-tier throttling), SearchController
  overlay/                   OverlayManager (pooled DOM labels), NodeCard, HUD (search box / glow slider / cruise switch / legend)
  theme/ThemeService.ts      reads the --graph-* CSS variables; subscribes to 'css-change' and body.theme-dark
  quality/QualityManager.ts  Platform.isMobile + a manual override + an FPS watchdog → the tier
```

The dependency direction is strict: `data/settings/theme/quality` know nothing of three.js; `layout/render` know nothing of Obsidian; `GraphController` is the only thing that knows everything.  **There is exactly one pair of interfaces, LayoutEngine/GraphRenderer** —— not over-abstraction but a defence against the predecessors' cause of death (the second use case has already been paid for by four corpses).

### The data layer
- Nodes: path (the primary key) / title / folder (TFile.parent) / tags (getAllTags) / degree / ctime / mtime / isUnresolved / isAttachment; edges: src / dst / count.
- **V1 renders only resolved links by default** (3,225 n / ~19.5k e); "show unresolved" (+6,548 ghost nodes) becomes a performance-gated switch whose fate M0's S3 benchmark decides —— a deliberate divergence from graph.json parity, because naive parity means recreating the predecessors' death scene.
- The update strategy: **a full rebuild + an identity-preserving merge** (a Map<path,node> reusing the object references in place → x/y/z survive and the layout does not explode); an 800ms trailing debounce on `metadataCache.on('resolved'/'changed'/'deleted')` + `vault.on('rename'/'delete')` (a rename is not covered by 'changed').  Rick has readwise/cubox bulk-sync plugins installed —— M1's verification includes a real Cubox sync storm.
- The warm start: the plugin's data.json caches the settled coordinates, so an unchanged vault reopens and appears in under 1.5s.

### Inheriting graph.json (a one-off import, try/catch, never written back)
Rick already has 9 folder-based colorGroups (the colours as decimal ints and the queries carrying trailing spaces —— parsing has to trim and be defensive).  The mapping: colorGroups → group colours (int→hex); nodeSizeMultiplier (0.93) passed through; repelStrength (14.8) → charge ≈ -14.8×12, linkDistance (264) → ×0.3 (2D and 3D force spaces are incommensurable, and the calibration constants are tuned at the preview gate); showTags/showAttachments/hideUnresolved/showOrphans → filters.  A failure falls back silently to the built-in palette.

### The lifecycle (the predecessors' number-one bug is leaking, so this is a contract)
- onOpen only builds the DOM skeleton; WebGL initialisation is deferred to the first non-zero-size onResize (avoiding the 0×0 bug of a deferred or restored layout).
- The RAF runs only while the document is visible **and** the leaf is actually shown (IntersectionObserver + 'layout-change').
- **The onClose destruction list (in order)**: timers → the Overlay DOM → worker.terminate + revokeObjectURL → pauseAnimation → bloom pass dispose → traverse the scene disposing geometries/materials/textures → controls.dispose → renderer.dispose + forceContextLoss → ForceGraph3D._destructor() → null the references.
- `webglcontextlost` → an overlay button reading "the render context was lost, press to rebuild".
- **The leak canary**: the debug command "open and close the view ×10 and measure memory", passing at a heap growth of <20MB with no context warnings, **run at every milestone**.

### The quality tiers (static presets, three of them)
| Knob | desktop-high | desktop-low | mobile |
|---|---|---|---|
| pixelRatio / MSAA | min(dpr,2) / 4x | 1 / off | ≤1.5 / off |
| bloom | full resolution | half resolution, strength ≤1.0 | **off** (the shader's own hot core supplies the glow) |
| starfield points | 3,750 (3 size classes) | 1,500 | 1,200 |
| node cap | none | none | 1,500 (sorted by degree; measured, the top 1,500 keeps 94% of the link quality, "it still looks like Rick's brain") |
| link budget | all of them | all of them | the top 12k (by min(endpoint degree)) |
| DOM label pool | 40 | 20 | 8 |
| picking | throttled to 30ms | 80ms | tap only |

Platform.isMobile is a hard ceiling; a manual override wins outright; the FPS watchdog samples only after the layout settles (a 5s rolling mean <30fps steps down once, one way, with a Notice, and never comes back up within a session).

## The visual spec (the shipping target = AggregateRenderer)

**The key ruling (integrating the three perspectives' disagreement)**: aggregate rendering is not a contingency but V1's shipping target —— because the "glowing orb" NASA look needs a custom radial-falloff shader anyway, and that same shader compresses ~22.7k draw calls to **under 45**.  Performance and aesthetics converge here.  3d-force-graph stays as the host for layout, camera and controls.  M0 measures the real numbers with the library's default pipeline first (see the milestones).

### The scene's composition (the Direction A baseline)
- The background is `#000003` (near-black rather than pure black, which bloom's falloff needs); no fog, no vignette, no grain.
- **The starfield**: 3 size classes of THREE.Points, 3 draw calls in total (2,600/900/250 points, distributed on a shell of radius 6.5× the graph radius, 85% cool white, 10% warm white, 5% blue; ~3% large stars at ×1.8 brightness getting bloom to themselves); the whole thing rotating slowly at 0.0008 rad/s.
- **Nodes = 1×THREE.Points + a custom shader** (a white-hot core through smoothstep + a soft edge; per-vertex color/size/dimFactor).  The size is `2.2×(1+0.5√degree)` clamped at 6× (the top hub at degree=689 must not swallow the screen) × the user's 0.93 factor.  **A near-field promotion pool**: 32 Sprites, promoting from Points any node whose projected radius exceeds 48px (the GPU's point-size cap makes a node flown towards actually get *smaller* —— this is core behaviour, not an easter egg, and matters most on Intel integrated graphics).
- **Links = 1×LineSegments** (~19.5k edges drawn at once): 1px, the endpoint group colours blended 50/50 then desaturated 60%, **NormalBlending at alpha ≈0.16** (additive whites out at the hub core, a measured failure mode; kept as a "link glow" switch); depthWrite:false, so nodes always cover the link mesh.
- **States**: hover = a shared additive halo Sprite (2.6× the radius, fading in over 120ms); selected = a 3.2× halo + a 2.4s breath + a brightened core; **focus mode** = non-neighbours' dimFactor → 0.12 (280ms easing), restored by ESC or clicking empty space; the selected node's own links go to full saturation at alpha 0.85 and everything else to 0.04.
- Unresolved nodes get ghost treatment (no hot core, alpha ×0.45, the --graph-node-unresolved colour); orphans render normally (drifting to the periphery under the physics *is* their visual identity).

### The post-processing chain
`RenderPass → UnrealBloomPass(threshold 0.10, strength 0.9 [a user slider, 0–2.5], radius 0.45) → OutputPass` (required from three r152+, or the linear colours wash out).  ACES tone mapping at exposure 1.05 (falling back to NoToneMapping at the preview gate if the group colours shift).  **No OutlinePass and no FXAA.**

### The camera choreography (the numbers are settled; the implementer needs no taste of their own)
- **The opening shot (the first impression is everything)**: the Worker pre-warms the layout to alpha<0.05 (or a 1.8s cap) behind a `#000003` mask with a pulsing "building the star map…" label; within 600ms of the unveiling the camera **pulls out from inside the graph** (0.5× → 2.2× the graph radius, elevation +18°, 3.2s easeInOutSine) while bloom falls 1.6 → 0.9 in step; then it hands straight over to the cruise.  Any input cuts to the final frame in 250ms.  It plays only when the view is opened.
- **Flight**: the target distance is `clamp(node radius × 12, 40, 140)`; the azimuth is offset by 15° (not head-on, leaving the neighbourhood visible); the duration is `clamp(800 + 0.45 × the journey, 800, 1800)ms` on easeInOutCubic; interruptible; it selects on arrival.  A tween wrapper of our own drives `cameraPosition(pos, lookAt, 0)` every frame rather than depending on the library's internal tween.
- **The cruise**: orbiting the centre of mass at 0.022 rad/s (about 4.8 minutes per revolution); the elevation ±8° (90s) and the radius ±4% (60s) on two incommensurable periods → the path never repeats ("a spacecraft" rather than "a turntable"); with something selected it orbits that node instead (the NASA feel of tracking an asteroid); any input pauses it, and after 10s with no input it eases back in over 2s.

### The two visual directions (per Rick's protocol: run them and choose by eye, at gate G2)
Both token sets live entirely in `src/render/presets.ts`, with the runtime command "switch visual direction" giving instant A/B:
- **A "deep space"**: always dark and cinematic, ignoring the app theme (the deliberate contrast of a video player); the UI is dark glass (blur 16px).
- **B "follow the theme"**: dark = the same scene as A; light = **a purpose-designed "daylight drafting room"** —— a warm paper ground `#F6F4EF`, 600 dust motes replacing the starfield, **bloom off** (a glow on a light ground is haze), ink-disc nodes (the 9 hues re-targeted from L 60% to 44%) + a 1px dark rim + a contact shadow, and pencil-line links (which go to full-saturation coloured ink on selection —— the light mode's moment).  A css-change switch fades out over 180ms → swaps the tokens → fades in over 220ms, with no WebGL rebuild.
- The losing direction stays behind a flag (a cost of roughly one token object).  This is a gate on the default, not a gate on deletion.

### Labels and the card (a DOM overlay, a hard budget of 40 elements)
1 selected + 1 hover + ≤20 neighbours + the top-14 permanent hub labels (by degree); each frame does Vector3.project → translate3d (GPU compositing, no reflow); two font sizes, 11px/13px (continuous scaling shimmers and is forbidden); at a distance only the brightest "constellation names" surface.  **The selection card**: the title / the path with a group-colour dot / tag chips ≤5 / "↩N backlinks · →M outlinks · modified …" / an asynchronous 120-character snippet (vault.cachedRead, cancellable) / "open note" (workspace.openLinkText) + "focus".  **All of it through createEl(), innerHTML forbidden** (a store-review red line).  Mobile switches to a bottom drawer (40vh, dismissed by swiping down).

## Milestones and decision gates (~6–8 weeks; there is a visible "wow" by the end of week 1.5)

> The development-environment ruling: **develop against a local clone of the vault** (rsync of *.md only —— the link structure is identical, so the graph and the performance numbers are identical), with esbuild watch + hot-reload pointing at the clone; the real iCloud vault is touched only for milestone verification and mobile testing (avoiding fileproviderd churn, conflict copies pushed to the iPhone, and a development build freezing the library Rick is actually using).

**M0 — a performance spike on real data (2–3 days) → gate G0**
The repository scaffold (the sample-plugin template + eslint + vitest + GitHub Actions); the design documents copied into `docs/design/`; a throwaway-quality spike: stock 3d-force-graph + real metadataCache data + Stats.js + a bloom switch + the benchmark commands.  The benchmark scenarios (a deterministic seeded layout):
- S1: 3,225n / 19.5k e + bloom, a scripted 20s orbit after settling → mean and p95 FPS
- S2: a cold-start layout —— the settling time and the longest main-thread block (the longtask observer)
- S3: with unresolved included (9.8k n / 27.8k e) —— deciding that switch's fate
- S4: the ×10 open/close leak canary
**G0's criteria**: S1 ≥45fps = green (the library pipeline holds to M2); 30–45 = amber (aggregate links are pulled forward as an M3 must); <30 = red (aggregate rendering goes in immediately at M1 and the library degrades to a layout shell —— a plan, not a crisis).  An S2 block >1s ⇒ the Worker is confirmed as M3's first item.  The numbers go into WORKLOG.

**M1 — a walking skeleton + the first "wow" (about a week) → gate G1 (the morale demo)**
The formal scaffold + the lifecycle contract (deferred/disposal/leak canary) + both interfaces in place + the T1 visuals: a black ground, the starfield, the glow slider, click-to-fly and the idle cruise.  GraphStore + queries unit tests green.
*Verification*: a manual checklist (open from the ribbon → orbit → click to fly → 10s of cruise → an interaction pauses it); the leak canary; CI green; trigger one real Cubox sync and confirm the merge stays smooth.
**G1**: Rick takes one look —— "does it look like NASA Eyes yet?"  If not, fix T1 before adding features.

**M2 — both visual directions + the four pieces (1–1.5 weeks) → gate G2 (the design gate)**
The A/B presets switchable at runtime, each fully wearing the four pieces: the graph.json group-colour import (trim / int→hex / a fallback on failure), search + flight, DOM labels + the selection card, the cruise polished, and the opening shot.
*Verification*: a checklist per preset × {Minimal-dark, Minimal-light}: the 9 group colours visibly match the 2D graph; searching the top hub's name flies to it; hover shows a label in <100ms; the card opens the note; B re-skins live on a theme switch with no reload; the leak canary; the mapping unit tests green.
**G2**: Rick runs a 10-minute script in the real vault (opening → search → flight → 5 hovers → select the hub → focus dimming → 30s of cruise) and **chooses the default direction by eye** (if both satisfy, both stay as settings).

**M3 — performance hardening (1–2 weeks) ⚠ the highest-risk milestone → gate G3**
The Worker layout (the fx/fy/fz write-back mechanism —— the one mechanism in the whole design that is unverified, so a half-day spike against the full real dataset is the very first item; if it fails, Plan B is the main thread + cooldownTicks tuning until the aggregate-rendering stage consumes the Worker's coordinates directly); AggregateRenderer landing (1×Points + 1×LineSegments + the promotion pool); LOD and label distance fade; a debounced incremental update; the warm-start cache.
*Verification*: rerun M0's whole benchmark suite and post a before/after table in WORKLOG.  The passing line: S1 ≥45fps sustained; no main-thread block >200ms in a cold layout; editing a note with the graph open has no perceptible jank; a rename or delete is reflected in <3s; a warm reopen in <1.5s.
**G3**: the numbers decide mobile go/no-go and whether the unresolved switch reaches the desktop build.
*Fallback*: each optimisation lands independently —— if instancing fights picking, ship "aggregate links" alone first (the bigger win).

**M4 — the mobile tier (about a week) → gate G4**
The full mobile tier + touch (orbit/pinch/tap) + contextlost handling + the bottom-drawer card.  A smoke test on Rick's iPhone happens right after M2 (iCloud sync installs it, so it does not wait for M4).
*Verification (on Rick's iPhone)*: it opens without crashing; orbiting ≥25–30fps; the tap-selected card is readable; it survives ×5 open/close; Obsidian is fine after backgrounding and returning.
**G4**: below the floor → flip `isDesktopOnly: true` for the launch and park mobile in V2 (the graceful exit agreed in advance).

**M5 — release preparation + a beta soak (about a week)**
Polishing the settings page (Chinese and English copy); an English README (for the store) + a Chinese README + screenshots/GIF; a GitHub Action publishing the release (main.js/manifest.json/styles.css); BRAT beta; a line-by-line self-audit against the official review checklist (innerHTML / style injection / unload cleanup / no network, no telemetry); Rick using it daily in the real vault for ≥1 week.
*Verification*: CI green; a BRAT install into a clean test vault succeeds; a soak week with zero crashes and zero leak warnings → submit to the store portal (the galaxy-view duplicate check happens here —— it needs a query).

## The test strategy
- **vitest unit tests** (pure modules, not importing obsidian): GraphModel construction / deduplication / degree / top-N truncation; graph.json parsing (including the trailing spaces and int colours —— the real config really is that dirty); the Worker protocol (d3-force-3d runs in Node, so a deterministic tick round trip and the warm start can be tested); settings serialisation.
- **Manual checklists** (visual, fixed in the repo under `docs/checklists/`): as above per milestone, always run in both Minimal-dark and Minimal-light, always ending with the leak canary.
- **Performance benchmarks** (a debug command inside the plugin): a deterministic layout + a scripted 20s orbit → avg/p95/worst frame + the longtask JSON; the result tables stay visible per commit in WORKLOG.
- Explicitly not done: WebGL screenshot diffing, driving Obsidian end to end.

## The risk table (ordered by lethality)
| # | Risk | Mitigation | Early warning |
|---|---|---|---|
| R1 | The per-object rendering wall (the predecessors' cause of death; stock is ≈22.7k draw calls) | Aggregate rendering as the shipping target (<45 calls); the staging is pre-decided by G0's numbers | M0's S1 hard thresholds of 45/30 |
| R2 | Main-thread simulation freezing all of Obsidian (a cold start, and every save firing resolved) | A Worker (Blob) + the warm-start cache + an 800ms debounce + the opening mask turning the wait into theatre | M0's S2 longtask >1s |
| R3 | Leaking on view open/close (the WebGL context pool is 8–16 and exhausting it is fatal) | The onClose destruction contract + a leak canary at every milestone | ×10 with heap growth <20MB |
| R4 | iOS jetsam (a bloom HDR chain at dpr3 can eat 100MB+ of GPU memory) | The mobile tier (no bloom / a 1,500-node cap = 94% link quality); a smoke test right after M2; the isDesktopOnly exit | The iPhone smoke test at ≥25fps |
| R5 | Late rework from the store review | obsidianmd eslint from the first commit; createEl only; zero network, zero telemetry | CI lint |
| R6 | iCloud development-loop churn | Develop against a local clone; the real vault only for verification | — |
| R7 | The fx/fy/fz Worker write-back mechanism is unverified (the only one in the design) | A half-day spike on M3's first day against the full real dataset; Plan B is ready | The spike is recorded in WORKLOG |
| R8 | "Impressive" has no convergence criterion (a schedule risk with the PM as judge) | The NASA restraint list *is* the spec; G2 rules on both presets by eye; a 2-minute demo at every milestone | The gate mechanism itself |

A three.js conflict with other plugins has been investigated and closed: all 29 installed plugins' main.js were grepped and not one bundles three or WebGLRenderer; plugin module scopes are isolated and cannot collide.

## YAGNI (not in V1) / the V2 car park
Not done: the local graph, time-evolution playback and link particles (the T3 car park), tag and attachment nodes (Rick's own config has them off), shaped layouts (a brain, a galaxy —— gimmicks), fog / vignette / grain, WebGPU, full search-query parity, writing graph.json back, multiple windows, an i18n framework (a zh/en string table suffices).
V2: the local-graph mode (the headline), time-growth playback, unresolved nodes on mobile, camera bookmarks, `obsidian://` deep-link flight.

## Documentation (per the global protocol, in Chinese, inside the repository)
- `README.zh.md`: what the project is / how to run it / the product judgements (the problem being solved / the key trade-offs / the rejected options —— cosmos.gl, Babylon, forking, additive links, OutlinePass and the rest are already written up)
- `WORKLOG.md`: append-only, inverted pyramid, appended every session; the benchmark number tables maintained per commit
- `docs/design/`: all three design perspectives in full; `docs/checklists/`: the manual verification checklists
