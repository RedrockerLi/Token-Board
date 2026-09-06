"""Agent subscriptions, instances and bindings for Token-Board V2."""

from app.core.time import billing_period, parse_runtime_timestamp, utc_now
from app.db.proxy.common import (
    _billing_period_month, _next_month, _parse_iso_date, _period_start,
    _subscription_date, datetime, json, sqlite3, timedelta, uuid,
)


def _iso_start(value: object | None) -> str:
    return _subscription_date(value)


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
    @staticmethod
    def _ensure_subscription_current_charge(conn: sqlite3.Connection,
                                            subscription_id: int,
                                            moment) -> bool:
        """Freeze every current instance before changing its live graph."""
        from app.db.proxy.billing import (
            agent_billing_ready, materialize_agent_subscription_charges_conn,
        )
        materialize_agent_subscription_charges_conn(
            conn, moment, current_only=True)
        if not agent_billing_ready(conn, subscription_id, moment):
            return False
        now = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        return conn.execute(
            "SELECT 1 FROM agent_subscription_period_charges c "
            "JOIN agent_subscription_instances i ON i.id=c.instance_id "
            "WHERE i.subscription_id=? AND c.period_start=("
            "SELECT max(c2.period_start) FROM agent_subscription_period_charges c2 "
            "WHERE c2.instance_id=c.instance_id AND c2.period_start<=?) "
            "AND c.finalized_at IS NULL LIMIT 1",
            (subscription_id, now),
        ).fetchone() is None

    def _subscription_price_rule(self, conn: sqlite3.Connection, data: dict) -> str:
        del conn, data
        return "next_period"

    def _subscription_end(self, conn: sqlite3.Connection,
                          valid_from: object, now: datetime) -> datetime:
        config = self._billing_config_conn(conn)
        if config["cancellation_mode"] == "immediate":
            return now
        anchor = (_parse_iso_date(valid_from) if not isinstance(valid_from, str)
                  or "T" not in valid_from
                  else parse_runtime_timestamp(valid_from).date())
        current = _billing_period_month(now, anchor.day)
        return _period_start(_next_month(current), anchor.day) - timedelta(seconds=1)

    @staticmethod
    def _update_instance_row(conn: sqlite3.Connection, row: sqlite3.Row,
                             data: dict, now: str, effective_rule: str) -> bool:
        fields, values = [], []
        if "label" in data:
            label = str(data.get("label") or "").strip() or "实例 1"
            fields += ["label=?"]
            values.append(label)
        if "valid_from" in data or "start_time" in data:
            fields += ["valid_from=?"]
            values.append(_iso_start(data.get("valid_from") or data.get("start_time")))
        if fields:
            fields += ["updated_at=?"]
            values += [now, row["id"]]
            conn.execute("UPDATE agent_subscription_instances SET " +
                         ",".join(fields) + " WHERE id=?", values)
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
                             row: sqlite3.Row, now_dt: datetime) -> str:
        end = self._subscription_end(conn, row["valid_from"], now_dt)
        end_text = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE agent_subscription_instances SET ends_at=?,updated_at=? "
                     "WHERE id=?", (end_text, end_text, row["id"]))
        return end_text

    @staticmethod
    def _instance_payload(row: sqlite3.Row, currency: str,
                          price: float | None = None) -> dict:
        return {
            "id": row["id"], "uuid": row["uuid"], "label": row["label"],
            "valid_from": row["valid_from"], "ends_at": row["ends_at"],
            "currency": currency,
            "monthly_price": price if price is not None else 0,
            "updated_at": row["updated_at"],
        }

    def _subscription_instances(self, conn: sqlite3.Connection,
                                subscription_id: int, currency: str) -> list[dict]:
        rows = conn.execute(
            "SELECT i.*,COALESCE((SELECT r.recurring_price "
            "FROM agent_subscription_rate_events r WHERE r.instance_id=i.id "
            "ORDER BY r.effective_at DESC,r.id DESC LIMIT 1),0) monthly_price "
            "FROM agent_subscription_instances i WHERE i.subscription_id=? "
            "AND (i.ends_at IS NULL OR i.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            "ORDER BY i.id", (subscription_id,),
        ).fetchall()
        return [self._instance_payload(row, currency, row["monthly_price"])
                for row in rows]

    def get_agent_subscriptions(self) -> list[dict]:
        conn = self._connect()
        try:
            result = []
            rows = conn.execute(
                "SELECT s.id,s.uuid,s.name,s.currency,s.valid_from,s.ends_at,"
                "s.created_at,s.updated_at FROM agent_subscriptions s "
                "WHERE s.ends_at IS NULL OR s.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "ORDER BY s.name COLLATE NOCASE"
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["instances"] = self._subscription_instances(
                    conn, row["id"], row["currency"])
                item["software_ids"] = [r[0] for r in conn.execute(
                    "SELECT software_id FROM agent_subscription_bindings "
                    "WHERE subscription_id=? AND (ends_at IS NULL OR ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                    "ORDER BY software_id", (row["id"],)
                ).fetchall()]
                item["monthly_price"] = (item["instances"][0]["monthly_price"]
                                         if item["instances"] else 0)
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
        result = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("订阅实例格式错误")
            result.append({
                "id": item.get("id"),
                "label": str(item.get("label") or "实例 1").strip() or "实例 1",
                "valid_from": _iso_start(item.get("valid_from") or
                                           item.get("start_time") or parent_start),
                "monthly_price": _number(item.get("monthly_price", 0), "月费必须是数字"),
            })
        return result

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
            moment = utc_now()
            if _parse_iso_date(parent_start) == billing_period(
                    moment, _parse_iso_date(parent_start).day).start.date():
                from app.db.proxy.billing import materialize_agent_subscription_charges_conn
                materialize_agent_subscription_charges_conn(conn, moment, current_only=True)
            conn.commit()
            return int(sid)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("订阅或实例数据冲突") from exc
        finally:
            conn.close()

    @staticmethod
    def _insert_instances(conn: sqlite3.Connection, subscription_id: int,
                          instances: list[dict], now: str) -> list[int]:
        ids = []
        for item in instances:
            iid = conn.execute(
                "INSERT INTO agent_subscription_instances"
                "(uuid,subscription_id,label,valid_from,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), subscription_id, item["label"],
                 item["valid_from"], now, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO agent_subscription_rate_events"
                "(instance_id,recurring_price,effective_at) VALUES(?,?,?)",
                (iid, item["monthly_price"], item["valid_from"]),
            )
            ids.append(int(iid))
        return ids

    def get_agent_subscription_instances(self, subscription_id: int) -> list[dict]:
        conn = self._connect()
        try:
            parent = conn.execute(
                "SELECT currency FROM agent_subscriptions WHERE id=? "
                "AND (ends_at IS NULL OR ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (subscription_id,),
            ).fetchone()
            return self._subscription_instances(conn, subscription_id, parent["currency"]) if parent else []
        finally:
            conn.close()

    def create_agent_subscription_instance(self, subscription_id: int,
                                            data: dict) -> int:
        conn = self._connect()
        try:
            parent = conn.execute(
                "SELECT currency,valid_from,ends_at FROM agent_subscriptions WHERE id=?",
                (subscription_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("订阅不存在")
            if parent["ends_at"] is not None:
                raise ValueError("订阅已进入结束流程，不能新增实例")
            parsed = self._validate_instances(
                {"instances": [data]}, parent["currency"], parent["valid_from"])[0]
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            iid = self._insert_instances(conn, subscription_id, [parsed], now)[0]
            conn.execute("UPDATE agent_subscriptions SET updated_at=? WHERE id=?",
                         (now, subscription_id))
            conn.commit()
            return iid
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("实例数据冲突") from exc
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
                "WHERE i.id=? AND (i.ends_at IS NULL OR i.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (instance_id,),
            ).fetchone()
            if row is None:
                return False
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            changed = self._update_instance_row(
                conn, row, data, now, self._subscription_price_rule(conn, data))
            conn.commit()
            return changed
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("实例数据冲突") from exc
        finally:
            conn.close()

    def delete_agent_subscription_instance(self, instance_id: int) -> dict | bool:
        conn = self._connect()
        try:
            now_dt = utc_now()
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT i.*,s.currency FROM agent_subscription_instances i "
                "JOIN agent_subscriptions s ON s.id=i.subscription_id "
                "WHERE i.id=? AND (i.ends_at IS NULL OR i.ends_at>?)",
                (instance_id, now),
            ).fetchone()
            if row is None:
                return False
            if not self._ensure_subscription_current_charge(
                    conn, int(row["subscription_id"]), now_dt):
                conn.rollback()
                return {"ok": False, "status": 409,
                        "error": "当前计费周期汇率未能固化，删除已取消"}
            if self._billing_config_conn(conn)["cancellation_mode"] == "immediate":
                from app.db.proxy.deletion import purge_agent_subscription_instance
                purge_agent_subscription_instance(conn, instance_id)
                conn.commit()
                return {"ok": True, "deferred": False, "effective_ends_at": None}
            end = self._delete_instance_row(conn, row, now_dt)
            conn.commit()
            return {"ok": True, "deferred": True, "effective_ends_at": end}
        finally:
            conn.close()

    def update_agent_subscription(self, subscription_id: int, data: dict) -> bool:
        if "price_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT * FROM agent_subscriptions WHERE id=? "
                "AND (ends_at IS NULL OR ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (subscription_id,),
            ).fetchone()
            if current is None:
                return False
            fields, values = [], []
            if "name" in data:
                name = str(data.get("name") or "").strip()
                if not name:
                    raise ValueError("订阅名称不能为空")
                fields.append("name=?")
                values.append(name)
            if "valid_from" in data or "start_time" in data:
                fields.append("valid_from=?")
                values.append(_iso_start(data.get("valid_from") or data.get("start_time")))
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            if fields:
                fields.append("updated_at=?")
                values += [now, subscription_id]
                conn.execute("UPDATE agent_subscriptions SET " + ",".join(fields) +
                             " WHERE id=?", values)
            if "monthly_price" in data and "instances" not in data:
                iid = conn.execute(
                    "SELECT id FROM agent_subscription_instances "
                    "WHERE subscription_id=? AND ends_at IS NULL ORDER BY id LIMIT 1",
                    (subscription_id,),
                ).fetchone()
                if iid is None:
                    raise ValueError("订阅没有可更新的实例")
                price = _number(data["monthly_price"], "月费必须是数字")
                conn.execute(
                    "INSERT INTO agent_subscription_rate_events"
                    "(instance_id,recurring_price,effective_at,effective_rule) "
                    "VALUES(?,?,?,?) ON CONFLICT(instance_id,effective_at,effective_rule) "
                    "DO UPDATE SET recurring_price=excluded.recurring_price",
                    (iid["id"], price, now, self._subscription_price_rule(conn, data)),
                )
            if "instances" in data:
                parsed = self._validate_instances(
                    {"instances": data["instances"]}, current["currency"],
                    data.get("valid_from") or current["valid_from"])
                existing = {int(row["id"]): row for row in conn.execute(
                    "SELECT * FROM agent_subscription_instances "
                    "WHERE subscription_id=? AND ends_at IS NULL", (subscription_id,)
                ).fetchall()}
                seen = set()
                for item in parsed:
                    if item["id"] is None:
                        self._insert_instances(conn, subscription_id, [item], now)
                    else:
                        iid = int(item["id"])
                        if iid not in existing:
                            raise ValueError("实例不属于当前订阅")
                        seen.add(iid)
                        self._update_instance_row(
                            conn, existing[iid], item, now,
                            self._subscription_price_rule(conn, data))
                for iid, row in existing.items():
                    if iid not in seen:
                        self._delete_instance_row(conn, row, utc_now())
            conn.commit()
            return bool(fields or "monthly_price" in data or "instances" in data)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("订阅或实例数据冲突") from exc
        finally:
            conn.close()

    def delete_agent_subscription(self, subscription_id: int) -> dict | bool:
        conn = self._connect()
        try:
            now_dt = utc_now()
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = conn.execute(
                "SELECT * FROM agent_subscriptions WHERE id=? "
                "AND (ends_at IS NULL OR ends_at>?)", (subscription_id, now),
            ).fetchone()
            if row is None:
                return False
            if not self._ensure_subscription_current_charge(
                    conn, subscription_id, now_dt):
                conn.rollback()
                return {"ok": False, "status": 409,
                        "error": "当前计费周期汇率未能固化，删除已取消"}
            if self._billing_config_conn(conn)["cancellation_mode"] == "immediate":
                from app.db.proxy.deletion import purge_agent_subscription
                purge_agent_subscription(conn, subscription_id)
                conn.commit()
                return {"ok": True, "deferred": False, "effective_ends_at": None}
            instances = conn.execute(
                "SELECT * FROM agent_subscription_instances "
                "WHERE subscription_id=? AND (ends_at IS NULL OR ends_at>?)",
                (subscription_id, now),
            ).fetchall()
            ends = [self._delete_instance_row(conn, item, now_dt) for item in instances]
            end = max(ends, default=self._subscription_end(conn, row["valid_from"], now_dt).strftime(
                "%Y-%m-%dT%H:%M:%SZ"))
            conn.execute("UPDATE agent_subscriptions SET ends_at=?,updated_at=? WHERE id=?",
                         (end, now, subscription_id))
            conn.execute("UPDATE agent_subscription_bindings SET ends_at=?,updated_at=? "
                         "WHERE subscription_id=? AND (ends_at IS NULL OR ends_at>?)",
                         (end, now, subscription_id, now))
            conn.commit()
            return {"ok": True, "deferred": True, "effective_ends_at": end}
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
            rows = conn.execute(
                f"SELECT id FROM agent_subscriptions WHERE id IN ({placeholders}) "
                "AND (ends_at IS NULL OR ends_at>?)",
                (*cleaned, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).fetchall()
            if len(rows) != len(cleaned):
                raise ValueError("绑定的订阅不存在或已进入结束流程")
        active = {row[0] for row in conn.execute(
            "SELECT subscription_id FROM agent_subscription_bindings "
            "WHERE software_id=? AND (ends_at IS NULL OR ends_at>?)",
            (software_id, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
        )}
        for sid in active - set(cleaned):
            conn.execute("DELETE FROM agent_subscription_bindings "
                         "WHERE subscription_id=? AND software_id=?", (sid, software_id))
        now = utc_now()
        for sid in cleaned:
            conn.execute(
                "INSERT INTO agent_subscription_bindings"
                "(subscription_id,software_id,valid_from,ends_at,updated_at) "
                "VALUES(?,?,?,NULL,?) ON CONFLICT(subscription_id,software_id) "
                "DO UPDATE SET ends_at=NULL,updated_at=excluded.updated_at",
                (sid, software_id, now.date().isoformat(),
                 now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
