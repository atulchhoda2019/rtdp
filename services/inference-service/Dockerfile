FROM python:3.11-slim AS base
WORKDIR /app
COPY services/inference-service/pyproject.toml ./
COPY services/inference-service/rtdp_inference ./rtdp_inference
COPY gen/python ./gen/python
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 50051
CMD ["python", "-m", "rtdp_inference.server"]
