---
title: Feature flag conventions
tags: [aurora, infra]
summary: Flags are temporary; every flag has an owner and a removal date
created: 2026-03-22
---
Every feature flag in Aurora declares an owner and a planned removal date in the flag registry. A weekly job files a ticket for flags past their date. Search-v2 shipped behind a flag for six weeks; the flag was deleted the sprint after full rollout. Permanent toggles are config, not flags.
