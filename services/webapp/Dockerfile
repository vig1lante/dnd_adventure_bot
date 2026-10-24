# Образ dev-сервера Mini App (Vite) для docker compose. Контекст сборки — корень репозитория.
FROM node:24-slim

RUN mkdir -p /app/services/webapp && chown -R node:node /app
USER node
WORKDIR /app/services/webapp

# Сначала package.json и lock-файл: слой node_modules пересобирается только при их смене.
COPY --chown=node:node services/webapp/package.json services/webapp/package-lock.json ./
RUN npm ci

COPY --chown=node:node services/webapp ./

# 0.0.0.0 — внутри контейнера: наружу порт публикует compose и только на 127.0.0.1 хоста.
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
