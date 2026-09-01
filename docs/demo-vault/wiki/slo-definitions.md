---
title: SLOs for Aurora search
tags: [aurora, lighthouse]
summary: Availability 99.9, latency p95 120ms, error budget policy
created: 2026-05-10
---
Aurora's SLOs: 99.9% availability, p95 latency 120ms measured at the gateway, 28-day rolling window. When the error budget is exhausted, feature work pauses and reliability work takes the sprint. The budget burned twice in Q2: the stale-cache incident and the embedding model rollout.
