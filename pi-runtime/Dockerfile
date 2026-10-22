FROM node:22.19-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY src ./src

ENV PI_RUNTIME_HOST=0.0.0.0
ENV PI_RUNTIME_PORT=8124
EXPOSE 8124
USER node
CMD ["node", "src/server.mjs"]
