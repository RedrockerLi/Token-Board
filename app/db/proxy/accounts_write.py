"""ProxyDatabase methods for ProxyAccountWriteMixin."""

import logging

from app.core.time import utc_now
from app.db.proxy.common import (
    ACCOUNT_TYPES, ConflictError, _parse_iso_date, _subscription_date,
    is_subscription, json, spec, sqlite3, uuid,
)


log = logging.getLogger(__name__)


def _validated_subtype(value: object, type_spec) -> str | None:
    """Validate a requested subtype against the type's declarative spec.

    Empty/absent input clears the subtype.  A non-empty value must be listed
    by the account type's ``subtypes`` table — the check asks the spec, never
    the raw string, so a type without subtypes only accepts empty/null.
    """
    if value in (None, ""):
        return None
    subtype = str(value).strip()
    if not subtype:
        return None
    if subtype not in type_spec.subtypes:
        if not type_spec.subtypes:
            raise ValueError("该账户类型不支持子类型")
        raise ValueError("账户子类型必须是 " + " / ".join(type_spec.subtypes))
    return subtype


def _carried_subtype(stored: object, type_spec) -> str | None:
    """Keep a stored subtype only while the type's spec still declares it."""
    if stored in (None, ""):
        return None
    subtype = str(stored)
    return subtype if subtype in type_spec.subtypes else None


# Upstream wire fields the proxy understands.  Both the C++ runtime and the
# dashboard probe helpers degrade an unknown value to a default
# (`parse_api_format` → openai, `resolve_auth_scheme` → bearer), which made a
# typo look like a working configuration.  The write path is therefore strict:
# only these spellings are stored, normalized to their canonical form.
API_FORMATS = ("openai", "openai_responses", "anthropic", "google")
API_FORMAT_ALIASES = {"gemini": "google"}
AUTH_SCHEMES = ("auto", "bearer", "x-api-key", "x-goog-api-key")


def _validated_api_format(value: object, default: str | None) -> str | None:
    """Normalize a requested wire format; empty keeps *default*.

    ``gemini`` is the provider name for the same wire format the codec calls
    ``google``, so it is stored canonically rather than rejected.
    """
    if value in (None, ""):
        return default
    fmt = str(value).strip().lower()
    if not fmt:
        return default
    fmt = API_FORMAT_ALIASES.get(fmt, fmt)
    if fmt not in API_FORMATS:
        raise ValueError("API 格式必须是 " + " / ".join(API_FORMATS))
    return fmt


def _validated_auth_scheme(value: object, default: str | None) -> str | None:
    """Normalize a requested auth scheme; empty keeps *default*."""
    if value in (None, ""):
        return default
    scheme = str(value).strip().lower()
    if not scheme:
        return default
    if scheme not in AUTH_SCHEMES:
        raise ValueError("认证方式必须是 " + " / ".join(AUTH_SCHEMES))
    return scheme


def _validated_max_concurrency(value: object, default: int | None) -> int | None:
    """Normalize a requested concurrency limit; empty keeps *default*.

    The column carries a ``>= 0`` CHECK, so a negative or non-numeric value
    used to surface as an IntegrityError (500) or as text stored in an INTEGER
    column that the proxy then read as 0.  Both are refused here instead.
    """
    if value in (None, ""):
        return default
    if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()):
        raise ValueError("并发限额必须是非负整数")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ValueError("并发限额必须是非负整数") from None
    if limit < 0:
        raise ValueError("并发限额必须是非负整数")
    return limit


