---
title: Why the search service is Go
tags: [aurora, infra]
summary: Go over Python for the serving path
created: 2026-03-05
---
The Aurora serving path is Go: single static binary, predictable p99, no GIL contention under concurrent fan-out to BM25 and vector backends. Python keeps the offline pipeline (indexing, embedding, evaluation) where iteration speed matters more than tail latency.
