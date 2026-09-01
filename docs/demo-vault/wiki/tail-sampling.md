---
title: Tail-based sampling decision
tags: [lighthouse, tracing]
summary: Why head sampling lost to tail sampling
created: 2026-04-05
---
Head-based sampling drops traces before knowing if they matter; error traces vanish at exactly the rate you sample. Lighthouse moved to tail-based sampling: buffer spans for 30 seconds, keep 100% of error traces and 10% of the rest. Memory cost is bounded by the buffer window. Grafana Tempo made this practical.
