-- V1.10: software management has only present/deleted states.
--
-- The old enabled column is retained for old snapshots and code that still
-- knows the column exists, but it is no longer a software lifecycle switch.
-- The shared account row is the source of truth: active means present and
-- deleted is a tombstone kept for request-log foreign keys.

UPDATE agent_software
SET enabled=1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE enabled<>1;

UPDATE accounts
SET lifecycle_state='active',
    disabled_at=NULL,
    deleted_at=NULL,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE account_kind='agent' AND lifecycle_state='disabled';

-- V1.8 called the compatibility instance “默认实例”.  It is now an
-- ordinary peer instance.  Avoid a unique-name conflict if a user already
-- created an instance named “实例 1”.
UPDATE agent_subscription_instances
SET label=CASE
        WHEN EXISTS (
            SELECT 1 FROM agent_subscription_instances peer
            WHERE peer.subscription_id=agent_subscription_instances.subscription_id
              AND peer.id<>agent_subscription_instances.id
              AND peer.label='实例 1'
        ) THEN '实例 ' || agent_subscription_instances.id
        ELSE '实例 1'
    END,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE label='默认实例';
