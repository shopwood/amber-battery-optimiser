FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aiohttp

# Repo is bind-mounted from the host where it's owned by a non-root user.
# Git refuses to operate on a repo with "dubious ownership" by default; whitelist /repo.
RUN git config --system --add safe.directory /repo

WORKDIR /app
COPY refresh.py .

EXPOSE 8080
CMD ["python", "refresh.py"]
