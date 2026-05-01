FROM python:3.12-slim

# git for pulling, plus docker CLI + compose plugin so we can `compose up --build`
# the optimiser container after pulling new source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aiohttp

# Repo is bind-mounted from the host where it's owned by a non-root user.
# Git refuses to operate on a repo with "dubious ownership" by default; whitelist /repo.
RUN git config --system --add safe.directory /repo

WORKDIR /app
COPY refresh.py .

EXPOSE 8080
CMD ["python", "refresh.py"]
