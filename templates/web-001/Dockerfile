FROM python:3.12-slim

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install essential packages and Node.js
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        patch \
        curl \
        ca-certificates \
        gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y sudo 2>/dev/null || true && \
    rm -rf /var/lib/apt/lists/*

# Install Playwright and browser dependencies
RUN npm install -g @playwright/test && \
    npx playwright install --with-deps chromium || true

# Create unprivileged agent user (UID 10001) and workspace
RUN groupadd -g 10001 agent && \
    useradd -u 10001 -g 10001 -m -s /bin/bash agent && \
    mkdir -p /workspace && \
    chown -R 10001:10001 /workspace /home/agent

# Remove setuid / setgid bits from binaries to prevent privilege escalation
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# Set environment
USER agent
WORKDIR /workspace
ENV HOME=/home/agent \
    USER=agent \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
