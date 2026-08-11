#!/bin/sh
set -e

apk add --no-cache wireguard-tools bash curl iptables unzip ca-certificates sqlite python3 2>&1 | tail -2

# Установка amneziawg-tools
VER_AWG="v3.0.20260805"
if [ ! -f /usr/local/bin/awg ]; then
    curl -L -o /tmp/awg-tools.zip \
        https://github.com/amnezia-vpn/amneziawg-tools/releases/download/${VER_AWG}/alpine-3.19-amneziawg-tools.zip
    cd /tmp && unzip -o awg-tools.zip
    chmod +x /tmp/alpine-3.19-amneziawg-tools/awg /tmp/alpine-3.19-amneziawg-tools/awg-quick
    mv /tmp/alpine-3.19-amneziawg-tools/awg /usr/local/bin/awg
    mv /tmp/alpine-3.19-amneziawg-tools/awg-quick /usr/local/bin/awg-quick
fi

# Установка amneziawg-go
if [ -f /init/amneziawg-go ]; then
    cp /init/amneziawg-go /usr/local/bin/
    chmod +x /usr/local/bin/amneziawg-go
fi

# Генерируем ключи
if [ ! -f /data/server.key ]; then
    wg genkey | tee /data/server.key | wg pubkey > /data/server.pub
    wg genpsk > /data/psk.key
fi

# Создаём минимальный конфиг
if [ ! -f /data/awg0.conf ]; then
    PRIVATE_KEY=$(cat /data/server.key)
    cat > /data/awg0.conf <<EOF
[Interface]
PrivateKey = ${PRIVATE_KEY}
ListenPort = 51820
EOF
fi

# IP forwarding
sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true

# Запускаем awg-go
LOG_LEVEL=verbose nohup amneziawg-go awg0 > /tmp/awg-go.log 2>&1 &
echo "amneziawg-go started"

# Ждём UAPI
for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -S /var/run/amneziawg/awg0.sock ] || [ -S /var/run/wireguard/awg0.sock ]; then
        break
    fi
    sleep 1
done

# Загружаем минимальный конфиг
awg setconf awg0 /data/awg0.conf 2>&1 || echo "base config failed"

# Устанавливаем Amnezia-specific через UAPI (первый аргумент - интерфейс)
awg set awg0 jc 5 jmin 50 jmax 1000 s1 67 s2 149 h1 987654321 h2 123456789 h3 1357924680 h4 2468013579 2>&1 || echo "amnezia params failed"

# Загружаем ВСЕХ peer'ов из БД (если panel.db смонтирован)
if [ -f /data/panel.db ]; then
    echo "Loading peers from panel.db..."
    # Строим один файл со всеми peer'ами (setconf заменяет весь список)
    > /tmp/all-peers.conf
    while IFS='|' read pub psk ip; do
        cat >> /tmp/all-peers.conf <<PEER
[Peer]
PublicKey = ${pub}
PresharedKey = ${psk}
AllowedIPs = ${ip}/32

PEER
    done < <(sqlite3 /data/panel.db "SELECT public_key, preshared_key, ip_address FROM peers")
    awg setconf awg0 /tmp/all-peers.conf 2>&1 && echo "All peers synced"
fi

# IP и NAT
ip link set awg0 up 2>/dev/null
ip addr add 10.8.1.1/24 dev awg0 2>/dev/null
iptables -A FORWARD -i awg0 -j ACCEPT 2>/dev/null
iptables -A FORWARD -o awg0 -j ACCEPT 2>/dev/null
iptables -t nat -A POSTROUTING -s 10.8.1.0/24 -j MASQUERADE 2>/dev/null

echo "=== awg show ==="
awg show awg0

# Держим контейнер живым
exec tail -f /tmp/awg-go.log
