"""Agent software identities and parser configuration."""

from app.core.time import utc_now
from app.db.proxy.common import json, sqlite3, uuid
from app.db.proxy.agent_subscriptions import ProxySubscriptionMixin, _json_object
from app.services.agent_usage.registry import AGENT_TYPES


# Kept as the public compatibility name used by integrations.  The parser
# registry is the single source of truth so a newly added adapter is accepted
# by the API and is actually importable by the worker.
SUPPORTED_AGENT_TYPES = AGENT_TYPES


class ProxyAgentMixin(ProxySubscriptionMixin):
    def get_agent_types(self) -> list[dict]:
        return [{"kind": kind, **payload}
                for kind, payload in SUPPORTED_AGENT_TYPES.items()]

    def get_agent_software(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT s.id,s.uuid,s.name,s.agent_kind,s.config_json,"
                "s.created_at,s.updated_at "
                "FROM agent_software s JOIN accounts a ON a.id=s.id "
                "WHERE a.account_kind='agent' "
                "ORDER BY s.name COLLATE NOCASE"
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["config"] = json.loads(item.pop("config_json") or "{}")
                except (TypeError, ValueError):
                    item["config"] = {}
                item.pop("status_json", None)
                item["subscription_ids"] = [r[0] for r in conn.execute(
                    "SELECT subscription_id FROM agent_subscription_bindings "
                    "WHERE software_id=? "
                "AND (ends_at IS NULL OR ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                    "ORDER BY subscription_id", (row["id"],)).fetchall()]
                result.append(item)
            return result
        finally:
            conn.close()

    def create_agent_software(self, data: dict) -> int:
        name = str(data.get("name") or "").strip()
        kind = str(data.get("agent_kind") or data.get("kind") or "").strip().lower()
        if not name:
            raise ValueError("软件名称不能为空")
        if kind not in SUPPORTED_AGENT_TYPES:
            raise ValueError(f"暂不支持的软件类型: {kind}")
        config_json = _json_object(data.get("config", data.get("config_json")))
        conn = self._connect()
        try:
            software_id = self._next_shared_id(conn)
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,valid_from,account_kind) "
                "VALUES(?,?,?,?, 'agent')",
                (software_id, str(uuid.uuid4()), name,
                 now[:10]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO account_identities"
                "(id,uuid,name,account_kind,created_at,updated_at) "
                "SELECT id,uuid,name,'agent',created_at,updated_at "
                "FROM accounts WHERE id=?", (software_id,)
            )
            conn.execute(
                "INSERT INTO agent_software(id,uuid,name,agent_kind,config_json,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (software_id, str(uuid.uuid4()), name, kind, config_json,
                 1, now, now),
            )
            conn.execute(
                "INSERT INTO agent_software_runtime(software_id) VALUES(?)",
                (software_id,),
            )
            self._replace_bindings(conn, software_id,
                                   data.get("subscription_ids") or [])
            conn.commit()
            return int(software_id)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("软件数据冲突") from exc
        finally:
            conn.close()

    def update_agent_software(self, software_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT s.* FROM agent_software s "
                "JOIN accounts a ON a.id=s.id WHERE s.id=? "
                "AND a.account_kind='agent'",
                (software_id,),
            ).fetchone()
            if current is None:
                return False
            fields, values = [], []
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            if "name" in data:
                name = str(data.get("name") or "").strip()
                if not name:
                    raise ValueError("软件名称不能为空")
                fields.append("name=?")
                values.append(name)
                conn.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=?",
                             (name, now, software_id))
                conn.execute(
                    "UPDATE account_identities SET name=?,updated_at=? WHERE id=?",
                    (name, now, software_id),
                )
            kind = data.get("agent_kind", data.get("kind"))
            if kind is not None:
                kind = str(kind).strip().lower()
                if kind not in SUPPORTED_AGENT_TYPES:
                    raise ValueError(f"暂不支持的软件类型: {kind}")
                fields.append("agent_kind=?")
                values.append(kind)
            if "config" in data or "config_json" in data:
                fields.append("config_json=?")
                values.append(_json_object(data.get("config", data.get("config_json"))))
            if fields:
                fields.append("updated_at=?")
                values.extend([now, software_id])
                conn.execute("UPDATE agent_software SET " + ",".join(fields)
                             + " WHERE id=?", values)
            if "subscription_ids" in data:
                self._replace_bindings(conn, software_id,
                                       data.get("subscription_ids") or [])
            conn.commit()
            return bool(fields or "subscription_ids" in data)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("软件数据冲突") from exc
        finally:
            conn.close()

    def delete_agent_software(self, software_id: int) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.id FROM agent_software s JOIN accounts a ON a.id=s.id "
                "WHERE s.id=? AND a.account_kind='agent' "
                "", (software_id,)
            ).fetchone()
            if row is None:
                return False
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE request_log SET account_identity_id=COALESCE(account_identity_id,account_id),"
                "account_id=NULL,agent_software_id=NULL WHERE account_id=?",
                (software_id,),
            )
            conn.execute(
                "UPDATE request_attempts SET account_id=NULL WHERE account_id=?",
                (software_id,),
            )
            conn.execute(
                "DELETE FROM agent_subscription_bindings WHERE software_id=?",
                (software_id,),
            )
            conn.execute(
                "DELETE FROM agent_software_runtime WHERE software_id=?",
                (software_id,),
            )
            conn.execute(
                "DELETE FROM agent_software WHERE id=?", (software_id,)
            )
            conn.execute(
                "DELETE FROM account_importers WHERE account_id=?", (software_id,)
            )
            conn.execute(
                "DELETE FROM billing_rate_events WHERE contract_id IN "
                "(SELECT id FROM billing_contracts WHERE account_id=?)",
                (software_id,),
            )
            conn.execute("DELETE FROM billing_contracts WHERE account_id=?", (software_id,))
            conn.execute("DELETE FROM accounts WHERE id=? AND account_kind='agent'",
                         (software_id,))
            conn.commit()
            return True
        finally:
            conn.close()
