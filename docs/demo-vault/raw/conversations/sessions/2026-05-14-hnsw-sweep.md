---
title: Session — HNSW sweep readout
created: 2026-05-14
summary: Parameter sweep results and the ef=64 decision
---
Readout of the HNSW sweep: recall plateaus at ef=64 M=16 for our corpus size; larger ef doubles latency for one point. Decision recorded to wiki with the sweep table. Action item: re-run the sweep when the corpus passes 50k documents, tracked in the flag registry with a date.
