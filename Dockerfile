FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /install /usr/local
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

EXPOSE 11435

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "11435"]
