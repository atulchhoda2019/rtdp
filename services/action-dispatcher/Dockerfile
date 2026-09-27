FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/action-dispatcher ./services/action-dispatcher
RUN go build -o /bin/action-dispatcher ./services/action-dispatcher

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/action-dispatcher /bin/service
ENTRYPOINT ["/bin/service"]
