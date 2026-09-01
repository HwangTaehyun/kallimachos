---
title: Lighthouse — observability stack
tags: [lighthouse, telemetry]
summary: Metrics, traces and logs for Aurora and friends
created: 2026-03-28
---
Lighthouse is the observability stack behind Aurora: OpenTelemetry collectors feed Prometheus for metrics, Tempo for traces, Loki for logs. Grafana dashboards are provisioned from code. The collector runs as a sidecar; sampling is tail-based at 10% with full retention for error traces. Alert rules live next to the dashboards they explain.
