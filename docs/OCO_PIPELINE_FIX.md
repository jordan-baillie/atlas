# OCO Pipeline Fix — Stop-Loss + Take-Profit Orders

## Problem (Before Fix)

When `sync_all_protective_orders()` ran with Path A (positions with both SL and TP):

1. **SL STOP SELL** placed first → ✅ success, claims all position shares
2. **TP LIMIT SELL** attempted → ❌ skipped with "qty_held_by_stop"

**Root cause**: Alpaca doesn't allow two independent SELL orders on the same position. The SL order already holds all shares, so the TP order is rejected.

**Result**: Positions had stop-loss protection but no automated profit-taking.

## Solution (After Fix)

Replace separate SL + TP placement with **OCO (One-Cancels-Other)** orders:

- **Both legs placed atomically** in a single API call
- When one leg fills, the other **auto-cancels**
- No share allocation conflicts

## Implementation Details

### Path A Logic (has TP)

```
1. Check if BOTH SL and TP already exist
   → YES: Skip (no action needed)
   → NO: Continue to step 2

2. Cancel any existing individual SL or TP orders
   → Prevents conflicts with new OCO order

3. Place single OCO order with both legs:
   - Stop-loss leg: STOP SELL @ stop_price
   - Take-profit leg: LIMIT SELL @ take_profit
   - Order class: OCO (one-cancels-other)
   - Time in force: GTC (good-til-canceled)

4. Error handling:
   - PDT error → defer to next pre-market sync
   - Other error → fallback to SL-only (position still protected)
```

### Code Changes

**File**: `/root/atlas/brokers/alpaca/broker.py`

**Added import**:
```python
from alpaca.trading.requests import TakeProfitRequest
```

**OCO order submission**:
```python
request = MarketOrderRequest(
    symbol=ticker,
    qty=pos.shares,
    side=AlpacaSide.SELL,
    order_class=OrderClass.OCO,
    take_profit=TakeProfitRequest(limit_price=take_profit),
    stop_loss=StopLossRequest(stop_price=stop_price),
    time_in_force=TimeInForce.GTC,
)
order = self._client.submit_order(request)
```

### Unchanged Components

- **Path B** (trailing stop for positions without TP) → completely untouched
- **PDT error detection** → same `_is_pdt_error()` helper
- **Price matching** → same `_prices_match()` helper
- **Result counters** → same `sl_placed`, `tp_placed`, etc.
- **Logging patterns** → consistent with existing style

## Testing

### Import Test
```bash
python3 -c "from brokers.alpaca.broker import AlpacaBroker; print('✅ Import OK')"
```

### Key Verification Points

✅ `TakeProfitRequest` imported  
✅ `OrderClass.OCO` used in Path A  
✅ Fallback to SL-only on OCO failure  
✅ PDT handling preserved  
✅ Cancel existing orders before OCO placement  
✅ Path B (trailing stop) untouched  

## Expected Behavior Changes

### Before (Broken)
```
Position: AAPL 10 shares @ $150.00
Plan: SL=$145.00, TP=$160.00

sync_protective runs:
  ✅ Placed STOP SELL 10 @ $145.00 → order_123
  ❌ Skipped LIMIT SELL 10 @ $160.00 (qty_held_by_stop)

Result: Position protected by SL, but no TP
```

### After (Fixed)
```
Position: AAPL 10 shares @ $150.00
Plan: SL=$145.00, TP=$160.00

sync_protective runs:
  ✅ Placed OCO SELL 10 → order_456
      - Leg 1: STOP @ $145.00
      - Leg 2: LIMIT @ $160.00
      - When one fills, other auto-cancels

Result: Position protected by SL AND has automated profit-taking
```

## Fallback Safety

If OCO placement fails (non-PDT error):
1. Log warning: "OCO order failed — falling back to SL-only"
2. Attempt to place simple STOP SELL (current behavior)
3. Position still gets stop-loss protection
4. TP is skipped with reason "oco_failed_sl_fallback"

**Why**: Ensures positions are ALWAYS protected by at least a stop-loss, even if OCO doesn't work.

## Ticket Result Fields

New/changed fields in `per_ticker[ticker]`:

- `sl_action`: "oco_placed" | "dry_run_oco" | "placed_fallback" | (existing values)
- `tp_action`: "oco_placed" | "dry_run_oco" | (existing values)
- `oco_order_id`: Parent OCO order ID (when successful)
- `canceled_orders`: List of canceled order IDs (e.g., `["stop:abc123", "tp:def456"]`)
- `oco_error`: OCO failure message (when fallback used)

## Next Steps

1. **Monitor first OCO placements in pre-market sync**
   - Check logs for "placed OCO SELL GTC"
   - Verify both legs appear in Alpaca dashboard
   
2. **Verify one-cancels-other behavior**
   - When SL fills → TP should auto-cancel
   - When TP fills → SL should auto-cancel

3. **Check for edge cases**
   - Same-day entry positions (PDT deferral)
   - Partial fills (should be handled by Alpaca)
   - Reconnection after network error

## Related Files

- `/root/atlas/brokers/alpaca/broker.py` — Main implementation
- `/root/atlas/brokers/base.py` — BrokerAdapter interface (unchanged)
- `/root/atlas/tasks/todo.md` — Task tracking

## References

- [Alpaca OCO Orders Documentation](https://alpaca.markets/docs/trading/orders/#bracket-oco-and-oto-orders)
- Atlas protective order sync: runs daily pre-market and post-close
- Related lesson: "Never write paper_state files when broker is offline"
