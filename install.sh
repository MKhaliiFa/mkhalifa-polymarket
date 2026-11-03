#!/usr/bin/env bash
# One-time install on the Hetzner server (run as root):
#   curl -fsSL https://raw.githubusercontent.com/MKhaliiFa/mkhalifa-polymarket/main/install.sh | bash
#
# Everything lives in /opt/mkhalifa-polymarket and runs as its own user (pmbot).
# The live bot (/opt/mkhalifa, its Docker container and its timers) is never changed: the only thing read
# from it is the two Telegram lines of its .env, copied once so the daily report goes to the same chat.
set -euo pipefail
D=/opt/mkhalifa-polymarket
REPO_URL=https://github.com/MKhaliiFa/mkhalifa-polymarket.git

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
python3 -c 'import zoneinfo, sqlite3' || { echo "python3 is missing zoneinfo/sqlite3"; exit 1; }

id pmbot >/dev/null 2>&1 || useradd --system --home-dir "$D" --no-create-home --shell /usr/sbin/nologin pmbot
mkdir -p "$D/data"

if [ -d "$D/repo/.git" ]; then
  git -C "$D/repo" fetch -q origin main && git -C "$D/repo" reset -q --hard origin/main
else
  git clone -q --depth 20 "$REPO_URL" "$D/repo"
fi

if [ ! -s "$D/.env" ]; then
  if [ -r /opt/mkhalifa/.env ]; then
    grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' /opt/mkhalifa/.env > "$D/.env" || true
  fi
  touch "$D/.env"
fi
chown -R pmbot:pmbot "$D/data" "$D/.env"
chmod 600 "$D/.env"

(cd "$D/repo" && python3 -m unittest -q 2>&1 | tail -2)

install -m 644 "$D/repo/deploy/mkhalifa-polymarket.service" /etc/systemd/system/
install -m 644 "$D/repo/deploy/mkhalifa-pm-update.service" /etc/systemd/system/
install -m 644 "$D/repo/deploy/mkhalifa-pm-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mkhalifa-polymarket.service mkhalifa-pm-update.timer >/dev/null 2>&1
systemctl restart mkhalifa-polymarket.service

echo
echo "== live check (what this server sees)"
CHECK=$(runuser -u pmbot -- bash -c "cd $D/repo && set -a && . ./config.env && . $D/.env && set +a && python3 -m pmbot.main --check" 2>&1 || true)
echo "$CHECK"
echo
sleep 5
PM=$(systemctl is-active mkhalifa-polymarket.service || true)
LIVE=$(docker inspect -f '{{.State.Status}}' mkhalifa-bot 2>/dev/null || echo 'not found')
echo "== paper bot: $PM"
echo "== live bot container: $LIVE"
echo "DONE. First report: tomorrow 09:01 Cairo."

# one confirmation message on Telegram (the owner may not see this console)
TOK=$(grep "^TELEGRAM_BOT_TOKEN=" "$D/.env" | cut -d= -f2- || true)
CHAT=$(grep "^TELEGRAM_CHAT_ID=" "$D/.env" | cut -d= -f2- || true)
if [ -n "$TOK" ] && [ -n "$CHAT" ]; then
  SUMMARY=$(echo "$CHECK" | { grep -E '^(OK|FAIL|ALL|SOME|Telegram)' | sed -E 's/[<>&]//g; s/(book: ).*/\1ok/' || true; } | cut -c1-120)
  MSG="🎯 <b>Polymarket paper bot installed</b> (no real money)
Paper bot: $PM · live bot: $LIVE
<code>$SUMMARY</code>
Daily report from tomorrow at 09:01."
  curl -s -m 20 -o /dev/null "https://api.telegram.org/bot$TOK/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "parse_mode=HTML" --data-urlencode "text=$MSG" || true
fi
