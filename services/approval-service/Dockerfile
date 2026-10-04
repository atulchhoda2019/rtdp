FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/approval-service ./services/approval-service
RUN go build -o /bin/approval-service ./services/approval-service

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/approval-service /bin/service
ENTRYPOINT ["/bin/service"]
