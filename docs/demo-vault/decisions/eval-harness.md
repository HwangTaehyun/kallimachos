---
title: Evaluation harness
tags: [aurora, ml]
summary: 991 judged pairs, nDCG@10, refusal to ship without a gate
created: 2026-04-30
---
Every retrieval change to Aurora must pass the eval harness: 991 judged query-document pairs, nDCG@10 primary metric, bootstrap confidence intervals. A change ships only if the interval excludes regression. The harness caught the e5-base swap that looked better in demos but lost 2 points on tail queries.
