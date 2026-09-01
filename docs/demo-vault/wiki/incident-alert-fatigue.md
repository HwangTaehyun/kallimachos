---
title: Postmortem — alert fatigue week
tags: [lighthouse, incident]
summary: 400 pages in one week, three actionable
created: 2026-05-06
---
After the Prometheus migration, threshold alerts fired 400 times in a week; three were actionable. Root cause: static thresholds copied from the old system without load context. Fix: burn-rate SLO alerts replaced 80% of threshold rules, paging only when the error budget burns faster than 14x. Pages dropped to single digits.
