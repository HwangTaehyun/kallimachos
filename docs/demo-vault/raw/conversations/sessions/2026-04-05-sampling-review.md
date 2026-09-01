---
title: Session — trace sampling review
created: 2026-04-05
summary: Where tail-based sampling won the argument
---
Review of trace volume costs. Head sampling at 10% was losing 90% of error traces, which is exactly the diagnostic set. Tail-based buffering keeps all errors for a bounded memory cost. Agreed to adopt Tempo with a 30-second decision window; alert rules move to burn-rate SLOs in the same quarter.
