# Galaxy View release guide

> For "me in three months with no context": how to put this local plugin on GitHub, give it to BRAT, and finally list it in the Obsidian community store.
> Current state: `0.6.1` was published as Latest on 2026-07-29; the repository is `Longwind1984/galaxy-view`.

## 0. Before releasing

First confirm the GitHub CLI login and Git transport both work:

```bash
gh auth status
git ls-remote origin HEAD
```

If the credentials have expired, log in again with `gh auth login`.  When `0.6.1` was released, both HTTPS and SSH transport were blocked at the network layer on this machine, so the PR was merged through the GitHub App and the assets uploaded through the web interface; the Release carries a public SHA-256 for verification but has none of the Actions provenance a tag-push workflow provides.  A later version should restore the automatic release chain below as a priority.

## 1. Merge the release branch

The repository root is `/Users/rick/Claude_Code/Galaxy_View` and the remote is `https://github.com/Longwind1984/galaxy-view.git`.  The release branch is merged into `main` through a PR first; do not tag directly from a dirty working tree.

```bash
cd /Users/rick/Claude_Code/Galaxy_View
git switch main
git pull --ff-only
```

> Note that `.gitignore` already excludes `dev-vault/` (the local clone of your notes), `node_modules/`, `main.js` and `dist/` —— your notes and build output will not be pushed.

## 2. Tag to trigger the automatic release

CI (`.github/workflows/release.yml`) watches for a tag push: it runs lint + test + build automatically, then publishes `main.js`, `manifest.json` and `styles.css` as release assets.

```bash
node -p "require('./manifest.json').version"
git tag -a 0.6.2 -m "Release 0.6.2"
git push origin 0.6.2
```

The tag has to match `manifest.json`'s `version` exactly, with no `v` prefix.  Check the repository's Actions page for a successful Release workflow; the Releases page should show a formal Release at that version, the three plugin attachments and a provenance attestation.

If the automatic chain is unavailable and a release is nevertheless necessary, a tag at the same version can be created from `main` on the GitHub Release page, then the locally built `dist/main.js`, `dist/manifest.json` and `dist/styles.css` (from `npm run build`) uploaded.  After publishing, every remote asset's SHA-256 must be checked one by one; this manual path must never be described as an Actions build.

## 3. Installing through BRAT (beta distribution / soak testing)

1. Install the BRAT plugin in any vault (search the community store for "BRAT")
2. BRAT → Add beta plugin → enter `Longwind1984/galaxy-view`
3. It fetches and installs the latest release automatically

BRAT follows the Latest Release, which suits continuous soaking before a release and verifying the upgrade path.

## 4. Listing in the community store (optional, once the soak is satisfactory)

The official process (through the portal from 2026, rather than the old PR-to-obsidian-releases):

1. Open `community.obsidian.md` in a browser, sign in with an Obsidian account, and link GitHub to prove repository ownership
2. Plugins → New plugin → accept the developer policy
3. The automatic review gives feedback; a change means a new release (the version has to be bumped —— `npm version patch` updates the manifest and versions and tags automatically)
4. **Check for duplicates before submitting**: search the store for `galaxy-view` / `Galaxy View` to confirm the id and name are unused (the id rule: it must not contain "obsidian", which is already satisfied)

The store listing also needs screenshots or a GIF (the README has none at present; find a few showing deep space, the galaxy preset and the focus card).

## The pre-release self-check (all passing; worth re-checking before submitting)

- [x] `npm run lint` with 0 errors (including the obsidianmd rules)
- [x] `npm test` all green (0.6.1: 18 test files, 137 tests)
- [x] `npm run build` succeeds, with all three dist artifacts present
- [x] No `innerHTML`/`outerHTML` (the cards use `createEl` throughout)
- [x] No network requests, no telemetry
- [x] No styles injected from JS (all of it in `styles.css`)
- [x] onClose destroys everything (the WebGL context, the worker, the events —— it passes the leak canary)
- [x] The benchmark commands are stripped from a store build (gated on `__GALAXY_DEV__`; measured 0 residue in prod)
- [x] The plugin id does not contain "obsidian"

## How the version number moves

```bash
npm version patch    # 0.6.1 → 0.6.2: syncs package / manifest / versions and creates a git tag
git push origin main
git push origin 0.6.2
```
`versions.json` records the minimum Obsidian version each plugin version requires (currently `0.6.1: 1.8.7`).
