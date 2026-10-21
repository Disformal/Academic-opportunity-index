# Smokescreen egress proxy (ADR 0023), built from a pinned release tag.
# Stripe publishes no container image, so we build the binary ourselves.
# It refuses connections to private, loopback, link-local and other
# non-public ranges at connect time, after DNS resolution and redirects.

FROM golang:1.25-alpine AS build
RUN CGO_ENABLED=0 go install github.com/stripe/smokescreen@v0.1.0

FROM alpine:3.20
COPY --from=build /go/bin/smokescreen /usr/local/bin/smokescreen
USER nobody
EXPOSE 4750
ENTRYPOINT ["smokescreen", "--listen-ip=0.0.0.0", "--listen-port=4750"]
