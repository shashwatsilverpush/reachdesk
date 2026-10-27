FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

ENV LEADCORE_DB_URL=sqlite:////app/leadcore.db

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn leadcore.web.app:app --host 0.0.0.0 --port 8000"]
