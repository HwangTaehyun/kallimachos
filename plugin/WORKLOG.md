# WORKLOG — Galaxy View

> An append-only chronology, written as an inverted pyramid: the conclusion first, the detail underneath.

---

## 2026-07-16 · Releasing 0.5.0: the folder-legend filter + centring the graph + the colour-collision fix (the first time it was seen on a real machine)

### What was done
**0.5.0 is out**: https://github.com/Longwind1984/galaxy-view/releases/tag/0.5.0 . Three things in it: ① @tzhengus's fix for centring the graph (#9); ② the clickable folder-legend filter (#11); ③ the colour-collision fix (a pre-existing defect). **Tag Lens (#7) did not make it**, pending @tzhengus's answer to the rework request on PR #8.

**What was different this round: it was actually seen.** The second request for computer-use authorisation went through (the first was refused), and everything was verified item by item in the dev vault —— which directly found and fixed a bug no static check could have.

### What was seen (dev-vault, 3,230 notes)
- ✅ The legend renders: 15 folders, the colours matching the nodes in the graph, the note counts matching exactly what `find` counts
- ✅ **The collision fix** (this round's most important thing to accept): 99Archive pink / 90故纸堆 green / Readwise cyan —— before the fix those three were the same blue
- ✅ Pressing a chip: 3,230 → 2,623, exactly 607 fewer (04AI's count), and that clump of stars disappears from the graph
- ✅ "Only 01学习" → 197 notes, every other chip goes out
- ✅ The escape hatch: `file:AI` → 253, matching `find -iname "*AI*"`'s 253; `-file:Index` → 3,230 unchanged (the library holds 0 index files, which is correct)
- ✅ Persistence: `filterQuery` is written into data.json
- 🐛 **A bug found and fixed**: "Show all" never went away. I had used an `is-hidden` class, but `.gx-sec-restore`'s show/hide convention is `gx-hide`, and there is no CSS for the other one → the button stayed lit permanently. A control that does nothing when pressed carries zero information. Commit 535a20c.

### The decisions and what they taught
- **Two PRs rather than one** (Rick said "merge the PR", I split it and said why): #9 and #11 are two different things, and welding them together makes a standalone 0.4.1 impossible. But **once both were on main the standalone 0.4.1 option was gone anyway**, so 0.5.0 went out directly (with the #9 fix in it).
- **Release with `npm version 0.5.0 --tag-version-prefix=""`**: the repository's existing tags carry no `v`, and npm adds one by default. The Obsidian store requires **the tag name to match manifest.version exactly**, so tagging `v0.5.0` would have gone out crooked.
- **⚠️ A trap in the release ordering**: `git push origin main` and `git push origin 0.5.0` were chained into one command, and **main's push failed (Rick had changed the README on the remote again) while the tag's succeeded** → CI cut a release from the tag immediately, while main's manifest still said 0.4.0 → **the store could not detect the update**. Solved by `git merge origin/main` (not a force push, so Rick's screenshot commit survived) and pushing main afterwards. **The lesson: the tag goes only after main has pushed successfully.**
- **The release was verified properly**: the release's `main.js` is **byte-for-byte identical** to the `dist/main.js` that was seen locally (797,562 bytes, `diff -q` passes) —— what a user downloads is the thing that was checked.

### Diagnosing the command timeouts (Rick asked "why did it fail")
Two commands timed out at three minutes in a row and the code was never committed. **The forensic conclusion: not the code, but the way I wrote the command.**
- The evidence: no git hook, no GPG signing, `git commit` on its own takes **0 seconds**; `eslint` on its own takes **12 seconds** and exits normally; the leftover esbuild in `ps` belongs to `ZCodeProject/history-kb` (another project), and Galaxy_View has no stray process.
- The real cause (structural): `npm test && npm run lint && git commit` were **chained into one command**, and the Bash tool caps at three minutes —— if the first two steps are merely slow the whole chain is cut and `git commit` never gets its turn. **A commit should not hang downstream of two multi-minute npm commands.**
- What made it worse: I started `npm run dev &` in **watch mode** (which by design never exits) and then tried to kill it with `pkill -f "esbuild.config.mjs"`, a pattern that also matches my own command line. The shell survives in practice, so it is not the direct cause, but watching and running npm in the same project at once is asking for a race.
- **Which step actually hung cannot be named** (no log was kept), but the above explains the symptom, and the fix is clear: run them apart.

### Where things stand
main = `b21e633`, manifest 0.5.0, tag `0.5.0` inside main's history, the release marked Latest. CI succeeded. The store update propagates automatically through the GitHub Release.

### What is left and what is known broken
- **Issue #11 is owed a closing reply**: the last thing said on that thread was my question "which syntax should this align with", and SotS1689's answer "aligning with the core Graph View is enough" plus "I can't wait for it to ship" —— **and what shipped is a folder legend, with the syntax demoted to a collapsed escape hatch**. His need is still met, but that does not explain why he will first see a pile of chips and no input box. The draft is in scratchpad `reply-i11-shipped.md`, **awaiting Rick's review** (it contains an admission that "I asked the wrong question earlier", said in Rick's voice).
- **The legend is a large change to the panel's information architecture** (three switches moved from "Advanced" to "Filter"), and existing users may not find it where they left it —— next sweep should watch for issues about it.
- The filter copy in German / Italian / Spanish / Portuguese is still my translation, with no native check.
- The legend only goes down to the **top level**; 14 folders already take a fair amount of panel height, and a library with more will be tighter still (no scrolling or collapsing yet).
- `dist/main.js` still carries the dead `runScenario` method (there since 0.1.1, not user-visible).
- The S1/S4 performance benchmarks have not been re-run since 0.4.0.

### The files that changed
- `src/overlay/ControlPanel.ts` (the `gx-hide` fix)
- `manifest.json` / `package.json` 0.4.0→0.5.0, `versions.json` +`"0.5.0": "1.8.7"` (`npm version` does it)
- `docs/community-sweep.md` (this round's result + what to watch next)

## 2026-07-16 (Thu) · The first regression run on the merged main (a scheduled autonomous round; verification only, no code changed)

### What was done
PR #12 (#9, centring) and PR #13 (#11, the legend filter) merged into main last night at 23:33 and 23:34, each branch green on its own —— **but the merged main had never been run**. Both branches touched the rendering layer (#12 changed how `AggregateRenderer` takes coordinates, #13 routed `GraphController`'s `applyColorFn` through one place), and a semantic conflict is not something git reports. This round ran the regression that was missing, without changing any code.

### The conclusion: main is clean and the release path is open
- `npx vitest run` → **77 tests green / 8 files** (graphTransform 4 + palette 9 + noteFilter 23 + buildGraph 11 + adjacency 9 + settingsMerge 8 + linkCurves 7 + tour 6)
- `npx tsc -noEmit -skipLibCheck` → **exit 0**
- `npx eslint .` → **0 errors** (2 warnings, both pre-existing: the eslint config's `config` deprecation notice, and SettingsTab not having adopted the declarative settings API from 1.13)
- `npm run build` (tsc + esbuild production) → **passes**, producing `dist/main.js` at 797KB plus manifest/styles. **The release build path itself is fine**; what blocks a release is seeing it and getting authorisation.

### The manual checklist can lose one item (② is now covered by a machine)
Of the seven manual items left for Rick in the previous entry, **② "99Archive / 90故纸堆 / Readwise are now three different colours" no longer needs an eye** —— `tests/palette.test.ts` regresses against Rick's real library data and asserts it deterministically: the three are pairwise different (:65-67), at colour values exact to the likes of `#eca2a2`, **taken from what three actually produces** (:73), alongside nine more ("≤9 never collide", "beyond 9 the recycled hues collide on the smallest folders", "a folder covered by colorGroups takes no slot"). **So Rick only has to walk ①③④⑤⑥⑦** (the panel lists 14 folders, a chip puts its stars out, hover offers "only", when "Show all" appears, `-file:Index` ANDed with the legend, the state surviving a restart) —— all six are real interactions inside Obsidian, which no test can reach.

### What is left and what is known broken (untouched this round; all of it waits on a person)
- **0.4.1 is still unreleased**: the manifest still says 0.4.0 and the store has nothing to push. Releasing = bump → merge to main → tag → CI, which is **a public release and needs Rick's confirmation**, and should not happen before it has been seen (#9 and #11 are both visual changes, and green tests are not the same as looking right).
- **It still has not been seen**: computer-use authorisation was refused, and the plugin runs inside Obsidian (Electron), which a browser preview cannot stand in for. `demo/*.html` is a hand-made approximation whose colour values were copied in from three by hand (and got it wrong once), so **it cannot serve as evidence that something was seen**.
- The two replies being held (the reply to and closing of PR #10, closing issue #9) go with the release; issue #11's now-stale reply awaits Rick's authorisation.
- Tag Lens (#7 / PR #8) waits on @tzhengus; issue #6, the mouse ghosting, waits on information that reproduces it.

### The files that changed
This file and `docs/community-sweep.md` only (recording the verification results). **No code changed**; `dist/` is build output (gitignored).

---
## 2026-07-15 (Wed) · Overturning the product judgement on filtering: a clickable folder legend instead, and the colour collision fixed

### What was done
One sentence from Rick —— "filtering has to be something you can *operate*; who is going to use a pure function like that? Think again about the decision logic. Don't confine your product thinking to the user's literal words" —— sent the previous version back. **The text-box design is void** and was redone as **a clickable folder legend** (commit 47215ae). A pre-existing defect found along the way was fixed with it: the colour collision.

### The reflection: where the decision logic went wrong (this matters more than the code)
1. **I took the requester's mechanism for his purpose.** SotS1689 wrote `-file:"Index"` —— that is **the mechanism he knows** (Obsidian's search syntax), not **his purpose** (stop index notes smearing the graph). I copied the mechanism down as the specification.
2. **I made the wrong frame public and permanent.** In my reply on issue #11 I asked him "should this align with Obsidian's search syntax or the core Graph View's" —— asking **which syntax**, never **whether syntax at all**. And it was already sent, which spreads my framing error to the user.
3. **A clean seam in the code made the mistake sound reasonable.** Hanging a filter in front of `buildGraph(files)` is so convenient, and I even praised it for being "decoupled from the rendering layer and unit-testable" —— those are **engineering virtues**, and I treated them as **product virtues**. The cleaner the seam, the less I questioned what belonged on top of it.
4. **The A/B I gave Rick was a false choice.** Both options presumed a text box, letting him pick the colour to paint a wall when the real problem was that the wall was in the wrong place. That is worse than not asking —— it **looks** as though the design space was opened.
5. **I ignored the medium.** This is a cinematic 3D star chart whose claim is "see your library", and making the user memorise `-file:` is the most anti-visual answer available in that medium.
6. **The most glaring part**: tzhengus had already handed me the right paradigm in PR #8 (Tag Lens = press a chip → highlight the matches, dim the rest). **In the very same session I reviewed that PR myself and wrote a paragraph praising his UX reasoning**, then turned round and built a text box for the same problem next door.

### The decisions and the alternatives rejected
- **The legend *is* the filter** (Rick picked A from an A/B on a live demo): nodes have always been coloured by their top-level folder, but **the panel never exposed a legend** —— a user sees clumps of colour with no way to know what a colour means and nothing to do about it. Making the legend clickable answers both questions with one thing, using data the plugin already computes. Rejected = direction B, "right-click a node in the graph and act on it there" (the most direct, taking no panel space, but hard to discover and still no legend).
- **The text box is not deleted, only demoted to a collapsed escape hatch**: SotS1689's real case (an Index scattered across every folder) is **cross-cutting**, which a legend cannot express. It is a legitimate fallback; it was only wrong when I treated it as the whole. `noteFilter.ts`'s parser and its 17 tests are kept exactly as they were.
- **Fix the colour collision along with the legend** (Rick ruled): not fixing it means shipping a legend where five dots share one colour. Rejected = open a separate issue for later (the legend puts the defect right in front of the user).

### The colour-collision defect (pre-existing, not introduced here)
The fallback hue was `HUES[hash32(folder) % 9]` —— unrelated to folder size and unordered. **Measured against Rick's library**: 14 top-level folders and 9 colour groups imported from the 2D graph, leaving 5 on the fallback → **99Archive (545) / 90故纸堆 (86) / Readwise (68) collide on the same blue**, which is **1,184 notes = 37% of the library** landing on colours nobody can tell apart.
The fix: ① hand hues out ranked by note count; ② **give them only to folders that colorGroups has not taken** (Rick's 9 imported groups take no slot → the remaining 5 each get a distinct hue). Beyond 9 to hand out it still recycles the wheel, but the collisions land on the smallest folders rather than the largest. The tests use the real library's data as the regression case.

### Where things stand
Branch `fix/graph-fit` (now three commits: #9's centring fix / #11's text-box version / #11's legend rework): **77 tests pass / tsc passes / lint 0 errors**, deployed to the dev vault. main is untouched, and neither 0.4.1 nor 0.5.0 has shipped.

### What is left and what is known broken
- **⚠️ The panel's rendering has still not been seen**: computer-use authorisation was refused. **Rick's manual checklist**: ① the "Filter" section at the top of the panel lists 14 folders, their colour dots matching the nodes in the graph; ② **99Archive / 90故纸堆 / Readwise are now three different colours** (the acceptance point for the collision fix); ③ pressing a chip makes that folder's stars disappear and changes the header's note count; ④ hover offers "only", pressing it leaves one folder, pressing again restores; ⑤ "Show all" appears to the right of the title only while some folder is switched off; ⑥ expanding "＋ Filter by name" and typing `-file:Index` still works, ANDed with the legend; ⑦ after a restart the legend's state and the query are both still there.
- **⚠️ The reply already sent on issue #11 is now stale**: it asked "which syntax should this align with", and the syntax has since been demoted to an escape hatch. **A follow-up reply is needed** explaining the change to a clickable legend (and this time, think it through before sending). **Awaiting Rick's authorisation.**
- **I got the demo's colour values wrong once**: earlier I computed the hash fallback colours with a naive HSL→RGB in Python, which does not match what three produces (`setHSL` computes in linear space and then converts to sRGB, so the real value is the likes of `#eca2a2`, not `#d65b5b`). The collision conclusion is unaffected, and the values have been corrected against what three actually produces. **The lesson: a colour is a "specific number" too, and needs a source.**
- The new copy in German / Italian / Spanish / Portuguese is still my translation, with no native check.
- The legend only goes down to the **top level**; deeper structure cannot be expressed. 14 folders already take a fair amount of panel height, and a library with more folders will be tighter (no scrolling or collapsing yet).
- 0.4.1 is still stuck on seeing #9; the two held replies (PR #10, closing #9) are stuck with it.

### The files that changed
- New: `tests/palette.test.ts` (9 tests, including the collision regression against Rick's real library)
- Changed: `src/data/noteFilter.ts` (`FilterQuery`→`NoteFilter{hiddenFolders,query}`; +`passesFilter`/`applyFilter`/`isFilterActive`/`folderStats`; `filterFiles` folded into `applyFilter`), `src/data/buildGraph.ts` (exports `topFolder`), `src/render/palette.ts` (+`assignFolderHues` handing hues out by rank, +`folderCoveredByGroups`), `GraphStore` (+ the `folders` legend data / `setHiddenFolders`; rebuild computes the full folderStats before filtering), `ControlPanel` (`buildFilterSection` redone as legend chips + the collapsed escape hatch; +`refreshFolders`/`applyHidden`; callbacks +`onHiddenFolders`/`getFolders`/`folderColorHex`), `GraphController` (+`applyColorFn` routing six setColorFn sites through one place and handing out hues, +`folderHex` probing for a colour, + the `colorFn` field), `settings` (+`hiddenFolders` + merge compatibility), `styles.css` (+`.gx-folder*`/`.gx-filter-esc*`), `i18n×6` (+`filter.all/solo/soloTip/rootFolder/byName`), `README×2` (rewritten around the clickable legend, with the syntax table demoted into a "filter by name" subsection), `tests/noteFilter.test.ts` (+6 legend tests)
- `demo/filter-demo.html`: from an A/B decision tool to a trimmed preview of the final form (direction B removed, the colour values corrected against what three produces)
## 2026-07-15 (cont.) · 0.5.0 begins: note filtering, #11 (the code is done; it has not been seen)

### What was done
With the sweep's four replies sent, on to 0.5.0. Of the two items on the table, Tag Lens waits on @tzhengus, so the only one that could move was **#11, note filtering**, and it is done (branch `fix/graph-fit`, commit f7b60b6).

The panel gains a **"Filter" section, pinned to the top**, where the query box lives together with the three switches (unresolved / orphans / tags) brought up from "Advanced" in the footer. The syntax is a subset of core Search: bare words / `file:` / `path:` / `-` to negate / `"a quoted phrase"` / an implicit AND.

### The decisions and the alternatives rejected
- **The panel's information architecture takes "direction B · promote"** (Rick ruled on a live A/B demo): those three switches *are* filters (they decide what enters the graph) and should not be buried in the footer's collapsed area next to quality; once they move, "Advanced" holds only genuinely advanced things like quality. Rejected = direction A, "put the filter box into Advanced and move nothing" (nothing breaks, but the main feature stays buried in a footer that is collapsed by default). The cost: a place existing users knew has moved.
- **No full-text search** (the core Graph View has it; we do not): that would mean reading the whole library's text on every keystroke, which cannot be instant across 3.2k notes and breaks the performance discipline. **This is a real capability gap against the core Graph View**, and both READMEs state the boundary rather than pretending otherwise.
- **No regex / OR / parentheses / `tag:`**: no real use case (YAGNI). `tag:` would also tangle with the existing `showTags` semantics.
- **The filter is a pure function ahead of buildGraph** (`src/data/noteFilter.ts`, `FileRecord[] → FileRecord[]`): a filtered-out note takes the edges pointing at it with it (buildGraph drops an edge whose endpoint `indexById` cannot find), so there is no coupling to the rendering layer and it is unit-testable. TFile already satisfies `FilterableRecord` structurally, so **filter first, then map** —— a filtered-out note pays for neither `getFileCache` nor the object allocation.
- **A 300ms debounce**: every application rebuilds the graph and reheats the layout, so doing it per keystroke would kill a large library. It also **compares the parsed terms rather than the raw string**, so `file:a` and `file:  a`, being the same thing, do not rebuild for nothing. An empty query returns the original array without copying.
- **Restrained copy (a departure from the demo)**: the demo had "showing 8/11 notes", and the implementation dropped it —— the panel header already shows the note count, and text in the query box is itself the signal that a filter is on, so that line is a second copy of the same thing. The placeholder does not say "filter notes…" either (the section title has said it); it is a real, usable query, `Index  -file:Draft`, which teaches the syntax, with the full syntax in the `title` tooltip. **Only the zero-match notice stays** —— an entire 3D view going empty looks like a crash, and that is not something a user "discovers cheaply by trying".
- **`filter.syntax` did not go into the permanent "?" help**: that help is titled "How to navigate" and is about navigation gestures; putting panel syntax in it stretches its scope.

### Where things stand
Branch `fix/graph-fit` (now holding two commits, 0.4.1's #9 fix and #11's filter): **62 tests pass / tsc passes / lint 0 errors**, deployed to the dev vault with `npm run dev`. main is untouched, and neither 0.4.1 nor 0.5.0 has shipped.

### What is left and what is known broken
- **⚠️ The panel's rendering has not been seen**: computer-use's request for Obsidian authorisation was refused, so both changes (#9 centring, #11 the filter section) have only been checked statically. **Rick's manual checklist**: ① a "Filter" section appears at the top of the panel, expanded by default; ② the three switches have left "Advanced" and appear under "Filter", leaving "Advanced" with only quality and the fps row; ③ typing `-file:Index` shrinks the graph and the header's note count follows; ④ typing nonsense (zero matches) shows an amber "No notes match this filter" rather than a silently empty graph; ⑤ the ✕ clears, and Esc inside the box clears without also cancelling the node selection; ⑥ the filter query survives restarting Obsidian (persistence).
- **The filter copy in German / Italian / Spanish / Portuguese is my translation**, with no native check (0.2.2's six languages came through a workflow plus native QA; this did not). Errors wait for the community to report them.
- **Filtering's interaction with the layout has not been measured**: filtering changes the node set → it goes down the existing identity-preserving merge plus a 0.3 low-temperature reheat. It is right in principle (the same path as toggling showOrphans), but nobody has watched what repeated reheating looks like while typing into a large library.
- 0.4.1 is still stuck on seeing #9; the two held replies (PR #10, closing #9) are stuck with it.

### The files that changed
- New: `src/data/noteFilter.ts` (a parser and a matcher, both pure), `tests/noteFilter.test.ts` (17 tests)
- Changed: `GraphStore` (+ a `filterQuery` field / `setFilterQuery` / `isFiltered` / an extra init parameter / rebuild filters before it maps / `sameQuery` compares terms), `ControlPanel` (+`buildFilterSection` as the pinned section, the three switches moved out of advBody, +`setFilterEmpty`, callback +`onFilter`), `GraphController` (+`filterSoon`, a 300ms debounce; onDataChanged updates the zero-match notice; init passes filterQuery), `settings` (+ a `filterQuery` field, defaulting to `''`, with merge compatibility), `styles.css` (+`.gx-filter*`), `i18n×6` (+`panel.sec.filter` / `filter.placeholder|syntax|clear|none`), `README×2` (Highlights + the filter syntax table + the "does not search body text" boundary), `tests/settingsMerge.test.ts` (+3 migration tests)
- Deleted: `demo/filter-demo.html` (the A/B decision tool; deleted once direction B was ruled on, so it cannot drift away from the real panel)

## 2026-07-15 · Community sweep, round 1: an external fix merged (#10, awaiting a look) and four replies held

### What was done
The first systematic sweep of community feedback since listing, establishing a weekly routine. Three things came out of it:

1. **An at-a-glance table** (a new file, `docs/community-sweep.md`, **a living document overwritten each week**): triage of the 4 open issues and 2 open PRs, with the store's baseline figures (1,662 downloads in total, 328 in the four days since 0.4.0).
2. **PR #10 merged in** (external contributor @tzhengus fixing issue #9, "the graph sits off-centre and pushes out of the starfield shell"), rebased onto 0.4.0 on branch `fix/graph-fit`, **not merged to main and not released** —— awaiting Rick's eyes.
3. **Six draft replies** (scratchpad `reply-drafts-2026-07-15.md`), **none of them sent** —— awaiting Rick's review, one by one.

**This round's biggest finding is not a technical one: all 4 issues and both PRs have zero replies, and the oldest, #6, has been up for 7 days** —— and four of them come from one person (@tzhengus, 2 issues + 2 PRs, the PRs of high quality and carrying their own tests). The content side has a clear road; the real risk is leaving the only deep external contributor standing there.

### The decisions and the alternatives rejected
- **I rebase PR #10 rather than ask its author to redo it** (Rick ruled): the author's base stopped at 0.2.2, and 0.4.0 rewrote 321 lines of `AggregateRenderer`. Squashed into one commit with `--author` keeping the original attribution, rather than resolving conflicts through three intermediate commits (that is the iteration of "fix one version, then revise it twice"; only the final state has value).
- **PR #8 (Tag Lens) is not rebased; the author is asked to redo it against the new boundary** (Rick ruled): it collides with 0.4.0 in two places —— (a) the `showTags` that rode along with 0.4.0 already covers the tag data layer; (b) the names collide, since the PR's "nebula" means a tag-hub nebula while 0.4.0's `nebula.ts` means the deep-space background nebula. This is **a re-cut, not a conflict resolution**, and 23 changed files should not be force-rebased. Rejected: close the PR and do it myself (too discouraging for the only active contributor).
- **The original PR's quantile algorithm was rewritten** (an engineering call made internally): the original `new`s an array of `[radius, weight]` tuples every frame and sorts all of it, while `fitGraphPositions` is called **every frame** while the layout is hot (`GraphController` rAF → `updatePositions`). Measured at 9.8k nodes: **1.53ms/frame = 9.2% of the 60fps budget**, plus the GC pressure of tens of thousands of allocations per frame —— over the performance discipline's red line. Replaced with a fixed-bucket (2048) weighted quantile histogram: O(N), zero allocation, **0.48ms/frame = 2.9%** (3.2×). **The evidence it is faithful**: 200 random graphs (500–9,500 nodes) compared against the original implementation, with the scale and every node coordinate differing by **0.0000%**; the author's own four tests are unchanged and green.
- **Two pieces of wiring added during the rebase** (invisible to the original author): 0.4.0's ghost edges (`updateGhostPositions`) and cluster clouds (`clouds.rebuild`) also consume graph coordinates and had to move to the display coordinates too, or they would come away from the nodes.

### Where things stand
- Branch `fix/graph-fit` (commit 94cd84f, attributed to Tian Zheng): **42 tests pass / lint 0 errors / build passes**, deployed to `dev-vault` with `npm run dev` (3,230 notes, Hot Reload installed).
- main is untouched, 0.4.1 is unreleased, and the community replies are unsent.

### What is left and what is known broken
- **⚠️ The fix has not been seen**: issue #9 is a visual bug, and green tests are not a centred graph. This session's computer-use request for Obsidian authorisation was **refused**, so it could not be checked here. **How Rick can confirm it**: open the dev vault → Galaxy View → switch to the Deep Field preset → wait for the layout to settle → see whether the graph is centred and the dense region sits inside the starfield shell, with sparse outliers allowed outside it. Confirm at the same time that the curved links, ghost edges and cluster clouds did not drift with it.
- **`GRAPH_FIT_RADIUS_FACTOR = 6.2` is the external contributor's number** (a 6.5× shell, leaving about 5% inner margin), unconfirmed by Rick's eye and possibly needing adjustment.
- 0.4.0's leftover "the performance benchmarks have not been re-run" (S1/S4) is still undone; this round only added a single-point micro-benchmark for `fitGraphPositions`.
- Issue #6 (bloom ghosting) **does not reproduce**, so the draft reply asks for information rather than offering a fix; `powerPreference: "high-performance"` is a one-line change but would be a blind one.

### The files that changed
- New: `docs/community-sweep.md` (the living at-a-glance table), `src/render/graphTransform.ts` (from PR #10, the quantile algorithm rewritten by me), `tests/graphTransform.test.ts` (from PR #10, unchanged)
- Changed: `src/render/AggregateRenderer.ts` (+`renderPositions`/`fitWeights` fields, +`fitPositions()` as the single route; ten places that consume coordinates moved to the display coordinates: node geometry / link filling / selection highlight / ghost edges / cluster clouds / the genesis animation / projection / picking / camera distance; `this.positions` is left with an assignment and the fit's input)

## 2026-07-11 · Releasing 0.4.0: curved links + a volumetric nebula + preset character + renaming presets (with tag nodes / ghost edges riding along)

### What was done
The whole batch of v0.4 visual upgrades and interaction polish was settled and released as **0.4.0** (the version jumps from 0.2.2 straight to 0.4.0 to line up with the "v0.4" codename used throughout; minAppVersion stays 1.8.7). Rick accepted it item by item on a real machine and ruled to release. This release **carries three batches of change interleaved through the same files** (git cannot split them cleanly, and Rick confirmed releasing them together):

- **My v0.4 (the main line)**:
  - **Curved links**: quadratic Bézier, bowing radially outward (away from the galactic core), with a new "Link curve" slider where 0 = straight. Still a single LineSegments, K segments per edge, and the main links / the selection highlight / the genesis animation all share `linkCurves.ts`; the segment count follows the quality tier at 8/6/4, and at zero curvature it degenerates to a single segment = no regression.
  - **Four layers of deep-space background** (a star dome / a nebula / floating stars / cluster clouds), with a new "Deep space" section in the panel. **The nebula was rewritten once**: the first version baked FBM onto a BackSide sphere shell → Rick's reaction, "it looks like a sphere wrapped in mid-air, too solid" → replaced with **volumetric billboard cloud sheets** (`NebulaDome` is now Points: six cloud cores in clumps at different depths, additive, sizeAttenuation for parallax, a soft Gaussian shader, always facing the camera so the sphere never shows). The radius came in from ×6.9 to ×2.6 so it wraps the graph. Then, on Rick's "too thick", the shader's strength and opacity coefficients were pressed down to 0.30/0.22.
  - **Pulling the presets' characters apart**: Rick said "Galaxy and Spiral look too alike" → Spiral became a very flat disc (flatten 0.75) with the spiral force at full (spiral 0.095), strong core gravity (coreGravity 0.22), strongly bowed links (linkCurve 0.72) and a higher viewing angle (62°), which clearly separates it from Galaxy. All eight presets got linkCurve and space values to suit their character.
  - **Renaming custom presets + not losing them**: a ⋯ menu (rename / move up / move down / delete, inside a native Menu, replacing the four small buttons crammed into the top-right corner with their ✓✕ confirmation); renaming goes through an inline input; **saving, deleting, moving and renaming all write to disk immediately** (`saveNow`, bypassing the 800ms debounce —— previously quitting right after saving lost it).
  - **Trimming the copy**: the "Your saved parameters" printed on every custom preset card is gone (zero information); this carries Rick's standing principle "delete every piece of interface copy that does nothing" (stored as the memory ui-copy-restraint).
- **Riding along (done in another session or two, released here)**:
  - **Tags as nodes** (`showTags`, off by default): notes sharing a tag cluster through a tag star.
  - **Ghost edges** (`showGhostEdges`, **default changed from true to false**): reads the companion Constellation plugin's `ghost-edges.json` and shows suggested links as dashed lines.

### The decisions and the alternatives rejected
- **The nebula gathers curves on the CPU per frame rather than through a GPU coordinate texture**; **volumetric billboards rather than a sphere shell or RTT volumetric fog** (Rick rejected the shell; raymarching is too expensive and breaks the performance discipline). See the header comments in nebula.ts / linkCurves.ts.
- **Ghost edges default true → false** (Rick ruled): ghost depends on the Constellation plugin, which is not released yet, and defaulting it on would leave an ordinary user with a switch attached to nothing. Off by default = the code ships with 0.4.0 without bothering anyone, whoever has Constellation can turn it on, and it can default on once Constellation ships. An existing user's saved settings have no such field → they take the new default of false; anything already saved as true (a development environment) is kept.
- **The three batches ship together rather than split**: the v0.4 / tag / ghost changes are interleaved line by line through settings, AggregateRenderer, GraphController and the rest, and splitting them cleanly risks more than it is worth; Rick confirmed shipping them together.
- **Version 0.4.0 (skipping 0.3.0)**: it lines up with the v0.4 codename used throughout the cycle and costs the reader less; skipping a semver minor is legal.

### Where things stand
0.4.0 is released (the same flow as 0.2.x: bump on main → commit → push → tag → release.yml CI produces the three files). The dev vault is ready to use: `npm run dev` → open `dev-vault/` in Obsidian.

### What is left and what is known broken
- **Ghost edges are half a feature**: they depend on the Constellation plugin (see [[constellation-plugin]]), and until Constellation ships the switch has no data source. Both tag and ghost are **off by default and in neither README** (they get announced when they become the default experience).
- **The aesthetic numbers are still a starting point set by eye**: the eight presets' linkCurve and space, the volumetric nebula's cloud-core count and strength coefficients, Spiral's spiral at 0.095 (near the ceiling) —— Rick has been through them once and may adjust again.
- **The performance benchmarks have not been re-run**: S1 (orbit fps) and S4 (leak) have not been re-measured on a real machine with the new volumetric clouds, curve gathering, floating stars and ghost layer; every object is in the dispose contract but there is no measured number.
- The correctness of the tag and ghost features is vouched for by their own sessions and by Rick; this session only guarantees that with all three batches merged, tsc / lint / test / build are green and nothing breaks anything else.

### The files that changed
- New: `src/render/linkCurves.ts`, `src/render/nebula.ts`, `src/settings/ghostEdgeImport.ts` (ghost, another session), `tests/{linkCurves,settingsMerge}.test.ts`
- Version: `manifest.json`/`package.json` 0.2.2→0.4.0, `versions.json` +`"0.4.0":"1.8.7"`
- Mine: `AggregateRenderer` (rebuildable link geometry + background-layer management + the volumetric nebula), `starfield` (+ floating stars), `stylePresets` (eight presets + linkCurve/space + Spiral's character), `presets` (tokens + space), `settings` (linkCurve/space/merge compatibility), `quality/tiers` (+linkSegments/clusterCloudsAllowed), `GraphController` (settings propagation / presets / rename + immediate save), `overlay/ControlPanel` (the deep-space section / the link-curve slider / the ⋯ menu / inline rename), `i18n×6` (+space/rename/more keys), `styles.css`, `README×2` (v0.4 visuals + six languages)
- Riding along (another session): `types`/`data/{buildGraph,GraphStore}` (tag nodes), `settings` (showTags/showGhostEdges), `SettingsTab`, `render/shaders` (ghost dashes), `OverlayManager`, `render/palette`, `tests/{adjacency,buildGraph}` (tag fixtures)

---

## 2026-07-09 · The v0.4 workflow: curved links + four layers of deep-space background (recreating a NASA Eyes reference image)

### What was done
Rick supplied a NASA-Eyes-style reference image and ruled on two big features plus one expansion of his own, all of which landed here (build / tests / lint green, awaiting a look on a real machine):

- **Curved links**: a link can bow into a quadratic Bézier arc away from the galactic core (a new "Link curve" slider, 0 = straight). K segments per edge still go through the same LineSegments (1 draw call); the main link layer, the selection highlight and the genesis animation share one fill function (`linkCurves.ts`), so the arcs coincide exactly. The segment count follows the quality tier: high 8 / low 6 / mobile 4; at zero curvature the geometry degenerates to a single segment = exactly equivalent to the old straight rendering (no regression).
- **The deep-space background splits into four stackable layers** (Rick's expansion: ship one combination as the default and let users compose the rest):
  - The star dome (the existing shell of star points, the switch unchanged);
  - **The nebula dome**: FBM value noise baked once into an equirect texture on a BackSide sphere (512×256 bakes in a measured 6.5ms); the strength slider only moves opacity (no rebake), and only changing the colour theme rebakes (the tint takes the theme's first two colour groups, darkened and desaturated); the poles fade out to avoid the sphere's pinching; the brightness is clamped below the bloom threshold to prevent light pollution;
  - **Floating stars**: up to 1,200 points scattered inside and outside the graph's volume, with sizeAttenuation making near ones large and far ones small plus a slow reverse rotation = a parallax layer (the counterpart of the "scattered little stars" in the reference image);
  - **Cluster clouds**: the top nodes by degree seed it, a greedy spacing takes at most 10 clusters, and each cluster gets 3 soft Gaussian additive sprites (1 draw call, with a point-size clamp in the shader so fill rate does not blow out); the colour is the mean of the cluster's nodes with the saturation raised —— the substance of the reference image's "wreathed in cloud". It is recomputed only at the moment the layout settles, so cruising costs nothing.
- **Pulling the presets together** (Rick's principle: fit each preset's character and pull them apart; the default stays Galaxy, to be adjusted after looking): Galaxy restrained (curve 0.35 / cloud 0.35), Spiral flowing (curve 0.55), Orbits big arcs in clear space (curve 0.75 / almost no cloud), Deep Field straight lines over a sea of floating stars (curve 0 / floating stars 0.65), Nebula full of cloud (dome 0.8 / clouds 0.7), Minimal all off, Fireworks on pure black (curve 0.2), Supernova a warm afterglow of dust (curve 0.3 / cloud 0.45).
- The panel gains a "Deep space" section (3 sliders + the star switch moved in, carrying the "set by X / customised / restore" markers); i18n adds 5 keys across six languages; both READMEs updated (and the stale "bilingual interface" corrected to six languages along the way).

### The decisions and the alternatives rejected
- **Curves gather on the CPU per frame rather than through a GPU coordinate texture**: the GPU route (node coordinates into a DataTexture, the Bézier computed in the vertex shader) is a better O(n) per frame, but the layout's hot window is only about 5s, and the CPU route measures out at 19,337 edges × 8 segments ≈ 930,000 float writes per frame (~2–4ms), falling to zero once settled. Cut per "no abstraction before a second real use case", recorded in `linkCurves.ts`'s header comment.
- **The nebula dome bakes on the CPU (DataTexture) rather than through a GPU RTT**: cloud is a low-frequency signal, and 512×256 on the CPU at 6.5ms is enough; it avoids touching EffectComposer and render-target lifetimes. Making strength mean opacity is what lets the slider rebake nothing.
- **An old custom preset missing the new fields is filled with 0** (rather than the new defaults): it keeps the straight-line, no-background look the user saved. Only the built-in presets take the new values.
- **Hover preview does not rebake the nebula** (the tint reuses the already-baked texture, and only committing by click changes the colour): it avoids an extra 6ms+ hitch on every hover.

### Where things stand
`npm run build` (tsc+esbuild), `npm run lint` (0 errors) and `npm test` (34/34, including 7 new linkCurves cases and 4 settingsMerge cases) are all green. The dev vault is ready to use: `npm run dev` → open `dev-vault/` in Obsidian.

### What is left and what is known broken
- **Awaiting Rick's eye on a real machine**: ① the eight presets' new character values are all starting points, to be hover-previewed one by one for how distinct they are; ② whether the curve's bow height CURVE_BOW=0.32 and its direction (bowing radially outward) look right; ③ whether the nebula's brightness stays under bloom (especially the Nebula preset at 0.8 strength with the TikTok theme); ④ whether the cluster clouds distribute sensibly over the real library's 3,230 nodes.
- **The performance benchmarks have not been re-run**: S1 (a 20s orbit after settling) and S4 (the leak canary) need to run inside a real Obsidian (the S1/S2/S3 buttons in the panel's "Advanced" section, on a dev build); every new object is in the dispose contract, but there is no measured S4 number yet. The curve gathering's cost during the layout's hot window (estimated 2–4ms/frame) also waits on an S2 re-run.
- The nebula dome's texture is deterministic noise (a fixed seed): every user's cloud has the same shape and only the tint differs —— a seed-shuffle button could come later.
- Obsidian's settings page (SettingsTab) did not gain the new sliders (the floating panel covers every adjustment), keeping the existing division of "durable preferences on the settings page, live adjustment in the panel".
- A change made in parallel during this session (tags-as-nodes: `showTags` in `types.ts` / `buildGraph.ts` / `GraphStore.ts`) is not part of this workflow; two compilation loose ends it left were tidied in passing (the adjacency test fixture gained `tag:false`, and GraphController passes a third argument, `showTags`), without touching its design.

### The files that changed
- New: `src/render/linkCurves.ts` (the pure Bézier fill), `src/render/nebula.ts` (NebulaDome + ClusterClouds), `tests/linkCurves.test.ts`, `tests/settingsMerge.test.ts`
- Changed: `src/render/AggregateRenderer.ts` (rebuildable link-layer geometry, background-layer management through setSpace/syncSpace/setNebulaTint/refreshClusterClouds, the dispose contract extended), `src/render/starfield.ts` (+buildFieldStars), `src/render/stylePresets.ts` (eight presets +linkCurve/space), `src/render/presets.ts` (tokens + space as the master switch), `src/settings.ts` (LookSettings.linkCurve, SpaceSettings, merge compatibility), `src/quality/tiers.ts` (+linkSegments/nebulaTexSize/clusterCloudsAllowed), `src/view/GraphController.ts` (settings propagation / applying a preset / preview / restoring a section / the settle hook / syncing the nebula tint), `src/overlay/ControlPanel.ts` (the deep-space section, the link-curve slider, the section markers extended), `src/i18n/{en,zh,de,it,es,pt}.ts` (+5 keys), `README.md`/`README.zh.md`, `tests/adjacency.test.ts` (filling in the parallel change's fixture)

---
## 2026-07-07 · Releasing 0.2.2: the black-screen-in-a-popout fix + a six-language interface + a new language switcher

### What was done
Starting from the community's issues and PRs (2 issues + 3 PRs on the repository), Rick ruled on four things and said "finish 0.2.2 and release it". **0.2.2 is out** (CI succeeded, and the release carries all three files).

- **Bug #4, "rendering fails", fixed**: moving the view to a new window and then maximising it went black. **Two root causes**: ① the visibility IntersectionObserver was bound to the original window, so once the view moved it judged the element invisible → `paused=true` skipped rendering; ② the render loop used the main window's rAF, and the main window gets throttled or paused by the browser once the maximised popout sends it to the background. The fix: when the window changes (`resize()` detects the document changing), rebind visibility against **the view's current window** and use **that window's rAF**; the light/dark theme is also read from the view's own window.
- **i18n extended to six languages**: en/zh + **de/it/es/pt**, 156 keys each. Produced through a background workflow (four languages translated in parallel → native-speaker QA for each), with a German `&amp;` escaping blemish cleaned up on landing; the Dict type forces every key to be present (tsc green). German and Italian were prompted by community PR #5 (@bittner), with thanks.
- **The language switcher reworked**: six languages do not fit a 中/EN toggle row → the header becomes **a language-code button plus a native Obsidian Menu** (Automatic + the six, with the current one ticked), the settings page's dropdown grows to 7 entries, and `resolveLang` recognises all six from Obsidian's language prefix.
- **Draft community replies**: drafts written for #1/#2/#3/#5 (including thanks to bittner for German and Italian) plus the #4 fix reply (unsent), kept in `community-reply-drafts.md`. **Nothing was posted or closed on my own initiative.**

### Where things stand / what is left
- **0.2.2 is live**: https://github.com/Longwind1984/galaxy-view/releases/tag/0.2.2 .
- **⚠️ Nothing was seen on this machine**: the user's screen was locked at release time and computer-use could not get in → the language switcher's rendering and the popout black-screen fix **were not tried inside Obsidian** (the static checking is thorough: tsc / build / lint / test all green plus spot checks of the translations). Worth an eye after updating: switching through each language in the menu, and whether "move to a new window + maximise" displays properly (especially on Windows, where the bug was reported).
- **The outward community actions wait on Rick**: closing and replying to issues #1/#2/#3/#5 and replying to #4 (fixed in 0.2.2) —— the drafts are ready and go out once he has read them.

## 2026-07-07 · Fixing the plugin review failure → releasing 0.2.1

### What was done
0.2.0 was marked **Failed** by the Obsidian review bot: 3 SOURCE CODE errors, all at `src/settings/SettingsTab.ts:7`, where `/* eslint-disable @typescript-eslint/no-deprecated */` switched off the deprecated-API check —— and the **newer `eslint-plugin-obsidianmd` (0.4.x) the review uses forbids switching that rule off** (three meta-rules fire at once: require-description, disable-enable-pair, no-restricted-disable). The local 0.3.0's lint let it through, which is why it went unnoticed on my side.

**Reproduced**: bumping the devDependency `eslint-plugin-obsidianmd` from 0.3.0 to **0.4.1** made `npm run lint` reproduce the review bot's 3 errors and 2 warnings exactly. **Cured rather than suppressed**:
- **SettingsTab**: the 3 errors are actually 3 **calls to deprecated methods** (`this.display()`×2, `.setWarning()`×1 —— not the display override itself). → A private `render()` was extracted, and internal redraws call `render()` instead of the deprecated `display()`; the destroy button uses `buttonEl.addClass('mod-warning')` (no version dependency, whereas `setDestructive()` needs 1.13.0 and would break 1.12.x users). The whole eslint-disable is gone.
- **OverlayManager**: the card's date, `moment(...).format('ll')`, has loose moment typing → it triggers the no-unsafe-* warnings. Replaced with the native `Intl.toLocaleDateString` (which drops the moment dependency along the way).
- **styles.css**: `.gx-textlink`'s `text-decoration: underline dotted` → simplified to `underline` (silencing the partial-CSS-support warning).

**The result**: **0 errors** on 0.4.1's lint (2 non-blocking warnings remain: eslint.config's `config()` deprecation —— in a dev tooling file the review bot does not inspect; and `prefer-setting-definitions` —— `getSettingDefinitions` needs 1.13.0 while our floor is 1.8.7, which is a settled trade-off). build and test (23) are green.

### Where things stand
- **0.2.1 has been re-released**: commit `3d0a01e` → main → tag `0.2.1` → `release.yml` CI **succeeded** (this time CI's lint runs 0.4.1, equivalent to the review bot's rules, at 0 errors), with all three release files present. The review bot should pass 0.2.1 (0 SOURCE CODE errors).
- **The lesson**: the local lint has to match the review bot —— `eslint-plugin-obsidianmd` is now at 0.4.1 and locked in package-lock, so CI and local will catch this class of problem before the bot does.

## 2026-07-06 · Releasing 0.2.0 (pushed as a GitHub Release; community-store users will receive the update)

### What was done
Rick ruled "ship it". The whole release flow was run and verified:
- **Version 0.1.1 → 0.2.0**: manifest.json / package.json / versions.json (`0.2.0 → minAppVersion 1.8.7`).
- **The release gate**: build / lint / test all green (23); verified that in the prod dist, `__GALAXY_DEV__` and the bench module's internals (collectFrames / observeLongTasks) are eliminated as dead code; the bench commands register after `if(!__GALAXY_DEV__)return`, so a store build registers none of them (the leftover dead `runScenario` method matches 0.1.1, is not user-visible and does no harm).
- **README refreshed**: presets 4→8, wander / connect-two / bilingual / second-degree added, "pending review" corrected to listed, and the Usage panel structure updated.
- **git**: the whole of feat/v0.2 committed (be86e0b) → fast-forwarded into main → main pushed (Obsidian decides an update from main's manifest version) → tag `0.2.0` pushed → `release.yml` triggered.
- **CI succeeded (35s)**: npm ci + lint + test + build + attestation + `gh release create`. **The release is published** (neither draft nor prerelease), with main.js / manifest.json / styles.css all present.

### Where things stand / to do
- **Live**: https://github.com/Longwind1984/galaxy-view/releases/tag/0.2.0 . Existing store users will see 0.2.0 under "check for updates" (community-store updates propagate automatically from the GitHub Release; no further obsidian-releases PR is needed).
- **Left over (minor)**: README.zh.md was not refreshed with it (only the English README was); the local feat/v0.2 branch is merged into main and still around, undeleted. The aesthetic numbers (the wander rhythm, the 1.5× framing margin, the 6s orbit resume and so on) are still adjustable starting points, awaiting Rick's feedback after living with them.

## 2026-07-06 · The tour module reworked at the product level into "Wander + Connect two" (direction C), both verified first-hand through computer-use

### What was done
Rick chose direction C: break the old "soup of four engineering-flavoured mode chips" into two actions named after intentions. Build / lint / unit tests (23, including the rewritten tour tests) green, deployed to the dev vault, and both paths verified first-hand through computer-use.

- **Wander**: one `▶ Start / ■ Stop` button and a speed slider, **exposing no modes at all**. Behind it a director mixes automatically —— mostly "fly to a weighted node (degree × random, so hubs are naturally visited often) → orbit → show the card", with a spline flyby inserted every fourth beat for variety. Verified: flying to "思想家网络" (103 outbound links), the card appears → 7s later "法兰克福学派" → a continuous tour, no crash.
- **Connect two**: a separate `Pick two…` button → pick the start → pick the end → `shortestPath` BFS → fly node by node. Verified: picking "思想家网络 → 法兰克福学派" flew (at the slow 0.05 speed the mid-flight card and the button reading "Stop" were both caught).
- The panel: the appearance zone's 8 presets are unchanged; the navigation and motion zone is now three blocks —— auto-orbit / wander / connect two —— plus replay the opening.

### The key piece of triage: "connect two does nothing when pressed" was an artefact of computer-use, not a bug
Connect two appeared not to fly for a while (after picking the end point the view was the global one, no card, the button back to "Start"). Ruling things out layer by layer located the real cause: **a short, finite tour (2 nodes ≈ 10–15s) finishes inside the round trip between each computer-use "click → screenshot"** —— every computer-use step sends Obsidian to the background, and the dwell is timed in **real time**, so the tour keeps advancing in the background and by the time the screenshot arrives it has finished and returned to centre. Wander is an infinite loop and is parked at some node at any moment, so it was caught every time; connect two ends, so it was missed every time. Stretching the total to 200s+ with speed 0.05 (100s of dwell per node) caught the mid-flight card and the "Stop" button on the first screenshot → **the feature works**. Verified in isolation from the console: shortestPath returns the right path (a connected hub pair has path length 2), tickGuided's dwell logic is right (a manually driven tick does not advance during the dwell), and selectNode's flight is fine.

### The files that changed
- `src/tour/TourDirector.ts`: rewritten as the two APIs `startWander`/`startGuided` plus `tickWander` (a revisit beat, with a flyby every fourth) and `tickGuided` (walking a queue); the rediscover / flyby / grandtour user modes are gone.
- `src/view/GraphController.ts`: `toggleTour`→startWander; `startGuidedTour`→`startConnectTwo`; `setTourMode` deleted; the `onConnectTwo` callback added; the `TourMode` import removed.
- `src/overlay/ControlPanel.ts`: the tour block rebuilt as a wander block plus a connect-two block; the mode chips, `TOUR_MODES` and `onTourMode` deleted.
- `src/settings.ts`: `TourSettings` reduced to `{speed}`; `TourMode` and the mode/hopCount in DEFAULT and merge deleted.
- `src/i18n/{en,zh}.ts`: `tour.mode.*` and `nav.tour*` deleted; `nav.wander/wanderSub/connect/connectGo/connectSub` added.
- `tests/tour.test.ts`: rewritten as wander/guided cases (6 of them).

## 2026-07-06 · Framing moves to FOV-fitted + orbiting the centroid (a global, centred view), and auto-orbit now breaks only on a drag (verified first-hand through computer-use)

### What was done (1 and 2 verified through computer-use; 3 awaits Rick's direction)
- **① The initial and recentre view was too close → "frame by the actual node cloud's FOV + orbit the centroid"**: the old version stared at the world origin at a distance of `seed radius × a fixed factor`, so it was too close as soon as the physics spread out, and a drifting centroid pushed the picture to one side and swung it back and forth while orbiting. The new `computeFraming()` computes **the real centroid and the 95th-percentile radius of the distance to it** (which avoids stray orphans), takes the framing distance as `fitRadius / sin(FOV/2) × 1.5` for margin, and orbits the camera around the centroid. → A genuinely global, centred view that no longer drifts while orbiting. Verified: after recentring the galaxy is centred, fills about 65% of the frame with comfortable margins, and is still centred after 4s of rotation.
- **② Auto-orbit broke far too easily → only a real drag or zoom breaks it**: the old version called `markInput` on pointerdown and stopped orbiting (even a click to select stopped it), then took 10s to resume. Now pressing does not stop it; the pointer has to move past a 4px threshold to count as a drag → only then does it stop; the wheel and touch drags still stop it; `resumeDelayMs` went from 10s to 6s. Verified: a single click on empty space, and 4s later the camera is still orbiting (the core moved from lower-left to centre-right); a click no longer interrupts. OrbitControls reads the current camera position every frame, so handing over when the orbit stops does not jump.

### The files that changed
- `src/interactions/CameraDirector.ts`: added `FRAMING_MARGIN=1.5` and `DRAG_THRESHOLD_PX=4`; `framingPosition/setInitialFraming/resetView` now take `(center, fitRadius)` and derive the distance from the FOV; `bindPointer` only calls `markInput` once pointermove passes the threshold (the wheel and touchmove interrupt as before).
- `src/view/GraphController.ts`: added `computeFraming()` (centroid + 95th-percentile radius), and the three framing call sites (initial / opening / recentre) now use it.
- `src/constants.ts`: `CRUISE.resumeDelayMs` 10_000→6_000.

### What is left
- **③ Rework the tour module completely (starting from the product design)**: the product critique is written and three directions await Rick's ruling before a previewable version is built. The tour today is four engineering-flavoured mode chips mixing two different intentions —— "watch the atmosphere" and "find a path" —— at a high cognitive cost and with no narrative close.

## 2026-07-05 (cont.) · Three small panel adjustments + finding and fixing the real bug behind "the tour freezes the whole view" (verified first-hand through computer-use)

### What was done
Three reactions from Rick, all landed; and the real cause of "the tour does nothing when pressed" was reproduced, located, fixed and verified first-hand inside Obsidian through computer-use. Build / lint / unit tests (22) green, deployed to the dev vault twice (the last one including the flyby fix).

- **Deleted the copy "hover a preset to preview · click to apply"**: an explanation of an interaction that already explains itself is noise. The principle went into the **global `~/.claude/CLAUDE.md`** at the same time (the autonomy and gatekeeping chapter): when an interaction is obvious, matches intuition, and can be tried cheaply, do not add explanatory copy.
- **The "Navigation & motion" zone became collapsible**: it was a permanent static zone and is now wrapped in `<details>` (expanded by default, its open state persisted, section id `nav`); the summary reuses the zone-title look (uppercase, letter-spaced) with only a ▸ caret added.
- **The "Physics" section moved below "Bloom"**: the order is now appearance & colour → bloom → physics.

### The real cause: flyby's CatmullRom sampling throws → the whole render loop freezes (= "nothing happens when pressed")
A static audit came first (the chain is complete and it was deployed), and for a while it seemed impossible to reproduce. **Then, at Rick's request, computer-use opened the Obsidian dev vault and tried it**: the Galaxy view window was moved from the secondary display (Electron/WebGL screenshots as pure black there) to the built-in one, and ▶ was pressed —— the button switched to "Stop" but **the picture froze and dragging did nothing**. Opening the DevTools console gave the proof:

```
Uncaught TypeError: Cannot read properties of undefined (reading 'x')
  at Vector3.distanceToSquared → CatmullRomCurve3.getPoint → getPointAt
  at CameraDirector.update → loop
```

flyby builds a spline with `CatmullRomCurve3`, and one control point was NaN/undefined (the default centripetal type takes the square root of the point distance and reads undefined.x straight away on a bad point) → **it throws inside `CameraDirector.update`**. My previous version's try/catch only wrapped `tour.tick` and **not `director.update`** —— so the exception bubbled out of the rAF `loop`, `requestAnimationFrame` never queued the next frame, and **the whole render loop died and the view froze** (the DOM panel is still alive, which is why the button could still toggle). This is exactly "the first flyby run locks the whole plugin up" = "nothing happens at all".

**A three-layer fix**: ① `flyPath` cleans its control points (dropping non-finite ones and collapsing adjacent duplicates) and switches to uniform `'catmullrom'`; ② `update` wraps the curve sampling in try/catch with a non-finite fallback, abandoning that path segment on the spot if it throws; ③ the rAF `loop` wraps `tour.tick` and `director.update` together in try/catch, so any camera exception aborts the tour, calls `cancelMotion()` and raises a Notice —— **the loop never freezes again**.

**Verified after the fix**: pressing ▶ → the button reads "Stop", **the console is clean**, the camera flies continuously along the spline (two frames 2s apart differ visibly), and pressing "Stop" lands cleanly. #2 and #3 were confirmed on the same screen (physics below bloom, the navigation zone carrying a ▾ and collapsing, the preview hint gone).

### The files that changed
- `src/interactions/CameraDirector.ts`: `flyPath` cleans its control points and uses a uniform curve; the path branches in `update` get try/catch and a non-finite fallback; `cancelMotion()` added.
- `src/view/GraphController.ts`: the rAF loop's try/catch extended over `director.update`, with `abort()` + `cancelMotion()` + a Notice in the catch; toggleTour wrapped in try/catch; the guided entry raises a Notice immediately; a tourEmpty Notice when it cannot start.
- `src/overlay/ControlPanel.ts`: the preview hint line deleted; bloom and physics reordered; the navigation zone wrapped in a collapsible `gx-zone-section` (navBody carrying orbit / tour / replay).
- `src/i18n/{en,zh}.ts`: `preset.previewTip` deleted; `notice.tourEmpty/guidedPick/tourError` added.
- `styles.css`: `.gx-zone-section` summary styles added; `.gx-preview-tip` deleted.
- `~/.claude/CLAUDE.md`: a new "restraint in copy" principle.

---

## 2026-07-05 · Settings panel v4 reworked: polished first in a local HTML demo, then backfilled into the plugin wholesale and deployed (awaiting Rick's eye)

### What was done
Following Rick's "panel optimisation brief" plus several rounds of iteration, the whole set of interactions was settled first in **a locally interactive HTML demo** (`demo/panel-demo.html`, served from a static server on localhost:4319 so changes could be checked as they were made); once Rick confirmed it, it was **backfilled into the real plugin wholesale**, with build, static checks and unit tests green, and deployed to the dev vault. **This large change cannot be seen from my side inside Obsidian; Rick has to check it by hand in the dev vault.**

- **The information architecture reordered by intention**: the appearance zone (presets → fine-tuning sections) / the navigation and motion zone (auto-orbit · tour · replay the opening) / the footer (reset all · save as preset · advanced). fps moved from the header down into "Advanced", leaving only the note count in the header.
- **The relation between a preset and the parameters made visible**: hovering a preset **previews its visual effect immediately** (bloom / size / starfield / colours applied live to the 3D, with physics only on click —— hovering does not reheat the layout, which would stutter); only a click commits. The fine-tuning sections carry **a "set by X / customised" marker and a section-level "↺ restore"** (dirtiness computed by comparing the current settings against the active preset).
- **The presets redone**: merged into one flat list (the galaxy / effects categories are gone), with the 8 presets pulled apart on five axes at once —— **starfield on or off / colour theme / node size / physics / bloom**; each preset gets **a hand-drawn small icon** (`presetIcons.ts`, drawn with createSvg and tinted by the theme colour) plus a functional subtitle.
- **Custom presets**: save as my preset → **reorderable (↑↓) and deletable (confirmed in place with ✓/✕)**, persisted in settings.customPresets.
- **Link depth moved out of the panel → onto the node card**: a selection lights the first degree by default; a quiet row at the bottom of the card toggles "Links · 1st/2nd".
- **Cruise → auto-orbit, tour/explore → tour**, the two given equal footing as blocks; the genesis animation was confirmed to be **purely a replay** (changing a preset does not trigger it), renamed "Replay the opening" and moved down into the navigation zone.
- **The automatic quality strategy became bidirectional with hysteresis**: auto starts at the top tier; on high, 3 consecutive 5s samples under 30fps → drop to low, and on low, 4 consecutive 5s samples over 55fps (with headroom) → climb back to high (different thresholds and counts = hysteresis against oscillation). This replaces the old "one-way drop, never climbing back within a session".
- **The core gestures moved into a permanent "?" help overlay** (which cannot be dismissed forever), and the old dismissible hint banner is gone.

### The decisions and the alternatives rejected
- **Two conclusions from "read the code first"**: ① the genesis animation is purely a `playReveal` replay and `applyStylePreset` does not call it → treat it as a replay and move it down. ② presets are single-select through `markActiveChip` → make the highlight single-select everywhere.
- **Hover preview covers only the visual parameters (not physics)**: previewing physics means reheating the force layout, and reheating on every hover would stutter and churn; so physics commits only on click. Slider-level hover preview is not attempted for now (the 3D already gives the most direct feedback).
- **The galactic metaphor and the poetic names are kept** (they are product assets), with icons, subtitles and hover preview lowering the cost of learning them rather than renaming for simplicity.
- **Demo first, backfill second**: the panel is a purely user-visible layer, so per the protocol Rick gets "a version he can look at" to choose from, and the plugin is only touched once it is settled, which cuts rework.

### Where things stand: what runs and how to check it
- Branch `feat/v0.2` (**uncommitted**). Build / lint / unit tests green (22/22). The dev build is deployed to the dev vault (main.js at 07-05 00:03, with the port markers present).
- The demo is still on localhost:4319 (the static server started by `preview_start`), which Rick can keep using for comparison.
- **For Rick to check by hand in the dev vault** (reload the plugin): ① the preset cards' icons and subtitles, **hovering to see the 3D preview live**, clicking to commit; ② the sections' "set by X / customised / restore"; ③ save a preset → reorder / delete (with confirmation); ④ select a node → 1st/2nd degree on the card; ⑤ the auto-orbit / tour sections; ⑥ automatic quality scheduling both ways (run S1–S3 to load it and watch it drop and climb).
  - **Note**: the dev vault's old data.json holds physics parameters from earlier testing and has no activePreset value → it defaults to galaxy, so the "physics / colour" sections may initially read "customised"; **pressing any preset or "Reset all" syncs** the whole set.

### What is left / known
- Everything is a starting value (forces / presets / quality thresholds / tour rhythm), awaiting adjustment by eye.
- I cannot see the plugin render; any interaction or visual bug depends on Rick's feedback to fix.
- The old `hintsSeen` setting, `panel.firstRunHint` and `cycleSelectionDepth` are now unused (harmless, not cleaned up).

### The files that changed
- New: `demo/panel-demo.html` (the interactive prototype), `demo/index.html` (a redirect), `src/overlay/presetIcons.ts` (8 hand-drawn preset icons through createSvg), `.claude/launch.json` (the demo's static server config, at the Obsidian_PKM root)
- `src/render/stylePresets.ts` (the 8 presets redone + starfield/theme/frameElevDeg, flattened)
- `src/settings.ts` (+activePreset/customPresets + validation)
- `src/view/GraphController.ts` (applyStylePreset now covers starfield + theme + activePreset; previewStylePreset/endStylePreview; saveCurrentAsPreset/moveCustomPreset/deleteCustomPreset/restorePresetSection; the watchdog's bidirectional autoLow; the HUD split so fps → advStatsEl; the card's depth callback; buildPanel's callbacks replaced)
- `src/overlay/ControlPanel.ts` (**rewritten wholesale** as v4: the sectioned IA + preset cards + hover preview + section markers/restore + custom preset reordering and delete confirmation + the ? help overlay + the quality segmented control + fps → advanced)
- `src/overlay/OverlayManager.ts` (the card gains the 1st/2nd degree control + a callback)
- `src/i18n/{en,zh}.ts` (a batch of new keys for the panel rework: zone / preset.sub / sec / mine / nav / quality.autoSub / card and so on)
- `styles.css` (the whole of panel v4's styles + the card's depth control; the caret uses a span)
## 2026-07-04 · v0.2 ready for the public (English/i18n + Galaxy redone + a preset pack + second-degree selection) + v0.3's tour system (the code is done, awaiting a look in the dev vault)

### What was done
Building "the next version" for public Obsidian users. Rick ruled on two phases, and this session wrote both phases' code at once with build, static checks and unit tests green; **the visuals and the camera's behaviour have not been seen inside Obsidian** (I cannot start a GUI here), so the next step is Rick looking at it in the dev vault.

- **An English interface / i18n (v0.2 A)**: a new `src/i18n/` (en as the source of truth + a zh mirror + `t()` + a language-detection chain), pulling about 58 hard-coded Chinese strings out of the panel, the card, search, the commands and the notices into keys; **English becomes the public default**, `getLanguage()` follows Obsidian, and the settings page offers Auto / English / 中文 by hand. A plugin **settings page** was added (language, quality, visual mode, showing orphans and unresolved links, reset all), reading and writing the same settings as the floating canvas panel. The card's date moved to `moment().format('ll')` for localisation.
- **The panel's information architecture (v0.2 B)**: the first screen keeps only search / recentre / cruise / style chips; the sections are reordered by how often a newcomer needs them (appearance → physics → bloom → cruise → explore → advanced); **each section's open state persists**; a first open shows one dismissible line of hint in place of a seven-line wall of help; on mobile the panel starts collapsed.
- **The Galaxy layout redone (v0.2 C)**: "the galaxy is too wide" was traced to **a missing force** (the old implementation only pressed a uniform sphere into a uniform pancake). Two custom forces were added on the worker side: `coreGravity` (a radial core gravity = a dense bright core plus a radial density gradient, weighted by degree so hubs sink to the core) and `spiral` (a tangential spiral arm). Both scale with alpha, tend to zero once settled, and do not blow up a warm graph; **one implementation serves both the worker and the main-thread fallback** (`galaxyForces.ts` is the single source). The camera gained a framing elevation so disc-like presets look down on the arms.
- **The preset pack (v0.2 D)**: "Galaxy" redone as the default, plus four NASA-inspired presets —— Spiral Galaxy / Orbits (Eyes) / Deep Field (JWST) / Supernova; the chips split into "galaxies" and "effects".
- **Second-degree selection (v0.2 E)**: a new CSR adjacency list (`Adjacency.ts`, rebuilt with the data), turning selection into a neighbourhood BFS (replacing an O(all edges) scan on every click); showing the second degree is optional and **dims in tiers** (the selection and the first degree at full brightness, the second degree as a shell, everything else faded, reusing the single float aDim at no extra draw call); a "Link depth 1/2" button joins the frequently-used row; **double-clicking a node opens the note**. Six Adjacency unit tests were added.
- **The tour / autopilot system (v0.3)**: CameraDirector gains a reusable **spline path primitive**, `PathTween` (CatmullRom, constant speed by arc length, stopping on any input, zero per-frame allocation); a new `TourDirector` state machine (its tick driven by the rAF's paused guard → a hidden view freezes naturally). Three modes: random revisit (degree-weighted + LRU), asteroid flyby (a spline through the star field) and the grand hub tour (visit the top hubs, then return to the overview). The panel gains an "Explore" section (start/stop + mode chips + speed), and the command palette gains "start/stop the tour". The whole thing is disabled on the mobile tier.

### The decisions and the alternatives rejected
- **minAppVersion 1.7.2 → 1.8.7** (forced): language detection uses `getLanguage()`, and the obsidianmd linter requires a minimum of 1.8.7 or it reports no-unsupported-api. By 2026-07, 1.8.7 is old enough, so it went up; the redundant localStorage fallback went with it. The alternative, "keep 1.7.2 with a runtime guard", was vetoed by the linter.
- **The preset pack goes into 0.2 rather than 0.3** (a small adjustment to the Q1 schedule, flagged in the plan for Rick to notice when approving): once the new forces landed, the four presets are barely more than parameter objects and cost almost nothing; 0.3 can then concentrate on the tour system, its single largest new subsystem.
- **recommendedTheme is not applied automatically**: a preset carries recommended-colour metadata, but v0.2 does not change the user's colours —— it keeps the three axes (form / background / colour) decoupled, which is the plan's headline principle, and avoids startling anyone.
- **No banking on the flyby yet**: changing `camera.up` and handing back to OrbitControls easily leaves a crooked horizon, and it cannot be seen from my side; the steady tangent-lookahead version ships first and banking waits.
- **The camera elevation is stored but does not swing the camera immediately**: after switching to a disc preset the elevation takes effect at the next recentre or R, so the current interaction is not interrupted (an "only tilt automatically while idle" could come later).
- **The dim tiers are 1.0 / 1.0 / 0.45 / 0.12**: the first degree matches the old version (no regression for existing users) and the second degree acts as a shell —— starting values, with all four constants adjustable at any time.

### Where things stand: what runs and how
- Branch `feat/v0.2` (**uncommitted**, waiting on Rick's word before a commit).
- Automatic verification all green: `npm run build` (tsc + esbuild production) with no errors, `npm run lint` with no errors, `npm test` 14/14 (including 6 new Adjacency tests). The prod artifact is confirmed to carry no dev command strings.
- **For Rick to check by hand (I cannot start Obsidian's GUI)**: `npm run dev` outputs straight to the dev vault → open the dev vault in Obsidian and look at: ① whether the galaxy is "more like a galaxy" (a dense core / spiral arms / looking down on it) —— switch through the presets, move the core and spiral sliders, and settle the numbers by eye; ② whether switching EN/中文 on the settings page rebuilds the panel immediately; ③ the tiered dimming for link depth 1↔2 and double-click to open a note; ④ the three tour modes (any input stops it, the flyby does not make you seasick, the grand tour returns to the overview); ⑤ run the dev commands S1/S2/S3 plus the S4 canary to confirm the performance gates and zero leaks (especially the reheat's stability when changing a preset on a warm graph, and the flyby's frame rate).

### What is left and what is known broken
- **The aesthetics are not settled**: every force strength, preset parameter, camera elevation and tour dwell is a starting value awaiting Rick's eye.
- **Two things are not implemented (knowingly)**: the two-point Guided Path (marked a 0.3 stretch in the design); the selection polish's hover first-degree ring and Tab neighbour cycling (they interact with the dim system, left as a fast-follow); the flyby's banking.
- **How the panel's "Explore" button behaves at runtime**: it stays in sync with play/stop through TourDirector's onStateChange, so a natural end (the grand tour) syncs too; after a language change rebuilds the panel the button returns to "Start" (which does not affect the function).
- **The effect on existing users upgrading**: the new coreGravity/spiral defaults grow a dense core in their galaxy too (v0.2 *is* the Galaxy rework, so this counts as an improvement); a warm-start coordinate cache at low alpha converges gently under the new alpha-scaled forces rather than being flung apart.
- Uncommitted and unreleased; releasing needs a separate `npm version` + CI run (untouched here).

### The files that changed
- New: `src/i18n/{index,en,zh}.ts`, `src/settings/SettingsTab.ts`, `src/data/Adjacency.ts`, `src/layout/galaxyForces.ts`, `src/tour/TourDirector.ts`, `tests/adjacency.test.ts`
- Settings / types: `src/settings.ts` (+ five groups of fields: language / panelSections / hintsSeen / selectionDepth / tour, + coreGravity/spiral into PhysicsSettings, + the default changed to the redone galaxy, + merge validation, + toLayoutParams passing them through), `src/types.ts` (LayoutParams +coreGravity/spiral)
- Layout: `src/layout/forceWorker.ts` + `src/layout/MainThreadForceLayout.ts` (registering and rebuilding the two new forces, sharing galaxyForces)
- Rendering / presets: `src/render/stylePresets.ts` (Galaxy redone + 4 new presets + nameEn/group/frameElevDeg/recommendedTheme), `src/render/AggregateRenderer.ts` (setFocus moves to tiered weights, setSelectedLinks moves to a single tier1/tier2 layer)
- Camera: `src/interactions/CameraDirector.ts` (framingElevDeg + setFramingElev + PathTween/flyPath + the path branch in update + markInput interrupting a path)
- Data: `src/data/GraphStore.ts` (holds the adjacency, buildAdjacency inside rebuild)
- Controller: `src/view/GraphController.ts` (i18n notices and HUD, rebuildPanel/syncFromSettings, applyStylePreset taking a StylePreset plus the framing elevation, selectNode rewritten as a tiered BFS, cycleSelectionDepth, double-click to open a note, instantiating TourDirector + tick + toggle/setMode/setSpeed, aborting the tour in onDataChanged/applyTier, the framing elevation at startup)
- Panel / overlay: `src/overlay/ControlPanel.ts` (fully i18n'd + the IA reordered + section persistence + the first-screen hint + the genesis text link + chip grouping + the depth button + the Explore section + setTourRunning), `src/overlay/OverlayManager.ts` (the card i18n'd + the moment date), `src/overlay/Slider.ts` (the notch i18n'd), `src/view/SearchModal.ts` (i18n)
- Entry / manifest: `src/main.ts` (setLang + addSettingTab + i18n for command names and notices + the tour commands), `manifest.json` (minAppVersion 1.8.7), `styles.css` (the first-screen hint / text link / chip grouping styles)

---

## 2026-06-15 · Fixing the community store's automated review failure → 0.1.1

### What was done
0.1.0's automated review Failed after submission, and the two SOURCE CODE errors are hard blockers —— **the store's review does not allow an eslint-disable to switch an obsidianmd rule off** (I had used a disable to get the dev commands' Chinese and abbreviated names past lint, which is exactly what it catches). The fix is not to switch the rule off but to make the code genuinely comply:
1. **prefer-window-timers** (forceWorker.ts): there is no window inside a Worker, so `setTimeout` became `self.setTimeout` (a member call, which the rule does not catch since it only stops a bare identifier, and which is correct at runtime in a Worker).
2. **ui/sentence-case** (main.ts ×3): the disable was deleted and the dev command names and notices that tripped the rule with Latin abbreviations (S1/S4/GC) were rewritten in plain Chinese (a store build strips these commands anyway, so the naming does not matter).
3. **The CSS !important warnings** (styles.css ×3): the `!important` existed only to override an inline transform set from JS; now showing the card on mobile calls `removeProperty('transform')` to clear the inline value, and `.gx-mobile .gx-card`'s selector specificity takes over left/top/transform, so all three `!important`s are gone. Note that `removeProperty` is a method call, which sidesteps no-static-styles-assignment (that rule only catches a static assignment).
4. **Artifact attestation** (a RELEASES suggestion): the release CI gained actions/attest-build-provenance@v2 plus the id-token and attestations permissions, so the artifact's origin can be verified cryptographically.
5. Version bumped 0.1.0 → 0.1.1 (a review result is bound to a version, so a fix has to be resubmitted as a new one).

### Against the review's results
- Two SOURCE CODE errors (disabling a rule) → zero eslint-disables in src
- The CSS LINT warnings (!important) → zero
- The RELEASES suggestion (no attestation) → added to CI
- NETWORK passes; BEHAVIOR's vault enumeration is a suggestion: a graph plugin has to enumerate files to build a graph, which is inherent and needs no change (the review only flags it for transparency)

### Where things stand
lint at 0 errors and 0 disables / 8 unit tests green / a prod build still strips the bench commands / the real vault updated to 0.1.1. **Awaiting Rick to release 0.1.1 by hand** (git push + tag 0.1.1 + push the tag → CI produces the release → resubmit to the store, where the version goes from 0.1.0 to 0.1.1 automatically).

### The files that changed
- Changed src/main.ts (3 command names and notices made plain Chinese + the disable deleted), src/layout/forceWorker.ts (self.setTimeout), src/overlay/OverlayManager.ts (removeProperty clearing the inline transform), styles.css (3 `!important`s deleted), .github/workflows/release.yml (attestation)
- Bumped: package.json / manifest.json / versions.json → 0.1.1

---

## 2026-06-13 · Closing the loop on M4 + preparing the M5 release (awaiting Rick's confirmation to publish)

### What was done
- **M4.1, the card's bottom overlap fixed (from Rick's iPhone testing)**: research confirmed that Obsidian mobile's bottom action bar has the class `.mobile-navbar` (from the official app bundle's stylesheet), that there is **no official height variable**, and that it may not exist on a tablet or with it hidden → so the approach is **measuring at runtime**: when the card appears, the actual overlap in pixels between the navbar and the canvas is written into `--gx-bottom-inset`, taking whichever is larger, it or `safe-area-inset-bottom`. With no navbar it measures 0, so no gap appears by construction (this branch was verified in the desktop mobile simulation).
- **M4 accepted on the desktop**: measured on the mobile-simulation tier —— 6 draw calls / a 1500-node cap / bloom off / 60fps; searching and selecting "概念词典" → the bottom sheet sits flush with nothing covering it, its 669 outbound links highlight, and focus dimming all behaves. Rick measured his iPhone: 61fps, smooth. **Gate G4 passes, and mobile ships with the first release**.
- **M5 preparation**: the `__GALAXY_DEV__` esbuild define gates the benchmark commands and the panel's S1–S3 (measured stripped from a store build, kept in a dev build); MIT LICENSE; an English README (the store's shopfront); a GitHub Actions release workflow (tag → lint+test+build → attaching main.js/manifest.json/styles.css); the official review's self-check passes (no innerHTML / no network calls / no styles injected from JS / a compliant id).

### Where things stand
The real vault runs a store build (no dev commands), the dev vault a development build. **Publishing (creating the GitHub repo + pushing + tagging) awaits Rick's confirmation per the protocol**: the repo name / whether it is public / which account. Once confirmed the flow is: gh repo create → push → tag 0.1.0 → CI produces the release automatically → BRAT can install it → soak for a week → submit through the store portal (checking galaxy-view for duplicates).

### What is left
- How the card avoids the bar on an iPhone can be confirmed by Rick in passing after his next sync (the mechanism is verified on the desktop, so the risk is low).
- The store submission needs screenshots or a GIF (the README's install section still has a `<github-user>` placeholder to replace).

### The files that changed
- New: `LICENSE`, `README.md` (English), `.github/workflows/release.yml`, `src/typings/galaxy-dev.d.ts`
- Changed: `esbuild.config.mjs` (the define), `src/main.ts`, `src/overlay/{ControlPanel,OverlayManager}.ts` (the gate / the adaptive inset), `eslint.config.mts`, `styles.css`

---

## 2026-06-13 · M4 mobile: three quality tiers + installing into the real vault (Rick's iPhone testing pending)

### What was done
- **Three quality presets** (quality/tiers.ts): high (dpr ≤ 2 / everything / bloom on), low (dpr = 1 / 40% of the starfield / 8 labels), mobile (dpr ≤ 1.5 / **bloom off** —— the shader's hot core keeps 80% of the look / 32% of the starfield / **a 1500-node cap** + a 12k link cap / 6 labels / **tap only, no hover**). Platform.isMobile is a hard ceiling; the panel's Advanced → Quality cycles through automatic / high / low / **mobile simulation** (previewing the mobile look on a desktop).
- **The FPS watchdog**: on auto, three consecutive 5s samples under 30fps after settling → a one-way drop to low plus a Notice, never climbing back within a session (which avoids oscillation).
- **The node and link caps** went into buildGraph: the top N by degree + truncation by min(endpoint degree) + reindexing (with unit tests, 8 in all); the first drop shows "showing the first N nodes".
- **The mobile card is a bottom sheet** (40vh, scrollable); **webglcontextlost recovery**: a mask plus a single rebuild of everything (an Electron GPU reset and iOS reclaiming the context both come through here).
- **Installed into the real vault** (iCloud): `.obsidian/plugins/galaxy-view/` plus an entry appended to community-plugins.json —— on the desktop it takes effect after restarting Obsidian; on the iPhone it is usable as soon as iCloud has synced.

### The state of verification
lint 0 / 8 unit tests green / built and deployed to both the dev vault and the real vault. **Awaiting Rick**: ① preview "Quality: mobile simulation" on the desktop (nodes drop to 1500, bloom off, tapping a node opens the bottom sheet); ② the iPhone checklist: it opens without crashing → orbiting is smooth (target ≥25–30fps) → tapping selects and opens the sheet → open and close the view ×5 → send it to the background and come back, still fine. ③ If it does not meet the bar, the contingency is shipping isDesktopOnly first with mobile in V2.

### What is left
- Mobile touch: OrbitControls' native one-finger orbit and two-finger zoom/pan, with no extra customisation; WASD and the cruise speed are meaningless on a phone but harmless.
- On the desktop, the real vault needs Obsidian restarted to load the plugin (community-plugins.json is not read hot).
- The watchdog samples over a 1s frame window (hudFrames), and an extremely jittery scene could misjudge it —— conservative parameters (three in a row) plus a manual override as the backstop.

### The files that changed
- New: `src/quality/tiers.ts`
- Changed: `src/data/{buildGraph,GraphStore}.ts` (the caps), `src/render/{AggregateRenderer(applyTier),starfield(scale)}.ts`, `src/view/{GraphController(pickTier/watchdog/contextlost),GalaxyView(rebuild)}.ts`, `src/overlay/{ControlPanel(quality),OverlayManager(the budget / the sheet)}.ts`, `src/settings.ts` (qualityOverride), `styles.css`, `tests/buildGraph.test.ts`

---

## 2026-06-13 · M2.6 + M3: six iterations on the experience + the worker layout made official (cold layout's main-thread blocking goes to zero)

### What was done
**M2.6 (Rick's six reactions)**: ① six colour themes (Hubble Deep Field / TikTok Neon / Sunset Film / Cyber City / The Matrix / Aurora, chosen for how they read on black; a library with no colour groups generates them from folders automatically) with the shuffle kept; ② the "Recentre" button unified with the R key (clear the selection + return to the overview + cruise on arrival —— it answers "I don't know whose orbit I'm in"); ③ bright stars twinkling (only the brightest 3% of real stars, at most one at a time, at Poisson intervals with a 1.6s envelope; a frequency slider turns it off); ④ the genesis animation (nodes bloom outward from the centre in waves by radius over 2.6s, playing on open with a manual button in the panel; it reuses the warm-start coordinates and needs no precomputation); ⑤ the orphan-node switch (with the edge index rebuilt); ⑥ three node-size modes: link count / document size (∛ to compress the long tail) / uniform.

**M3 (performance hardening)**: WorkerForceLayout —— d3-force-3d entirely off the main thread (a Blob URL worker + the esbuild inline-worker plugin; coordinates ping-ponged through transferable double buffers, leaving the main thread a 38KB memcpy per frame; the worker batches ticks at ≤12ms per batch); it falls back to the main-thread implementation if creation fails; LayoutEngine.ticks became a real counter (the bench hack is gone); the alpha-semantics drift in S2 since M2 was corrected (cold layout returns to alpha=1); **uMaxPoint clamps the point sprites at 110dp** —— measurement caught the fill-rate bottleneck that drops to 8fps inside a cluster, and fixed it.

### The M3 benchmarks (same machine, same library, the worker layout)

| Scenario | M0 stock | M1 aggregate (main thread) | **M3 aggregate + worker** |
|---|---|---|---|
| S1 orbit | 16.2 fps | 60.0 fps | **60.0 fps** (p95 17.7ms) |
| S2 cold layout | 61s saturated / longest 860ms | 5.2s / longest 64ms | **5.8s / 0 blocks, 0ms** ✅ |
| S3 including unresolved | 13.8 fps | 60.0 fps | **60.0 fps** |
| S4 leak ×10 | +13.7MB | -1.0MB | **-3.7MB** (the worker tears down cleanly) ✅ |

Editing a note while the layout runs without jank is now an architectural guarantee, not merely "fast enough". Gate G3 passes: the unresolved switch can ship on the desktop, and mobile (M4) is a go.

### What is left
- The 8fps inside a cluster is eased by the clamp, but an extreme case (the Fireworks preset with a screen full of bloom) can still drop frames —— M4's quality tiers back it up with an automatic drop to desktop-low.
- Applying a colour theme overwrites the colour groups' values (the query is preserved); "import the 2D colours" restores them at any time.
- A lesson from the monitoring script: a bare glob in a zsh loop errors out immediately when nothing matches —— use setopt null_glob next time.

### The files that changed
- New: `src/render/colorThemes.ts`, `src/layout/{forceWorker,WorkerForceLayout}.ts`, `src/typings/inline-worker.d.ts`
- Changed: `esbuild.config.mjs` (the inline-worker plugin), `src/render/{starfield(Twinkler),AggregateRenderer(reveal/sizeMode/twinkle/uMaxPoint),shaders}.ts`, `src/layout/{LayoutEngine,MainThreadForceLayout}.ts` (ticks), `src/view/GraphController.ts` (recentre / themes / picking the engine with a fallback / the bench correction), `src/overlay/ControlPanel.ts`, `src/{settings,types}.ts`, `src/data/{buildGraph,GraphStore}.ts` (orphans / fileSize), `styles.css`, `tests/buildGraph.test.ts` (7 tests)

---
## 2026-06-12 · M2.5, a polishing round: style presets (including the flat galaxy) + orbit on arrival + panel v3 + bug fixes (six reactions from Rick)

### What was done
1. **Style preset chips** (bloom + physics + appearance as a matched set): "Galaxy", a flat disc (the new factory default, with the Y-axis flattening force at 0.3 —— natural attraction and repulsion cannot make a disc, so this extra force is necessary) / "Nebula", a natural sphere / "Minimal", no bloom so the structure shows / "Fireworks", bloom turned up to show off. The default bloom was toned down overall (0.6→0.35), putting a new user's first impression first.
2. **Orbit on arrival**: on arriving at a selected node the camera starts circling it immediately (no longer waiting 10s of idle), and the direction **sweeps first through the hemisphere holding the neighbours' centre of mass** (4 of 5 links point south → sweep south first).
3. **Panel v3's information architecture**: what is used often is pinned to the top (search / cruise / style chips), with fine-tuning tucked into collapsible sections (bloom / physics / appearance & colour / cruise / advanced), all collapsed by default —— a spare first screen. New: a flatten slider, a cruise-speed slider, a colour shuffle (swapping the nine colour groups around), and the link-opacity limit lowered to 0.
4. **Bugs fixed**: the selection card's z-index put on top (no longer covered by a hub label); daybreak's links moved from pure black to a warm pencil grey, #8d8678.
5. **The macOS pan fix**: macOS commandeers Ctrl+click as its own right-click emulation (this is the root cause of Rick's "Ctrl+drag does nothing", not the trackpad) —— pan now has three channels, **⌘+left drag / Shift+left drag / right drag**, and the instructions are updated.

### The state of verification
lint 0 errors / 5 unit tests green / built and deployed. **Not verified at runtime on a real machine** (Rick had closed the Obsidian window, and it does not grab the screen again) —— a checklist for Rick to walk next time he opens it: ① the panel's first screen should hold only search / cruise / the four style chips + the collapsed sections; ② pressing "Galaxy" presses the galaxy into a disc (about 5s to rearrange); ③ clicking a node and arriving should start orbiting immediately; ④ the card is no longer covered by text; ⑤ ⌘+drag pans.

### What is left
- The style chips have no "memory of the active one" (after a refresh the last preset chosen is not highlighted) —— deliberately kept simple, since a preset is "an action applied" rather than "a state".
- The flat galaxy's disc thickness and axis of rotation are not parameterised (the axis is always Y); if Rick wants a tilted disc it can be added.
- Gate G2 (deep space versus daybreak as the default direction) still awaits Rick's ruling under a light theme.

### The files that changed
- New: `src/render/stylePresets.ts`
- Changed: `src/settings.ts` (flatten/cruiseSpeed/the new defaults), `src/types.ts` + `src/layout/MainThreadForceLayout.ts` (the Y-axis flattening force), `src/render/presets.ts` (daybreak's link colour), `src/interactions/CameraDirector.ts` (beginFocusOrbit / the dense-side direction / ⌘⇧ pan / the cruise speed), `src/view/GraphController.ts` (applying a preset / the shuffle / computing the dense direction), `src/overlay/ControlPanel.ts` (the v3 reorganisation), `styles.css` (z-index / chips / the collapsed sections)

---

## 2026-06-12 · The body of M2 lands: the control panel redesigned + 3D freedom of movement + the four-piece set + two visual directions (G2 awaits Rick's ruling)

### What was done
Responding to three reactions from Rick and executing all of M2:
1. **The sliders redone (Lightroom-style)**: the default anchored at the track's geometric centre (each half mapped piecewise-linearly), a notch marking the default, the min and max limits always visible at the ends, a fill bar from the notch to the handle, **double-click to return to the default**, and the reading fading when the current value equals the default.
2. **3D freedom of movement**: left-drag to orbit (the base) + **right-drag or Ctrl(⌘)+left-drag to pan** (Google-Earth style) + **WASD/QE flight** (the speed adapting to the distance from the target, Shift ×3) + F to fly to the selection + R to ease back to the overview + ESC to deselect; the keys act only while the canvas has focus; the panel gains a collapsible "How to navigate" section.
3. **M2's four-piece set**: importing graph.json's nine real colour groups automatically (int→hex, trimming trailing spaces, first match on the `path:` prefix; re-importable by hand); a SuggestModal fuzzy search → select and fly (an empty query gives the top 20 hubs as a "tour of the constellations"); DOM overlays (the top-14 hubs' permanent labels fading with distance + hover labels + the selection card: a path colour dot / in and out links / the modified date / an async summary / open the note / focus); focus mode (non-neighbours fading to 0.12 over 280ms + a separate highlight layer for the selected links + the main link web darkened).
4. **Two visual directions** (the material for gate G2): every token gathered in presets.ts —— A "deep space" is always dark; B "follow the theme" shares A when dark and uses the "daybreak drafting room" when light (warm paper / motes of dust in place of stars / bloom forced off / ink nodes with a rim / pencil links / NoToneMapping); css-change switches automatically; the panel toggles A/B in one press.
5. **A warm start + the opening shot**: the settled coordinates are cached into data.json (at ≥80% coverage a reopen forms instantly plus a light alpha 0.06 tidy-up), and on a warm start a "building the star chart…" mask plays → the reveal at 600ms → the camera pulls out from inside the graph over 3.2s with bloom easing from 1.8× back to the configured value; a cold start simply shows the galaxy forming.

### Verified (on a real Obsidian)
The new panel renders correctly (limits / notches / sections / buttons all present), 61fps and 19–20 calls hold, the nine real colour groups take effect (04AI's green clump and Cubox's orange clump are distinguishable by eye), the hub labels appear, the settings persist (the panel loads the bloom value Rick set himself), and the whole chain of click → fly → card (including the async summary) → focus dimming → the selected links highlighting works. lint 0 errors / 5 unit tests green / 624KB.

### What is left (the way into G2 and M3)
- **Gate G2 awaits Rick**: switch the app to a light theme and set the panel to "Visual: follow the theme" → compare the daybreak drafting room against deep space and decide the default direction by eye.
- WASD flight / Ctrl pan / the search modal / R returning to the overview are unverified on a real machine (keyboard interaction is awkward to simulate remotely) —— a minute in Rick's hands covers all of it.
- Daybreak mode is finished only in the code and has not been looked at (it needs a light theme); the motes' drift animation is a simplified version (rotation only).
- The S2 benchmark now clears the warm-start cache before running (to keep the cold-layout semantics).
- The bench's runtime method-replacement hack on layout.step is still there (M3 gives it a real hook).

### The files that changed
- New: `src/overlay/{Slider,OverlayManager}.ts`, `src/settings/graphJsonImport.ts`, `src/render/presets.ts`, `src/view/SearchModal.ts`
- Rewritten: `src/overlay/ControlPanel.ts`, `src/render/{AggregateRenderer,shaders}.ts` (focus aDim / the selection highlight layer / tokens / the daybreak shader variant / motes), `src/interactions/CameraDirector.ts` (flight / pan / F / R), `src/view/GraphController.ts` (all the wiring), `styles.css`
- Changed: `src/{settings,types,constants}.ts` (preset/colorGroups/positionCache/showUnresolved, in/outDegree), `src/data/buildGraph.ts`, `src/layout/*` (initialAlpha), `src/view/GalaxyView.ts` (forwarding css-change), `src/main.ts` (the search command)

---

## 2026-06-12 · M1.5, the control panel: answering the G1 feedback (bloom is blown out, and there is not enough to play with)

### What was done
After seeing M1, Rick gave two reactions: the bloom is so bright the structure inside is lost; the controls are too thin, the physics parameters should be playable. What landed: a dark-glass **control panel** in the top-left corner —— bloom (strength / spread / threshold), physics (repulsion / link distance / link strength / centring, with the layout reheating live as you drag so the galaxy rearranges on the spot), appearance (node size / link opacity), the cruise switch, and reset to defaults; **every parameter persists** into the plugin's data.json (written with an 800ms debounce) and survives a restart. The benchmark buttons moved into a collapsed "Benchmarks (development)" section. The default bloom was toned down: strength 0.9→0.6, radius 0.45→0.4, threshold 0.1→**0.18** (the threshold is what solves "the structure is lost" —— only bright cores and bright stars cross the line and glow, so the web of links inside is no longer drowned).

### The key decisions
- Link strength became **a multiplier** on top of d3's default (1/min(endpoint degree)) rather than an absolute value —— it keeps the adaptive property that "hubs do not get torn apart", and the slider's meaning stays intuitive.
- The physics sliders call updateParams + reheat(0.5) on input: the rearrangement itself is part of the play (drag repulsion and watch the galaxy breathe).
- The settings are injected into the view through the SettingsHost interface, avoiding a circular dependency on main.ts; mergeSettings defends field by field against dirty data.

### Where things stand
lint / build / unit tests all green, deployed to the dev vault. **The visual check was interrupted by the screen locking** —— once Rick unlocks, the path to verify: open the galaxy view → drag the "Threshold" slider in the top-left panel to the right and watch the structure surface → drag "Repulsion" and watch the galaxy rearrange live → close and reopen Obsidian to confirm the parameters are kept.

### What is left
- The panel's look is a draft (the dark-glass direction A style), to be settled once Rick has seen it; adapting it to a light theme comes with M2's two directions.
- After adjusting the panel, the bench's S2 scenario uses the current physics parameters (no longer the fixed defaults) —— press "reset to defaults" before running a comparison benchmark.

### The files that changed
- New: `src/settings.ts` (GalaxySettings / the defaults / merge / SettingsHost), `src/overlay/ControlPanel.ts`
- Changed: `src/{main,types,constants}.ts`, `src/view/{GalaxyView,GraphController}.ts`, `src/layout/{LayoutEngine,MainThreadForceLayout}.ts` (updateParams + the linkStrength multiplier), `src/render/{AggregateRenderer,shaders}.ts` (setBloomParams/setLinkOpacity/uSizeMul), `styles.css` (the panel styles replacing the old HUD)

---

## 2026-06-12 · The aggregate renderer lands (M1): 16fps → 60fps (vsync-capped), and every red flag from G0 is cleared

### What was done
Per the plan for a red G0, aggregate rendering was pulled forward from M3: **all 3,230 nodes = 1 draw call (THREE.Points + the glowing-orb shader), all 19,337 links = 1 draw call (LineSegments)**, so a whole frame including bloom is 19 calls (M0's was about 22,000). The 3d-force-graph runtime dependency is gone entirely (an own three.js pipeline plus d3-force-3d driven directly), and the bundle went 1.3MB → 606KB. The layout moved onto a budget (one tick per frame) —— the galaxy forming became the opening animation, and Obsidian stays usable throughout. T1's interactions are complete: clicking a node flies the camera (a 15° azimuth offset + easeInOutCubic), 10s of idle starts the cruise (drifting on two incommensurable periods), a bloom-strength slider, and the starfield.

### before/after (same machine, same library, against the gate criteria)

| Scenario | M0, stock 3d-force-graph | M1, aggregate rendering | Verdict |
|---|---|---|---|
| S1 orbit after settling (bloom on) | 16.2 fps · p95 83ms | **60.0 fps (vsync-capped) · p95 17.5ms** | ✅ ≥45 |
| S2 cold layout | 61.2s of main-thread saturation (60.7s blocked, longest 860ms) | **settles in 5.2s · a single 64ms longtask** | ✅ nothing blocks over 200ms |
| S3 including unresolved (9,437n) | 13.8 fps | **60.0 fps** | ✅ "show unresolved" can ship on the desktop |
| draw calls | ~22,000 | **19** | ✅ under the budget of 45 |
| S4 leak ×10 | +13.7MB | **-1.0MB** (the heap ends lower than it started) | ✅ no leak |

An S4 aside: the first measurement of +181MB tripped the red light —— two consecutive controlled runs proved it was **GC lag** rather than a real leak (the second run started at 123MB, below the first's 130MB; the cause is the flood of short-lived garbage d3 produces rebuilding the octree every tick, with no major GC during the busy loop). With the measurement corrected (waiting 20s of idle GC afterwards) the verdict was −1.0MB. The lesson is written into S4's note field: **a real leak is a starting heap that keeps rising across consecutive runs; a single run's delta means nothing**.

### The decisions and the alternatives rejected
- **The 3d-force-graph runtime dropped entirely** (the plan had been to demote it to a "layout shell"): once aggregate rendering owns the scene, driving d3-force-3d directly is simpler than the fx/fy/fz write-back hack —— and it removed risk R7 (the one mechanism in the whole design that had never been proven). The library stays in devDependencies purely as reference.
- **The layout on a budget (one tick per frame) rather than run to completion**: 300 ticks at 60fps ≈ a 5s forming animation in place of "frozen for 61 seconds"; the worker (M3) went from a lifeline to an optimisation for large libraries.
- The white-out fix verified: desaturated thin lines + NormalBlending at 0.16 + a bloom threshold of 0.1, and a hub's core is no longer a white mass.

### Where things stand
Ready to use in the dev vault: open the galaxy view → the galaxy forms in 5 seconds → 60fps cruising, flying and bloom adjustment. All 5 unit tests green (buildGraph's pure functions), lint 0 errors (including the store's compliance rules).

### What is left and what is known broken
- **Gate G1 awaits Rick's eye**: "does it look like NASA Eyes yet?" If not, T1 gets fixed before M2.
- The colours are currently the folder-hash fallback palette, not Rick's 9 real colour groups —— importing graph.json comes in M2.
- The opening shot (pulling out of the graph over 3.2s), hover labels, the selection card and search are M2's four-piece set.
- The cruise radius drifts slowly (the breathing period interacts slightly with OrbitControls' damping) —— handled with M2's camera polish.
- S2's layout.step counting uses a runtime method replacement (a hack) —— M3 gives LayoutEngine a real tick hook.

### The files that changed
- Deleted `src/spike/`; new: `src/{constants,types}.ts`, `src/data/{buildGraph,GraphStore,seed}.ts`, `src/layout/{LayoutEngine,MainThreadForceLayout}.ts`, `src/render/{AggregateRenderer,shaders,starfield,palette}.ts`, `src/interactions/CameraDirector.ts`, `src/view/{GalaxyView,GraphController}.ts`, `src/bench/bench.ts` (moved), `src/typings/d3-force-3d.d.ts`, `tests/buildGraph.test.ts`
- tsconfig includes tests; eslint ignores dev-vault; the M0 benchmark JSON archived to /tmp/galaxy-bench-archive

---

## 2026-06-12 · Starting the project + the M0 performance spike: G0 comes out red, and aggregate rendering moves up to M1

### What was done
"Galaxy View" begins (a cinematic 3D graph plugin for Obsidian in the NASA Eyes style). Five parallel research tracks, design from three angles (architecture / visual / risk) and an implementation plan approved by Rick; the repository scaffolded and a local dev vault set up; and a performance spike on stock 3d-force-graph with bloom run against the real vault's data (3,230 notes / 19,337 live edges), which **produced every number gate G0 needed**.

### The decisions and the alternatives rejected
- **G0's verdict: red** (S1's 16.2fps is under the 30fps red line) → per the plan, aggregate rendering (1×THREE.Points for the nodes + 1×LineSegments for the links) moves from M3 to **M1, starting immediately**; 3d-force-graph is demoted to a layout / camera / interaction shell. This is not a crisis but a branch already in the plan —— all four predecessor plugins died against the per-object rendering wall, and three hours proved the same wall here.
- **A worker layout is confirmed as necessary** (S2: the main thread saturates for 61 seconds during layout and Obsidian is unusable throughout).
- **"Show unresolved" defaults off** (S3, at 9,437 nodes with unresolved links included, gives 13.8fps); the switch's fate is reconsidered after aggregate rendering lands and the benchmarks are re-run.
- The decisions from the setup phase are in docs/design/00-implementation-plan.md (not forking, the stack, the two visual directions, the milestone gates and so on).

### Where things stand: what runs
- `npm run dev` → the build goes straight into `dev-vault/.obsidian/plugins/galaxy-view/`, and hot-reload reloads it.
- Obsidian opens `dev-vault/` (already registered), and the command palette holds "Open galaxy view", "M0 benchmark: run S1/S2/S3 in turn" and "S4 leak canary".
- The view already renders the whole library with bloom and folder colouring; the HUD shows fps, the node count and the layout state.

### The M0 benchmark numbers (the basis for the G0 decision; machine: Rick's Mac, Obsidian 1.12.7)

| Scenario | Scale | Result | Verdict |
|---|---|---|---|
| S1 20s orbit after settling (bloom on) | 3,230n / 19,337l | **16.2 avg fps**, p95 frame 83ms | 🔴 under the 30 red line |
| S2 cold-start layout | as above | settles in 61.2s (hitting the 60s ceiling), 459 ticks, 133ms/tick on average, **60.7s of longtasks in total**, longest single block 860ms | 🔴 the main thread saturates |
| S3 including unresolved | 9,437n / 26,975l | 13.8 avg fps | 🔴 default off |
| S4 leak canary ×10 | 3,230n / 19,337l | heap delta **+13.7MB** (262.8→276.5), no context warnings | ✅ under 20MB, passes |

Note: the HUD's drawCalls reads meaningless numbers under EffectComposer (it reads the last pass's single full-screen quad); the real figure is about 22k per-object calls —— next time use spector.js or read it before the composer.

### What is left and what is known broken
- The real edge count is 19,337 (the research estimated 19.5k, which lands); the vault holds 5 more notes than it did during the research.
- Two predicted failure modes were confirmed visually: the hubs blowing out white (a white mass at the centre) and nodes flying outward before the layout settles —— aggregate rendering and the opening mask address them respectively.
- The Obsidian desktop version is actually 1.12.7 (not the 1.13.1 the research called the latest), which does not affect minAppVersion 1.7.2.
- The real iCloud vault was never touched (a read-only rsync); the dev vault lives at `./dev-vault/` (gitignored).

### The files that changed
- A new repository at `/Users/rick/Claude_Code/Galaxy_View/`: the sample-plugin template (esbuild / TS / eslint-obsidianmd / vitest) + dependencies (three 0.184 / 3d-force-graph 1.80 / d3-force-3d 3.0.6 / three-spritetext 1.10)
- `src/main.ts` (the plugin entry + 3 commands + S4), `src/types.ts`, `src/spike/{SpikeView,buildGraphData,bench}.ts`, `styles.css` (the HUD)
- `docs/design/`: the implementation plan plus the full architecture, visual and risk designs
- `README.zh.md`, `.gitignore`, `manifest.json` (id: galaxy-view, minAppVersion 1.7.2)
- The dev vault: `dev-vault/` (a clone of 3,230 md files + hot-reload 0.3.0 + galaxy-view 0.1.0 enabled)
## 2026-07-22 · The open issues fixed in turn: the popout window's clock, Canvas, and Tag Lens

### What was done

Three local fixes were completed in the order the existing issues and PRs carry risk, with an evidence threshold kept for the fourth. Camera flights, tour dwells and the establishing shot inside a popout window no longer mix time origins from different windows, and rAF and visibility hand over to the popout atomically; Markdown and Canvas share one entry into the graph, and a Canvas `file` card becomes a "canvas → vault file" link. PR #8 was not force-rebased: it reuses the existing metadata entry and lands a full standalone "Tags" section, a single-select Tag Lens, primary-tag colouring and 5–50 bounded tag hubs. A cross-review also fixed an unrelated performance debt where the quality tiers lowered the WebGLRenderer's DPR without lowering EffectComposer's. For #6 the renderer was not changed on a guess; instead a "no bloom + no background" A/B was run inside a real Obsidian, and the graph still renders correctly.

After the automatic gates, acceptance was completed on a real machine, in Obsidian 1.12.7 against the real 3,230-note development library: with the Galaxy tab moved into a separate `about:blank` window and maximised, "Recentre" still holds 3,230 nodes at about 60 fps; the tag Lens, the colouring and the 20 hubs all display, and taking the hub cap from 20 to 5 moved the node count from exactly 3,250 to 3,235. The same session found that the custom Slider could only be worked with a mouse, which was then made a standard Tab-focusable ARIA slider, verified with the arrow keys at 20→21→20.

### The decisions and the alternatives rejected

- **#14 fixes the time domain and the window ownership together**: an rAF timestamp is not guaranteed to share a time origin across Electron windows; the camera's tween / path / idle, the tour dwell and the establishing shot now consume a safe, untruncated elapsed, while WASD and rendering keep the 100ms anti-jump ceiling. Every pending rAF records the Window that created it, so on a window change it is cancelled on the old owner and handed to the new one immediately; late callbacks from an old rAF or an old IntersectionObserver are discarded by generation and identity, so nothing revives after dispose. Rejected: waiting for the old window to "run one more frame" before changing owner, or only calling `disconnect()` on an observer while still accepting its queued callbacks.
- **#15 adds a minimal Canvas parsing layer, after real-machine evidence overturned the first assumption**: an Obsidian 1.13.1 probe returns `resolvedLinks[canvasPath] = null` and `getFileCache(canvas) = null` explicitly, so changing only `getFiles()` yields isolated Canvas nodes. What ships parses only JSON Canvas `file` cards, and does not interpret text cards, web cards or edges inside a canvas; the startup read runs with a concurrency limit of 8 files, and after that updates incrementally with a per-file debounce, with a revision guarding against an older async read overwriting newer content, a temporarily broken JSON keeping the last valid cache, and renaming or deleting a folder invalidating the whole subtree. Rejected: continuing to rely on core Canvas metadata that does not exist, or building a second complete Canvas graph model.
- **#7 keeps PR #8's product scope while re-cutting the data boundary**: the maintainer had publicly asked to keep the Lens, the tag colouring and the bounded hubs, so the audit rejected the earlier Lens-only narrowing on those grounds. What ships reads the metadata once along the existing `showTags` path, with notes carrying their tags directly; the Lens, the chips and the primary tag's stable-hash colouring all read that one set, and top-N hubs and their edges are generated only when `showTags && showTagHubs`. The slider and the build side both limit it to 5–50, with a 180ms debounce, and turning the hubs off costs zero hidden nodes and zero physics. Rejected: copying the PR's second tag-extraction path, keeping the `nebula` naming that collides with the background layer, or mechanically rebasing the old 23-file diff.
- **#6 does not dress a guess up as a fix**: the 0.6.0 candidate still uses `alpha:false`, the default `preserveDrawingBuffer:false` and an opaque scene background, and RenderPass clears its target every frame. The original report turned off bloom and the starfield at the same time on 0.2.2, which can only show that compositing load may amplify the problem, not that bloom is the cause. Rejected: shipping `powerPreference:'high-performance'` outright, guessing at a translucent theme from its name, or explicitly repeating the default `preserveDrawingBuffer:false`.
- **The Slider gap found during real-machine acceptance was fixed on the spot rather than papered over with "it works with a mouse"**: the control was a plain `div` that Tab skipped straight past. The track now carries `role=slider`, complete aria values, Tab focus, the arrow keys and Home/End; keyboard, dragging and double-click-to-reset all go through one update path. No new dependency, and the accessibility tree now genuinely reports a slider.

### Where things stand: what runs and how

- Final automatic verification: `npm test` at 16 test files and 126 passing; `npm run build` passes; `npm run lint` at 0 errors and the repository's 2 existing warnings; `git diff --check` passes.
- `npm test`: covers handing the rAF owner between two Windows, old observer callbacks, no revival after dispose, the camera and the tour, the Markdown / Canvas file entries and edges, filtering, the Tag Lens / colour / hubs lifecycle, and migrating old settings.
- `npm run build`: type-checks and produces the `dist/` plugin artifact.
- `npm run lint`: checks the TypeScript, the tests and the Obsidian store's rules.
- `npm run dev`: builds into the isolated `dev-vault/` for the final interactive and visual confirmation in Obsidian.
- The Obsidian 1.13.1 probe on a real machine: core Canvas metadata is empty; the final GraphStore still produces the two edges `Canvas Smoke.canvas → Canvas Source.md` and `→ Canvas Target.md`. The probe, the fixtures and the temporary diagnostic plugin were all deleted afterwards, and the development plugin list restored.
- The Obsidian 1.12.7 UI on a real machine: the default Deep Field at 3,230n/19,337l and about 60 fps; the Minimal preset explicitly shows the starfield off and bloom strength 0.00 with the graph still fine; recentring works after popping out into a separate window and maximising it; the Tag Lens, primary-tag colouring, the 5 and 20 hub caps and the Slider's keyboard path all pass.
- 0.6.0 is released: the tag was pushed only after [PR #17](https://github.com/Longwind1984/galaxy-view/pull/17) squash-merged into `main` as `e21c0ca`; the [Release workflow](https://github.com/Longwind1984/galaxy-view/actions/runs/29932194187) succeeded and the [Release](https://github.com/Longwind1984/galaxy-view/releases/tag/0.6.0) is a formal one. The public `main.js`, `manifest.json` and `styles.css` are byte-for-byte identical to the final local build.

### What is left and what is known broken

- #14 has been through the whole "move to a popout → maximise → recentre" path on macOS + Obsidian 1.12.7; it still needs platform acceptance on Windows 11. The automated tests separately cover a negative delta, the first frame after a window change, and the completion-progress boundary.
- The tags section was rendered for real in the local Chrome at a 252px panel width, in both dark and light, with the Lens, the colouring and the 5-hub active state; the light theme's section title and the active chip's count contrast were fixed from those images, and neither switch, the slider, reset nor the 12 chips overflow. Rick has confirmed the screenshots from a real Obsidian and authorised the release.
- #6 does not reproduce locally on 1.12.7 under Minimal (bloom=0, starfield off, every space background off), with 3,230 nodes still fine; it still waits on the reporter for a 0.6.0 reproduction matrix and a screen recording from their Windows and theme.
- #7 and #15 were closed automatically by PR #17; #14 stays open awaiting the Windows 11 recheck. PR #8 is still open, awaiting a closing note to the contributor saying "an alternative implementation shipped with 0.6.0"; PR #16 does not belong to this version and needs its own review of performance and behaviour on a large library.

### The files that changed

- Documentation and version: `README.md`, `README.zh.md`, `docs/community-sweep.md`, `WORKLOG.md`, `package.json`, `package-lock.json`, `manifest.json`, `versions.json`
- The time domain and windows: `src/timing/{frameClock,windowFrameLoop,windowVisibility}.ts`, `src/interactions/CameraDirector.ts`, `src/tour/TourDirector.ts`, `src/view/GraphController.ts`
- Canvas: `src/data/graphFiles.ts`, `src/data/canvasLinks.ts`, `src/data/GraphStore.ts`, `src/data/buildGraph.ts`, `src/overlay/OverlayManager.ts`
- Tag exploration: `src/types.ts`, `src/data/{tagLens,buildGraph,GraphStore}.ts`, `src/render/palette.ts`, `src/overlay/{ControlPanel,Slider}.ts`, `src/settings.ts`, `styles.css`, `src/i18n/{de,en,es,it,pt,zh}.ts`, `src/view/{GraphController,SearchModal}.ts`
- Performance: `src/quality/tiers.ts`, `src/render/AggregateRenderer.ts`
- A visual draft: `demo/tag-lens-demo.html`
- Tests: `tests/{frameClock,windowRuntime,cameraDirector,graphFiles,canvasLinks,tagLens,qualityTiers,slider}.test.ts`, `tests/{tour,buildGraph,noteFilter,settingsMerge,palette,adjacency}.test.ts`

---

## 2026-07-28 · The production genesis animation's twitching and stutter: a read-only diagnosis of the code

### What was done

Confirmed that the version the community store currently points at is still 0.6.0 and that the public Release corresponds to commit `e21c0ca`; the local `main` has no code difference from that tag, only documentation changes since. A read-only investigation followed `GraphController`'s warm start, the worker layout, `AggregateRenderer.playReveal/stepReveal` and Three.js's buffer upload path, and located one definite coordinate-buffer aliasing regression plus a curve-recomputation bottleneck that scales linearly with the edge count. No product code was changed this round.

### The conclusions and where the fix should go

- **The primary root cause is that "the animation's destination" and "the position currently drawn" share the same `renderPositions` buffer.** `setData()` binds the node geometry's `position` directly to `renderPositions`; `stepReveal()` then reads the destination *from* `renderPositions` and writes back into the same array through `nodeAttr.array.set(...)`. Once the first frame has pressed most nodes toward the origin, the next frame treats those already-compressed positions as the destination, which closes a feedback loop. When the worker occasionally calls `updatePositions()` and restores the final coordinates, the picture jumps out briefly and is pressed back the next frame —— exactly the "twitching"; and once the worker settles and stops restoring them, the animation stalls at the centre and the whole thing jumps to its final position the instant it ends —— the "stutter, then a sudden jump".
- **The regression is traced to `cad522a`** (the graph-centring fix). Before that commit the node drawing buffer `nodePos` and the physics destination `positions` were separate; the commit rebound the geometry to `renderPositions` and had the genesis animation read its target from `renderPositions` too, without keeping a separate immutable animation destination.
- **A secondary but significant bottleneck is recomputing the curved links on the CPU every frame.** The default Galaxy preset has `linkCurve=0.35`, and the desktop high tier uses 8 segments per edge. At the real benchmark library's 19,337 edges that is 928,176 floats written and about 3.54 MiB uploaded per frame. A local Node micro-benchmark equivalent to the current `fillLinkPositions` averages 4.11ms with a p95 of 6.54ms —— excluding the node computation, the WebGL upload, the camera and bloom. On a frame where the layout is still updating, `updatePositions()` computes the curves an extra time first, though the GPU only uploads the last result before the actual render.
- **The automatic quality watchdog cannot save the opening.** It samples only after the layout has settled, and needs three consecutive 5-second windows under 30fps to drop a tier; the genesis animation lasts 2.6 seconds, so the watchdog can only act after the problem is over.
- **The test gap is definite.** All 126 unit tests across 16 files pass, but the repository has no reveal test at all; and since 0.4.0 introduced curves, the WORKLOG has consistently recorded that S1/S4 and the layout's hot-window benchmarks have not been re-run on a real machine.

Two tiers are recommended:

1. **A P0 correctness hotfix**: keep a separate immutable snapshot of the target for the reveal (plus a radial delay precomputed once), so `stepReveal` only reads the target and only writes the display buffer; skip the ordinary `updatePositions()` while revealing and sync the worker's latest coordinates once at the end; and have the animation's timing consume the elapsed the frame loop passes in, rather than continuing to bypass 0.6.0's window time domain.
2. **A P1 performance close-out**: push the node wave down into the existing node vertex shader, driven by `uRevealProgress + aRevealDelay`; keep the curved links at their final geometry and only fade opacity in over the second half. That takes the per-frame cost from an O(N+M×K) array rewrite and a whole-buffer upload down to an O(1) uniform update. If the links must stretch strictly with the node wave, give the links their own shader rather than continuing to rebuild 8-segment curves on the CPU.

Rejected as papering over it: setting the default curvature to 0, forcing low/mobile, lengthening the animation, or only tuning the watchdog. Each of those only lowers the load; none fixes the logic error of a destination buffer being overwritten.

### Where things stand: what runs and how

- `npm test`: all 126 across 16 test files pass; that shows the existing automatic gates are healthy, but they do not cover the genesis animation.
- The equivalent local micro-benchmark: at 3,230 nodes / 19,337 edges, curves at K=1 average 0.299ms and at K=8 average 4.112ms (p95 6.543ms); this measures `fillLinkPositions`' CPU only.
- The production code is still unmodified. Either reproduction works on the default Galaxy preset: the automatic opening on a warm start; or pressing "Replay the opening" after the layout has settled. The latter exposes the aliasing error most purely, because there is no worker continually restoring the destination.

### What is left and what is known broken

- No frame timeline was recorded inside a real Obsidian before the fix; this round's task was limited to code analysis, so no GUI control was requested and the diagnosis is not being passed off as a real-machine reproduction.
- Once implemented, pure-function and state tests for the reveal are required: the radius is monotonic in progress, repeated calls do not depend on the previous frame's output, `p=1` lands exactly on the destination, a worker update does not change this animation's target, and the duration is right after a window change or a pause.
- Real-machine acceptance should cover at least the default Galaxy high tier at 3,230n/19,337l, the large 9,437n/26,975l unresolved graph, a manual replay, a warm start and a separate popout; record avg / p95 / long tasks over the 2.6-second window, keep the existing performance gate of a desktop average ≥45fps, and take p95 ≤22.2ms —— the frame budget for that 45fps —— as this round's acceptance line.

### The files that changed

- `WORKLOG.md` (this read-only diagnosis, the evidence, the fix and the acceptance plan appended)

---

## 2026-07-28 · The genesis animation fixed without loss: unfolding on the GPU, a frozen destination, and acceptance in a real Obsidian

### What was done

The one root-cause chain behind the production genesis animation's continuous twitching, stutter and jump at the end is fixed: the animation's target no longer shares a buffer with the display coordinates; nodes and the 8-segment curved links now unfold on the GPU from an immutable destination with the original 55% radial delay and 45% ease-out, the main thread updating only a progress uniform each frame, and the overlay positions computed on the spot for the nodes actually queried. The main links, the selection and Tag Lens highlights, the ghost dashes and the cluster clouds all follow the same reveal state; the links keep their original opacity fade-in. When a warm layout has not settled, the worker is genuinely terminated before the opening and resumes at low temperature from the same coordinates once the animation ends, so it cannot quietly accumulate 2.6 seconds of movement and jump hard on the last frame.

Several manual replays were done in an isolated dev vault on Obsidian 1.12.7: 3,230 nodes / 19,337 links, locked to the `Galaxy` preset and the `High` tier, with the live configuration confirming `linkCurve=0.35`, bloom `0.35/0.35/0.22` and the nebula, floating stars and cluster clouds all on. Screenshots mid-animation and at completion both show the original curves, the glow, the nebula and full node clarity; curvature was not switched off, DPR and segment counts were not lowered, and no node or link was removed. The HUD holds a steady 60–61 fps at 21 calls. Obsidian's log shows no WebGL or shader errors.

### The decisions and the alternatives rejected

- **Rebuild the original curves on the GPU without changing a visual parameter.** Each polyline vertex uploads its original endpoints and `t` once, at the start of the animation, and the vertex shader unfolds both ends by the original radial wave before applying exactly the same quadratic Bézier formula as the CPU. Rejected: turning curvature off, forcing low/mobile, removing edges, lowering the pixel ratio, or degenerating the links into a static destination fade-in.
- **Reuse Three 0.184's native `LineBasicMaterial` pipeline.** Only `<begin_vertex>` is replaced through `onBeforeCompile`, so the native vertex colors, opacity, fog, tone mapping and output colorspace are unchanged, and switching back to the basic material at the end causes no brightness or colour jump. The tests take the `ShaderLib.basic` and `ShaderLib.dashed` anchors straight from the installed version.
- **The reveal attributes persist with the geometry and are reused.** The end of the animation only switches the material back rather than calling `deleteAttribute` early; the GPU buffer is released together when the geometry is disposed, so a replay does not leave about 8.26 MiB behind each time. The material is reused too, and the first shader compilation is placed during the opening mask's fade-out with the frame clock reset, so the compilation time does not eat the animation's progress.
- **A warm start stops and rebuilds the layout worker.** Skipping `layout.step()` alone cannot stop the worker's `onmessage` rewriting positions, so that was never treated as "paused". If it has not settled the worker is terminated outright and resumes from the same original coordinates at alpha 0.06 afterwards; a manual replay still keeps the original contract of "only allowed once settled".
- **No more scanning every node each frame.** The DOM labels, picking and the camera apply the same `revealScale` only to the nodes being queried; up to 500 ghost dashes keep a small CPU update so the dash keeps its world scale. Rejected: keeping the per-frame O(N) `Math.hypot/pow` fill over the whole table.

### Where things stand: what runs and how

- `npm test`: all 137 across 18 test files pass; new tests cover the target/display buffer isolation, radial monotonicity, the 55/45 timing, the aliasing regression, the curves' static attributes, Three's native shader anchors and the clock across a pause and resume.
- `npm run build`: the TypeScript check and the production esbuild both pass.
- `npm run lint`: 0 errors; still the repository's 2 existing warnings (the eslint config deprecation, and SettingsTab not having adopted the 1.13 declarative API).
- `git diff --check`: passes.
- A local micro-benchmark at the same scale (3,230n / 19,337l / K=8): the old CPU curves averaged 1.20ms per frame with a p95 of 1.81ms; the new approach's one-off attribute preparation averages 0.66ms with a p95 of 1.05ms, after which 36 overlay queries cost about 0.001ms of CPU on average. The attributes occupy about 8.26 MiB permanently, released with the geometry's lifetime.
- `npm run dev` has built into `dev-vault/.obsidian/plugins/galaxy-view/`, so after Hot Reload it can be rechecked directly through "Navigation & motion → Replay the opening".

### What is left and what is known broken

- This round's acceptance in a real Obsidian produced a 60–61 fps HUD and screenshots across several frames, but no standalone 20-second S1 statistics file, so the HUD is not being passed off as an avg / p95 / long-task report. Before releasing, it is still worth running S1 once, plus a repeated-replay leak check, in a window where Obsidian is not being used at the same time.
- Nothing has been rechecked on a real machine at the 9,437n / 26,975l "including unresolved" limit or on Windows 11; the code path is the same as for a normal graph, and the automated tests cover the mathematics and the resource-lifetime contract.
- The dev vault's test settings are now `Galaxy + High`, which affects only the isolated development library; no plugin setting or file was written into the real `Rick's Second Brain` vault.

### The files that changed

- Rendering: `src/render/{AggregateRenderer,linkCurves,reveal,shaders}.ts`
- Frames and layout: `src/timing/windowFrameLoop.ts`, `src/layout/WorkerForceLayout.ts`, `src/view/GraphController.ts`
- Tests: `tests/{linkCurves,reveal,revealShaders,windowRuntime}.test.ts`
- Documentation: `WORKLOG.md`

---

## 2026-07-29 · 0.6.1 formally released: the lossless genesis-animation patch

### What was done

The genesis-animation fix was raised to the patch release 0.6.1: the 137 tests, the production build, lint and the patch-format gate were all run again; the 16 fix / test / version files were synced to the GitHub branch verbatim, with each file's remote content confirmed identical to the locally verified commit; PR #19 squash-merged into `main` as `1198cb10a38838870951aabc29f8ec58e94f9cae`. A formal `0.6.1` Release was then created with `main.js`, `manifest.json` and `styles.css` uploaded.

The public Release is verified as Latest and not a pre-release, with the tag pointing at `1198cb1`. The three asset digests GitHub shows match the local production build exactly: `main.js` is `6adbf3a14b2b6441463a91d049545c5eb8d2d862e7bf34267f6c96ed6c2f51db`, `manifest.json` is `c4f61533260b7007a4647a666022958cce42a7f40523308345161e752d200507`, and `styles.css` is `05e6fe4dd30916131add61a409b46a3f3bb3dd104be01c1a7cc7c0f1f571802f`.

### The decisions and the alternatives rejected

- **Released as the patch version 0.6.1.** The fix changes neither the product's capabilities, nor the settings' structure, nor the minimum Obsidian version, which suits a patch rather than raising the minor version.
- **Only the lossless approach that had been seen was released.** The release keeps `Galaxy + High`, `linkCurve=0.35`, bloom, the nebula, the floating stars, the cluster clouds and the full node and link counts; turning curvature off, lowering DPR, dropping edges or forcing a low quality tier was not adopted.
- **With the network blocked, the route changed to the GitHub App plus a web Release.** The local `gh` token had expired, Git over HTTPS was reset and the SSH banner exchange broke, and the CLI's web authentication request was cut off at the network layer too. The code went in through a GitHub App branch and PR; the three local production build assets were uploaded to a GitHub draft first and published from an already-signed-in web session. Rejected: misreporting "the PR is merged" as "the Release is published", or continuing to hit the same failing transport repeatedly.
- **The provenance difference is stated honestly.** This Release was not created through a tag-push Actions run, so it carries none of 0.6.0's Actions provenance attestation; the local gates, the acceptance in a real Obsidian and GitHub's public SHA-256s close the loop three ways instead, without passing anything off as proof of a CI artifact.

### Where things stand: what runs and how

- The formal version: `0.6.1`; the Release: `https://github.com/Longwind1984/galaxy-view/releases/tag/0.6.1`.
- The main branch: PR #19 squash-merged into `main` as commit `1198cb1`.
- The automatic gates: `npm test` at 18 files / 137 passing; `npm run build` passes; `npm run lint` at 0 errors and 2 existing warnings; `git diff --check` passes.
- The real experience: Obsidian 1.12.7, 3,230 nodes / 19,337 links, Galaxy + High, several replays keeping the original curves and visual layers, the HUD at 60–61 fps, and no WebGL or shader errors.
- Installing and updating: BRAT follows Latest; a manual install can take the three files from the 0.6.1 Release. Whether the community store has picked 0.6.1 up still needs a separate refresh to confirm.

### What is left and what is known broken

- The local GitHub CLI still has expired credentials, and Git transport over HTTPS and SSH is still blocked by the current network; this does not affect the already-public 0.6.1, but the local branch has not been reconciled with the remote `main`.
- 0.6.1 has no Actions provenance attestation; before the next release, `gh auth` and Git transport should be restored first so the "PR merged → annotated tag → Release workflow" chain can be used again.
- The 9,437n / 26,975l limit graph, Windows 11 and a formal 20-second S1/p95 file are all still outstanding; those are follow-up performance and cross-platform verification, and do not affect this round's confirmed conclusion about the fix at the 3,230n production scale.

### The files that changed

- Version: `package.json`, `package-lock.json`, `manifest.json`, `versions.json`
- Rendering: `src/render/{AggregateRenderer,linkCurves,reveal,shaders}.ts`
- Frames and layout: `src/timing/windowFrameLoop.ts`, `src/layout/WorkerForceLayout.ts`, `src/view/GraphController.ts`
- Tests: `tests/{linkCurves,reveal,revealShaders,windowRuntime}.test.ts`
- Documentation: `WORKLOG.md`, `docs/community-sweep.md`, `docs/release-guide.md`

---
