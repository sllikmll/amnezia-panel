FROM alpine:3.20

RUN apk add --no-cache \
    python3 \
    py3-pip \
    bash \
    curl \
    ca-certificates \
    unzip \
    sqlite \
    wireguard-tools \
    docker-cli \
    && curl -L -o /tmp/awg-tools.zip \
       https://github.com/amnezia-vpn/amneziawg-tools/releases/download/v3.0.20260805/alpine-3.19-amneziawg-tools.zip \
    && cd /tmp && unzip -o awg-tools.zip \
    && chmod +x /tmp/alpine-3.19-amneziawg-tools/awg /tmp/alpine-3.19-amneziawg-tools/awg-quick \
    && mv /tmp/alpine-3.19-amneziawg-tools/awg /usr/local/bin/awg \
    && mv /tmp/alpine-3.19-amneziawg-tools/awg-quick /usr/local/bin/awg-quick \
    && rm -rf /tmp/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py multi_server.py crypto.py aggregate.py discover.py notify.py realtime.py .
COPY static /app/static

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8080/ || exit 1

CMD ["python3", "main.py"]
