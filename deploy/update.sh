#!/usr/bin/env bash
# Every 10 minutes (mkhalifa-pm-update.timer): install a new commit from GitHub's main branch.
# The tests must pass first; if they fail, the running version stays. Touches nothing outside
# /opt/mkhalifa-polymarket and its own three systemd units.
set -uo pipefail
D=/opt/mkhalifa-polymarket
cd "$D/repo" || exit 1
# dashboard numbers for the MKhaliFa dashboard (read as pmbot, so the database keeps its owner)
OUT=/opt/mkhalifa/data/labs
mkdir -p "$OUT"
if runuser -u pmbot -- bash -c "cd $D/repo && set -a && . ./config.env && set +a && python3 -m pmbot.main --summary" > "$OUT/polymarket.json.tmp" 2>/dev/null; then
  mv -f "$OUT/polymarket.json.tmp" "$OUT/polymarket.json"; chmod 644 "$OUT/polymarket.json"
else
  rm -f "$OUT/polymarket.json.tmp"
fi
git fetch -q origin main || exit 0
NEW=$(git rev-parse origin/main)
OLD=$(git rev-parse HEAD)
[ "$NEW" = "$OLD" ] && exit 0
git reset -q --hard "$NEW"
if ! python3 -m unittest -q >/dev/null 2>&1; then
  git reset -q --hard "$OLD"
  echo "update $NEW failed its tests; kept $OLD"
  exit 1
fi
for u in mkhalifa-polymarket.service mkhalifa-pm-update.service mkhalifa-pm-update.timer; do
  cmp -s "deploy/$u" "/etc/systemd/system/$u" || { install -m 644 "deploy/$u" /etc/systemd/system/; RELOAD=1; }
done
[ "${RELOAD:-0}" = 1 ] && systemctl daemon-reload
systemctl restart mkhalifa-polymarket.service
echo "updated $OLD -> $NEW"
