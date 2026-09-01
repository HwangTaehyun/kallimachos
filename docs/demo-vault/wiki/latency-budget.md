---
title: Latency budget breakdown
tags: [aurora, performance]
summary: Where the 120ms p95 goes
created: 2026-05-02
---
Aurora's 120ms p95 budget splits as: 15ms gateway, 25ms BM25, 40ms vector search, 10ms RRF fusion, 20ms hydration, 10ms headroom. Vector search is the risk item; HNSW ef=64 keeps recall at 0.97 while staying under budget. Exceeding ef=128 doubles latency for one point of recall.
