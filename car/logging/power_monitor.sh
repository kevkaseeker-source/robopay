#!/bin/bash
# Logs the RPi's power/throttle state to the journal, so a crash can be
# traced back to undervoltage (or ruled out) after the fact.
#
# - Every SAMPLE_S: read `vcgencmd get_throttled` (+ 5V input on a Pi 5).
#   Any change in the throttled bits is logged immediately; if undervoltage
#   is active right now the line is logged at CRIT, which journald syncs to
#   disk at once (not after SyncIntervalSec) - so it survives the crash it
#   is warning about.
# - Every HEARTBEAT_S: one info line with voltage/temp, so the journal shows
#   how the supply looked right before the log stops.
#
# Runs as power-monitor.service (root: /dev/vcio is root-only).
# Lines are prefixed "<N>" so systemd maps them to syslog priorities.

SAMPLE_S="${SAMPLE_S:-2}"
HEARTBEAT_S="${HEARTBEAT_S:-30}"
LOW_VOLT="${LOW_VOLT:-4.90}"   # Pi 5 flags undervoltage around 4.63V; warn earlier

describe() {
    # Decode the get_throttled bitmask into readable flags.
    local v=$(( $1 )) out=""
    (( v & 0x1 ))     && out+=" UNDERVOLTAGE_NOW"
    (( v & 0x2 ))     && out+=" FREQ_CAPPED_NOW"
    (( v & 0x4 ))     && out+=" THROTTLED_NOW"
    (( v & 0x8 ))     && out+=" SOFT_TEMP_LIMIT_NOW"
    (( v & 0x10000 )) && out+=" undervoltage_since_boot"
    (( v & 0x20000 )) && out+=" freq_capped_since_boot"
    (( v & 0x40000 )) && out+=" throttled_since_boot"
    (( v & 0x80000 )) && out+=" soft_temp_limit_since_boot"
    echo "${out:- ok}"
}

read_state() {
    throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    throttled=${throttled:-unknown}
    # Pi 5 only; empty on older models.
    volt=$(vcgencmd pmic_read_adc EXT5V_V 2>/dev/null | sed -n 's/.*=\([0-9.]*\)V/\1/p')
    temp=$(vcgencmd measure_temp 2>/dev/null | sed -n "s/temp=\([0-9.]*\).*/\1/p")
}

last=""
last_heartbeat=0
while true; do
    read_state
    now=$(date +%s)
    summary="throttled=$throttled ext5v=${volt:-n/a}V temp=${temp:-n/a}C"

    if [[ "$throttled" != "$last" ]]; then
        if [[ "$throttled" == unknown ]]; then
            echo "<4>power: cannot read vcgencmd ($summary)"
        elif (( throttled & 0x1 )); then
            echo "<2>power: UNDERVOLTAGE right now -$(describe "$throttled") ($summary)"
        elif (( throttled & 0xF )); then
            echo "<4>power: state changed -$(describe "$throttled") ($summary)"
        else
            echo "<5>power: state changed -$(describe "$throttled") ($summary)"
        fi
        last="$throttled"
        last_heartbeat=$now
    elif [[ -n "$volt" ]] && awk -v v="$volt" -v lo="$LOW_VOLT" 'BEGIN{exit !(v < lo)}'; then
        echo "<4>power: input voltage low ($summary)"
        last_heartbeat=$now
    elif (( now - last_heartbeat >= HEARTBEAT_S )); then
        echo "<6>power: $summary"
        last_heartbeat=$now
    fi

    sleep "$SAMPLE_S"
done