class ProxyAccountWriteMixin:
    def create_account(self, data: dict) -> int:
        account_type = data.get("account_type", "api")
        if account_type not in ACCOUNT_TYPES:
            raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
        currency = data.get("currency", "CNY")
        if currency not in ("CNY", "USD"):
            raise ValueError("币种必须是 CNY / USD")
        keys, new_labels = self._normalize_keys_and_labels(data)
        type_spec = spec(account_type)
        subtype = _validated_subtype(data.get("subtype"), type_spec)
        api_format = _validated_api_format(data.get("api_format"), "openai")
        auth_scheme = _validated_auth_scheme(data.get("auth_header"), "bearer")
        max_concurrency = _validated_max_concurrency(
            data.get("max_concurrency"), 0)
        raw_valid_from = data.get("valid_from")
        valid_from = (_parse_iso_date(_subscription_date(raw_valid_from))
                      if raw_valid_from not in (None, "") else None)
        conn = self._connect()
        try:
            # The shared id is intentionally allocated by the application
            # because accounts, route sets and identities share one id space.
            # Serialize the read and every dependent insert as one write
            # transaction so concurrent creators cannot observe the same max.
            conn.execute("BEGIN IMMEDIATE")
            shared_id = self._next_shared_id(conn)
            now = utc_now()
            start_date = (valid_from or now.date()).isoformat()
            if is_subscription(account_type):
                contract_start = _subscription_date(data.get("valid_from"))
            elif valid_from:
                contract_start = f"{start_date}T00:00:00Z"
            else:
                contract_start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            rate_start = contract_start
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,valid_from) VALUES(?,?,?,?)",
                (shared_id, str(uuid.uuid4()), data["name"], start_date),
            )
            conn.execute(
                "INSERT OR IGNORE INTO account_identities"
                "(id,uuid,name,account_kind,created_at,updated_at) "
                "SELECT id,uuid,name,'proxy',created_at,updated_at "
                "FROM accounts WHERE id=?", (shared_id,)
            )
            contract_id = conn.execute(
                "INSERT INTO billing_contracts"
                "(uuid,account_id,charge_type,billing_scope,currency,billing_anchor_day,"
                "cooldown_policy_json,valid_from) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), shared_id,
                 "recurring" if is_subscription(account_type) else "metered",
                 "credential" if type_spec.subscription_unit == "per_key" else "account",
                 currency, (valid_from or now.date()).day,
                 json.dumps({"kind": type_spec.cooldown or "none"}), contract_start),
            ).lastrowid
            if is_subscription(account_type):
                conn.execute(
                    "INSERT INTO billing_rate_events"
                    "(contract_id,recurring_price,effective_at,effective_rule) VALUES(?,?,?,'next_period')",
                    (contract_id, float(data.get("monthly_price", 0) or 0), rate_start),
                )
            upstream_id = conn.execute(
                "INSERT INTO upstreams"
                "(account_id,name,base_url,api_format,auth_scheme,endpoint_path,"
                "max_concurrency,subtype) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (shared_id, data["name"], data.get("base_url", ""),
                 api_format, auth_scheme,
                 data.get("endpoint_path", ""), max_concurrency,
                 subtype),
            ).lastrowid
            conn.execute(
                "INSERT INTO route_sets(id,uuid,account_id,name) VALUES(?,?,?,?)",
                (shared_id, str(uuid.uuid4()), shared_id, data["name"]),
            )
            conn.execute(
                "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
                "VALUES(?,'*',0,?)", (shared_id, upstream_id),
            )
            if keys and type_spec.holds_keys:
                self._set_upstream_keys(
                    conn, shared_id, [], keys,
                    new_valid_froms=data.get("new_valid_froms"),
                    new_labels=new_labels,
                    account_type=account_type)
            # A subscription account owns its current charge from the moment
            # it exists. The materializer resolves that period itself (a fresh
            # observed reset, else the anchor day) and a future-dated start
            # yields no period yet.
            if is_subscription(account_type):
                from app.db.proxy.billing import materialize_period_charges_conn
                materialize_period_charges_conn(conn, now, current_only=True)
            conn.commit()
            return shared_id
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _duplicate_name(conn: sqlite3.Connection, base_name: str) -> str:
        """A display name for the copy: 「原名 副本」, numbered when taken.

        Names are display attributes (no UNIQUE constraint), but a copy that
        reuses its source's name would be indistinguishable in the accounts
        table, so the copy gets a distinct default the user can rename.
        """
        names = {row[0] for row in conn.execute(
            "SELECT name FROM accounts WHERE account_kind='proxy'")}
        candidate = f"{base_name} 副本"
        index = 2
        while candidate in names:
            candidate = f"{base_name} 副本 {index}"
            index += 1
        return candidate

    def duplicate_account(self, account_id: int) -> dict:
        """Create a second account carrying a copy of this account's config.

        Copies the routing fields, the current subscription price and every
        local (plaintext) upstream key with its display label, then reuses
        :meth:`create_account` so the copy owns its own contract, route set
        and credential rows.  A duplicate is a fresh account: the source's
        billing dates, per-key start dates, usage snapshots and scheduled
        deletion are deliberately not carried over.  The cached model catalog
        is copied best-effort, so the copy answers ``/v1/models`` like its
        source before the first 更新.

        Raises ``LookupError`` for an unknown account, ``ConflictError`` when
        the source's contract has already ended (the account is being purged)
        and ``ValueError`` when it has no enabled upstream to copy.
        """
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._connect()
        try:
            account = conn.execute(
                "SELECT a.id,a.name,"
                "(SELECT bc.id FROM billing_contracts bc "
                "WHERE bc.account_id=a.id AND (bc.ends_at IS NULL OR bc.ends_at>?) "
                "ORDER BY CASE WHEN bc.ends_at IS NULL THEN 0 ELSE 1 END,"
                "bc.id DESC LIMIT 1) contract_id "
                "FROM accounts a WHERE a.id=? AND a.account_kind='proxy'",
                (now, account_id),
            ).fetchone()
            if account is None:
                raise LookupError("账户不存在")
            if account["contract_id"] is None:
                raise ConflictError("账户已到期，等待清理")
            contract = conn.execute(
                "SELECT charge_type,currency,"
                "(SELECT recurring_price FROM billing_rate_events "
                "WHERE contract_id=billing_contracts.id "
                "ORDER BY effective_at DESC,id DESC LIMIT 1) current_price "
                "FROM billing_contracts WHERE id=?",
                (account["contract_id"],),
            ).fetchone()
            if contract is None:
                # 读到合同 id 后合同被清理任务删掉：与「已到期」同一语义。
                raise ConflictError("账户已到期，等待清理")
            upstream = conn.execute(
                "SELECT id,base_url,api_format,auth_scheme,"
                "COALESCE(endpoint_path,'') endpoint_path,max_concurrency,subtype "
                "FROM upstreams WHERE account_id=? AND enabled=1 "
                "ORDER BY id LIMIT 1",
                (account_id,),
            ).fetchone()
            if upstream is None:
                raise ValueError("账户没有可复制的上游配置")
            key_rows = conn.execute(
                "SELECT s.secret_value,c.label FROM upstream_credentials c "
                "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                "WHERE c.upstream_id=? AND c.enabled=1 "
                "AND (c.ends_at IS NULL OR c.ends_at>?) "
                "ORDER BY c.position,c.runtime_id",
                (upstream["id"], now),
            ).fetchall()
            new_name = self._duplicate_name(conn, account["name"])
            source_upstream_id = int(upstream["id"])
        finally:
            conn.close()

        # 同一明文只留一行（与 _set_upstream_keys 的 new_keys 去重一致），
        # labels 与 upstream_keys 按下标对齐。
        keys: list[str] = []
        labels: list[object] = []
        for row in key_rows:
            if row["secret_value"] in keys:
                continue
            keys.append(row["secret_value"])
            labels.append(row["label"])

        new_id = self.create_account({
            "name": new_name,
            "account_type": ("plan" if contract["charge_type"] == "recurring"
                             else "api"),
            "subtype": upstream["subtype"],
            "base_url": upstream["base_url"],
            "api_format": upstream["api_format"],
            "auth_header": upstream["auth_scheme"],
            "endpoint_path": upstream["endpoint_path"],
            "max_concurrency": upstream["max_concurrency"],
            "monthly_price": float(contract["current_price"] or 0),
            "currency": contract["currency"] or "CNY",
            "upstream_keys": keys,
            "new_labels": labels,
        })

        # 已缓存的模型目录属于账户当前可用的模型集合，随配置一起复制；best-effort：
        # 账户已经建好，目录复制失败不该把成功的复制报成失败（首次「更新」可补）。
        # 但不得静默：失败留 warning（含账号与完整异常），排障与健康检查可见。
        try:
            conn = self._connect()
            try:
                target = self._v1_route_account(conn, new_id)
                if target is not None and target["upstream_id"] is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO upstream_model_catalog"
                        "(upstream_id,model_id) SELECT ?,model_id "
                        "FROM upstream_model_catalog WHERE upstream_id=?",
                        (target["upstream_id"], source_upstream_id),
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            log.warning("model catalog copy failed for duplicated account %s: %s",
                        new_id, exc, exc_info=True)

        return {"id": new_id, "name": new_name, "keys_copied": len(keys)}

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            return self._update_account_v1(conn, account_id, data)
        finally:
            conn.close()

    def _update_account_v1(self, conn: sqlite3.Connection, external_id: int,
                           data: dict) -> bool:
        if "price_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        route = self._v1_route_account(conn, external_id)
        real_id = route["account_id"] if route and route["account_id"] is not None else external_id
        original = conn.execute(
            "SELECT a.*,bc.id contract_id,bc.charge_type,bc.billing_scope,bc.currency,"
            "bc.cooldown_policy_json,bc.ends_at contract_ends_at,"
            "bc.valid_from contract_valid_from,"
            "(SELECT recurring_price FROM billing_rate_events WHERE contract_id=bc.id "
            "ORDER BY effective_at DESC,id DESC LIMIT 1) current_price FROM accounts a "
            "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
            "AND (bc.ends_at IS NULL "
            "OR bc.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            "WHERE a.id=? AND a.account_kind='proxy' "
            "ORDER BY CASE WHEN bc.ends_at IS NULL THEN 0 ELSE 1 END,bc.id DESC LIMIT 1",
            (real_id,),
        ).fetchone()
        if original is None:
            return False
        if original["contract_id"] is None:
            # The account exists but its contract is already past its end
            # boundary; the deletion finalizer is about to purge it.  Refuse
            # instead of falling through to a NULL contract_id write.
            raise ConflictError("账户已到期，等待清理")
        original_type = ("plan" if original["charge_type"] == "recurring" else "api")
        final_type = data.get("account_type", original_type)
        if final_type not in ACCOUNT_TYPES:
            raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
        if original["contract_ends_at"] is not None and final_type != original_type:
            # A pending deletion keeps its contract live only until the end
            # boundary.  Retyping mid-way would either strand the current
            # period charge (a metered contract is not a billing unit) or
            # disable the "cannot add keys" guard and leave an unpurgeable
            # account behind.  Cancel the deletion first.
            raise ConflictError("账户已进入结束流程，请先撤销结束")
        final_spec = spec(final_type)
        # Absent/empty keeps the stored wire settings: the update below feeds
        # these through COALESCE, and the insert branch (an account whose
        # upstream row is missing) falls back to the creation defaults.
        api_format = _validated_api_format(data.get("api_format"), None)
        auth_scheme = _validated_auth_scheme(data.get("auth_header"), None)
        max_concurrency = _validated_max_concurrency(
            data.get("max_concurrency"), None)
        upstream = conn.execute(
            "SELECT id,subtype FROM upstreams WHERE account_id=? ORDER BY id LIMIT 1",
            (real_id,)
        ).fetchone()
        stored_subtype = upstream["subtype"] if upstream else None
        if "subtype" in data:
            final_subtype = _validated_subtype(data.get("subtype"), final_spec)
        elif "account_type" in data:
            # Only an explicit type change can drop the stored sub-category
            # (when the new type's spec no longer declares it).  The column is
            # written explicitly, never COALESCE, so NULL persists.
            final_subtype = _carried_subtype(stored_subtype, final_spec)
        else:
            # An update that does not touch the type keeps its sub-category.
            final_subtype = stored_subtype
        currency = data.get("currency", original["currency"] or "CNY")
        if currency not in ("CNY", "USD"):
            raise ValueError("币种必须是 CNY / USD")
        name = data.get("name", original["name"])
        if "valid_from" in data:
            raw_start = data["valid_from"]
            parsed_start = (_parse_iso_date(_subscription_date(raw_start))
                            if raw_start not in (None, "") else None)
            start = parsed_start.isoformat() if parsed_start else None
        else:
            start = original["valid_from"]
        if is_subscription(final_type):
            contract_start = (_subscription_date(data.get("valid_from"))
                              if "valid_from" in data else
                              (start or original["contract_valid_from"]))
        else:
            contract_start = (f"{start}T00:00:00Z" if start
                              else original["contract_valid_from"])
        anchor_day = None
        if "valid_from" in data and parsed_start is not None:
            anchor_day = parsed_start.day
        conn.execute(
            "UPDATE accounts SET name=?,valid_from=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?", (name, start, real_id),
        )
        conn.execute(
            "UPDATE account_identities SET name=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?", (name, real_id),
        )
        charge = "recurring" if is_subscription(final_type) else "metered"
        scope = "credential" if final_spec.subscription_unit == "per_key" else "account"
        conn.execute(
            "UPDATE billing_contracts SET charge_type=?,billing_scope=?,currency=?,"
            "cooldown_policy_json=?,valid_from=?,"
            "billing_anchor_day=COALESCE(?,billing_anchor_day) WHERE id=?",
            (charge, scope, currency,
             json.dumps({"kind": final_spec.cooldown or "none"}),
             contract_start, anchor_day, original["contract_id"]),
        )
        if final_spec.routable:
            if upstream is None:
                upstream_id = conn.execute(
                    "INSERT INTO upstreams"
                    "(account_id,name,base_url,api_format,auth_scheme,endpoint_path,"
                    "max_concurrency,subtype) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (real_id, name, data.get("base_url", ""),
                     api_format or "openai", auth_scheme or "bearer",
                     data.get("endpoint_path", ""),
                     max_concurrency or 0, final_subtype),
                ).lastrowid
            else:
                upstream_id = upstream["id"]
                conn.execute(
                    "UPDATE upstreams SET name=?,base_url=COALESCE(?,base_url),"
                    "api_format=COALESCE(?,api_format),auth_scheme=COALESCE(?,auth_scheme),"
                    "endpoint_path=COALESCE(?,endpoint_path),max_concurrency=COALESCE(?,max_concurrency),"
                    "subtype=?,enabled=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (name, data.get("base_url"), api_format,
                     auth_scheme, data.get("endpoint_path"),
                     max_concurrency, final_subtype, upstream_id),
                )
            route_row = conn.execute("SELECT id FROM route_sets WHERE id=?", (external_id,)).fetchone()
            if route_row is None:
                conn.execute(
                    "INSERT INTO route_sets(id,uuid,account_id,name) VALUES(?,?,?,?)",
                    (external_id, str(uuid.uuid4()), real_id, name),
                )
                conn.execute(
                    "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
                    "VALUES(?,'*',0,?)", (external_id, upstream_id),
                )
            else:
                conn.execute("UPDATE route_sets SET name=?,enabled=1 WHERE id=?", (name, external_id))
            if data.get("keys_edited") and final_spec.holds_keys:
                keep_ids = [int(value) for value in (data.get("keep_key_ids") or [])
                            if str(value).isdigit()]
                keys, new_labels = self._normalize_keys_and_labels(data)
                self._set_upstream_keys(
                    conn, external_id, keep_ids, keys,
                    keep_valid_froms=data.get("keep_valid_froms"),
                    new_valid_froms=data.get("new_valid_froms"),
                    keep_labels=data.get("keep_labels"),
                    new_labels=new_labels, account_type=final_type)
        if is_subscription(final_type):
            price_supplied = "monthly_price" in data
            new_price = (float(data.get("monthly_price") or 0)
                         if price_supplied else None)
            current_price = original["current_price"]
            # A rename-only save must not write a billing fact: append a rate
            # event only when the submitted price really changes, or when the
            # account just became a subscription without any rate event yet.
            needs_event = (
                (price_supplied and float(current_price or 0) != new_price)
                or (original_type != final_type and current_price is None)
            )
            if needs_event:
                conn.execute(
                    "INSERT INTO billing_rate_events"
                    "(contract_id,recurring_price,effective_at,effective_rule) "
                    "VALUES(?,?,?,'next_period') "
                    "ON CONFLICT(contract_id,effective_at,effective_rule) "
                    "DO UPDATE SET recurring_price=excluded.recurring_price",
                    (original["contract_id"],
                     new_price if new_price is not None
                     else float(current_price or 0),
                     utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
        conn.commit()
        return conn.total_changes > 0
