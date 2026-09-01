---
title: Redis eviction policy for sessions
tags: [auth, infra]
summary: noeviction plus TTL beats allkeys-lru for session data
created: 2026-03-25
---
Sessions must never be evicted by memory pressure — that logs users out at random. Redis runs noeviction with an alarm at 80% memory; TTLs bound growth. Capacity math: 40k sessions at 600 bytes is 24MB, three orders below the instance. allkeys-lru was rejected after the staging incident where a batch job evicted live sessions.
