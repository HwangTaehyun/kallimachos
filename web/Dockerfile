# The frontend is static files.  nginx serves them and forwards only /api to the backend.
#
# Why there is no node at runtime
#   The build output is HTML, JS and CSS alone, so there is no reason to run a node process.
#   nginx:alpine is around 20MB and a node runtime is around 200MB.

FROM oven/bun:1-alpine AS build
WORKDIR /build
# The lock file is copied too, for a reproducible install.  The install layer is reused when only the source changes.
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile
COPY . .
# The galaxy bundle comes out of plugin/ and is not visible from this build context (./web).
# `just build-galaxy` has to run on the host first —— without it the main screen is entirely
# empty, so rather than passing quietly it stops here.
RUN test -f public/galaxy/manifest.json || { \
      echo "public/galaxy/manifest.json is missing — run 'just build-galaxy' on the host first"; \
      exit 1; }
RUN bun run build

FROM nginx:1.29-alpine
COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz || exit 1
