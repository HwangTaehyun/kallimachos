---
title: Auth decision — signed session cookies
tags: [auth, security]
summary: Why we picked HttpOnly signed cookies over JWT
created: 2026-03-14
---
We evaluated JWT and signed session cookies for Aurora's dashboard. JWTs cannot be revoked after issue without a denylist, which reintroduces server state. Signed HttpOnly SameSite cookies keep revocation trivial and cut the attack surface. Decision: session cookies, rotated weekly. Redis holds the session table. JWT was rejected.
