FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

RUN mkdir -p /data

ENV LEADCORE_DB_URL=sqlite:////data/leadcore.db

EXPOSE 8000

CMD ["uvicorn", "leadcore.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
