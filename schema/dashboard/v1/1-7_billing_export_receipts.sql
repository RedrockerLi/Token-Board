-- V1.7: retain shared receipts for immutable billing export events.
-- Receipts survive visible-account deletion so another machine cannot
-- resurrect a previously delivered recurring charge.

CREATE TABLE billing_export_receipts (
    event_key TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    billing_unit_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_billing_export_receipts_account
    ON billing_export_receipts(account_id, month);

-- Existing visible frozen rows are already part of the archive.  A legacy
-- bucket receipt lets a newly provisioned machine skip reconstructing those
-- rows from its local runtime ledger.
INSERT OR IGNORE INTO billing_export_receipts
    (event_key,account_id,month,billing_unit_id,payload_hash)
SELECT 'legacy:' || account_id || ':' || month || ':' || billing_unit_id,
       account_id,month,billing_unit_id,''
FROM monthly_recurring_costs
WHERE charge_frozen_at IS NOT NULL;
