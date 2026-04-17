FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Australia/Sydney

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/
RUN pip install --no-cache-dir httpx==0.27.* pydantic==2.* apscheduler==3.10.*

CMD ["python", "-u", "/app/main.py"]
