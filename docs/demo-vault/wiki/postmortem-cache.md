---
title: Postmortem — stale cache served deleted documents
tags: [aurora, incident]
summary: Deleted docs kept appearing in search for 36 hours
created: 2026-05-19
---
After the April purge, deleted documents kept surfacing in Aurora search results for 36 hours. Root cause: the inverted index rebuild skipped tombstones because df/idf statistics are global and the partial delete path never invalidated the BM25 cache. Fix: deletion now triggers a full index rebuild, and a nightly verifier diffs the index against the source of truth. Guard added to CI.
