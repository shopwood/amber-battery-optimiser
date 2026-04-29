FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aiohttp

WORKDIR /app
COPY refresh.py .

EXPOSE 8080
CMD ["python", "refresh.py"]
