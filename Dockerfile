FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="Shopping Telegram Bot" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN chmod -R a=rX /app \
    && mkdir /data \
    && chown 10001:10001 /data

USER 10001:10001

CMD ["python", "-m", "app.bot"]
