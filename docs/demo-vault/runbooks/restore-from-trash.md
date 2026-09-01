---
title: Runbook — restore a deleted document
tags: [casket, runbook]
summary: Reference row re-insert within the 14-day grace window
created: 2026-05-01
---
Within 14 days of deletion the blob still exists; restore is re-inserting the reference row from the audit log and reindexing the document. After the grace window the blob is swept and restore means re-ingesting from the original source. The audit log keeps the mapping either way.
