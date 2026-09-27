FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/feature-materializer ./services/feature-materializer
RUN go build -o /bin/feature-materializer ./services/feature-materializer

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/feature-materializer /bin/service
ENTRYPOINT ["/bin/service"]
