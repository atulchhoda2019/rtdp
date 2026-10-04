FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/agent-gateway ./services/agent-gateway
RUN go build -o /bin/agent-gateway ./services/agent-gateway

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/agent-gateway /bin/service
COPY assets/seed/agent/gateway_tools.yaml /app/gateway_tools.yaml
ENV RTDP_GATEWAY_TOOLS=/app/gateway_tools.yaml
ENTRYPOINT ["/bin/service"]
