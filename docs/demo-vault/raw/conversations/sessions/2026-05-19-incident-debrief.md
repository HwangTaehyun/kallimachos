---
title: Session — cache incident debrief
created: 2026-05-19
summary: Debrief after stale search results incident
---
Debrief of the stale-cache incident. Timeline reconstructed: purge ran 02:10, first user report 09:40 next day. Detection gap traced to missing verifier. Agreed: nightly index-vs-source diff, tombstone-aware rebuild, alert when deleted doc ids appear in top-20 results. Postmortem written to wiki.
