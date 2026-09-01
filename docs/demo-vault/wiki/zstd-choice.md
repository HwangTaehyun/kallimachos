---
title: zstd over gzip for Casket
tags: [casket, performance]
summary: 3.2x faster decompression at similar ratio
created: 2026-04-09
---
Benchmarked gzip -6 against zstd -3 on 10k documents: compression ratio within 4%, decompression 3.2x faster with zstd. Hydration is read-heavy, so decompression speed wins. Dictionary training on 1MB samples bought another 11% ratio on small documents.
