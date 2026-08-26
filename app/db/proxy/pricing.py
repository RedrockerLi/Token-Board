"""ProxyDatabase methods for ProxyPricingMixin."""

import math

from app.core.time import utc_now
from app.db.proxy.common import sqlite3


class ProxyPricingMixin:
    _TIMEOUT_GROUPS = ("anthropic", "openai_responses", "openai")
    _TIMEOUT_FIELDS = ("streaming_first_byte_timeout",
                       "streaming_idle_timeout",
                       "non_streaming_timeout")
    _LENGTH_PRICE_FIELDS = ("input_price", "cache_read_price", "output_price")

    def get_pricing(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                    "SELECT pr.id,pr.model_pattern,r.input_price,r.output_price,"
                    "r.cache_read_price,r.currency,r.id rate_id FROM pricing_rules pr "
                    "JOIN pricing_rates r ON r.pricing_rule_id=pr.id "
                    "WHERE pr.enabled=1 AND r.valid_until IS NULL "
                    "ORDER BY pr.priority,pr.id"
                ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["slots"] = [dict(slot) for slot in conn.execute(
                    "SELECT id,start_minute,end_minute,multiplier FROM pricing_slots "
                    "WHERE pricing_rate_id=? ORDER BY id", (row["rate_id"],))]
                item["length_tiers"] = [dict(tier) for tier in conn.execute(
                    "SELECT threshold_tokens,input_price,cache_read_price,output_price "
                    "FROM pricing_length_tiers WHERE pricing_rate_id=? "
                    "ORDER BY threshold_tokens", (row["rate_id"],))]
                item.pop("rate_id", None)
                result.append(item)
            return result
        finally:
            conn.close()

    @classmethod
    def _validate_length_tiers(cls, tiers) -> list[dict]:
        """Normalize and validate input-length price overrides.

        The API stores canonical token counts.  Each nullable price is an
        explicit inheritance marker; zero is a valid override and must not be
        confused with an omitted field.
        """
        if not isinstance(tiers, list):
            raise TypeError("length_tiers 必须是数组")

        normalized = []
        seen_thresholds = set()
        for index, tier in enumerate(tiers, start=1):
            if not isinstance(tier, dict):
                raise TypeError(f"第 {index} 个条件档必须是对象")
            threshold = tier.get("threshold_tokens")
            if (isinstance(threshold, bool) or not isinstance(threshold, int)
                    or threshold <= 0):
                raise ValueError(f"第 {index} 个条件档的门槛必须是大于 0 的整数 token")
            if threshold in seen_thresholds:
                raise ValueError(f"条件档门槛不能重复: {threshold} tokens")
            seen_thresholds.add(threshold)

            values = {"threshold_tokens": threshold}
            has_override = False
            for field in cls._LENGTH_PRICE_FIELDS:
                value = tier.get(field)
                if value is None:
                    values[field] = None
                    continue
                if isinstance(value, bool):
                    raise TypeError(f"第 {index} 个条件档的 {field} 必须是数字")
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"第 {index} 个条件档的 {field} 必须是数字")
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"第 {index} 个条件档的 {field} 必须是非负有限数字")
                values[field] = number
                has_override = True
            if not has_override:
                raise ValueError(f"第 {index} 个条件档至少要覆盖一个价格字段")
            normalized.append(values)

        return sorted(normalized, key=lambda tier: tier["threshold_tokens"])

    @staticmethod
    def _insert_pricing_slots(conn, pricing_id: int, slots) -> None:
        """Insert time-slot multipliers for a pricing row.

        slots: iterable of {start_minute, end_minute, multiplier} with
        boundaries in UTC+0 minutes ([0,1439]; start>end means overnight).
        """
        if not slots:
            return
        rate_id = pricing_id
        row = conn.execute(
            "SELECT id FROM pricing_rates WHERE pricing_rule_id=? AND valid_until IS NULL "
            "ORDER BY valid_from DESC,id DESC LIMIT 1", (pricing_id,)
        ).fetchone()
        if row is None:
            raise ValueError("计价规则没有当前 rate")
        ProxyPricingMixin._insert_pricing_slots_for_rate(conn, row[0], slots)

    @staticmethod
    def _insert_pricing_slots_for_rate(conn, rate_id: int, slots) -> None:
        if not slots:
            return
        for s in slots:
            conn.execute(
                "INSERT INTO pricing_slots"
                "(pricing_rate_id,start_minute,end_minute,multiplier) VALUES(?,?,?,?)",
                (rate_id, int(s["start_minute"]), int(s["end_minute"]),
                 float(s.get("multiplier", 1.0))),
            )

    @staticmethod
    def _insert_pricing_length_tiers(conn, rate_id: int, tiers) -> None:
        if not tiers:
            return
        conn.executemany(
            "INSERT INTO pricing_length_tiers"
            "(pricing_rate_id,threshold_tokens,input_price,cache_read_price,output_price) "
            "VALUES(?,?,?,?,?)",
            [(rate_id, tier["threshold_tokens"], tier["input_price"],
              tier["cache_read_price"], tier["output_price"])
             for tier in tiers],
        )

    def create_pricing(self, data: dict) -> int:
        conn = self._connect()
        try:
            currency = data.get("currency", "CNY")
            if currency not in ("CNY", "USD"):
                raise ValueError("币种必须是 CNY / USD")
            length_tiers = self._validate_length_tiers(
                data["length_tiers"]) if "length_tiers" in data else []
            input_price = data["input_price"]
            cache_read_price = data.get("cache_read_price")
            if cache_read_price is None:
                cache_read_price = input_price
            priority = conn.execute(
                "SELECT COALESCE(max(priority),-1)+1 FROM pricing_rules"
            ).fetchone()[0]
            pid = conn.execute(
                "INSERT INTO pricing_rules(model_pattern,priority) VALUES(?,?)",
                (data["model_pattern"], priority),
            ).lastrowid
            conn.execute(
                "INSERT INTO pricing_rates"
                "(pricing_rule_id,input_price,cache_read_price,output_price,currency,valid_from) "
                "VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (pid, input_price, cache_read_price,
                 data["output_price"], currency),
            )
            rate_id = conn.execute(
                "SELECT id FROM pricing_rates WHERE pricing_rule_id=? "
                "AND valid_until IS NULL ORDER BY valid_from DESC,id DESC LIMIT 1",
                (pid,),
            ).fetchone()[0]
            self._insert_pricing_slots_for_rate(conn, rate_id, data.get("slots"))
            self._insert_pricing_length_tiers(conn, rate_id, length_tiers)
            conn.commit()
            return pid
        finally:
            conn.close()

    def update_pricing(self, pricing_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT pr.model_pattern,r.* FROM pricing_rules pr JOIN pricing_rates r "
                "ON r.pricing_rule_id=pr.id WHERE pr.id=? AND pr.enabled=1 "
                "AND r.valid_until IS NULL", (pricing_id,)
            ).fetchone()
            if current is None:
                return False
            currency = data.get("currency", current["currency"])
            if currency not in ("CNY", "USD"):
                raise ValueError("币种必须是 CNY / USD")
            length_tiers = None
            if "length_tiers" in data:
                length_tiers = self._validate_length_tiers(data["length_tiers"])
            if "model_pattern" in data:
                conn.execute("UPDATE pricing_rules SET model_pattern=? WHERE id=?",
                             (data["model_pattern"], pricing_id))
            rate_changed = any(key in data for key in
                               ("input_price", "output_price", "cache_read_price",
                                "currency", "slots", "length_tiers"))
            if rate_changed:
                now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
                old_slots = [dict(row) for row in conn.execute(
                    "SELECT start_minute,end_minute,multiplier FROM pricing_slots "
                    "WHERE pricing_rate_id=? ORDER BY id", (current["id"],))]
                old_length_tiers = [dict(row) for row in conn.execute(
                    "SELECT threshold_tokens,input_price,cache_read_price,output_price "
                    "FROM pricing_length_tiers WHERE pricing_rate_id=? "
                    "ORDER BY threshold_tokens", (current["id"],))]
                input_price = data.get("input_price", current["input_price"])
                cache_read_price = data.get("cache_read_price", current["cache_read_price"])
                if cache_read_price is None:
                    cache_read_price = input_price
                conn.execute("UPDATE pricing_rates SET valid_until=? WHERE id=?",
                             (now, current["id"]))
                new_rate_id = conn.execute(
                    "INSERT INTO pricing_rates"
                    "(pricing_rule_id,input_price,cache_read_price,output_price,currency,valid_from) "
                    "VALUES(?,?,?,?,?,?)",
                    (pricing_id, input_price, cache_read_price,
                     data.get("output_price", current["output_price"]), currency, now),
                ).lastrowid
                self._insert_pricing_slots_for_rate(
                    conn, new_rate_id, data.get("slots", old_slots))
                self._insert_pricing_length_tiers(
                    conn, new_rate_id,
                    length_tiers if length_tiers is not None else old_length_tiers)
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_pricing(self, pricing_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("UPDATE pricing_rules SET enabled=0 WHERE id=? AND enabled=1",
                         (pricing_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def reorder_pricing_order(self, pricing_ids: list[int]) -> bool:
        """Persist the complete active pricing-rule order atomically."""
        if not isinstance(pricing_ids, list):
            raise TypeError("ids 必须是数组")
        if any(isinstance(pid, bool) or not isinstance(pid, int)
               for pid in pricing_ids):
            raise TypeError("ids 必须只包含整数")
        if len(set(pricing_ids)) != len(pricing_ids):
            raise ValueError("ids 不能包含重复条目")

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,priority FROM pricing_rules WHERE enabled=1 "
                "ORDER BY priority,id"
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            if set(pricing_ids) != set(current_ids):
                raise ValueError("排序列表已过期，请刷新后重试")
            if pricing_ids == current_ids:
                return True

            # Move every rule to a collision-free temporary range first.  The
            # table only guarantees uniqueness per pattern, so a two-phase
            # update is required even when the final order is a permutation.
            all_rows = conn.execute(
                "SELECT id FROM pricing_rules ORDER BY id"
            ).fetchall()
            minimum = conn.execute(
                "SELECT COALESCE(MIN(priority),0) FROM pricing_rules"
            ).fetchone()[0]
            temporary_base = minimum - len(all_rows) - 1
            for offset, row in enumerate(all_rows):
                conn.execute(
                    "UPDATE pricing_rules SET priority=? WHERE id=?",
                    (temporary_base - offset, row["id"]),
                )

            # Active rules own the visible order.  Disabled rules are placed
            # after them so a later create remains appended to active rules.
            for priority, pricing_id in enumerate(pricing_ids):
                conn.execute(
                    "UPDATE pricing_rules SET priority=? WHERE id=?",
                    (priority, pricing_id),
                )
            disabled_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM pricing_rules WHERE enabled=0 ORDER BY id"
            ).fetchall()]
            for offset, pricing_id in enumerate(disabled_ids, len(pricing_ids)):
                conn.execute(
                    "UPDATE pricing_rules SET priority=? WHERE id=?",
                    (offset, pricing_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_timeout_config(self) -> list[dict]:
        """All proxy_timeout_config rows (one per client wire format)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT CASE endpoint_kind WHEN 'messages' THEN 'anthropic' "
                "WHEN 'responses' THEN 'openai_responses' ELSE 'openai' END app_type,"
                "streaming_first_byte_timeout,streaming_idle_timeout,non_streaming_timeout "
                "FROM proxy_timeout_config WHERE endpoint_kind IN ('messages','responses','chat') "
                "ORDER BY endpoint_kind"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_timeout_config(self, app_type: str, data: dict) -> bool:
        """Validate and upsert one timeout group (all three fields required).

        Ranges mirror cc-switch's UI: first-byte 1-120, idle 0-600 (0 = disabled),
        non-streaming 60-1200.  Raises ValueError on invalid input.
        """
        if app_type not in self._TIMEOUT_GROUPS:
            raise ValueError(f"未知的线格式分组: {app_type}")
        values = {}
        for key in self._TIMEOUT_FIELDS:
            if key not in data:
                raise ValueError(f"缺少字段: {key}")
            try:
                v = int(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数")
            if key == "streaming_first_byte_timeout":
                if not (1 <= v <= 120):
                    raise ValueError("流式首字节超时范围 1-120 秒")
            elif key == "streaming_idle_timeout":
                if not (0 <= v <= 600):
                    raise ValueError("流式静默超时范围 0-600 秒（0=禁用）")
            else:
                if not (60 <= v <= 1200):
                    raise ValueError("非流式超时范围 60-1200 秒")
            values[key] = v
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO proxy_timeout_config"
                "(endpoint_kind,streaming_first_byte_timeout,streaming_idle_timeout,non_streaming_timeout) "
                "VALUES(?,?,?,?) ON CONFLICT(endpoint_kind) DO UPDATE SET "
                "streaming_first_byte_timeout=excluded.streaming_first_byte_timeout,"
                "streaming_idle_timeout=excluded.streaming_idle_timeout,"
                "non_streaming_timeout=excluded.non_streaming_timeout",
                ({"anthropic": "messages", "openai_responses": "responses",
                  "openai": "chat"}[app_type], values["streaming_first_byte_timeout"],
                 values["streaming_idle_timeout"], values["non_streaming_timeout"]),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()
