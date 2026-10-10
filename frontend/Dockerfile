# Multi-stage build, same idea as the gateway image.
#
# Node is needed to compile the console and has no business in the shipped
# image, which is static files behind nginx.

FROM node:22-alpine AS build

WORKDIR /build

# Copied alone so the dependency layer stays cached when only source changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# Where the console's proxy sends things. The defaults match the compose
# service names; override them to point the console at another stack.
ENV GATEWAY_URL=http://gateway:8000 \
    SERVICE_A_URL=http://service-a:8001 \
    SERVICE_B_URL=http://service-b:8002 \
    SERVICE_C_URL=http://service-c:8003 \
    NGINX_RESOLVER=127.0.0.11

# The nginx image runs envsubst over /etc/nginx/templates at start, filling
# in the variables above and leaving nginx's own $variables alone.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
