---
title: Monorepo for Aurora services
tags: [infra, decision]
summary: One repo, per-service deploy, shared CI cache
created: 2026-03-08
---
Aurora, Lighthouse and Casket live in one repository. Shared protobuf definitions stop drift; one CI cache makes builds fast; a change touching producer and consumer lands atomically. Deploys stay per-service via path filters. The polyrepo alternative died on cross-repo版本 coordination cost.
