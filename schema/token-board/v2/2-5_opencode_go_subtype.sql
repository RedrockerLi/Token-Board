-- Token-Board V2.5: OpenCode Go upstream subtype, per-key display name and
-- the per-key usage snapshot written by the usage poller.

ALTER TABLE upstreams ADD COLUMN subtype TEXT;

ALTER TABLE upstream_credentials ADD COLUMN label TEXT;

ALTER TABLE upstream_credentials ADD COLUMN usage_json TEXT;
