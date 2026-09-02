# Freeze recurring charges at period start

Recurring subscription charges are selected at each billing unit's `period_start` and frozen as immutable ledger entries; later price changes apply only to the next period, and cancellation does not rewrite the current charge. Dashboard projection uses frozen entries plus durable account exclusions, while zero-only charges remain in the financial ledger but are hidden from the Dashboard. This replaces the previous open-period reconciliation because repeatedly rewriting current charges caused deleted users to be recreated during export.
