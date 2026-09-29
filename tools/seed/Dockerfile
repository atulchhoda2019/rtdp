# rtdp-tools: in-cluster bootstrap image — topics admin, schema migrations,
# platform seed. Models are trained at build time (deterministic seeds).
FROM golang:1.25-bookworm AS gobuild
WORKDIR /src
COPY go.mod go.sum ./
COPY gen/go ./gen/go
RUN go mod download
COPY internal ./internal
COPY cmd ./cmd
RUN CGO_ENABLED=0 go build -o /out/rtdp-topics ./cmd/rtdp-topics

FROM python:3.12-bookworm AS models
WORKDIR /app
RUN pip install --no-cache-dir \
    "numpy>=2.0,<3" "scikit-learn>=1.5,<2" "skl2onnx>=1.17,<2" "onnxruntime>=1.19,<2"
COPY ml/seed-model ./ml/seed-model
RUN python ml/seed-model/train.py

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    "pyyaml>=6.0.2" "protobuf>=5.29,<6" "grpcio>=1.68,<2" "boto3>=1.35,<2"
WORKDIR /app
COPY assets ./assets
COPY contracts ./contracts
COPY ml/slm-narrative ./ml/slm-narrative
COPY gen/python ./gen/python
COPY services/control-plane ./services/control-plane
COPY tools/seed ./tools/seed
COPY services/control-plane/migrations ./migrations
COPY --from=models /app/build/models ./build/models
COPY --from=gobuild /out/rtdp-topics /usr/local/bin/rtdp-topics
ENTRYPOINT ["python", "tools/seed/seed.py"]
