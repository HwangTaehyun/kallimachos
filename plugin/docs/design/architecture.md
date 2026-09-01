# Architecture Design: "Vault Galaxy" — Cinematic 3D Graph Plugin for Obsidian

Proposed plugin id: `vault-galaxy` (no "obsidian" substring, per store rules; final name is Rick's call — the id is referenced in exactly 3 places: `manifest.json`, the esbuild output path, and the repo folder name `/Users/rick/Claude_Code/Vault_Galaxy/`, so renaming before M1 is a 5-minute change).

---

## 1. Module Decomposition

```
/Users/rick/Claude_Code/Vault_Galaxy/
├── manifest.json            id: vault-galaxy, minAppVersion: "1.7.2", isDesktopOnly: false
├── versions.json
├── package.json             three@0.184.0, 3d-force-graph@1.80.0, d3-force-3d@3.0.6,
│                            three-spritetext@1.10.0, esbuild@0.25.x, TS 5.8,
│                            eslint + eslint-plugin-obsidianmd
├── esbuild.config.mjs       dev: watch → "/Users/rick/Library/Mobile Documents/iCloud~md~obsidian/
│                            Documents/Rick's Second Brain/.obsidian/plugins/vault-galaxy/"
│                            + writes .hotreload marker; prod: ./dist (main.js, manifest.json, styles.css)
├── styles.css               ALL overlay DOM styles (labels, node card, HUD) using --graph-* and
│                            --background-primary/--text-normal CSS vars
├── README.md (Chinese)  WORKLOG.md (Chinese)
└── src/
    ├── main.ts              Plugin subclass. onload: loadSettings → registerView → addRibbonIcon
    │                        → addCommand×3 → addSettingTab. Owns nothing else.
    ├── constants.ts         VIEW_TYPE_GALAXY, DEFAULT_SETTINGS, TIER_PRESETS table
    ├── types.ts             GraphNode, GraphLink, GraphData, ThemeTokens, QualityTier,
    │                        LayoutParams, ColorGroup — pure types, zero imports
    ├── data/
    │   ├── GraphStore.ts    vault → GraphData. Sole reader of metadataCache. Emits 'data-changed'.
    │   └── queries.ts       parse/match the colorGroup query subset (path:, tag:, file:) and
    │                        the search matcher. Pure functions, unit-testable.
    ├── settings/
    │   ├── settings.ts      VaultGalaxySettings interface + defaults + version/migration field
    │   ├── SettingsTab.ts   PluginSettingTab UI (incl. "import settings from the core graph" button)
    │   └── graphJsonImport.ts  read .obsidian/graph.json via vault.adapter.read(normalizePath(
    │                        app.vault.configDir + '/graph.json')), try/catch, mapping table (§4)
    ├── view/
    │   ├── GraphItemView.ts ItemView subclass. Lifecycle ONLY (§5). Creates/destroys GraphController.
    │   └── GraphController.ts  The orchestrator. Wires Store→Layout→Renderer→Interactions→Overlay.
    │                        Owns RAF pause/resume policy and the QualityManager subscription.
    │                        This is the ONLY file that knows all the interfaces.
    ├── layout/
    │   ├── LayoutEngine.ts  interface (§3): init(data, params), start/stop, onPositions(cb),
    │   │                    updateParams(params), pinNode(id, xyz|null), dispose()
    │   ├── BuiltinForceLayout.ts  Phase 1: thin adapter that configures 3d-force-graph's
    │   │                    internal d3 sim (graph.d3Force('charge').strength(...), etc.)
    │   └── WorkerForceLayout.ts   Phase 2: d3-force-3d inside a Worker created from an inlined
    │                        Blob URL (URL.createObjectURL(new Blob([workerSource]))); ships
    │                        positions back as transferable Float32Array; main thread writes
    │                        node.fx/fy/fz (see §3 for why this exact mechanism)
    ├── render/
    │   ├── GraphRenderer.ts interface (§3): mount(el), setData, applyTheme, applyTier,
    │   │                    flyToNode(id, ms), setSelection(id|null), setHover(id|null),
    │   │                    projectNode(id)→screen xy (for overlay), resize(w,h),
    │   │                    pause()/resume(), dispose()
    │   ├── ForceGraphRenderer.ts  Phase 1 implementation wrapping ForceGraph3D():
    │   │                    .backgroundColor, .nodeThreeObject(), .nodeColor by group,
    │   │                    .linkOpacity/.linkWidth(0)=Line, .cameraPosition(),
    │   │                    .postProcessingComposer().addPass(UnrealBloomPass),
    │   │                    .pauseAnimation()/.resumeAnimation(), ._destructor()
    │   └── effects.ts       starfield (one THREE.Points + PointsMaterial, sized per tier),
    │                        selection halo (additive THREE.Sprite, canvas-generated radial
    │                        gradient texture, cached), bloom pass factory per tier
    ├── interactions/
    │   ├── CameraDirector.ts  fly-to via graph.cameraPosition(pos, lookAt, 1200) with the lib's
    │   │                    built-in tween; idle cruise: per-frame azimuthal rotation of
    │   │                    camera around controls.target; pauses on any pointer/wheel/touch
    │   │                    (registerDomEvent on the canvas), resumes after settings.idleOrbit
    │   │                    .resumeDelayMs (default 10000)
    │   ├── Picker.ts        hover/click. Uses 3d-force-graph's onNodeHover/onNodeClick in
    │   │                    Phase 1 (its raycast is internal); enforces tier throttle by
    │   │                    enabling/disabling .enablePointerInteraction() + own pointermove
    │   │                    throttle gate. Tap-only mode on mobile.
    │   └── SearchController.ts  fuzzy match over GraphStore (prepareFuzzySearch from 'obsidian')
    │                        → result list → CameraDirector.flyToNode
    ├── overlay/
    │   ├── OverlayManager.ts  absolutely-positioned div over the canvas. Owns a pooled set of
    │   │                    label divs (tier label budget). Each RAF (only while camera moves
    │   │                    or layout hot): picks top-K nearest/highest-degree visible nodes,
    │   │                    calls renderer.projectNode(), positions labels, distance-fades
    │   │                    via opacity. DOM labels, NOT in-canvas text (NASA recipe).
    │   ├── NodeCard.ts      hover/selection preview card: title, folder path, tags, in/out
    │   │                    degree, ctime/mtime, "open note" → workspace.openLinkText()
    │   └── HUD.ts           search input, bloom slider, idle-cruise toggle, group legend.
    │                        Plain DOM, styled by styles.css.
    ├── theme/
    │   └── ThemeService.ts  getComputedStyle(document.body) for --graph-node, --graph-line,
    │                        --graph-text, --background-primary, etc. → ThemeTokens; subscribes
    │                        workspace.on('css-change') and document.body class 'theme-dark'/
    │                        'theme-light'; emits 'theme-changed'
    └── quality/
        └── QualityManager.ts  Platform.isMobile + settings.qualityOverride + FPS watchdog
                             → QualityTier; emits 'tier-changed' (§6)
```

**Dependency direction (strict, enforced by review):**
`types.ts` ← everything; `data/`, `theme/`, `quality/`, `settings/` know nothing about three.js; `layout/` and `render/` know three.js but not Obsidian (except renderer mounting into an HTMLElement); `view/GraphController.ts` is the only composition point; `main.ts` only knows `view/`, `settings/`. `overlay/` knows DOM + the `GraphRenderer.projectNode` seam, never three.js directly.

This is deliberately NOT more abstract than needed: there is exactly one interface pair (LayoutEngine/GraphRenderer) and it exists because every predecessor plugin died on that wall — that is the "second use case" already paid for in corpses.

---

## 2. Data Layer

### GraphNode / GraphLink model
- `GraphNode`: `id` (vault path, the canonical key), `title` (basename minus extension), `folder` (TFile.parent?.path ?? ''), `tags` (string[] via `getAllTags(app.metadataCache.getFileCache(file))`), `degree` (computed in/out sum), `ctime`/`mtime` (TFile.stat), `isAttachment` (extension !== 'md'), `isUnresolved` (synthesized from unresolvedLinks keys), `group` (index into resolved ColorGroup, computed once per rebuild by `queries.ts`), plus mutable layout fields `x,y,z,vx,vy,vz,fx,fy,fz` (the d3/3d-force-graph contract).
- `GraphLink`: `source` (path), `target` (path), `count` (the value from resolvedLinks).

### Build (full)
1. `app.vault.getFiles()` → filter by settings.filters (showAttachments off ⇒ markdown only).
2. Walk `app.metadataCache.resolvedLinks` (Record<srcPath, Record<dstPath, count>>) → links + degree accumulation.
3. If `!hideUnresolved`: walk `app.metadataCache.unresolvedLinks`, synthesize phantom nodes keyed `unresolved:<linktext>`.
4. If `!showOrphans`: drop degree-0 nodes.
5. Resolve color groups: for each node run `queries.matchGroup(node, settings.groups)` (first match wins, same as core graph).

Cost at Rick's scale (3,225 files, ~43k link instances → est. 15–30k unique edges): this is pure dictionary iteration, **single-digit to low-tens of ms**. Conclusion: **full rebuild is the strategy; per-event patching is explicitly rejected as premature.**

### The one non-negotiable subtlety: identity-preserving merge
A naive rebuild creates fresh node objects ⇒ 3d-force-graph treats everything as new ⇒ layout re-randomizes ⇒ the galaxy "explodes" on every sync. So `GraphStore` keeps `Map<string, GraphNode>` across rebuilds and **mutates/reuses existing node objects** (preserving x/y/z/vx/vy/vz), removes stale entries, adds new ones, and passes the same object references into `graph.graphData()`. New nodes spawn near their first neighbor's position (small random offset) so additions bloom locally instead of flying in from origin.

### Update strategy & events (all via `this.registerEvent` on the view)
- `app.metadataCache.on('resolved')` — both the initial "cache ready" signal and the batch-update signal. **Debounce 800ms trailing.** This matters specifically because Rick runs readwise-official and cubox-sync, which batch-create dozens of files; debounce + identity merge means one smooth incremental settle instead of a storm.
- `app.metadataCache.on('changed')` — covers single-file edits (tags/frontmatter); folded into the same debounced rebuild.
- `app.vault.on('rename')` and `app.vault.on('delete')` — NOT covered by 'changed'; rename remaps the Map key while keeping the same object (position survives a rename).
- Listeners are registered by `GraphItemView` only while the view exists; no view ⇒ zero idle cost.
- After a merged rebuild, GraphController nudges the layout (`graph.d3ReheatSimulation()` in Phase 1 / `WorkerForceLayout.updateParams` reheat in Phase 2) with a low alphaTarget so changes settle gently.

---

## 3. The Two Isolation Interfaces — and the honest truth about 3d-force-graph

**Honest coupling assessment:** 3d-force-graph internally composes `three-forcegraph`, a single THREE.Object3D that owns BOTH the d3-force-3d simulation AND the node/link meshes; each internal tick advances the sim and copies `node.x/y/z` into mesh positions. There is no official "bring your own positions" mode. So the clean seam is **not inside the library — it is above it**, in GraphController, which only speaks `LayoutEngine` and `GraphRenderer`.

### LayoutEngine
- `BuiltinForceLayout` (Phase 1) is honestly a *configurator, not an engine*: it receives the ForceGraphRenderer's underlying instance via a narrow `ForceConfigurable` handle and applies `LayoutParams` through `.d3Force('charge').strength()`, `.d3Force('link').distance()/.strength()`, `.d3VelocityDecay()`, `.cooldownTime()`. The orchestrator doesn't know this; it just calls `updateParams()`.
- `WorkerForceLayout` (Phase 2, the proven escape hatch): run d3-force-3d in a Worker (inlined Blob URL — `workerSource` string bundled by esbuild, `URL.createObjectURL`, confirmed working in Obsidian plugins; remember `revokeObjectURL` + `worker.terminate()` in dispose). Worker posts `Float32Array` positions (transferable). Main thread writes them to `node.fx/fy/fz`. **Why fx/fy/fz works without forking the lib:** d3-force treats fixed coordinates as authoritative — each internal tick snaps x/y/z to fx/fy/fz — and we null the heavy internal forces (`d3Force('charge', null)`, `d3Force('link', null)`, `d3Force('center', null)`), leaving the internal sim as a near-free pass-through that keeps the lib's own position→mesh sync alive. This exact pattern is the FIRST spike to validate in M2 (half a day) because it's the only mechanism with any uncertainty.
- Interface shape: `init(data, params)`, `start()`, `stop()`, `updateParams(params)` (reheat), `pinNode(id, xyz|null)` (used by drag + fly-to focus), `onPositions(cb)` (Builtin: no-op, lib syncs itself; Worker: the fx/fy/fz writer), `isSettled()`, `dispose()`.

### GraphRenderer
- `ForceGraphRenderer` (Phase 1) wraps the whole library: `mount(containerEl)` constructs `ForceGraph3D()(el)`, sets `.backgroundColor('#000003')`, `.nodeColor`/`.nodeVal` from group/degree, link styling, `.enableNodeDrag(desktop only)`, adds starfield Points to `.scene()`, adds `UnrealBloomPass` to `.postProcessingComposer()`, exposes `projectNode(id)` by projecting node coords through `.camera()` (`Vector3.project(camera)` → CSS px).
- The future `InstancedRenderer` (the swap if the per-node-mesh / per-link-Line ceiling hurts — at 3.2k nodes + ~20k Line objects the **links** are the likelier wall) replaces 3d-force-graph entirely: one `THREE.InstancedMesh` for nodes, one `THREE.LineSegments` with a shared BufferGeometry for all links, own `EffectComposer`, own TrackballControls. It pairs ONLY with `WorkerForceLayout` (consuming the Float32Array directly — no fx/fy/fz detour, which is why positions flow through the interface, not through the library). Nothing in GraphController, Overlay, Picker-policy, ThemeService, or QualityManager changes — that is the whole point of the seam.

---

## 4. Settings Schema

### Plugin settings (`loadData()`/`saveData()` → data.json)
```
version: 1
visualDirection: 'cinematic-dark' | 'theme-adaptive'      ← decided at the M2 preview gate
bloom:        { enabled: true, strength: 1.2 (0–3 HUD slider), threshold, radius }
starfield:    { enabled: true }                            (density comes from tier, not settings)
idleOrbit:    { enabled: true, speedDegPerSec: 1.5, resumeDelayMs: 10000 }
labels:       { fadeStartDist, fadeEndDist }               (budget comes from tier)
nodeSizeMultiplier: number      linkOpacity: number
forces:       { repelStrength, linkDistance, linkStrength, centerStrength }   ← LayoutParams source
groups:       ColorGroup[] = { query: string, color: string(hex) }
filters:      { showTags, showAttachments, hideUnresolved, showOrphans }
qualityOverride: 'auto' | 'desktop-high' | 'desktop-low' | 'mobile'
mobileNodeCap: 1500
experiments:  { timeScrubber: false, linkParticles: false }   ← T3, settings-gated, default off
```

### Per-view ephemeral state (`ItemView.getState()`/`setState()`)
`cameraPos {x,y,z}`, `cameraTarget {x,y,z}`, `selectedNodeId`, `searchText` — so a workspace restore reopens the galaxy where you left it. `navigation = true` so it behaves like the core graph tab.

### graph.json → 3D mapping table (`graphJsonImport.ts`, best-effort try/catch, undocumented format)
| 2D field (verified present in Rick's file) | 3D target | transform |
|---|---|---|
| `colorGroups[].query` + `color.rgb` (int) | `groups[]` | rgb int → `#rrggbb` hex (`(rgb>>>16)&255`…); query string kept verbatim (path: prefix subset) |
| `nodeSizeMultiplier` (0.9298) | `nodeSizeMultiplier` | direct |
| `lineSizeMultiplier` (0.5708) | `linkOpacity` scale | direct multiply onto base 0.25 |
| `repelStrength` (14.83, 2D range ~0–20) | d3 charge strength | `charge = -repelStrength × 12` (calibration constant; tuned visually at the preview gate, stated honestly: 2D↔3D force spaces are not unit-compatible) |
| `linkDistance` (264) | d3 link distance | `× 0.3` initial calibration, same caveat |
| `linkStrength` (0.823) | d3 link strength | direct (both 0–1) |
| `centerStrength` (0.728) | d3 x/y/z centering strength | direct |
| `showTags / showAttachments / hideUnresolved / showOrphans / search` | `filters` | direct |
| `textFadeMultiplier` (0) | `labels.fadeStart/End` | scale defaults |
| `showArrow` (false) | `linkDirectionalArrowLength` | false → 0 |
| `scale`, `close`, `collapse-*` | — | ignored |

Import runs once on first install (and via the settings-tab button), writes into plugin settings; it never writes back to graph.json.

---

## 5. View Lifecycle (GraphItemView)

- **Plugin onload:** `registerView(VIEW_TYPE_GALAXY, leaf => new GraphItemView(leaf, plugin))`; `addRibbonIcon('orbit', 'open the 3D star map', …)`; commands: open view, focus search, toggle idle cruise. **onunload does NOT detach leaves** (current guideline; Obsidian empties them).
- **Open:** existing leaf? `getLeavesOfType(VIEW_TYPE_GALAXY)` → if `leaf.isDeferred` call `leaf.loadIfDeferred()` then `workspace.revealLeaf(leaf)`; else `workspace.getLeaf(true).setViewState({type, active:true})`.
- **onOpen:** build contentEl scaffold only; defer WebGL init to the first `onResize()` with nonzero dimensions (avoids 0×0 renderer bugs in deferred/restored layouts). Then construct GraphController → ThemeService.read → QualityManager.detect → renderer.mount → store.buildInitial (wait for `metadataCache.on('resolved')` if cache not yet populated).
- **Pause policy (battery + iCloud-Mac niceness):** RAF runs only when (a) document visible — `registerDomEvent(document, 'visibilitychange')`, (b) the leaf's containerEl is actually displayed — IntersectionObserver on contentEl plus `workspace.on('layout-change')` for tab-behind detection. Hidden ⇒ `renderer.pause()` (`pauseAnimation()`), layout `stop()`, OverlayManager freeze. Exception: idle-cruise counts as activity only while visible.
- **onResize:** `renderer.resize(contentEl.clientWidth, clientHeight)` → `.width()/.height()` on the lib (it updates camera aspect + composer size).
- **onClose disposal checklist (the predecessors' memory-leak fix), in order:**
  1. cancel debounce timers; 2. OverlayManager.dispose (remove DOM, disconnect IntersectionObserver); 3. layout.dispose (worker.terminate + revokeObjectURL); 4. renderer.dispose: `pauseAnimation()` → remove bloom pass + `pass.dispose()` → traverse scene disposing geometry/material/texture (starfield, halos) → `controls().dispose()` → `renderer().dispose()` + `renderer().forceContextLoss()` → `_destructor()` on the ForceGraph3D instance; 5. null all refs. (`registerEvent`/`registerDomEvent` auto-clean the rest.)
- **WebGL context loss:** `registerDomEvent(canvas, 'webglcontextlost')` → show a "the render context was lost, press to rebuild" overlay button that tears down and re-mounts the renderer.
- **Verification (manual, scripted in WORKLOG):** open/close the view 10×, watch Electron devtools Memory; confirm no growth and no orphaned WebGL contexts.

---

## 6. Quality-Tier System

`QualityTier` is a plain readonly preset object in `constants.ts` (`TIER_PRESETS`):

| Knob | desktop-high | desktop-low | mobile |
|---|---|---|---|
| pixelRatio | `min(devicePixelRatio, 2)` | 1 | 1 |
| antialias (renderer config, set at mount) | on | off | off |
| bloom | on, half-res target | on, quarter-res, strength clamped ≤1.0 | **off** (V1; biggest single GPU cost) |
| starfield points | 4000 | 1500 | 800 |
| node visual | sphere mesh + group color | sphere mesh | sprite dot only (`nodeThreeObject` → cached Sprite) |
| node cap | none (3,225 fine) | none | `mobileNodeCap` 1500, kept by degree rank (HananoshikaYomaru precedent), HUD shows "showing the first 1500 nodes" |
| label budget (DOM pool) | 40 | 20 | 8 |
| hover raycast | pointermove throttled 30ms | 80ms | **tap-only** (no hover on touch) |
| link particles / time scrubber (T3) | allowed if enabled | forced off | forced off |
| idle cruise | on | on | on (cheap — it's just camera math) |

**Selection logic in QualityManager:** (1) `Platform.isMobile` ⇒ tier `mobile`, full stop — mobile can never auto-promote. (2) Desktop: `settings.qualityOverride !== 'auto'` ⇒ that tier, watchdog disabled (user's explicit choice is absolute). (3) `auto`: start desktop-high; FPS watchdog samples a rolling 5s average *after* layout settles (`isSettled()` — never judge during warmup); avg < 30fps ⇒ drop to desktop-low once, fire `new Notice('switched to performance mode automatically; it can be changed back in settings')`, never auto-promote back within a session.

Tier changes are applied **live** via `renderer.applyTier(tier)`: `setPixelRatio`, add/remove bloom pass, rebuild starfield Points, OverlayManager pool resize, Picker throttle swap — no view reload (antialias is the one mount-time knob; changing it requires renderer re-mount, which applyTier handles by full re-mount only when antialias differs).

---

## 7. Implementation Sequence (with verification + the preview gate)

- **M0 — Scaffold & dev loop (0.5d):** repo from obsidian-sample-plugin, strip, eslint-plugin-obsidianmd, esbuild watch → real vault plugin dir + `.hotreload`. ⚠️ Prerequisite discovered in exploration: **pjeby/hot-reload is NOT yet installed in the vault** — install it first. *Verify:* edit a string, see it hot-swap in Obsidian.
- **M1 — Data layer (1d):** GraphStore + queries + debounced merge. *Verify:* dev command "dump graph stats" logs node/link counts; compare against core 2D graph; rename/delete/create a note and confirm single smooth update (no explosion), then trigger a Cubox sync as the batch stress test.
- **M2 — T1 look on 3d-force-graph + worker spike (2–3d):** black bg, starfield, bloom slider, click/search-less fly-to, idle cruise. Plus the **half-day fx/fy/fz worker spike** (validate the §3 escape hatch with all 3.2k nodes + real links; record tick ms + fps in WORKLOG). *Verify:* fps counter ≥50 on Rick's Mac with full vault; spike numbers written down.
- **GATE (user protocol, previewable, 2 directions):** two running builds behind `visualDirection`: **A "endless-night cinema"** (always #000003 + full bloom, ignores Obsidian theme) vs **B "breathing with the theme"** (dark = A; light = pale paper-space using --background-primary, bloom reduced/off because bloom on light bg washes out — stated honestly in the demo). Rick toggles live in Obsidian and picks. Decision recorded in WORKLOG; loser stays as a settings option only if free.
- **M3 — T2 playability (3–4d):** OverlayManager labels + fade, NodeCard, selection halo + thin desaturated links, color groups + legend, SearchController + fly-to, graph.json import. *Verify:* scripted manual checklist (search a folder name → flies to node → card shows tags → "open note" opens it); each of the 4 must-have features demoed.
- **M4 — Quality tiers + mobile (2d):** QualityManager, mobile tier, iCloud-sync the plugin folder to iOS, enable. *Verify:* runs on Rick's iPhone, pan/tap usable, no crash with 1500-node cap; desktop-low forced via override and eyeballed.
- **M5 — Release readiness (1–2d):** SettingsTab polish, a Chinese README + screenshots, GitHub Actions release (main.js/manifest.json/styles.css assets), BRAT beta install path documented. *Verify:* clean install from BRAT into a throwaway vault.

---

### Critical Files for Implementation
- /Users/rick/Claude_Code/Vault_Galaxy/src/view/GraphController.ts — the composition point; the two interfaces only pay off if this file alone wires everything
- /Users/rick/Claude_Code/Vault_Galaxy/src/data/GraphStore.ts — identity-preserving merge is the single trickiest correctness requirement (sync plugins will stress it daily)
- /Users/rick/Claude_Code/Vault_Galaxy/src/render/ForceGraphRenderer.ts — wraps the whole 3d-force-graph surface; disposal checklist lives here
- /Users/rick/Claude_Code/Vault_Galaxy/src/layout/LayoutEngine.ts — the seam that decides whether this plugin escapes its predecessors' fate
- /Users/rick/Claude_Code/Vault_Galaxy/src/view/GraphItemView.ts — lifecycle/deferred/visibility/memory, where prior plugins leaked

## KEY DECISIONS
- Define the LayoutEngine/GraphRenderer seam ABOVE 3d-force-graph (in GraphController), not inside it — Phase 1 BuiltinForceLayout is honestly just a force configurator, and the worker path uses the fx/fy/fz writeback trick with internal forces nulled — 3d-force-graph fuses sim+meshes inside three-forcegraph with no official external-positions mode; pretending there's a clean internal seam would be a lie. The fx/fy/fz mechanism (d3-force snaps fixed coords each tick) is the only way to externalize layout without forking, and the future InstancedRenderer swap replaces the library wholesale — so the orchestrator-level interface is the only seam that survives both moves. This is the exact wall that killed every predecessor plugin.
- Full rebuild with identity-preserving object merge (Map<path,GraphNode>, mutate-in-place, debounced 800ms on metadataCache 'resolved'/'changed' + vault 'rename'/'delete'), no per-event patching — At 3,225 nodes / ~20-30k edges a rebuild is single-digit ms — patching is premature complexity. But naive rebuilds re-randomize layout, and exploration confirmed Rick runs readwise-official and cubox-sync which batch-create files; reusing node object references preserves x/y/z so syncs settle gently instead of exploding the galaxy.
- Quality tiers as static preset objects (desktop-high/desktop-low/mobile) with Platform.isMobile as a hard ceiling, manual override absolute, and a one-way FPS watchdog (auto-drop only after layout settles, never auto-promote) — Live-tunable continuous quality is a rabbit hole; three fixed presets cover the real device matrix (Rick's Mac + iPhone). Watchdog sampling during force warmup would always trigger falsely, hence the isSettled() gate; one-way drop avoids oscillation.
- Bloom OFF entirely on mobile in V1 (sprite-only nodes, 1500-node degree-ranked cap, tap-only picking) — Postprocessing is the dominant mobile GPU cost and the NASA T1 look (black bg + starfield + eased fly-to + cruise) reads as cinematic even without bloom; a capped, bloom-free 60fps beats an uncapped 15fps slideshow. Cap-by-degree is a precedent proven in HananoshikaYomaru's plugin.
- DOM-overlay labels and cards (pooled divs positioned via Vector3.project each frame, tier-bounded budget 40/20/8) instead of in-canvas text sprites — Verified NASA Eyes production technique; DOM text is crisp at any pixelRatio, free to theme via Obsidian --graph-*/Minimal CSS vars, accessible, and decouples label rendering from the future InstancedRenderer swap. three-spritetext stays in the deps only as fallback.
- The visual-direction preview gate is a settings flag (visualDirection: 'cinematic-dark' | 'theme-adaptive'), with both directions built as running toggles before M3 — Rick's protocol requires picking visual style by LOOKING at running versions. Vault appearance is theme:'system' so light mode is real, but bloom on a light background washes out — the gate must show that trade-off honestly rather than bury it in text. A flag keeps the losing direction nearly free to retain.
- Per-view state (camera pos/target, selection, search) via ItemView getState/setState; vault-wide knobs in plugin data.json; one-time best-effort import from .obsidian/graph.json with an explicit 2D→3D calibration table (charge = -repelStrength×12, linkDistance×0.3) — Matches Obsidian's own split (graph.json is per-vault, workspace stores view state). The graph.json format is undocumented, so import is try/catch best-effort (Xallt precedent), never written back; 2D and 3D force spaces aren't unit-compatible so the mapping constants are declared as calibration knobs to be tuned at the preview gate, not silent magic.
- WebGL-only renderer, ~1.3MB single main.js via esbuild, plugin id 'vault-galaxy' (proposal), minAppVersion 1.7.2, isDesktopOnly:false — WebGPU is unsafe across the installed Electron base; bundle fits Sync's 5MB limit; 1.7.2 floor is required because the deferred-view API the lifecycle code depends on shipped there; id avoids the forbidden 'obsidian' substring for store submission.

## RISKS
- The fx/fy/fz worker-writeback seam is the only unproven mechanism in the design (d3 internal sim nulled + fixed-coord snapping). Mitigation: it is scheduled as a half-day spike inside M2 with the full real vault, results recorded in WORKLOG; if it fails, Plan B is keeping layout on-main with cooldownTicks tuning until the InstancedRenderer phase, which consumes worker positions directly.
- Links, not nodes, are the likely first performance wall: ~43k wikilink instances ≈ 15-30k unique edges become 15-30k individual THREE.Line objects in 3d-force-graph. Mitigation: measure real edge count in M1's stats command; keep links thin/desaturated (cheap material); the InstancedRenderer escape hatch merges all links into one LineSegments buffer and is pre-designed behind GraphRenderer.
- Batch-sync storms from Rick's readwise/cubox plugins could thrash rebuilds or explode the layout. Mitigation: 800ms trailing debounce on 'resolved', identity-preserving merge, gentle d3ReheatSimulation with low alphaTarget, and an explicit M1 verification step that triggers a real Cubox sync.
- Memory/WebGL leaks on repeated open/close killed predecessor plugins. Mitigation: the ordered disposal checklist in GraphItemView.onClose (composer passes, scene traversal dispose, controls.dispose, renderer.dispose + forceContextLoss, _destructor, worker.terminate + revokeObjectURL) plus a mandatory 10x open/close devtools memory check before any milestone is called done.
- Bloom + light theme conflict: vault appearance is 'system', but the cinematic recipe assumes near-black; a theme-adaptive light mode with full bloom looks washed out. Mitigation: the preview gate explicitly demos both directions with honest bloom degradation in light mode, so Rick decides with eyes, not text; cinematic-dark remains a safe always-available default.
- Dev loop writes builds into an iCloud-synced folder (the real vault), causing sync churn and possible mobile-side lag/partial files. Mitigation: acceptable for desktop dev (.hotreload pattern works); for mobile testing, build once then let iCloud settle, and keep a throwaway local vault for the M5 clean-install verification.
- graph.json format is undocumented and may change (rgb-int colors, trailing-space queries observed in Rick's file). Mitigation: import is one-shot, try/catch, never written back; queries.ts parses only the path:/tag:/file: subset and falls back to 'no group' on anything unparseable.