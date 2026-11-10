# Flow Atelier with Claude Code and Codex, for unattended runs on Cloud Run.
# See docs/running-unattended.md.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 \
    && rm -rf /var/lib/apt/lists/*

# Claude Code refuses to skip permission prompts as root, and unattended
# runs approve every tool call, so the agents run as an ordinary user.
RUN useradd --create-home --shell /bin/bash atelier
USER atelier
WORKDIR /home/atelier
ENV PATH="/home/atelier/.local/bin:/home/atelier/.atelier/bin:${PATH}" \
    ATELIER_NO_UPDATE_CHECK=1 \
    CODEX_HOME=/home/atelier/.codex

# Pin a release for reproducible images, e.g. flow-atelier==0.13.0.
ARG ATELIER_SPEC=flow-atelier
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv tool install "${ATELIER_SPEC}"

# Install the pinned adapters into the image, so a task starts without npx
# and a run never fetches packages.
RUN atelier harness install claude-code codex

COPY --chown=atelier:atelier entrypoint.sh /home/atelier/entrypoint.sh
ENTRYPOINT ["/home/atelier/entrypoint.sh"]
