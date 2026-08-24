-- V1.9: make the compatibility instance created by V1.8 deterministic.
--
-- V1.8 converted the old one-stream subscription into a default instance
-- using randomblob().  That made an unchanged V1.7 cloud artifact hash
-- differently when each machine upgraded it locally.  The legacy default
-- instance has no independent identity in V1.7, so derive its identity from
-- the stable parent subscription UUID.  User-created additional instances
-- already carry their synchronized UUID and are left untouched.

UPDATE agent_subscription_instances
SET uuid='agent-instance:' || (
    SELECT s.uuid FROM agent_subscriptions s
    WHERE s.id=agent_subscription_instances.subscription_id
)
WHERE label='默认实例'
  AND uuid NOT LIKE 'agent-instance:%';
