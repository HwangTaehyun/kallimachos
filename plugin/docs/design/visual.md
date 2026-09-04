# Visual & Rendering Pipeline Spec — "Stunning" 3D Graph for Obsidian

> **Inherited, unedited, from upstream.**  This is one of [galaxy-view](https://github.com/Longwind1984/galaxy-view)'s pre-implementation design
> documents by Rick, written for that project on that author's machine and published by them
> at `docs/design/` in that repository.  It describes the plugin **before** it existed and
> before this fork —— the absolute paths in it (`/Users/rick/…`) were build instructions there,
> not prose, and the vault figures are that author's, not this project's.  It is kept as
> written rather than rewritten: the reasoning is the value, and diverging from upstream's own
> published copy would gain nothing.  For what this fork actually ships, read
> [`../../README.md`](../../README.md) and [`../../WORKLOG.md`](../../WORKLOG.md).

Perspective: visual design + render pipeline. Every value below is a concrete starting point an implementer can type in without taste of their own; values marked **[tune@gate]** are expected to be adjusted at Rick's preview gate.

Ground truth pulled from the live vault (the folder names below are Rick's real ones and stay as they are —— the colour-group mapping is keyed on those exact strings):
- `/Users/rick/Library/Mobile Documents/iCloud~md~obsidian/Documents/Rick's Second Brain/.obsidian/graph.json` — 9 path colorGroups (decoded to hex below), `showTags:false`, `showAttachments:false`, `hideUnresolved:false` (unresolved ARE shown), `showOrphans:true`, `showArrow:false`.
- `appearance.json` — `theme:"system"`, `cssTheme:"Minimal"`, `accentColor:""` (default accent → always read CSS vars at runtime, never hardcode accent).
- Vault has ungrouped folders (60流浪, 80随记, 90故纸堆, 99Archive, Readwise, root-level files) that need a designed "neutral" node treatment.

## 0. Decoded color groups (authoritative palette)

`graph.json` stores colors as `rgb` integers. Decode: `r=(v>>16)&255, g=(v>>8)&255, b=v&255`. Verified values:

| Group | int | hex | hue |
|---|---|---|---|
| 01学习 | 14048348 | `#D65C5C` | red 0° |
| 02工作 | 14069084 | `#D6AD5C` | amber 40° |
| 03产品 | 11392604 | `#ADD65C` | lime 80° |
| 04AI | 6084188 | `#5CD65C` | green 120° |
| 05读书 | 6084269 | `#5CD6AD` | mint 160° |
| 06人 | 6073814 | `#5CADD6` | sky 200° |
| 30认真活着 | 6053078 | `#5C5CD6` | indigo 240° |
| Cubox | 11361494 | `#AD5CD6` | purple 280° |
| 00Meta | 15101696 | `#E66F00` | orange (saturated) |

All nine are the standard Obsidian swatch family hsl(h, 60%, 60%) except Meta. This is a well-spaced hue wheel — keep the hues, transform saturation/lightness per direction (Section 5). Color-group queries are `path:` prefixes with trailing spaces — trim before matching, match on `TFile.path.startsWith(folder + "/")`, first match wins (Obsidian semantics: top group wins).

---

## 1. Scene composition spec (Direction A baseline; B deltas in Section 4)

### 1.1 Background
- `scene.background = new THREE.Color(0x000003)` (NASA near-black; NOT pure black — pure black deadens bloom falloff). No fog. No vignette. No film grain. Premium = restraint.

### 1.2 Starfield
- One `THREE.BufferGeometry` per size class, 3 classes → **3 draw calls total**:
  - Class S: 2,600 pts, size 1.2px, `sizeAttenuation:false`
  - Class M: 900 pts, size 2.0px, `sizeAttenuation:false`
  - Class L: 250 pts, size 3.0px, `sizeAttenuation:false`
- Distribution: random on a spherical shell radius `6.5 × graphRadius` (graphRadius = bounding-sphere radius of laid-out graph, recomputed once after layout settles). Shell, not volume — effectively infinite, near-zero parallax (NASA treats stars as skybox).
- Colors: 85% cool whites lerp(`#9DA8C4`→`#FFFFFF`), 10% warm `#FFE9C9`, 5% blue `#BFD3FF`. Per-vertex color, `vertexColors:true`.
- Brightness: base opacity 0.55; ~3% of Class L stars get color × 1.8 (HDR-ish, they alone catch bloom → a few "real" bright stars). **[tune@gate]**
- Motion: entire starfield group rotates at 0.0008 rad/s around Y — alive but imperceptible. No twinkle shader in V1 (cost/benefit fails).
- Optional flourish (T3, default on, costs nothing): bias 30% of Class S points toward an inclined plane (tilt 28°, gaussian σ = 0.18 × shellRadius) → faint Milky Way band.

### 1.3 Nodes — "luminous orbs", not geometric spheres
The NASA look is a soft-edged glowing disc, which is exactly what a point sprite gives us. **Render all nodes as ONE `THREE.Points` with a custom ShaderMaterial (1 draw call for 3,225 nodes).** This is simultaneously the look AND the performance answer; predecessors died doing per-node meshes.

- Fragment shader (the entire node look lives here):
  - `d = length(gl_PointCoord - 0.5)`
  - hot core: `coreMix = smoothstep(0.18, 0.0, d)` → `color = mix(groupColor, white, coreMix * 0.55)` (white-hot center is what makes bloom read as "energy", not "blur")
  - edge: `alpha = smoothstep(0.5, 0.42, d)` (soft but defined rim)
  - per-vertex attributes: `color (vec3)`, `size (float)`, `dimFactor (float)` for focus mode.
- Size by degree: `radius = 2.2 * (1 + 0.5 * sqrt(degree))`, clamped to `[2.2, 13.2]` world units (6× cap so MOC hubs don't eclipse the scene). Multiply by user's `nodeSizeMultiplier` (0.93) read from graph.json. `gl_PointSize = radius * scaleFactor / -mvPosition.z`, `sizeAttenuation` manual in shader.
- **Proximity promotion pool**: `gl_PointSize` clamps at the GPU max (often 64–512px) — a node you fly right up to would shrink. Keep a pool of **32 `THREE.Sprite`s** (same radial texture baked to a 128px canvas) and each frame promote any node whose projected radius > 48px out of the Points layer (set its point alpha 0, position sprite). Selected + hovered nodes are always promoted. Pool = 32 draw calls worst case, usually <6.
- States:
  - **Hover**: additive halo `THREE.Sprite` (one shared, repositioned), scale 2.6 × nodeRadius, radial gradient groupColor → transparent, peak alpha 0.35, fade-in 120ms ease-out. Cursor: pointer.
  - **Selected**: halo sprite at 3.2 × radius, alpha 0.5, slow breathe: scale ×(1.0→1.06→1.0) sine period 2.4s. Core brightens: vertex color × 1.25.
  - **Focus mode (selection active)**: non-neighbors set `dimFactor → 0.12` (shader: `color = mix(bgColor, color, dimFactor)`, alpha × dimFactor max 0.35); 1-hop neighbors stay 1.0. Animate dimFactor over 280ms ease-out (CPU lerp into the attribute array, 3.2k floats — trivial). ESC / click-void restores over 280ms.
  - **Search match**: matched nodes flash to `--graph-node-focused` (fallback: `--interactive-accent`) ring halo for 1.2s, then fly-to the top hit.

### 1.4 Links — thin, desaturated, ONE draw call
**Hard requirement: all ~43,000 links in a single `THREE.LineSegments`** (one BufferGeometry: positions Float32Array 43k×2×3, colors 43k×2×4 RGBA). Per-link `THREE.Line` objects (3d-force-graph default) is forbidden — it is the documented cause of predecessor abandonment. Positions are rewritten from the force-engine output each tick; colors rewritten only on selection-state changes.

- Width: 1px (`LineBasicMaterial`; linewidth>1 is unsupported on most platforms anyway — and NASA trails are thin).
- Color: `mix(groupColor(src), groupColor(dst), 0.5)` then desaturate 60% and clamp luminance to ~0.35 → dusty, recedes behind nodes.
- Blending: **NormalBlending, global opacity feel 0.16** via per-vertex alpha (additive blending across 43k lines white-out at the hub core — tested failure mode). Settings toggle "link glow" switches to AdditiveBlending at alpha 0.05 for those who want it. **[tune@gate]**
- Depth: `depthWrite:false`, render order links < nodes (nodes always punch through the web).
- Highlight states (vertex-color rewrite, ~O(degree)):
  - selected node's own links: full-saturation groupColor, alpha 0.85
  - links between two neighbors: alpha 0.45
  - all other links in focus mode: alpha 0.04
- showArrow=false per user config: no arrowheads in V1 at all (they're visual noise at this density).
- T3 (settings-gated, default off): on selection, 40 transient particles run along the selected node's links (one shared Points buffer, 2s lifetime, then freed). Never ambient/global particles — that's the TagsRoutes look, not NASA.

### 1.5 Special node types
- **Unresolved** (shown, per `hideUnresolved:false`): ghost treatment — no hot core in shader (`coreMix×0`), alpha ×0.45, color `--graph-node-unresolved` (fallback `#7A8499`). Reads as "not yet real".
- **Orphans** (shown): normal rendering; force layout naturally drifts them outward — that IS their visual identity. No special styling.
- **Attachments / tags**: OFF by default (mirrors graph.json). If user enables in settings: attachments use a diamond sprite-mask variant, `--graph-node-attachment`; tags use a hollow-ring mask, `--graph-node-tag`. Implemented as a `shapeId` vertex attribute branching the fragment mask (still 1 draw call).

---

## 2. Post-processing chain

Built on `Graph.postProcessingComposer()` (3d-force-graph exposes its EffectComposer; it already contains the RenderPass).

**Pass order (Direction A, desktop High tier):**
1. `RenderPass` (provided by 3d-force-graph)
2. `UnrealBloomPass(new Vector2(w, h), strength, radius, threshold)`
   - **threshold: 0.10** — links at lum 0.35 × alpha 0.16 stay under; node cores and bright stars bloom.
   - **strength: 0.9** — exposed as the settings slider "glow strength" range 0–2.5, step 0.05, live-updating (`bloomPass.strength = v`). This is THE user-facing cinematic dial (NASA does exactly this).
   - **radius: 0.45** — tight glow; >0.7 reads as smear. **[tune@gate]**
3. `OutputPass` (three r152+ requirement: composer output bypasses renderer color-space conversion; without OutputPass everything renders washed-out linear).

- Tone mapping: `renderer.toneMapping = ACESFilmicToneMapping, exposure 1.05` in Direction A (filmic rolloff on bloom highlights). Caveat: ACES shifts hues slightly — acceptable in A (no theme-fidelity promise). If group colors drift objectionably at the gate, fall back to `NoToneMapping`. Direction B-light: `NoToneMapping` always (color fidelity on paper).
- Anti-aliasing: composer render target `samples: 4` (WebGL2 MSAA) on High tier; no FXAA pass (bloom already softens edges; FXAA blurs labels' canvas edge for nothing).
- **No OutlinePass.** NASA keeps it disabled until selection; our halo-sprite selection is cheaper and softer. OutlinePass at 3.2k objects re-renders the scene 2 extra times — cut entirely.
- **Cost notes / quality tiers:**
  - UnrealBloom ≈ 5 internal blur passes over mip chain; dominant cost is fill-rate at full res.
  - **High** (desktop default): bloom resolution = full, samples 4, pixelRatio = min(devicePixelRatio, 2).
  - **Medium** (auto-fallback): bloom resolution = `Vector2(w/2, h/2)` (half-res bloom is visually ~indistinguishable at radius 0.45), samples 0, pixelRatio ≤ 1.5.
  - **Mobile**: composer bypassed entirely — `renderer.render()` direct (Section 7).
  - Auto-tier watchdog: if mean fps < 45 sustained 5s → drop one tier, a notice saying "the quality was lowered automatically to keep it smooth"; a manual override in settings (high/medium/auto/mobile).
- Resize: on `ItemView` resize observer → `composer.setSize`, `bloomPass.resolution.set`, camera aspect update.
- Lifecycle: `onClose()` must dispose composer render targets, all geometries/materials/textures, call `renderer.dispose()`, `forceContextLoss()` is NOT needed but `webglcontextlost`/`restored` handlers are (Electron GPU resets) — predecessors leaked exactly here.

---

## 3. Camera choreography

All camera moves via our own tween wrapper (easeInOutCubic: `t<.5 ? 4t³ : 1-(-2t+2)³/2`) driving `Graph.cameraPosition(pos, lookAt, 0)` per frame — do not rely on the library's internal tween (we need interrupt/compose semantics).

### 3.1 Establishing shot (first impression — highest value moment)
1. Layout pre-warms hidden: run force engine (in worker) until `alpha < 0.05` or 1.8s wall-clock max; canvas covered by a `#000003` overlay div with a subtle pulsing "building the star map…" label.
2. Reveal: overlay fades 600ms; simultaneously camera **pulls back from inside the graph**: start at `0.5 × graphRadius` from centroid (inside the web, links streaming past), end at `2.2 × graphRadius`, elevation +18° above the dominant plane, duration **3.2s, easeInOutSine**. Bloom strength animates 1.6 → 0.9 over the same window (bright birth, settles to cruise).
3. Hand off directly into idle cruise (no dead stop).
4. Establishing shot plays only on view-open, not on every data refresh. Skippable: any pointer input cuts to final framing in 250ms.

### 3.2 Fly-to (search hit / double-click / the card's "focus")
- Target camera distance: `d = clamp(nodeRadius × 12, 40, 140)` world units — hubs framed wider, leaf notes intimate.
- Approach vector: keep the camera's current direction to the node but rotate **15° azimuth offset** so arrival never centers the node dead-on occluding its neighborhood; final `lookAt = node position`.
- Duration: `clamp(800 + 0.45 × travelDistance, 800, 1800)` ms, easeInOutCubic. Long jumps take visibly longer — that's the "travel" feel; never exceed 1.8s (impatience threshold).
- Interruptible: new input kills the tween at current position (no snap).
- On arrival: select the node (halo + focus dim + card).

### 3.3 Idle cruise
- Orbit around graph centroid: angular speed **0.022 rad/s** (~4.8 min/revolution — barely conscious motion).
- Organic drift, not a turntable: elevation oscillates ±8° (sine, period 90s); orbit radius breathes ±4% (sine, period 60s). Two incommensurate periods → path never visibly repeats.
- If a node is selected when idle kicks in: orbit the **selected node** at its fly-to distance instead (NASA "tracking an asteroid" feel), speed 0.03 rad/s.
- Pause: any pointerdown/wheel/touchstart. Resume: after **10s** of no input, easing from 0 → full angular speed over 2s (no jerk).
- Toggle: settings + the command "toggle cruise mode"; state shown as a small status icon in the view's action bar.

---

## 4. The two visual directions (Rick's preview gate)

Both ship behind one `preset: 'deep-space' | 'adaptive'` flag in settings + the command "switch visual direction" for instant A/B from the running vault. All preset-divergent values live in ONE tokens file (`src/render/presets.ts`) — nothing preset-specific hardcoded elsewhere.

### Direction A "deep space" — always-dark cinematic
Everything in Sections 1–3 as written. The view is a cinema pane: it stays deep space even when the app is in light mode (deliberate contrast, like a video player). UI chrome (cards/labels/toolbar) is dark glass regardless of theme: `background rgba(10,14,24,0.72)`, `backdrop-filter: blur(16px)`, border `1px solid rgba(255,255,255,0.08)`, text `#E8ECF6`. Node colors: group hexes used as-is (the hot-core shader supplies brilliance).

### Direction B "adaptive" — theme-adaptive
- **Dark mode**: identical code path to A (shared scene), bloom default 0.8, UI cards use Obsidian vars (`--background-primary`, `--background-modifier-border`) instead of fixed glass.
- **Light mode "the daylight drafting room"** — designed as ink-on-paper cartography, NOT a dimmed dark scene:
  - Background: warm paper `#F6F4EF`, with a barely-there radial grade to `#EDEAE3` at frame edges (6% — grounds the scene; this is the one place a vignette-like device is allowed).
  - **No starfield.** Replacement atmosphere: 600 "dust motes" — tiny `#D8D4CB` points, `sizeAttenuation:true`, drifting at 0.15 world-units/s with per-point phase → depth and life without faking night.
  - **Bloom OFF** (bloom on light bg = haze/dirt). Crispness replaces glow: node shader light-variant — no white-hot core; instead solid ink disc with a 1px darker rim (`rim = smoothstep` band at d∈[0.40,0.46], color × 0.75) and a soft contact-shadow halo sprite beneath hovered/selected nodes (multiply-blend, black 8% alpha, 2× radius) — nodes sit ON the paper.
  - Node colors: same 9 hues re-targeted for paper contrast — keep hue, S≈57%, **L 60%→44%**: 学习 `#B03030`, 工作 `#B08630`, 产品 `#86B030`, AI `#30B030`, 读书 `#30B086`, 人 `#3086B0`, 认真活着 `#3030B0`, Cubox `#8630B0`, Meta `#CC6300`. **[tune@gate]**
  - Links: ink construction lines — `#2E2A24` at alpha 0.10; highlight state = full saturated group color alpha 0.8 (colored ink over pencil — the selection moment is genuinely beautiful in light mode).
  - Selection emphasis (no bloom available): dual-ring halo sprite — inner ring groupColor alpha 0.9, outer ring `#1F2933` alpha 0.25 — plus the breathe pulse.
  - Labels `#1F2933` with paper-halo `text-shadow: 0 0 6px rgba(246,244,239,0.95)`; card = native Obsidian light card (`--background-primary` + `--shadow-s`).
  - Tone mapping: `NoToneMapping`. AA matters more without bloom: keep samples 4 even on Medium tier in light mode.
- Switch trigger: `workspace.on('css-change')` + `body.theme-dark` check. Transition: container fades to 0 over 180ms → swap scene tokens (background, materials' uniforms, starfield↔motes visibility, bloom on/off) → fade in 220ms. No WebGL teardown.

**The gate**: milestone M3 ships both presets toggleable live; Rick compares in his real vault (dark A vs dark B are near-identical — the real decision is "do I want a light mode at all, and is the daylight mode good enough to exist"). Decision recorded in WORKLOG.md; losing preset is kept behind the flag (cost ≈ one tokens object), so this is a default-choice gate, not a deletion gate.

---

## 5. Color system & CSS-var pipeline

- **Source of truth order**: (1) colorGroups parsed from `graph.json` via `vault.adapter.read(normalizePath(configDir + '/graph.json'))` in try/catch (undocumented format — on any parse failure fall back silently to step 2); (2) `--graph-*` CSS vars from `getComputedStyle(document.body)`: `--graph-node`, `--graph-node-unresolved`, `--graph-node-attachment`, `--graph-node-tag`, `--graph-node-focused`, `--graph-line`, `--graph-text`; (3) hardcoded fallbacks (`#9AA4B2` neutral / values above).
- Ungrouped nodes (60流浪, 80随记, 90故纸堆, 99Archive, Readwise, root files): `--graph-node` neutral — A: `#9AA4B2`, B-light: `#6B7280`. Neutral nodes get a weaker hot core (coreMix × 0.6) so the 9 colored constellations visually lead.
- Direction A consumes only `--graph-node-focused`/accent from the theme (search/selection highlight); B consumes the full var set. Re-read all vars on `css-change` (registered via `registerEvent` → auto-cleanup).
- Saturation policy: nodes carry full group color; **links carry the desaturated version** (Section 1.4); glow inherits node color automatically through bloom — never tint bloom separately (one global pass; per-color bloom is a selective-bloom trap, skip).
- Color updates on vault changes (file moved between group folders via `vault.on('rename')`): rewrite that node's vertex color in-place; no rebuild.

---

## 6. Label & selection-card system (DOM overlay)

In-canvas text is forbidden (NASA verdict + crisp CJK text needs DOM). Architecture:

- One absolutely-positioned overlay `<div class="sg-overlay">` covering the canvas, `pointer-events:none`; interactive children (card buttons) re-enable `pointer-events:auto`.
- **Tracked-node budget, hard cap 40 DOM elements**: selected (1) + hovered (1) + selection's neighbors (≤20, by degree if more) + persistent hub labels (top-14 nodes by degree; recomputed only on data change). Each frame: `vector.copy(node).project(camera)` → if `z>1` or offscreen → `display:none`; else `transform: translate3d(xpx, ypx, 0)` (GPU compositing, no layout thrash). 40 projections/frame is negligible.
- Label LOD (per label, evaluated per frame, all transitions via CSS opacity 150ms):
  - hide when projected node radius < 4px (too far to matter)
  - hub labels: alpha = `smoothstep(2.6R → 1.2R)` of camera-to-node distance (R = graphRadius) — far away only the brightest constellation names float, exactly the NASA far-view
  - neighbor labels appear only while a selection is active
  - font: `var(--font-interface)`; two discrete sizes only — 11px (far/neighbor), 13px (hover/selected/near) — continuous font scaling jitters, forbidden. Label text = basename without extension, ellipsis at 18ch.
- **Selection card** (single instance, reused):
  - Anchor: projected node position + 16px right offset; flip to left when overflowing; clamp 12px viewport margin; tracks node every frame while layout drifts.
  - Content: title (15px, 600 weight) · folder path with group-color dot (12px muted) · tag chips from `getAllTags(getFileCache(file))`, max 5 · the stats row "↩ N backlinks · → M outlinks · modified …" · snippet: first ~120 chars via `vault.cachedRead`, markdown-stripped, loaded async + cancellable (show skeleton 1 line meanwhile) · actions: "open note"(`workspace.openLinkText`, new leaf on mod-click) + "focus" (fly-to).
  - Styling: A = dark glass (Section 4); B = Obsidian-native vars. Enter animation: 160ms scale 0.96→1 + fade. Width 280px desktop.
- Mobile replaces the floating card with a **bottom sheet** (Section 7).

---

## 7. Mobile degraded tier — "premium small", not "broken big"

Trigger: `Platform.isMobile` (manifest `isDesktopOnly:false`).

**Keep (the soul):** `#000003` + starfield (1,200 pts, 2 size classes — Points are cheap everywhere) · establishing shot (shortened to 2.0s) · fly-to + idle cruise (cruise default ON — it's the showpiece on a phone) · 9-color groups + focus-mode dimming · search.

**The key trick — glow without bloom:** mobile skips the composer entirely, but the node fragment shader's white-hot core + soft falloff already reads as luminous, and the L-class stars keep their sparkle via the same baked falloff. 80% of the look survives at 0% postprocessing cost.

**Cut/adapt:** no EffectComposer (direct render, no OutputPass needed) · pixelRatio = min(dpr, 1.5) · MSAA off · link budget: render top 12,000 links ranked by `min(degree(src),degree(dst))` descending (hub structure survives; hairline periphery goes) — selected node's links always force-included · promotion pool 8 sprites · hub labels top-6, neighbor labels ≤8 · selection card = bottom sheet (`max-height 40vh`, drag-down dismiss, same content minus snippet) · tap = select, double-tap = fly-to, pinch/two-finger = library default orbit controls.
Direction B light-mode applies identically on mobile (it never had bloom to lose).

---

## 8. Render architecture (the interface that keeps us alive)

- `LayoutEngine` interface (positions in, tick events out) — d3-force-3d first on main thread (M1), then moved into a Web Worker via inlined Blob URL (M2); force params seeded from graph.json (`repelStrength 14.8 → charge ≈ -14.8×k`, `linkDistance 264 → scaled to scene units`, `centerStrength 0.73`) so the 3D layout's "shape" feels familiar from day one.
- `GraphRenderer` interface with two implementations: `LibRenderer` (stock 3d-force-graph objects — dev/debug only, link-capped) and `AggregateRenderer` (Section 1: 1×Points nodes + 1×LineSegments links + sprite pools) — **AggregateRenderer is the V1 ship target, not a contingency**, because 43k per-link objects is a proven killer.
- Total V1 desktop draw-call budget: 3 starfield + 1 nodes + 1 links + ≤34 sprites + bloom internals ≈ **<45 calls**. The debug command "show the performance panel" overlays fps / `renderer.info.render.calls` / triangles / tier — this is also how every visual milestone gets verified.

## 9. Milestone verification (per Rick's protocol)
- M1 walking skeleton: manual — view opens, graph visible, no console errors; automated — color-int decode + group-matching unit tests.
- M2 AggregateRenderer + bloom: perf panel shows ≥55fps sustained 60s in the real 3.2k/43k vault, draw calls <45; manual checklist: hot cores visible, links recede, no hub white-out.
- M3 **preview gate**: both presets live-toggleable; Rick runs 10-minute side-by-side script (open → establishing shot → search for a folder name → fly-to → hover 5 nodes → select hub → focus dim → idle 30s) in dark AND light app theme; picks default. 
- M4 labels/cards/choreography: manual checklist per Section 3/6 behaviors; automated test for projection math + LOD thresholds.
- M5 mobile: same script on iPhone via iCloud-synced vault, target ≥30fps.

---

### Critical Files for Implementation
(repo not yet created; `<repo>` = `/Users/rick/Claude_Code/<plugin-id>` — plugin id TBD, must not contain "obsidian")

- /Users/rick/Library/Mobile Documents/iCloud~md~obsidian/Documents/Rick's Second Brain/.obsidian/graph.json — live input: colorGroups, force params, display toggles (read-only, best-effort parse)
- `<repo>`/src/render/presets.ts — single source of ALL visual tokens for Direction A "deep space" and B "adaptive/daylight" (colors, bloom params, blending, label styles)
- `<repo>`/src/render/AggregateRenderer.ts — Points node shader, LineSegments link buffer, halo/promotion sprite pools, focus-mode attribute animation
- `<repo>`/src/render/PostFX.ts — composer chain (RenderPass→UnrealBloom→OutputPass), quality tiers, watchdog, resize/dispose lifecycle
- `<repo>`/src/camera/Choreographer.ts + `<repo>`/src/overlay/LabelLayer.ts — establishing shot, fly-to, idle cruise; DOM label/card projection loop

## KEY DECISIONS
- Render all 3,225 nodes as ONE THREE.Points draw call with a custom radial-falloff shader (white-hot core + soft rim), and all ~43k links as ONE LineSegments buffer — the AggregateRenderer is the V1 ship target, not a later optimization. — 3d-force-graph creates a THREE.Line object per link; at 43k links that is exactly the wall every predecessor plugin died on. The point-sprite falloff is simultaneously the NASA 'luminous orb' aesthetic and the perf answer (total scene <45 draw calls), and it gives mobile glow without any postprocessing.
- Post chain is exactly RenderPass → UnrealBloomPass(threshold 0.10, strength 0.9 user slider, radius 0.45) → OutputPass; no OutlinePass, no FXAA, no fog/vignette/grain. — Verified NASA Eyes recipe: bloom-as-user-slider plus restraint. OutlinePass re-renders the scene twice for an effect a halo sprite does better; OutputPass is mandatory in three r152+ or composer output is washed-out linear; low threshold lets node cores bloom while 0.16-alpha desaturated links stay quiet.
- Links use NormalBlending at ~0.16 alpha (additive offered only as an opt-in toggle), colored as 50/50 endpoint-group mix desaturated 60%. — 43k additive lines accumulate to white-out at the vault's dense hub core — a known failure mode. Desaturated normal-blend lines recede behind nodes, matching NASA's thin desaturated trails, while selection rewrites vertex colors to full saturation for contrast where it matters.
- Direction B light mode is a designed 'ink-on-paper cartography' scene (warm paper #F6F4EF, dust motes instead of stars, bloom OFF, rim-inked nodes at L44%, pencil-line links) — not a brightened dark scene. — Rick's protocol demands two genuinely distinct previewable directions; bloom on a light background reads as haze, so the light treatment must derive its premium feel from crispness (rims, contact shadows, colored-ink highlights) rather than glow.
- Both presets live behind one tokens file (src/render/presets.ts) and a runtime toggle command, shipped together at milestone M3 as the formal preview/decision gate. — Rick (PM, decides by looking at running versions) must compare deep space vs adaptive inside his real 3,225-note vault in both app themes; keeping divergence confined to one tokens object makes the dual build nearly free and the losing preset cheap to keep.
- Camera choreography: 3.2s inside-out pull-back establishing shot with bloom settling 1.6→0.9, fly-to distance = clamp(nodeRadius×12, 40, 140) with 15° azimuth offset and distance-proportional 0.8–1.8s easeInOutCubic, idle cruise at 0.022 rad/s with incommensurate ±8°/±4% drift periods, pause-on-input/resume-after-10s. — First impression carries the 'stunning' verdict; concrete numbers (speeds, clamps, easings) remove implementer taste from the loop, and the two incommensurate drift periods make the cruise path never visibly repeat — the difference between 'screensaver' and 'spacecraft'.
- Labels and the selection card are DOM overlay elements with a hard 40-element budget (selected + hovered + ≤20 neighbors + top-14 hubs), projected per frame via translate3d, two discrete font sizes only. — Verified NASA pattern (DOM labels, not in-canvas text); DOM gives crisp CJK rendering and Obsidian CSS-var theming for free; the fixed budget keeps per-frame projection cost negligible and prevents the label-soup failure of the core graph at 3k nodes.
- Decode and consume the user's existing graph.json (9 colorGroups verified to exact hexes #D65C5C…#E66F00, nodeSizeMultiplier, force params, showTags/showAttachments/hideUnresolved flags) as the default visual state, with --graph-* CSS vars and hardcoded values as fallback chain. — Rick's mental map of his vault is already trained on these 9 hues and display choices; inheriting them makes the 3D view feel like 'my graph, ascended' on first open rather than a foreign tool, and the try/catch fallback chain absorbs the undocumented-format risk.
- Mobile tier skips the EffectComposer entirely and relies on the shader-baked hot core for glow, caps links at top-12k by hub importance, halves star count, and swaps the floating card for a bottom sheet. — Postprocessing fill-rate is the dominant mobile cost; because the node look lives in the fragment shader rather than in bloom, ~80% of the aesthetic survives at zero postprocessing cost, keeping the degraded tier 'premium small' instead of 'broken big'.

## RISKS
- Hub white-out / visual mud at the vault's dense core (43k links converging): even with normal blending, the center may clip to a blob under bloom. Mitigation: low link alpha (0.16) + bloom threshold 0.10 are chosen against this; verify at M2 with the REAL vault (not synthetic data), and keep a per-link alpha attenuation by hub degree as a ready fallback knob.
- GPU max point size clamps node sprites when the camera flies close, making near nodes shrink — would silently ruin every fly-to arrival. Mitigation: the 32-slot proximity promotion pool (Points→Sprite swap above 48px projected radius) is specced as core behavior, not an extra; test on Intel iGPU where MAX_POINT_SIZE is smallest.
- ACES tone mapping shifts the 9 group hues, breaking the 'my colors, ascended' promise in Direction A. Mitigation: tone mapping is a single preset token; the M3 gate script explicitly compares scene colors against the 2D graph, with NoToneMapping as the one-line fallback.
- graph.json format is undocumented and may change in any Obsidian release (currently 1.13.1), silently dropping Rick's color groups. Mitigation: best-effort try/catch parse with full hardcoded fallback of the decoded palette baked into presets.ts, plus a settings notice when inheritance fails; never crash the view on parse errors.
- Per-frame attribute uploads (43k×2 link positions every tick) could bottleneck on bufferSubData bandwidth, capping fps below 55 on older desktops. Mitigation: stop uploading once layout alpha settles (static graph = zero uploads), mark buffers DynamicDrawUsage, and the auto-tier watchdog degrades bloom before the experience degrades.
- Direction B light mode may simply look worse than A no matter the polish, wasting build effort. Mitigation: B-dark shares ~95% of A's code path, so the incremental cost is only the light-token set and the no-bloom node shader variant; the M3 gate is explicitly allowed to conclude 'A always, B never ships as default' while keeping B behind the flag.
- Memory/context leaks on view close or Electron GPU resets (the documented sin of predecessor plugins). Mitigation: disposal checklist is part of M2's definition of done — composer render targets, geometries, materials, textures, worker termination, webglcontextlost/restored handlers — verified by open/close cycling 20× while watching renderer.info.memory in the perf panel.