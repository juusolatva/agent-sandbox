# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13.18-slim-bookworm
ARG GO_IMAGE=golang:1.27.3-bookworm
ARG RUST_IMAGE=rust:1.99.1-slim-bookworm
ARG NODE_IMAGE=node:24.22.1-bookworm-slim
FROM ${GO_IMAGE} AS go-source
FROM ${RUST_IMAGE} AS rust-source
FROM ${NODE_IMAGE} AS node-source
FROM ${PYTHON_IMAGE} AS base
# Optional CA secret supports managed build proxies without baking trust into images.
RUN --mount=type=secret,id=proxy_ca \
    apt-get update && apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates cmake curl git jq ninja-build \
    pkg-config procps ripgrep && rm -rf /var/lib/apt/lists/* && \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi && \
    pip install --no-cache-dir --only-binary :all: openai==2.6.1 && \
    useradd -m -u 1000 -s /bin/bash agent && \
    mkdir -p /home/agent/workspace && chown -R agent:agent /home/agent
COPY --chmod=444 agent.py /opt/sandbox/agent.py
RUN chmod 0755 /opt/sandbox
USER agent
WORKDIR /home/agent/workspace
ENTRYPOINT ["python3", "-u", "/opt/sandbox/agent.py"]

FROM base AS cpp
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang-14 clang-tidy-14 g++-12 gcc-12 libclang-rt-14-dev llvm-14 \
    && rm -rf /var/lib/apt/lists/*
USER agent

FROM base AS go
COPY --from=go-source /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" GOTOOLCHAIN=local

FROM base AS rust
COPY --from=rust-source /usr/local/cargo /usr/local/cargo
COPY --from=rust-source /usr/local/rustup /usr/local/rustup
ENV PATH="/usr/local/cargo/bin:${PATH}" RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/home/agent/.cargo
USER root
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    rustup component add clippy rustfmt && chmod -R go-w /usr/local/rustup /usr/local/cargo && \
    mkdir -p /home/agent/.cargo && chown -R agent:agent /home/agent/.cargo
USER agent

FROM base AS node
COPY --from=node-source /usr/local/bin/node /usr/local/bin/node
COPY --from=node-source /usr/local/lib/node_modules /usr/local/lib/node_modules
USER root
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
USER agent

# Build with PYTHON_IMAGE=python:3.14.10-slim-bookworm for the 3.14 environment.
FROM base AS python

# Preserve docker compose build's default target.
FROM base AS sandbox
