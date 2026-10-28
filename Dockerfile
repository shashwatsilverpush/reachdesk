FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn leadcore.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
