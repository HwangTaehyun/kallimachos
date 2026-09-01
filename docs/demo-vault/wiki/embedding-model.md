---
title: Embedding model choice
tags: [aurora, ml]
summary: multilingual-e5-small beat larger models on our latency budget
created: 2026-04-08
---
Benchmarked three embedding models for Aurora: e5-small (384d), e5-base, and bge-m3. On our 3k-document corpus e5-small held 97% of e5-base's nDCG while embedding 4x faster on CPU. bge-m3 won on recall but blew the 120ms p95 budget. Choice: multilingual-e5-small, quantized, served in-process.
