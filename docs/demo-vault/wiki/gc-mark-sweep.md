---
title: Casket garbage collection design
tags: [casket, storage]
summary: Mark-and-sweep with a two-week grace period
created: 2026-04-18
---
Deleting a document removes its reference row, not the blob. Weekly GC marks every blob reachable from the reference table, then sweeps unmarked blobs older than 14 days. The grace period exists because restore-from-trash was used 23 times in the first quarter — instant hard delete would have made each one a data loss.
