---
name: Search results look wrong
about: It runs, but the ranking or content it returns does not make sense
labels: retrieval
---

This repository treats retrieval-quality problems as **reproducible numbers**. With the following,
it gets fixed much faster.

## Query and results

```bash
just search "your query here"
```

```
# the top 5 with their scores. Redact filenames if they are sensitive — but keep the scores.
```

## What should have been at the top

## State

```bash
just status     # document and entity counts, what is stale
```

This part especially:

- **Did you run extraction** (`just run extract` → `just run index`)? If not, entities and
  relations are empty and that document's score is capped at 0.23.
- Did you use `--origin vault` / `--origin session`?
