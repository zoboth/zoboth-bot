# Runner for the zoboth bot on a self-hosted machine. The host (CentOS 7,
# glibc 2.17) is too old for current Claude Code and gh, so everything the bot
# needs lives in this image and the host only needs docker.
FROM node:20-bookworm-slim
ARG GH_VERSION=2.62.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates curl \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local --strip-components=1 \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
USER node
WORKDIR /work
