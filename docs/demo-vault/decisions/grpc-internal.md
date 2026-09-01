---
title: gRPC for internal calls
tags: [infra, decision]
summary: gRPC internal, REST at the edge
created: 2026-03-30
---
Internal service calls use gRPC: typed contracts from proto, streaming for the export pipeline, deadline propagation for free. The public edge stays REST/JSON because curl-ability matters for adopters. The gateway translates. Deadlines propagate end-to-end so a slow downstream cannot pin gateway threads.
