-- V1.4: invalidate the snapshot when the local model catalog changes.
-- Other route, credential and timeout tables already carry V1.0/V1.1
-- generation triggers; this migration only closes the remaining catalog gap.

CREATE TRIGGER config_catalog_ai AFTER INSERT ON upstream_model_catalog BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_catalog_au AFTER UPDATE ON upstream_model_catalog BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
CREATE TRIGGER config_catalog_ad AFTER DELETE ON upstream_model_catalog BEGIN
  UPDATE config_state SET generation=generation+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
