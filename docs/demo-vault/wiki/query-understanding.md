---
title: Query understanding layer
tags: [aurora, search]
summary: Spell repair and synonym expansion before retrieval
created: 2026-05-22
---
Two cheap transforms run before retrieval: SymSpell corrects edit-distance-1 typos against the corpus vocabulary, and a curated synonym table expands domain terms (auth = authentication = login). Both are deterministic and logged, so a surprising ranking can always be traced to the rewritten query.
