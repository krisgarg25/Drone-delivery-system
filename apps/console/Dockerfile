# DroneOps console image: static build served by an unprivileged nginx.
# The console contains presentation only; no scheduling or reservation logic runs here.
FROM node:24-alpine AS build
WORKDIR /build

COPY apps/console/package.json apps/console/package-lock.json ./
RUN npm ci --no-audit --fund=false

COPY apps/console ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

# The unprivileged image already runs as uid 101.
COPY --from=build /build/dist /usr/share/nginx/html
COPY apps/console/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["sh", "-c", "wget -q -O - http://127.0.0.1:8080/ >/dev/null || exit 1"]
