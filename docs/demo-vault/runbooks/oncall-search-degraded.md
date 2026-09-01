---
title: Runbook — search latency degraded
tags: [aurora, runbook]
summary: Check embedder saturation first, then HNSW cache, then Redis
created: 2026-06-20
---
When p95 breaches 120ms: check embedder CPU saturation first (it degrades before it fails), then HNSW cache hit rate (cold after deploys), then Redis latency. Mitigations in order: scale embedder replicas, warm the cache with the replay script, fail over Redis. Every step is a command in ops/, not prose.
