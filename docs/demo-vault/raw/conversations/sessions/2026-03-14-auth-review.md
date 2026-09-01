---
title: Session — auth design review
created: 2026-03-14
summary: Architecture review where the JWT vs cookie decision landed
---
Design review for Aurora auth. Team walked through token revocation scenarios. JWT denylist adds a Redis lookup per request anyway, erasing the stateless benefit. Signed HttpOnly cookies with SameSite=Lax chosen. Action items: rotate signing key weekly, add session table TTL, document the decision in the wiki.
