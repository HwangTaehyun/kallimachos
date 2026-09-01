---
title: RRF fusion weights
tags: [aurora, search]
summary: Reciprocal rank fusion with measured weights beat learned rerankers
created: 2026-04-22
---
We fuse four signals in Aurora: BM25, chunk vectors, entity vectors, and relation vectors. A grid search over 991 judged pairs picked the weights; reciprocal rank fusion with k=60 outperformed a cross-encoder reranker at 1/40th the latency. The cross-encoder stays in the eval harness as a quality ceiling reference.
