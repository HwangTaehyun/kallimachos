---
title: Deploy pipeline stages
tags: [infra, method]
summary: Build once, promote the same artifact through stages
created: 2026-06-05
---
One artifact per commit, promoted staging to canary to full. Canary takes 5% of traffic for 30 minutes with automatic rollback on SLO burn. The same binary moves through every stage — rebuilding per environment was banned after a staging-only flag leaked into a prod build that had been compiled separately.
