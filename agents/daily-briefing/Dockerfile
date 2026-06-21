FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/daily-briefing

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Europe/Belgrade /etc/localtime

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src/ ./src/

EXPOSE 8002
CMD ["python", "-m", "src.main"]
