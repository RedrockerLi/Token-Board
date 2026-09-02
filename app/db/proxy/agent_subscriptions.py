"""Subscription parents, billable instances and software bindings."""

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import (
    _billing_period_month, _next_month, _parse_iso_date,
    _period_start, datetime, json,
    sqlite3, timedelta, uuid,
)


def _iso_start(value: object | None) -> str:
    if value in (None, ""):
        return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and "T" in value:
        return parse_runtime_timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = _parse_iso_date(value)
    return parsed.isoformat() + "T00:00:00Z"


def _json_object(value: object | None) -> str:
    if value in (None, ""):
        return "{}"
    if not isinstance(value, dict):
        raise ValueError("解析器配置必须是 JSON 对象")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _number(value: object, message: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if result < 0:
        raise ValueError("月费不能为负数")
    return result


class ProxySubscriptionMixin:
    def _subscription_price_rule(self, conn: sqlite3.Connection,
                                 data: dict) -> str:
        return "next_period"

    def _subscription_end(self, conn: sqlite3.Connection,
                          valid_from: object, now: datetime) -> datetime:
        config = self._billing_config_conn(conn)
        if config["cancellation_mode"] == "immediate":
            return now
        if isinstance(valid_from, str) and "T" in valid_from:
            anchor = parse_runtime_timestamp(valid_from).date()
        else:
            anchor = _parse_iso_date(valid_from)
        current = _billing_period_month(now, anchor.day)
        return (_period_start(_next_month(current), anchor.day)
                - timedelta(seconds=1))

    @staticmethod
    def _update_instance_row(conn: sqlite3.Connection, row: sqlite3.Row,
                             data: dict, now: str,
                             effective_rule: str) -> bool:
        fields, values = [], []
        if "label" in data:
            label = str(data.get("label") or "").strip()
            if not label:
                raise ValueError("实例名称不能为空")
            fields.append("label=?")
            values.append(label)
        if "valid_from" in data or "start_time" in data:
            fields.append("valid_from=?")
            values.append(_iso_start(data.get("valid_from") or data.get("start_time")))
        if fields:
            fields.append("updated_at=?")
            values.extend([now, row["id"]])
            conn.execute("UPDATE agent_subscription_instances SET "
                         + ",".join(fields) + " WHERE id=?", values)
        if "monthly_price" in data:
            price = _number(data["monthly_price"], "月费必须是数字")
            conn.execute(
                "INSERT INTO agent_subscription_rate_events"
                "(instance_id,recurring_price,effective_at,effective_rule) "
                "VALUES(?,?,?,?) ON CONFLICT(instance_id,effective_at,effective_rule) "
                "DO UPDATE SET recurring_price=excluded.recurring_price",
                (row["id"], price, now, effective_rule),
            )
        return bool(fields or "monthly_price" in data)

    def _delete_instance_row(self, conn: sqlite3.Connection,
                             row: sqlite3.Row, now_dt: datetime) -> None:
        end = self._subscription_end(conn, row["valid_from"], now_dt)
        end_text = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE agent_subscription_instances SET lifecycle_state='deleted',"
            "valid_until=?,updated_at=? WHERE id=?",
            (end_text, end_text, row["id"]),
        )

    @staticmethod
    def _instance_payload(row: sqlite3.Row, currency: str,
                          price: float | None = None) -> dict:
        return {
            "id": row["id"], "uuid": row["uuid"], "label": row["label"],
            "valid_from": row["valid_from"], "currency": currency,
            "monthly_price": price if price is not None else 0,
            "updated_at": row["updated_at"],
        }

    def _subscription_instances(self, conn: sqlite3.Connection,
                                subscription_id: int,
                                currency: str) -> list[dict]:
        rows = conn.execute(
            "SELECT i.*,COALESCE((SELECT r.recurring_price "
            "FROM agent_subscription_rate_events r "
            "WHERE r.instance_id=i.id ORDER BY r.effective_at DESC,r.id DESC "
            "LIMIT 1),0) monthly_price "
            "FROM agent_subscription_instances i "
            "WHERE i.subscription_id=? AND (i.lifecycle_state!='deleted' "
            "OR (i.valid_until IS NOT NULL AND i.valid_until>"
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'))) "
            "ORDER BY i.id", (subscription_id,)).fetchall()
        return [self._instance_payload(row, currency, row["monthly_price"])
                for row in rows]

    def get_agent_subscriptions(self) -> list[dict]:
        conn = self._connect()
        try:
            result = []
            rows = conn.execute(
                "SELECT s.id,s.uuid,s.name,s.currency,s.valid_from,s.created_at,s.updated_at "
                "FROM agent_subscriptions s WHERE s.lifecycle_state!='deleted' "
                "ORDER BY s.name COLLATE NOCASE"
            ).fetchall()
            for row in rows:
                instances = self._subscription_instances(
                    conn, row["id"], row["currency"])
                bindings = [item[0] for item in conn.execute(
                    "SELECT software_id FROM agent_subscription_bindings "
                    "WHERE subscription_id=? AND lifecycle_state='active' "
                    "AND (valid_until IS NULL OR valid_until>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                    "ORDER BY software_id", (row["id"],)).fetchall()]
                item = dict(row)
                item["instances"] = instances
                item["software_ids"] = bindings
                item["monthly_price"] = instances[0]["monthly_price"] if instances else 0
                result.append(item)
            return result
        finally:
            conn.close()

    def _validate_instances(self, data: dict, currency: str,
                            parent_start: str) -> list[dict]:
        raw = data.get("instances")
        if raw is None:
            raw = [{"label": "实例 1",
                    "valid_from": data.get("valid_from") or data.get("start_time")
                    or parent_start, "monthly_price": data.get("monthly_price", 0)}]
        if not isinstance(raw, list) or not raw:
            raise ValueError("至少需要一个订阅实例")
        instances, labels = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("订阅实例格式错误")
            label = str(item.get("label") or "实例 1").strip() or "实例 1"
            if label in labels:
                raise ValueError("同一订阅下的实例名称不能重复")
            labels.add(label)
            instances.append({
                "id": item.get("id"), "label": label,
                "valid_from": _iso_start(item.get("valid_from") or
                                           item.get("start_time") or parent_start),
                "monthly_price": _number(item.get("monthly_price", 0), "月费必须是数字"),
            })
        return instances

    def create_agent_subscription(self, data: dict) -> int:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("订阅名称不能为空")
        currency = str(data.get("currency") or "CNY").upper()
        if currency not in {"CNY", "USD"}:
            raise ValueError("币种必须是 CNY 或 USD")
        parent_start = _iso_start(data.get("valid_from") or data.get("start_time"))
        instances = self._validate_instances(data, currency, parent_start)
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._connect()
        try:
            sid = conn.execute(
                "INSERT INTO agent_subscriptions"
                "(uuid,name,currency,valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), name, currency, parent_start, now, now),
            ).lastrowid
            self._insert_instances(conn, int(sid), instances, now)
            conn.commit()
            return int(sid)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("订阅名称或实例名称已存在") from exc
        finally:
            conn.close()

    @staticmethod
    def _insert_instances(conn: sqlite3.Connection, subscription_id: int,
                          instances: list[dict], now: str) -> None:
        for instance in instances:
            iid = conn.execute(
                "INSERT INTO agent_subscription_instances"
                "(uuid,subscription_id,label,valid_from,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), subscription_id, instance["label"],
                 instance["valid_from"], now, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO agent_subscription_rate_events"
                "(instance_id,recurring_price,effective_at) VALUES(?,?,?)",
                (iid, instance["monthly_price"], instance["valid_from"]),
            )

    def get_agent_subscription_instances(self, subscription_id: int) -> list[dict]:
        conn = self._connect()
        try:
            parent = conn.execute(
                "SELECT currency FROM agent_subscriptions "
                "WHERE id=? AND lifecycle_state!='deleted'", (subscription_id,)
            ).fetchone()
            return (self._subscription_instances(conn, subscription_id, parent["currency"])
                    if parent else [])
        finally:
            conn.close()

    def create_agent_subscription_instance(self, subscription_id: int,
                                            data: dict) -> int:
        conn = self._connect()
        try:
            parent = conn.execute(
                "SELECT currency,valid_from FROM agent_subscriptions "
                "WHERE id=? AND lifecycle_state!='deleted'", (subscription_id,)
            ).fetchone()
            if parent is None:
                raise ValueError("订阅不存在")
            parsed = self._validate_instances(
                {"instances": [data]}, parent["currency"], parent["valid_from"])[0]
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            self._insert_instances(conn, subscription_id, [parsed], now)
            conn.execute("UPDATE agent_subscriptions SET updated_at=? WHERE id=?",
                         (now, subscription_id))
            iid = conn.execute(
                "SELECT id FROM agent_subscription_instances "
                "WHERE subscription_id=? AND label=?", (subscription_id, parsed["label"])
            ).fetchone()[0]
            conn.commit()
            return int(iid)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("实例名称已存在") from exc
        finally:
            conn.close()

    def update_agent_subscription_instance(self, instance_id: int,
                                            data: dict) -> bool:
        if "price_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT i.*,s.currency FROM agent_subscription_instances i "
                "JOIN agent_subscriptions s ON s.id=i.subscription_id "
                "WHERE i.id=? AND i.lifecycle_state!='deleted'", (instance_id,)
            ).fetchone()
            if row is None:
                return False
            now_dt = utc_now()
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            changed = self._update_instance_row(
                conn, row, data, now, self._subscription_price_rule(conn, data))
            if changed:
                conn.execute("UPDATE agent_subscriptions SET updated_at=? WHERE id=?",
                             (now, row["subscription_id"]))
            conn.commit()
            return changed
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("实例名称已存在") from exc
        finally:
            conn.close()

    def delete_agent_subscription_instance(self, instance_id: int) -> bool:
        conn = self._connect()
        try:
            now_dt = utc_now()
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT i.*,s.valid_from subscription_valid_from "
                "FROM agent_subscription_instances i "
                "JOIN agent_subscriptions s ON s.id=i.subscription_id "
                "WHERE i.id=? AND i.lifecycle_state!='deleted'", (instance_id,)
            ).fetchone()
            if row is None:
                return False
            self._delete_instance_row(conn, row, now_dt)
            conn.execute("UPDATE agent_subscriptions SET updated_at=? WHERE id=?",
                         (now, row["subscription_id"]))
            conn.commit()
            return True
        finally:
            conn.close()

    def update_agent_subscription(self, subscription_id: int, data: dict) -> bool:
        if "price_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT * FROM agent_subscriptions WHERE id=? AND lifecycle_state!='deleted'",
                (subscription_id,),
            ).fetchone()
            if current is None:
                return False
            fields, values = [], []
            if "name" in data:
                name = str(data.get("name") or "").strip()
                if not name:
                    raise ValueError("订阅名称不能为空")
                fields += ["name=?"]
                values.append(name)
            if "currency" in data:
                currency = str(data.get("currency") or "").upper()
                if currency not in {"CNY", "USD"}:
                    raise ValueError("币种必须是 CNY 或 USD")
                if currency != current["currency"]:
                    raise ValueError("订阅实例沿用父订阅币种，不能单独切换币种")
            if "valid_from" in data or "start_time" in data:
                fields.append("valid_from=?")
                values.append(_iso_start(data.get("valid_from") or data.get("start_time")))
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            if fields:
                fields.append("updated_at=?")
                values.extend([now, subscription_id])
                conn.execute("UPDATE agent_subscriptions SET " + ",".join(fields)
                             + " WHERE id=?", values)
            if "monthly_price" in data and "instances" not in data:
                default = conn.execute(
                    "SELECT id FROM agent_subscription_instances "
                    "WHERE subscription_id=? AND lifecycle_state!='deleted' "
                    "ORDER BY id LIMIT 1", (subscription_id,)).fetchone()
                if default is None:
                    raise ValueError("订阅没有可更新的实例")
                price = _number(data["monthly_price"], "月费必须是数字")
                conn.execute(
                    "INSERT INTO agent_subscription_rate_events"
                    "(instance_id,recurring_price,effective_at,effective_rule) "
                    "VALUES(?,?,?,?) ON CONFLICT(instance_id,effective_at,effective_rule) "
                    "DO UPDATE SET recurring_price=excluded.recurring_price",
                    (default["id"], price, now,
                     self._subscription_price_rule(conn, data)),
                )
            if "instances" in data:
                raw = data["instances"]
                if not isinstance(raw, list) or not raw:
                    raise ValueError("至少需要一个订阅实例")
                parsed = self._validate_instances(
                    {"instances": raw}, current["currency"],
                    data.get("valid_from") or current["valid_from"])
                existing = {int(item["id"]): item for item in conn.execute(
                    "SELECT * FROM agent_subscription_instances "
                    "WHERE subscription_id=? AND lifecycle_state!='deleted'",
                    (subscription_id,)).fetchall()}
                seen: set[int] = set()
                rule = self._subscription_price_rule(conn, data)
                for item in parsed:
                    if item["id"] is not None:
                        iid = int(item["id"])
                        row = existing.get(iid)
                        if row is None:
                            raise ValueError("实例不属于当前订阅")
                        seen.add(iid)
                        self._update_instance_row(conn, row, item, now, rule)
                    else:
                        self._insert_instances(conn, subscription_id, [item], now)
                now_dt = utc_now()
                for iid, row in existing.items():
                    if iid not in seen:
                        self._delete_instance_row(conn, row, now_dt)
                conn.execute("UPDATE agent_subscriptions SET updated_at=? WHERE id=?",
                             (now, subscription_id))
            conn.commit()
            return bool(fields or "monthly_price" in data or "instances" in data)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("订阅名称或实例名称已存在") from exc
        finally:
            conn.close()

    def delete_agent_subscription(self, subscription_id: int) -> bool:
        conn = self._connect()
        try:
            now_dt = utc_now()
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT * FROM agent_subscriptions "
                "WHERE id=? AND lifecycle_state!='deleted'", (subscription_id,)
            ).fetchone()
            if row is None:
                return False
            instances = conn.execute(
                "SELECT * FROM agent_subscription_instances "
                "WHERE subscription_id=? AND lifecycle_state!='deleted'",
                (subscription_id,)).fetchall()
            end = self._subscription_end(conn, row["valid_from"], now_dt)
            ends = [self._subscription_end(conn, item["valid_from"], now_dt)
                    for item in instances]
            if ends:
                end = max([end, *ends])
            end_text = end.strftime("%Y-%m-%dT%H:%M:%SZ")
            deferred = end > now_dt
            conn.execute(
                "UPDATE agent_subscriptions SET lifecycle_state=?,valid_until=?,"
                "updated_at=? WHERE id=?",
                ("active" if deferred else "deleted", end_text, now, subscription_id))
            for instance in instances:
                self._delete_instance_row(conn, instance, now_dt)
            conn.execute(
                "UPDATE agent_subscription_bindings SET lifecycle_state=?,"
                "valid_until=COALESCE(valid_until,?),updated_at=? "
                "WHERE subscription_id=? AND lifecycle_state='active'",
                ("active" if deferred else "deleted", end_text, now, subscription_id))
            conn.commit()
            return True
        finally:
            conn.close()

    def _replace_bindings(self, conn: sqlite3.Connection, software_id: int,
                          subscription_ids: list[object]) -> None:
        cleaned = []
        for value in subscription_ids:
            try:
                sid = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("订阅 ID 无效") from exc
            if sid not in cleaned:
                cleaned.append(sid)
        if cleaned:
            placeholders = ",".join("?" for _ in cleaned)
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_subscriptions "
                f"WHERE id IN ({placeholders}) AND lifecycle_state!='deleted'", cleaned
            ).fetchone()[0]
            if count != len(cleaned):
                raise ValueError("绑定的订阅不存在")
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        active = {row[0] for row in conn.execute(
            "SELECT subscription_id FROM agent_subscription_bindings "
            "WHERE software_id=? AND lifecycle_state='active'", (software_id,))}
        for sid in active - set(cleaned):
            conn.execute(
                "UPDATE agent_subscription_bindings SET lifecycle_state='deleted',"
                "valid_until=?,updated_at=? WHERE subscription_id=? AND software_id=?",
                (now, now, sid, software_id))
        for sid in cleaned:
            conn.execute(
                "INSERT INTO agent_subscription_bindings"
                "(subscription_id,software_id,valid_from,valid_until,lifecycle_state,updated_at) "
                "VALUES(?,?,?,NULL,'active',?) ON CONFLICT(subscription_id,software_id) "
                "DO UPDATE SET valid_until=NULL,lifecycle_state='active',"
                "updated_at=excluded.updated_at", (sid, software_id, now, now))
