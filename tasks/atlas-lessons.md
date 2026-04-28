# Atlas Operational Lessons

## Auth & Broker Configuration

### `alpaca.paper` must match `~/.atlas-secrets.json:ALPACA_PAPER`
**Symptom**: One market repeatedly fails with `401 40110000` ("invalid credentials") on every retry while OTHER markets connect to Alpaca cleanly. Settlement / sync / order-placement all abort after exhausting retries.

**Root cause**: `config/active/<market>.json` has `alpaca.paper: true` but credentials in `~/.atlas-secrets.json` are configured for live trading (`ALPACA_PAPER: false`). The Alpaca client routes to `paper-api.alpaca.markets` and submits live keys, which the paper endpoint rejects.

**Fix**: Set `alpaca.paper` in the market config to match `~/.atlas-secrets.json:ALPACA_PAPER`. For production, both should be `false`.

**Audit one-liner**:
```bash
PAPER=$(python3 -c "import json; print(json.load(open('/root/.atlas-secrets.json')).get('ALPACA_PAPER'))")
for f in config/active/*.json; do
  P=$(python3 -c "import json; print(json.load(open('$f')).get('alpaca',{}).get('paper','UNSET'))")
  if [ "$P" != "UNSET" ] && [ "$P" != "$(echo $PAPER | python3 -c 'import sys; print(sys.stdin.read().strip().capitalize())')" ]; then
    echo "DRIFT: $f paper=$P (secrets=$PAPER)"
  fi
done
echo "Audit complete."
```

**History**: 2026-04-25 sector_etfs settlement aborted because of this. Audit found defensive_etfs + treasury_etfs also drifted.
