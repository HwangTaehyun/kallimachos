---
title: Redis session table
tags: [auth, infra]
summary: Session storage layout and TTL policy
created: 2026-03-20
---
Sessions for Aurora live in Redis with a 14-day sliding TTL. Keys are sess:<id>, values carry user id, rotation counter, and issue time. Weekly rotation invalidates the previous counter. Backup is RDB snapshots every 6 hours to object storage. The session table never leaves the VPC.
