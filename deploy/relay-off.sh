#!/bin/sh
# Force the actuator relay OFF at the hardware level.
#
# Used by systemd as ExecStartPre and ExecStopPost of dyson-pi.service, so it
# runs after every exit: clean stop, crash, OOM kill, kill -9.
# `pinctrl` writes the GPIO registers directly and the level stays latched
# after this script exits. No Python involved, so it works even if the venv
# is broken.
#
# Pin/level come from (first found):
#   /run/dyson-pi/relay-off.env   written by the running service (current config)
#   /etc/dyson-pi/relay-off.env   written by install.sh
#   built-in default: GPIO17, drive HIGH (= OFF for an active-low relay module)

RELAY_PIN=17
RELAY_OFF_LEVEL=dh
for f in /run/dyson-pi/relay-off.env /etc/dyson-pi/relay-off.env; do
    if [ -r "$f" ]; then
        # Parse, never source: this runs as root and the /run file is written
        # by the unprivileged service user. Only digits / dh|dl are accepted.
        pin=$(sed -n 's/^RELAY_PIN=\([0-9][0-9]*\)$/\1/p' "$f" | head -n1)
        lvl=$(sed -n 's/^RELAY_OFF_LEVEL=\(dh\|dl\)$/\1/p' "$f" | head -n1)
        if [ -n "$pin" ] && [ "$pin" -le 27 ] && [ -n "$lvl" ]; then
            RELAY_PIN=$pin
            RELAY_OFF_LEVEL=$lvl
            break
        fi
    fi
done

if command -v pinctrl >/dev/null 2>&1; then
    pinctrl set "$RELAY_PIN" op "$RELAY_OFF_LEVEL" && \
        echo "relay-off: GPIO$RELAY_PIN set to output $RELAY_OFF_LEVEL (relay OFF)"
elif command -v raspi-gpio >/dev/null 2>&1; then
    raspi-gpio set "$RELAY_PIN" op "$RELAY_OFF_LEVEL" && \
        echo "relay-off: GPIO$RELAY_PIN set to output $RELAY_OFF_LEVEL via raspi-gpio (relay OFF)"
else
    echo "relay-off: WARNING neither pinctrl nor raspi-gpio found; cannot latch relay OFF" >&2
fi
# Never fail the unit because of this script.
exit 0
