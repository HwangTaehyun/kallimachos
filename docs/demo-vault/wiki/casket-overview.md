---
title: Casket — blob storage layer
tags: [casket, storage]
summary: Content-addressed blob store behind the document pipeline
created: 2026-04-02
---
Casket stores every ingested document as a content-addressed blob: SHA-256 key, zstd compression, S3 backend with a local disk cache. Deduplication is free because identical bytes share a key. Garbage collection is mark-and-sweep over the reference table, run weekly. Casket serves Aurora's hydration step and the export pipeline.
