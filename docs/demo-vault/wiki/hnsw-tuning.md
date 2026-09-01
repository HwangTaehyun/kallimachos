---
title: HNSW parameter sweep
tags: [aurora, ml, performance]
summary: ef=64 M=16 is the sweet spot for our corpus
created: 2026-05-14
---
Swept HNSW parameters on the 3k-document corpus: M in 8/16/32, ef in 32/64/128/256. Recall@10 plateaus at 0.97 with M=16 ef=64; doubling ef to 128 adds one recall point for 2.1x latency. Build time scales linearly with M. Chose M=16 ef=64, revisit past 50k documents.
