# Stage 0, "deps-stage": install deps and bring in the source. Shared by the
# dev server and the production build.
FROM oven/bun:1 AS deps-stage

WORKDIR /app

COPY package.json bun.lock /app/

COPY frontend/package.json /app/frontend/

WORKDIR /app/frontend

RUN bun install

COPY ./frontend /app/frontend

# Dev stage: Vite dev server with HMR. Used by compose.override.yml for local
# dev — `docker compose watch` syncs ./frontend into the container and Vite
# hot-reloads. Not used in production.
FROM deps-stage AS dev-stage
EXPOSE 5173
CMD ["bun", "run", "dev", "--", "--host", "--port", "5173"]

# Build stage: compile the production bundle.
FROM deps-stage AS build-stage
ARG VITE_API_URL

RUN bun run build


# Stage 1, based on Nginx, to have only the compiled app, ready for production with Nginx
FROM nginx:1

COPY --from=build-stage /app/frontend/dist/ /usr/share/nginx/html

COPY ./frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY ./frontend/nginx-backend-not-found.conf /etc/nginx/extra-conf.d/backend-not-found.conf
