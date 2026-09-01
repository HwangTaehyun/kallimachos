---
title: Loki label cardinality budget
tags: [lighthouse, logs]
summary: Keep label cardinality under 30 series per stream
created: 2026-04-11
---
Loki performance collapses when labels carry high-cardinality values. Rule for Lighthouse: labels are service, environment, level only. Request ids and user ids go in the log line, found by grep at query time. A CI check rejects config that adds labels beyond the allowlist.
