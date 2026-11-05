#!/usr/bin/env bash
# Update Dyson Pi: git pull, reinstall the package and units, restart.
#   sudo /opt/dyson-pi/deploy/update.sh
set -euo pipefail
APP_DIR=/opt/dyson-pi
VENV=$APP_DIR/.venv
CONFIG=/etc/dyson-pi/config.yaml

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }
cd "$APP_DIR"
if [[ -d .git ]]; then
    echo "==> git pull"
    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only
    AFTER=$(git rev-parse HEAD)
    [[ "$BEFORE" == "$AFTER" ]] && echo "Already up to date ($AFTER)." || git --no-pager log --oneline "$BEFORE..$AFTER"
else
    echo "$APP_DIR is not a git checkout. Copy the new version somewhere and run its deploy/install.sh instead." >&2
    exit 1
fi

echo "==> Installing package"
"$VENV/bin/pip" install -q "$APP_DIR"
chmod 755 "$APP_DIR/deploy/"*.sh

echo "==> Validating config"
"$VENV/bin/dysonctl" --config "$CONFIG" config check

echo "==> Updating systemd units"
for u in dyson-pi.service dyson-pi-netwatch.service dyson-pi-netwatch.timer; do
    install -m 0644 "$APP_DIR/deploy/systemd/$u" "/etc/systemd/system/$u"
done
install -m 0644 "$APP_DIR/deploy/avahi/dyson-pi.service" /etc/avahi/services/dyson-pi.service
systemctl daemon-reload

echo "==> Restarting"
systemctl restart dyson-pi.service
for _ in $(seq 1 30); do
    if "$VENV/bin/dysonctl" --config "$CONFIG" health >/dev/null 2>&1; then
        echo "Service is up: $("$VENV/bin/python" -c 'import dyson_pi; print("v" + dyson_pi.__version__)')"
        exit 0
    fi
    sleep 1
done
echo "Service did not come back: journalctl -u dyson-pi -e" >&2
exit 1
