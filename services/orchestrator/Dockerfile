FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/orchestrator ./services/orchestrator
RUN go build -o /bin/orchestrator ./services/orchestrator

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/orchestrator /bin/service
ENTRYPOINT ["/bin/service"]
