"""Flatten versioned model pricing into one current configuration."""

from __future__ import annotations

import sqlite3

from app.db.schema_upgrade.transition_api import TransitionContext


TRANSITION_ID = "v1-pricing-current-only"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _stage_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pricing_current_stage (
            rule_id INTEGER PRIMARY KEY,
            rate_id INTEGER NOT NULL,
            model_pattern TEXT NOT NULL,
            priority INTEGER NOT NULL,
            input_price REAL NOT NULL,
            cache_read_price REAL NOT NULL,
            output_price REAL NOT NULL,
            currency TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pricing_current_stage_meta (
            id INTEGER PRIMARY KEY CHECK (id=1),
            prepared_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pricing_current_slots_stage (
            slot_id INTEGER PRIMARY KEY,
            rule_id INTEGER NOT NULL,
            start_minute INTEGER NOT NULL,
            end_minute INTEGER NOT NULL,
            multiplier REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pricing_current_tiers_stage (
            rule_id INTEGER NOT NULL,
            threshold_tokens INTEGER NOT NULL,
            input_price REAL,
            cache_read_price REAL,
            output_price REAL,
            PRIMARY KEY(rule_id, threshold_tokens)
        );
        """
    )


def apply(context: TransitionContext) -> None:
    if context.scope not in {
            "local-pair", "token-board-artifact", "dashboard-artifact"}:
        raise RuntimeError(
            f"unsupported {TRANSITION_ID} transition scope: {context.scope}")

    path = context.shadow("token-board")
    conn = sqlite3.connect(path)
    try:
        if not (_table_exists(conn, "pricing_rules") and
                _table_exists(conn, "pricing_rates") and
                _table_exists(conn, "pricing_slots") and
                _table_exists(conn, "pricing_length_tiers")):
            raise RuntimeError("V1 pricing history tables are incomplete")

        _stage_schema(conn)
        # Keep this positional projection in the same order as the staging
        # table below.  The transition runs before V1.14 drops the historical
        # pricing tables, so a swapped model_pattern/rate_id here silently
        # corrupts the published current-only rule.
        current_rules = conn.execute(
            "SELECT pr.id,r.id,pr.model_pattern,pr.priority,r.input_price,"
            "r.cache_read_price,r.output_price,r.currency "
            "FROM pricing_rules pr LEFT JOIN pricing_rates r "
            "ON r.pricing_rule_id=pr.id AND r.valid_until IS NULL "
            "WHERE pr.enabled=1 ORDER BY pr.id"
        ).fetchall()
        if any(row[1] is None for row in current_rules):
            missing = [row[0] for row in current_rules if row[1] is None]
            raise RuntimeError(
                f"enabled pricing rules without a current rate: {missing}")

        conn.execute("DELETE FROM pricing_current_tiers_stage")
        conn.execute("DELETE FROM pricing_current_slots_stage")
        conn.execute("DELETE FROM pricing_current_stage")
        conn.execute("DELETE FROM pricing_current_stage_meta")
        conn.executemany(
            "INSERT INTO pricing_current_stage"
            "(rule_id,rate_id,model_pattern,priority,input_price,"
            "cache_read_price,output_price,currency) VALUES(?,?,?,?,?,?,?,?)",
            current_rules,
        )
        conn.execute(
            "INSERT INTO pricing_current_stage_meta(id,prepared_at) "
            "VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
        conn.execute(
            "INSERT INTO pricing_current_slots_stage"
            "(slot_id,rule_id,start_minute,end_minute,multiplier) "
            "SELECT ps.id,pr.id,ps.start_minute,ps.end_minute,ps.multiplier "
            "FROM pricing_slots ps JOIN pricing_rates r "
            "ON r.id=ps.pricing_rate_id JOIN pricing_rules pr "
            "ON pr.id=r.pricing_rule_id "
            "WHERE pr.enabled=1 AND r.valid_until IS NULL"
        )
        conn.execute(
            "INSERT INTO pricing_current_tiers_stage"
            "(rule_id,threshold_tokens,input_price,cache_read_price,output_price) "
            "SELECT pr.id,t.threshold_tokens,t.input_price,t.cache_read_price,"
            "t.output_price FROM pricing_length_tiers t JOIN pricing_rates r "
            "ON r.id=t.pricing_rate_id JOIN pricing_rules pr "
            "ON pr.id=r.pricing_rule_id "
            "WHERE pr.enabled=1 AND r.valid_until IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


def _usage_totals(conn: sqlite3.Connection) -> tuple[int, int, int, float, float]:
    row = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(prompt_tokens),0),"
        "COALESCE(SUM(completion_tokens),0),"
        "COALESCE(SUM(equivalent_cost),0),"
        "COALESCE(SUM(billed_usage_cost),0) FROM request_log"
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2]), float(row[3]), float(row[4])


def verify(context: TransitionContext) -> dict:
    source = sqlite3.connect(context.source("token-board"))
    shadow = sqlite3.connect(context.shadow("token-board"))
    try:
        source_rules = None
        if context.scope != "dashboard-artifact":
            source_rules = source.execute(
                "SELECT COUNT(*) FROM pricing_rules WHERE enabled=1"
            ).fetchone()[0]
        shadow_rules = shadow.execute(
            "SELECT COUNT(*) FROM pricing_rules"
        ).fetchone()[0]
        if source_rules is not None and int(source_rules) != int(shadow_rules):
            raise RuntimeError(
                f"pricing rule count changed: source={source_rules} "
                f"shadow={shadow_rules}")

        source_pricing = [tuple(row) for row in source.execute(
            "SELECT pr.id,pr.model_pattern,pr.priority,r.input_price,"
            "r.cache_read_price,r.output_price,r.currency "
            "FROM pricing_rules pr JOIN pricing_rates r "
            "ON r.pricing_rule_id=pr.id AND r.valid_until IS NULL "
            "WHERE pr.enabled=1 ORDER BY pr.id"
        )]
        shadow_pricing = [tuple(row) for row in shadow.execute(
            "SELECT id,model_pattern,priority,input_price,cache_read_price,"
            "output_price,currency FROM pricing_rules ORDER BY id"
        )]
        if source_pricing != shadow_pricing:
            raise RuntimeError(
                "pricing rule identity/order/price changed during transition")

        source_slots = [tuple(row) for row in source.execute(
            "SELECT ps.id,pr.id,ps.start_minute,ps.end_minute,ps.multiplier "
            "FROM pricing_slots ps JOIN pricing_rates r "
            "ON r.id=ps.pricing_rate_id AND r.valid_until IS NULL "
            "JOIN pricing_rules pr ON pr.id=r.pricing_rule_id "
            "WHERE pr.enabled=1 ORDER BY ps.id"
        )]
        shadow_slots = [tuple(row) for row in shadow.execute(
            "SELECT id,pricing_rule_id,start_minute,end_minute,multiplier "
            "FROM pricing_slots ORDER BY id"
        )]
        if source_slots != shadow_slots:
            raise RuntimeError("pricing slot changed during transition")

        source_tiers = [tuple(row) for row in source.execute(
            "SELECT pr.id,t.threshold_tokens,t.input_price,"
            "t.cache_read_price,t.output_price "
            "FROM pricing_length_tiers t JOIN pricing_rates r "
            "ON r.id=t.pricing_rate_id AND r.valid_until IS NULL "
            "JOIN pricing_rules pr ON pr.id=r.pricing_rule_id "
            "WHERE pr.enabled=1 ORDER BY pr.id,t.threshold_tokens"
        )]
        shadow_tiers = [tuple(row) for row in shadow.execute(
            "SELECT pricing_rule_id,threshold_tokens,input_price,"
            "cache_read_price,output_price FROM pricing_length_tiers "
            "ORDER BY pricing_rule_id,threshold_tokens"
        )]
        if source_tiers != shadow_tiers:
            raise RuntimeError("pricing length tier changed during transition")

        source_usage = _usage_totals(source) if context.scope != "dashboard-artifact" else None
        shadow_usage = _usage_totals(shadow)
        if source_usage is not None and source_usage != shadow_usage:
            raise RuntimeError(
                f"request ledger changed: source={source_usage} "
                f"shadow={shadow_usage}")

        for table in ("pricing_rates", "pricing_current_stage",
                      "pricing_current_slots_stage", "pricing_current_tiers_stage"):
            if _table_exists(shadow, table):
                raise RuntimeError(f"legacy pricing table remains: {table}")
        columns = {
            row[1] for row in shadow.execute("PRAGMA table_info(request_log)")
        }
        if "pricing_rate_id" in columns:
            raise RuntimeError("request_log.pricing_rate_id remains")
        if "enabled" in {
                row[1] for row in shadow.execute("PRAGMA table_info(pricing_rules)")
        }:
            raise RuntimeError("pricing_rules.enabled remains")
        return {
            "source_active_rules": int(source_rules) if source_rules is not None else None,
            "target_rules": int(shadow_rules),
            "request_ledger": None if source_usage is None else {
                "rows": source_usage[0],
                "prompt_tokens": source_usage[1],
                "completion_tokens": source_usage[2],
                "equivalent_cost": source_usage[3],
                "billed_usage_cost": source_usage[4],
            },
        }
    finally:
        shadow.close()
        source.close()
