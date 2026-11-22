# syntax=docker/dockerfile:1
# Caddy with exactly two plugins: deSEC DNS-01 and per-key rate limiting. Images pinned by digest via build args.
ARG CADDY_BUILDER_IMAGE
ARG CADDY_RUNTIME_IMAGE
ARG CADDY_GO_TOOLCHAIN

FROM ${CADDY_BUILDER_IMAGE} AS build
ARG CADDY_VERSION
ARG CADDY_DNS_DESEC_VERSION
ARG CADDY_RATELIMIT_COMMIT
# Go toolchain pinned apart from the builder image, whose bundled Go lags the stdlib CVE fixes.
ENV GOTOOLCHAIN=${CADDY_GO_TOOLCHAIN}
RUN xcaddy build "v${CADDY_VERSION}" \
      --with "github.com/caddy-dns/desec@${CADDY_DNS_DESEC_VERSION}" \
      --with "github.com/mholt/caddy-ratelimit@${CADDY_RATELIMIT_COMMIT}" \
 && /usr/bin/caddy version

FROM ${CADDY_RUNTIME_IMAGE}
COPY --from=build /usr/bin/caddy /usr/bin/caddy
RUN caddy list-modules | grep -qx 'dns.providers.desec' \
 && caddy list-modules | grep -qx 'http.handlers.rate_limit'
