# Runner for the zoboth bot on a self-hosted machine. The host (CentOS 7,
# glibc 2.17) is too old for current Claude Code and gh, so everything the bot
# needs lives in this image and the host only needs docker.
# Node 22: current Claude Code requires node >=22; on node 20 npm silently installs the
# last compatible release (2.1.197), which rejects newer models such as claude-opus-5-5.
FROM node:22-bookworm-slim
ARG GH_VERSION=2.62.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates curl \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local --strip-components=1 \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
USER node
WORKDIR /work
