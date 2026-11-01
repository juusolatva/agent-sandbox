FROM python:3.12-slim

# Install basic CLI tools useful inside the sandbox
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    jq \
    procps \
    ripgrep \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Install the OpenAI SDK (used to talk to LM Studio's OpenAI-compatible server)
RUN pip install --no-cache-dir openai

# Create an unprivileged user (UID 1000 typically matches your default WSL2 user)
RUN useradd -m -u 1000 -s /bin/bash agent && \
    mkdir -p /home/agent/workspace && \
    chown -R agent:agent /home/agent

USER agent
WORKDIR /home/agent/workspace

# Run the agent loop by default
ENTRYPOINT ["python3", "-u", "/home/agent/agent.py"]
