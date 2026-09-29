FROM python:3.11-slim AS base
WORKDIR /app
COPY services/slm-service/pyproject.toml ./
COPY services/slm-service/rtdp_slm ./rtdp_slm
COPY gen/python ./gen/python
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 50056
CMD ["python", "-m", "rtdp_slm.server"]
