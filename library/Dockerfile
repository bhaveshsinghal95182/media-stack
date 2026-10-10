# 1. Compile the page's CSS with Tailwind's standalone CLI (no Node needed).
FROM alpine:3.22 AS css
ARG TAILWIND_VERSION=v4.3.3
RUN apk add --no-cache curl libgcc libstdc++ \
 && arch="$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/')" \
 && curl -fsSL -o /usr/local/bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${arch}-musl" \
 && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY web/styles web/styles
COPY web/templates web/templates
COPY web/static/app.js web/static/app.js
RUN tailwindcss --input web/styles/app.css --output app.css --minify

# 2. The app.
FROM python:3.13-alpine
# tzdata: the page shows air times and logs in the stack's TZ.
RUN apk add --no-cache tzdata
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY medialib medialib
COPY web web
COPY --from=css /src/app.css web/static/app.css
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8099
CMD ["python", "-m", "web"]
