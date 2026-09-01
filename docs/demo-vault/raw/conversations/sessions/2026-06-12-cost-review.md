---
title: Session — Q2 cost review
created: 2026-06-12
summary: Egress surprise and the delta-response fix
---
Walked the Q2 bill. Egress at 32% was the outlier; traced to dashboard polling shipping full payloads. Decided on delta responses plus a 60-second cache. Vector search cost fears did not materialise — 6% of spend. Follow-up: cost dashboard gets an egress-by-endpoint panel in Lighthouse.
