# Community sweep

After listing, collect GitHub issues / PRs / store feedback on a regular basis and produce an at-a-glance table of "what to do in the next version".
**This is a living document**: each round overwrites the content below directly, and the history stays in the git log and `WORKLOG.md`.

---

## 2026-07-29 · the 0.6.1 creation-animation patch

- [PR #19](https://github.com/Longwind1984/galaxy-view/pull/19) was squash-merged into `main` as commit `1198cb1`.
- The [0.6.1 Release](https://github.com/Longwind1984/galaxy-view/releases/tag/0.6.1) is set as Latest and not a pre-release; it fixes the creation animation's stutter, jank and end-of-animation jump, without lowering curvature, DPR, the node or link counts, bloom or the background layers' quality.
- The three plugin assets' SHA-256s match the local production build: `main.js` is `6adbf3a…f51db`, `manifest.json` is `c4f6153…507`, `styles.css` is `05e6fe4…802f`.
- The machine's GitHub CLI credentials had expired and both HTTPS and SSH push were blocked at the network layer, so the code was merged through the GitHub App and the tag created and the verified local production build uploaded through the GitHub web interface.  So there is none of the Actions provenance attestation the 0.6.0 release carried; the functional gate is still 137 tests, a production build, 0 lint errors and acceptance in a real Obsidian.

---

## This round: 2026-07-22 (round 2)

### The baseline

| Metric | Current state |
|---|---|
| Published version | 0.6.1 (2026-07-29) |
| This round's result | PR #19 merged; the 0.6.1 Release and all three plugin artifacts verified |
| Open issues | 2: #6, #14 |
| Open PRs | 2: #8, #16 |
| Store downloads | to be refreshed after the release, not carried over from the 15 July snapshot |

### The at-a-glance table

| # | Type | Current evidence | 0.6.0 disposition |
|---|---|---|---|
| [#6](https://github.com/Longwind1984/galaxy-view/issues/6) | Bug: mouse ghosting on a translucent theme | Reported against an older version and a specific platform/theme; not reproduced on macOS + Obsidian 1.12.7 with Minimal, glow 0 and the starfield off, and 3,230 nodes still fine | No speculative renderer change, and the issue is not closed; after the release, ask the reporter for a Windows/theme A/B and a screen recording on 0.6.0 |
| [#7](https://github.com/Longwind1984/galaxy-view/issues/7) + [PR #8](https://github.com/Longwind1984/galaxy-view/pull/8) | Request: tag exploration | 0.6.0 shipped a standalone Tag Lens, primary-tag colouring and 5–50 top hubs; verified on a real device at 3,250n/22,307l for 20 hubs and 3,235n/21,186l for 5 | #7 was closed automatically by PR #17; PR #8 still needs a closing note saying "the feature landed through a re-cut implementation", rather than merging the old conflicting diff |
| [#14](https://github.com/Longwind1984/galaxy-view/issues/14) | Bug: recentre goes blank after moving to a new window | The rAF window ownership, the cross-window time domain and the stale-callback race are fixed; "pop out → maximise → recentre" completed on a real macOS device at about 60 fps with 3,230 nodes | Marked handled in 0.6.0, but the issue is kept open awaiting the Windows 11 reporter's final confirmation |
| [#15](https://github.com/Longwind1984/galaxy-view/issues/15) | Bug: Canvas does not appear in the graph | Obsidian's core provides no Canvas metadata; 0.6.0 added bounded JSON Canvas `file` card parsing, and a real device produced the two expected edges | Closed automatically by PR #17 |
| [PR #16](https://github.com/Longwind1984/galaxy-view/pull/16) | Feature: zoom-aware hierarchical labels | A new PR with no dependency on this round's three fixes; label collision and the screen-space cost as the node count grows are not yet audited | Not squeezed into 0.6.0; a separate round for code review, benchmarking and eyeballing on a real large library |

### The release gate

- Automatic verification: 16 test files, 126 tests passing; the build passes; lint is 0 errors and 2 pre-existing warnings; `git diff --check` passes.
- In a real Obsidian: the Canvas file-card edges, recentre in a separate window, the Tag Lens / tag colouring / 5 and 20 hubs, and the Slider's keyboard operation are all verified.
- Performance boundaries: tag hubs are off by default; enabled, only a bounded top-N of nodes is generated, with N limited to 5–50.  Handing over to a separate window adds no continuous polling.
- The release: [PR #17](https://github.com/Longwind1984/galaxy-view/pull/17) squash-merged into `main` as `e21c0ca`; only then was the annotated `0.6.0` tag pushed.  The [Release workflow](https://github.com/Longwind1984/galaxy-view/actions/runs/29932194187) succeeded and the [0.6.0 Release](https://github.com/Longwind1984/galaxy-view/releases/tag/0.6.0) is neither a draft nor a pre-release.
- Artifact verification: the publicly downloaded `main.js` (813,255 B), `manifest.json` (240 B) and `styles.css` (22,689 B) are byte-for-byte identical to the final local build; their SHA-256s are `8d1d2e…73722`, `b03a74…2102` and `05e6fe…802f`.

### Next round

- Refresh the 0.6.1 store downloads and the new-issue baseline.
- Follow up on #6's Windows / translucent-theme reproduction matrix, and #14's Windows 11 regression result.
- Give PR #8 a closing note about the published alternative implementation.
- Review PR #16 on its own: correctness, label occlusion, frame rate on a large library, and the zero-cost boundary when it is off by default.
