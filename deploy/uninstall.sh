#!/usr/bin/env bash
# Remove Dyson Pi.
#   sudo ./uninstall.sh            remove services, keep config + data
#   sudo ./uninstall.sh --purge    also delete config, database, logs, app, user, Wi-Fi connection
set -euo pipefail
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }

echo "==> Stopping services (relay is forced OFF on stop)"
systemctl disable --now dyson-pi-netwatch.timer dyson-pi.service 2>/dev/null || true
/opt/dyson-pi/deploy/relay-off.sh 2>/dev/null || true
rm -f /etc/systemd/system/dyson-pi.service /etc/systemd/system/dyson-pi-netwatch.service \
      /etc/systemd/system/dyson-pi-netwatch.timer /etc/systemd/system.conf.d/dyson-pi-watchdog.conf
systemctl daemon-reload
rm -f /etc/avahi/services/dyson-pi.service /usr/local/bin/dysonctl
systemctl restart avahi-daemon 2>/dev/null || true

BOOTCFG=/boot/firmware/config.txt
[[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
if [[ -f $BOOTCFG ]] && grep -q '^# BEGIN dyson-pi' "$BOOTCFG"; then
    echo "==> Removing dyson-pi block from $BOOTCFG"
    echo "    (the relay GPIO will no longer be held OFF at power-on: disconnect the relay"
    echo "     or the air supply if the hardware stays installed)"
    sed -i '/^# BEGIN dyson-pi/,/^# END dyson-pi/d' "$BOOTCFG"
fi

if [[ $PURGE -eq 1 ]]; then
    read -r -p "Delete config, database, logs and the app? Type 'purge' to confirm: " c </dev/tty
    if [[ "$c" == "purge" ]]; then
        CONN=$(/opt/dyson-pi/.venv/bin/dysonctl --config /etc/dyson-pi/config.yaml config get network.connection_name 2>/dev/null || echo dyson-hotspot)
        rm -rf /opt/dyson-pi /etc/dyson-pi /var/lib/dyson-pi /var/log/dyson-pi
        userdel dyson-pi 2>/dev/null || true
        read -r -p "Also delete the Wi-Fi connection '$CONN'? (only if you have another way in) [y/N] " w </dev/tty
        [[ "$w" =~ ^[Yy]$ ]] && nmcli connection delete "$CONN" || true
        echo "Purged."
    else
        echo "Purge cancelled; data kept."
    fi
else
    echo "Kept: /etc/dyson-pi (config), /var/lib/dyson-pi (database), /opt/dyson-pi (app), Wi-Fi connection."
fi
echo "Hostname and timezone were left unchanged. Reboot to drop the hardware watchdog setting."
