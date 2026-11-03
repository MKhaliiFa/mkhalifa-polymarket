#!/usr/bin/env bash
# Every 10 minutes (mkhalifa-pm-update.timer): install a new commit from GitHub's main branch.
# The tests must pass first; if they fail, the running version stays. Touches nothing outside
# /opt/mkhalifa-polymarket and its own three systemd units.
set -uo pipefail
D=/opt/mkhalifa-polymarket
cd "$D/repo" || exit 1
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
