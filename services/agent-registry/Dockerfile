FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/agent-registry ./services/agent-registry
RUN go build -o /bin/agent-registry ./services/agent-registry

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/agent-registry /bin/service
ENTRYPOINT ["/bin/service"]
