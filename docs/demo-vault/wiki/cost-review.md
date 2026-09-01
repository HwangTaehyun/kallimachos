---
title: Quarterly infra cost review
tags: [infra, metric]
summary: Vector search is 6% of spend; egress dominates
created: 2026-06-12
---
Q2 review: compute 41%, egress 32%, storage 18%, managed services 9%. The surprise was egress — dashboard polling shipped full result payloads every 15 seconds. Fix: delta responses and a 60-second cache cut egress 40%. Vector search, feared expensive, is 6% of total.
