#pragma once

#include <string>
#include <vector>

/// Mirror of app/domain/account_types.py — the upstream account-type semantics
/// the C++ proxy needs at request time.  Keep the two in sync when adding a
/// type.
///
/// The authoritative spec lives in Python (routing / keys / billing / deletion
/// are decided there); this header only carries the two properties that affect
/// request-time behavior in C++:
///   * which types may serve proxied traffic (local-key routing / aggregate
///     targets) — expressed as the SQL filter applied at route resolution.
///   * the subscription account semantics used by the request-time router.
namespace account_types {

/// Account types that must never be routed to.  All other types are routable.
inline const std::vector<std::string> &non_routable_types() {
    static const std::vector<std::string> v = {"agent"};
    return v;
}

/// SQL fragment for proxy-routable account types. Agent software is not stored
/// as an upstream account anymore. Values are
/// compile-time constants (never user input), so concatenation is safe.
inline std::string routable_filter_sql(const char *col) {
    if (non_routable_types().empty()) return " ";
    std::string sql = " AND COALESCE(";
    sql += col;
    sql += ",'api') NOT IN (";
    const auto &blocked = non_routable_types();
    for (size_t i = 0; i < blocked.size(); ++i) {
        if (i) sql += ",";
        sql += "'";
        sql += blocked[i];
        sql += "'";
    }
    sql += ") ";
    return sql;
}

}  // namespace account_types
