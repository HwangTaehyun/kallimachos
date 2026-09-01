---
title: Schema migration rules
tags: [casket, infra]
summary: Expand-migrate-contract, never in one deploy
created: 2026-04-27
---
Every schema change follows expand-migrate-contract: add the new column nullable, backfill in batches, switch reads, then drop the old column in a later deploy. The one-deploy shortcut was banned after a rollback needed a column the deploy had just dropped. Migrations are forward-only; rollback means a new migration.
