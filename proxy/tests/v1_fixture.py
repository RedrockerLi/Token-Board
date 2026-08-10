"""Small normalized V1 fixtures shared by proxy integration tests."""

from __future__ import annotations

import uuid


def _id(conn, table: str) -> int:
    return int(conn.execute(
        f"SELECT COALESCE(MAX(id),0)+1 FROM {table}"
    ).fetchone()[0])


def add_upstream(conn, name: str, base_url: str, keys: list[str], *,
                 recurring: bool = False, api_format: str = "openai",
                 auth_scheme: str = "bearer", max_concurrency: int = 64):
    account_id = _id(conn, "accounts")
    conn.execute(
        "INSERT INTO accounts(id,uuid,name,valid_from) VALUES(?,?,?,'2020-01-01')",
        (account_id, str(uuid.uuid4()), name),
    )
    conn.execute(
        "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,"
        "cooldown_policy_json,valid_from) VALUES(?,?,?,?,?,'2020-01-01T00:00:00Z')",
        (str(uuid.uuid4()), account_id,
         "recurring" if recurring else "metered",
         "credential" if recurring else "account",
         '{"kind":"subscription_5h"}' if recurring else '{"kind":"none"}'),
    )
    upstream_id = conn.execute(
        "INSERT INTO upstreams(account_id,name,base_url,api_format,auth_scheme,"
        "max_concurrency) VALUES(?,?,?,?,?,?)",
        (account_id, name, base_url, api_format, auth_scheme, max_concurrency),
    ).lastrowid
    runtime = int(conn.execute(
        "SELECT COALESCE(MAX(runtime_id),0)+1 FROM upstream_credentials"
    ).fetchone()[0])
    key_ids = []
    for position, secret in enumerate(keys):
        credential_uuid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,"
            "position,key_masked,valid_from) VALUES(?,?,?,?,?,'2020-01-01')",
            (credential_uuid, runtime, upstream_id, position,
             secret[:3] + '…' + secret[-3:]),
        )
        conn.execute(
            "INSERT INTO upstream_secrets(credential_uuid,secret_value) VALUES(?,?)",
            (credential_uuid, secret),
        )
        key_ids.append(runtime)
        runtime += 1
    return account_id, upstream_id, key_ids


def add_plain_route(conn, local_key: str, name: str, upstream_id: int,
                    account_id: int, model_pattern: str = "*") -> int:
    route_set_id = _id(conn, "route_sets")
    conn.execute(
        "INSERT INTO route_sets(id,uuid,account_id,name) VALUES(?,?,?,?)",
        (route_set_id, str(uuid.uuid4()), account_id, name),
    )
    conn.execute(
        "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id)"
        " VALUES(?,?,0,?)", (route_set_id, model_pattern, upstream_id),
    )
    conn.execute(
        "INSERT INTO client_keys(uuid,key_value,label,route_set_id) VALUES(?,?,?,?)",
        (str(uuid.uuid4()), local_key, local_key, route_set_id),
    )
    return route_set_id


def build_aggregate_scenario(conn, local_key: str, model: str,
                             recurring_keys: list[str], fallback_key: str | None,
                             base_url: str = ""):
    plan_id, plan_upstream, plan_key_ids = add_upstream(
        conn, f"{local_key}-plan", base_url, recurring_keys, recurring=True)
    targets = [(plan_upstream, 0)]
    fallback_ids = []
    if fallback_key:
        _, fallback_upstream, fallback_ids = add_upstream(
            conn, f"{local_key}-metered", base_url, [fallback_key])
        targets.append((fallback_upstream, 1))
    route_set_id = _id(conn, "route_sets")
    conn.execute(
        "INSERT INTO route_sets(id,uuid,name) VALUES(?,?,?)",
        (route_set_id, str(uuid.uuid4()), f"{local_key}-aggregate"),
    )
    for upstream_id, priority in targets:
        conn.execute(
            "INSERT INTO route_rules(route_set_id,model_pattern,priority,"
            "upstream_id,target_model) VALUES(?,?,?,?,?)",
            (route_set_id, model, priority, upstream_id, model),
        )
    conn.execute(
        "INSERT INTO client_keys(uuid,key_value,label,route_set_id) VALUES(?,?,?,?)",
        (str(uuid.uuid4()), local_key, local_key, route_set_id),
    )
    return plan_id, plan_key_ids, fallback_ids
