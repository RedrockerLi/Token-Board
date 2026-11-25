-- Google Gemini GenerateContent is a client wire format of its own, so it gets
-- its own per-format timeout group like the other three.  Values start at the
-- table defaults; the settings page owns them from there.
INSERT OR IGNORE INTO proxy_timeout_config(endpoint_kind) VALUES ('generate');
