# mem0ry4ai — server install.
#
# This is the HEADLESS option: a NAS, a home server, a VM, anything always-on that you want to
# reach from a browser. It is NOT the recommended way to use mem0ry4ai on the machine where you
# actually run Claude Code. There, the git-clone or plugin install is simpler and strictly better:
# the hooks run inside the Claude Code process, and a containerised store would turn every
# SessionStart into a `docker exec`, adding a moving part and a daemon to a tool whose whole claim
# is that it needs neither.
#
#   docker build -t mem0ry4ai docker/
#   docker run -d --name mem0ry4ai --restart unless-stopped \
#       -p 8841:8841 -v /srv/mem0ry4ai:/data mem0ry4ai
#
# Or `docker compose up -d` from this directory.

FROM python:3.12-slim

# git is the only dependency the project has outside the standard library: the store IS a git
# repository, and mem.py shells out to it for checkpoints and history.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The mounted volume is owned by whatever uid the host uses, which git otherwise refuses to touch
# ("dubious ownership"). Commit identity is NOT configured here on purpose -- mem0ry4ai passes its
# own inline on every commit path, precisely so it works on a machine that has none.
RUN git config --global --add safe.directory '*'

ARG VERSION=main
RUN git clone --depth 1 --branch "$VERSION" \
        https://github.com/cremenescu/mem0ry4ai.git /app \
 && git -C /app remote remove origin
# The remote is removed, not merely left unused. The store is a git repo that this container
# commits to, and a clone of the PUBLIC repository sitting next to it is one `git push` away from
# publishing someone's private memories. With no remote, that push cannot happen at all.

WORKDIR /app

# The store lives on the volume, never inside the code clone: rebuilding or updating the image
# then cannot touch memories, and `docker rm` is not a data-loss event.
ENV MEM_DATA_DIR=/data \
    MEM_WEB_HOST=0.0.0.0 \
    MEM_WEB_PORT=8841 \
    PYTHONUNBUFFERED=1
# MEM_WEB_HOST=0.0.0.0 is required, not stylistic: the server binds loopback by default (the UI has
# no authentication), and publishing a port to a loopback-bound server yields a container that
# looks healthy and answers nothing.

VOLUME /data
EXPOSE 8841

# The UI has no authentication. Publish it on a trusted network only, or put a reverse proxy with
# auth in front of it. Do not expose this port to the internet.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8841/', timeout=4).status==200 else 1)"

CMD ["python3", "mem.py", "serve"]
