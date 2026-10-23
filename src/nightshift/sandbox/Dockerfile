# Base image for task sandboxes: Python, Node, git, uv, and the language servers the context tools use.
# Repo code only ever runs inside containers made from this image (or a per-repo image built on it).
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /usr/local/bin/

# Context tools live in their own venv so they never mix with a repo's dependencies.
RUN python -m venv /opt/ns-tools \
    && /opt/ns-tools/bin/pip install --no-cache-dir multilspy==0.1.1 jedi-language-server==0.41.3

# The TypeScript server, at the versions multilspy expects. Installed here (not on first use, in a
# home folder) so every task user can run it without network access.
RUN npm install --prefix /opt/ts-lsp --no-fund --no-audit typescript@5.5.4 typescript-language-server@4.3.3 \
    && npm cache clean --force

RUN useradd --create-home --uid 1000 worker && mkdir /work && chown worker /work
USER worker
WORKDIR /work
ENV PATH=/opt/ns-tools/bin:$PATH
CMD ["sleep", "infinity"]
