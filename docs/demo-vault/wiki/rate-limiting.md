---
title: Rate limiting at the gateway
tags: [aurora, security]
summary: Token bucket per key, 429 with Retry-After
created: 2026-06-01
---
The gateway enforces a token bucket per API key: 20 requests per second sustained, burst of 40. Excess gets 429 with Retry-After. Buckets live in Redis with Lua for atomicity. Internal callers are exempt by network, not by key — spoofing a header should not lift limits.
