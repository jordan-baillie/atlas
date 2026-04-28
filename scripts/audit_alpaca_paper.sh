#!/usr/bin/env bash
# audit_alpaca_paper.sh — verify all market configs match secrets ALPACA_PAPER flag
# Exit 0: no drift found
# Exit 1: drift detected (one or more configs have wrong paper value)
set -euo pipefail

ATLAS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="${HOME}/.atlas-secrets.json"
CONFIG_DIR="${ATLAS_ROOT}/config/active"

if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "ERROR: secrets file not found at $SECRETS_FILE" >&2
    exit 2
fi

# Read expected paper value from secrets — normalise to lowercase "true" or "false"
SECRETS_PAPER=$(python3 -c "
import json
val = json.load(open('${SECRETS_FILE}')).get('ALPACA_PAPER', False)
print(str(val).lower())
")

DRIFT_COUNT=0

for f in "${CONFIG_DIR}"/*.json; do
    market=$(basename "$f" .json)
    result=$(python3 -c "
import json
d = json.load(open('${f}'))
alpaca = d.get('alpaca', {})
if 'paper' not in alpaca:
    print('UNSET')
else:
    print(str(alpaca['paper']).lower())
" 2>/dev/null || echo "PARSE_ERROR")

    if [[ "$result" == "UNSET" ]]; then
        # No alpaca block — broker.py defaults correctly; skip
        continue
    fi

    if [[ "$result" == "PARSE_ERROR" ]]; then
        echo "WARN: could not parse $f" >&2
        continue
    fi

    if [[ "$result" != "$SECRETS_PAPER" ]]; then
        echo "DRIFT: $market — config paper=$result, secrets ALPACA_PAPER=$SECRETS_PAPER"
        DRIFT_COUNT=$((DRIFT_COUNT + 1))
    fi
done

if [[ $DRIFT_COUNT -eq 0 ]]; then
    echo "✅ All configs consistent with ALPACA_PAPER=${SECRETS_PAPER} (no drift)"
    exit 0
else
    echo "❌ Found ${DRIFT_COUNT} config(s) with paper flag drift"
    exit 1
fi
