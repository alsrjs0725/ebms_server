#!/usr/bin/env bash
# EBMS 캐싱 리버스 프록시 설치(Ubuntu, 오라클 클라우드 무료 VM 기준). 사이트(release·stage)마다 한 번씩 실행합니다.
#
#   sudo EBMS_DOMAIN=ebms.example.com EBMS_ORIGIN=100.64.0.10:8000 \
#        EBMS_PROXY_SECRET=<홈서버 env와 같은 값> EBMS_ACME_EMAIL=me@example.com ./setup.sh
#
# 선택: EBMS_NAME(기본 release, stage면 stage), EBMS_CACHE_MAX_SIZE(기본 20g), EBMS_CACHE_INACTIVE(기본 30d)
# 다시 실행해도 됩니다(설정만 덮어쓰고 인증서는 있으면 그대로 둠). 자세한 순서는 docs/proxy.md.
set -euo pipefail

: "${EBMS_DOMAIN:?EBMS_DOMAIN(외부 도메인)을 주세요}"
: "${EBMS_ORIGIN:?EBMS_ORIGIN(홈서버 주소:포트, 예: 100.64.0.10:8000)을 주세요}"
: "${EBMS_PROXY_SECRET:?EBMS_PROXY_SECRET(홈서버 env의 값)을 주세요}"
export EBMS_NAME="${EBMS_NAME:-release}"
export EBMS_CACHE_MAX_SIZE="${EBMS_CACHE_MAX_SIZE:-20g}"
export EBMS_CACHE_INACTIVE="${EBMS_CACHE_INACTIVE:-30d}"
export EBMS_DOMAIN EBMS_ORIGIN EBMS_PROXY_SECRET

if [[ $EUID -ne 0 ]]; then
    echo "root로 실행하세요(sudo)." >&2
    exit 1
fi
if [[ ! $EBMS_NAME =~ ^[a-z0-9_]+$ ]]; then
    echo "EBMS_NAME은 영문 소문자·숫자·_만 씁니다." >&2
    exit 1
fi
if [[ ! $EBMS_PROXY_SECRET =~ ^[A-Za-z0-9_-]{16,}$ ]]; then
    echo "EBMS_PROXY_SECRET은 16자 이상의 영문·숫자·_- 로 만드세요(예: openssl rand -hex 32)." >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SITE=/etc/nginx/sites-available/ebms-${EBMS_NAME}.conf

echo "== 패키지 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx certbot gettext-base curl iptables-persistent

echo "== 방화벽(80, 443) 열기"
# 오라클 Ubuntu 이미지는 iptables가 22번 외에는 막습니다. REJECT 규칙보다 앞에 넣습니다.
for port in 80 443; do
    if ! iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
        iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
            || iptables -I INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT
    fi
done
netfilter-persistent save >/dev/null 2>&1 || true

echo "== 오리진 연결 확인: http://${EBMS_ORIGIN}/api/version"
if ! curl -fsS --max-time 10 "http://${EBMS_ORIGIN}/api/version"; then
    echo
    echo "경고: 오리진에 닿지 않습니다. 홈서버와의 터널(Tailscale 등)과 EBMS_ORIGIN을 확인하세요. 설치는 계속합니다." >&2
fi
echo

mkdir -p /var/cache/nginx/ebms /var/www/letsencrypt
chown www-data:www-data /var/cache/nginx/ebms
rm -f /etc/nginx/sites-enabled/default

echo "== 공통 설정"
envsubst '${EBMS_CACHE_MAX_SIZE} ${EBMS_CACHE_INACTIVE}' \
    < "$HERE/nginx/ebms-common.conf.template" > /etc/nginx/conf.d/ebms-common.conf

if [[ ! -f /etc/letsencrypt/live/${EBMS_DOMAIN}/fullchain.pem ]]; then
    echo "== 인증서 발급(${EBMS_DOMAIN})"
    # 인증서가 없으면 443 설정을 읽지 못하므로 먼저 80만 여는 임시 설정으로 발급합니다.
    cat > "$SITE" <<EOF
server {
    listen 80;
    server_name ${EBMS_DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
}
EOF
    ln -sf "$SITE" "/etc/nginx/sites-enabled/ebms-${EBMS_NAME}.conf"
    nginx -t
    systemctl reload nginx || systemctl restart nginx
    email_args=(--register-unsafely-without-email)
    if [[ -n ${EBMS_ACME_EMAIL:-} ]]; then
        email_args=(--email "$EBMS_ACME_EMAIL")
    fi
    certbot certonly --webroot -w /var/www/letsencrypt -d "$EBMS_DOMAIN" \
        --agree-tos -n "${email_args[@]}" --deploy-hook "systemctl reload nginx"
fi

echo "== 사이트 설정(${SITE})"
umask 077
envsubst '${EBMS_NAME} ${EBMS_DOMAIN} ${EBMS_ORIGIN} ${EBMS_PROXY_SECRET}' \
    < "$HERE/nginx/ebms-site.conf.template" > "$SITE"
chmod 600 "$SITE"
ln -sf "$SITE" "/etc/nginx/sites-enabled/ebms-${EBMS_NAME}.conf"
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo
echo "완료: https://${EBMS_DOMAIN}"
echo "확인: curl -sI https://${EBMS_DOMAIN}/api/version"
