---
title: Aurora — internal search revamp
tags: [aurora, search]
summary: Rebuild of the product search stack around hybrid retrieval
created: 2026-03-02
---
Aurora replaces the legacy Elasticsearch-only search with a hybrid stack: BM25 plus dense vectors fused with RRF. The service is written in Go, embeddings come from e5-small, and results carry source citations back to the design docs. Latency budget is 120ms p95. Rollout is gated behind the search-v2 feature flag.
