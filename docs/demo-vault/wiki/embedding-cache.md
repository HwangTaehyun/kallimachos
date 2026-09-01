---
title: Embedding cache by content hash
tags: [aurora, ml, performance]
summary: Re-embedding unchanged chunks is pure waste
created: 2026-05-28
---
Chunks are embedded once per content hash; edits only re-embed changed chunks. Cache hit rate in steady state is 96%. The cache key includes the model name, so a model swap invalidates cleanly instead of serving mixed-space vectors — the bug class the eval harness caught in April.
