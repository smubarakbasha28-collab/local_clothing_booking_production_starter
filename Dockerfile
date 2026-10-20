FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data /app/static/uploads \
    && chown -R app:app /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app

EXPOSE 8000

CMD ["sh", "-c", "flask --app app init-db && exec gunicorn app:app --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 60"]
