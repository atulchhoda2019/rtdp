FROM golang:alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY services/ingress ./services/ingress
RUN go build -o /bin/ingress ./services/ingress

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/ingress /bin/service
ENTRYPOINT ["/bin/service"]
