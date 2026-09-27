FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/signal-resolver ./services/signal-resolver
RUN go build -o /bin/signal-resolver ./services/signal-resolver

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/signal-resolver /bin/service
ENTRYPOINT ["/bin/service"]
