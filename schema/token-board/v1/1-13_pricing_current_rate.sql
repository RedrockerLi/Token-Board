-- V1.13: keep at most one current rate per pricing rule.
--
-- Older sync merges could restore an older rate with valid_until=NULL while a
-- newer local rate was still current.  Keep the newest version and close all
-- older current rows at the winning version's start time before enforcing the
-- invariant for future writes and merges.
UPDATE pricing_rates
SET valid_until = (
    SELECT winner.valid_from
    FROM pricing_rates AS winner
    WHERE winner.pricing_rule_id = pricing_rates.pricing_rule_id
      AND winner.valid_until IS NULL
    ORDER BY winner.valid_from DESC, winner.id DESC
    LIMIT 1
)
WHERE pricing_rates.valid_until IS NULL
  AND EXISTS (
      SELECT 1
      FROM pricing_rates AS newer
      WHERE newer.pricing_rule_id = pricing_rates.pricing_rule_id
        AND newer.valid_until IS NULL
        AND (newer.valid_from > pricing_rates.valid_from
             OR (newer.valid_from = pricing_rates.valid_from
                 AND newer.id > pricing_rates.id))
  );

CREATE UNIQUE INDEX idx_pricing_rates_one_current
    ON pricing_rates(pricing_rule_id)
    WHERE valid_until IS NULL;
