---
title: Postgres over DynamoDB for the reference table
tags: [casket, decision]
summary: Transactions and joins beat theoretical scale
created: 2026-04-14
---
Casket's reference table needs transactions (mark-and-sweep GC) and ad-hoc joins (cost attribution). DynamoDB offers neither without contortions. At the current scale — 2 million rows — a single Postgres with a replica is boring and sufficient. Revisit at 100x, which the growth model does not predict inside two years.
