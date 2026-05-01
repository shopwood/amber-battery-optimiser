"""
Tiny webhook: POST /refresh → git pull (ff-only) and rebuild + restart the
optimiser container, only if there were new commits to pull.

`docker restart` alone doesn't pick up source changes — it just restarts the
existing image. We invoke `docker compose up -d --build <service>` which
rebuilds the image and recreates the container.

For compose to find paths the way the host's daemon expects, the host's
home-assistant directory must be bind-mounted into this container at the
SAME path (e.g. /home/scott/home-assistant on both sides). The compose CLI
reads the file inside the container, but the daemon receives host paths.

Lives on the docker internal network alongside HA + the optimiser. Not exposed
to the host. HA reaches it by container name via `rest_command`.

No auth — relies on docker network isolation.
"""
from __future__ import annotations

import asyncio
import os

from aiohttp import ClientSession, UnixConnector, web

REPO_DIR       = os.environ.get("REPO_DIR",       "/repo")
CONTAINER      = os.environ.get("TARGET_CONTAINER", "amber_battery_optimiser")
TARGET_SERVICE = os.environ.get("TARGET_SERVICE",   "amber_battery_optimiser")
COMPOSE_FILE   = os.environ.get("COMPOSE_FILE",     "/home/scott/home-assistant/compose.yaml")
PORT           = int(os.environ.get("PORT", "8080"))


async def _run(cmd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=REPO_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def _compose_up_build(service: str) -> tuple[int, str]:
    """Rebuild image and recreate container via docker compose."""
    # cwd doesn't matter — -f gives compose the absolute file path.
    proc = await asyncio.create_subprocess_shell(
        f"docker compose -f {COMPOSE_FILE} up -d --build {service}",
        cwd="/",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def refresh(_request: web.Request) -> web.Response:
    log: list[str] = []

    # Refuse if working tree is dirty — never silently lose local edits.
    # Use -c core.fileMode=false so spurious bit-flips on bind-mounts don't trip us.
    rc, _ = await _run("git -c core.fileMode=false diff --quiet --ignore-submodules HEAD --")
    if rc != 0:
        _, status = await _run("git -c core.fileMode=false status --porcelain")
        _, stat = await _run("git -c core.fileMode=false diff --stat HEAD")
        return web.Response(
            status=409,
            text=f"working tree dirty; aborting\n--- status ---\n{status}--- diff stat ---\n{stat}",
        )

    # Capture HEAD before/after to decide whether a restart is needed.
    rc, before = await _run("git rev-parse HEAD")
    if rc != 0:
        return web.Response(status=500, text=f"git rev-parse failed:\n{before}")
    before = before.strip()
    log.append(f"before: {before}")

    rc, out = await _run(
        "git -c core.fileMode=false fetch --quiet origin main && "
        "git -c core.fileMode=false merge --ff-only origin/main"
    )
    log.append(out.rstrip())
    if rc != 0:
        return web.Response(status=500, text="\n".join(log) + "\n")

    rc, after = await _run("git rev-parse HEAD")
    after = after.strip()
    log.append(f"after:  {after}")

    if before == after:
        log.append("no new commits; container not restarted.")
        return web.Response(status=200, text="\n".join(log) + "\n")

    rc, out = await _run(f"git --no-pager log --oneline {before}..{after}")
    log.append(out.rstrip())

    rc, build_out = await _compose_up_build(TARGET_SERVICE)
    # Compose chats a lot — keep just the tail lines so the HA notification stays readable.
    tail = "\n".join(build_out.rstrip().splitlines()[-15:])
    log.append(f"compose up --build (rc={rc}):\n{tail}")
    if rc != 0:
        return web.Response(status=500, text="\n".join(log) + "\n")

    return web.Response(status=200, text="\n".join(log) + "\n")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


def main() -> None:
    app = web.Application()
    app.router.add_post("/refresh", refresh)
    app.router.add_get("/health", health)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
