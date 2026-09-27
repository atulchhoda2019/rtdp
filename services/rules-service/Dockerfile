FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/rules-service ./services/rules-service
RUN go build -o /bin/rules-service ./services/rules-service

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/rules-service /bin/service
ENTRYPOINT ["/bin/service"]
