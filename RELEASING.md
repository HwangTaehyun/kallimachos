# Releasing

This repository has **two version lineages**, and they count different things. Not knowing that
makes `0.1.0` and `0.6.1` sitting side by side look like a bug.

| Lineage | Single source of truth | What it counts | Tag |
|---|---|---|---|
| **Project** | `version` in `.claude-plugin/plugin.json` | all of kal — pipeline · MCP · web UI · CLI | `v<X.Y.Z>` |
| **Obsidian plugin** | `version` in `plugin/manifest.json` | the Obsidian viewer alone | none (the Obsidian registry reads the manifest) |

The plugin is at `0.6.x` because it was **inherited from upstream** — it kept the numbering from
when it lived as `galaxy-view`, before being absorbed into this project. Rewinding it to match
the project version would break updates for anyone who already installed that plugin.

## Project release

```bash
# 1. Bump the version — **this is the single source of truth**
#    "version" in .claude-plugin/plugin.json

# 2. From the (private) workspace
just selftest          # boundary · fingerprints · undecided values · backup · links
just publish 0.2.0     # push kal/ only to the public repo + tag v0.2.0
```

What `publish` refuses to do:

- **Rejects** an argument that differs from `plugin.json` (the version is never written twice)
- Rejects a tag that already exists
- Rejects an uncommitted working tree
- Calls `check-publish` **itself** (the three-way name / content / history boundary check)
- Shows a human what will and will not go out, and asks for confirmation

## Tag **after** the push

Tagging first leaves a tag pointing at nothing if the push is rejected. The tag lands on the
**exact commit** that was just pushed (this repository's `HEAD`), so the tree the tag names and
the tree that went public are the same by construction.

## History is pushed whole —— with one recorded exception

This repository's history is pushed whole. Zero commits ever contained private documents (the two
history gates in `check-publish` verify that on every push), and squashing would erase the
contribution history along with it.

**The exception, once, after 0.1.2.** Everything up to that point was collapsed into a single
commit (`737e84a`) and the three tags were re-cut onto it.  That commit —— not a version number
—— is the boundary this section is about; **0.2.0 has not been cut**, `plugin.json` still says
`0.1.2`, and the 46 commits sitting on top of the collapsed root are what it will contain.

The reason is that the history had stopped being readable by the people it is for. This is a
public AGPL repository whose code, documentation, tests and comments are now English throughout,
and it reached that state through about seventy commits whose *own subjects and bodies were
Korean* —— the translation was performed by commits that were themselves untranslated. Rewriting
a commit message rewrites the commit, so there is no version of "keep the history and translate
it" that is not also a rewrite; the choice was between a public log a reader cannot read and one
commit they can. Nothing about the boundary changed: the two history gates still ran, and still
found zero commits carrying private documents.

⚠ **This cost real things, and they are worth naming so the next person does not reach for it
casually.** Every `git blame` line from before `737e84a` now says one thing. The reasoning behind
individual decisions —— which is normally recoverable from a commit body —— survives only where it
was also written into a comment or a document. And it was a force-push over published, tagged
commits, so anyone who had cloned the repository had to reset rather than pull.

**From the collapsed root on, the rule is the one above it: history is pushed whole** ——
and it has been: every commit since has gone out as written. Repeating this needs a
reason at least as strong, and it belongs in this section before it happens, not after.

## Not there yet

- **CHANGELOG** — the commit history plays that role for now. It gets written when the version
  number reaches two digits.
- **Automatic tagging** — at one-person scale, `just publish` does validation and tagging
  together. Once there are more contributors, "only a tree CI has validated may be tagged" needs
  to be enforced by machine.
