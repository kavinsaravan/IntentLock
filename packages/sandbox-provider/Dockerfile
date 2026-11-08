FROM node:22-alpine
RUN apk add --no-cache git
# Pinned at image-build time (the only time the network is used). Runtime containers run with --network none.
RUN npm install -g --ignore-scripts --no-audit --no-fund typescript@5.6.3 && npm cache clean --force
