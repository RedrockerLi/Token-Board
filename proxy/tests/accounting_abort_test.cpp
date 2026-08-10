// internal_abort UsageEvent gate (G).
//
// UsageReservation is the RAII holder for a request-log slot.  Its destructor
// used to release the slot silently on abnormal exit (exception, early return,
// a deferred streaming provider that never ran), losing the fact that an
// upstream attempt was begun.  This test locks the contract:
//
//   1. reserved + context + mark_upstream_started(), then destroyed without a
//      completed event  ->  an internal_abort (599) zero-token row appears.
//   2. reserved + context but never started, then destroyed  ->  only the slot
//      is released, no row.
//   3. reserved and consumed by a normal log_request, then destroyed  ->  the
//      normal row persists and no abort row is added.
//
// Usage: accounting_abort_test <schema_dir>

#include "store/db.h"

#include <assert.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#include "sqlite3.h"

namespace {

bool exec_sql(sqlite3 *db, const std::string &sql) {
    char *err = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
        fprintf(stderr, "sqlite error: %s\n%s\n", err ? err : "?", sql.c_str());
        if (err) sqlite3_free(err);
        return false;
    }
    return true;
}

// Row count for (model, status); status == -1 matches any status.
int count_log(const std::string &db_path, const std::string &model, int status) {
    sqlite3 *db = nullptr;
    if (sqlite3_open(db_path.c_str(), &db) != SQLITE_OK) return -1;
    int n = -1;
    const std::string sql = status == -1
        ? "SELECT COUNT(*) FROM request_log WHERE model=?1"
        : "SELECT COUNT(*) FROM request_log WHERE model=?1 AND status_code=?2";
    sqlite3_stmt *st = nullptr;
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &st, nullptr) == SQLITE_OK &&
        sqlite3_bind_text(st, 1, model.c_str(), -1, SQLITE_TRANSIENT) ==
            SQLITE_OK &&
        (status == -1 ||
         sqlite3_bind_int(st, 2, status) == SQLITE_OK) &&
        sqlite3_step(st) == SQLITE_ROW) {
        n = sqlite3_column_int(st, 0);
    }
    sqlite3_finalize(st);
    sqlite3_close(db);
    return n;
}

// Poll until a row (model, status) is persisted by the async writer.
bool wait_for_log(const std::string &db_path, const std::string &model,
                  int status, int timeout_ms = 5000) {
    const int deadline_ms = timeout_ms;
    int waited = 0;
    while (waited < deadline_ms) {
        if (count_log(db_path, model, status) > 0) return true;
        usleep(20 * 1000);
        waited += 20;
    }
    return false;
}

// Assert that no row (model, status) appears within a grace period.  Used to
// prove the slot-only release path and that a consumed event adds no abort.
bool stays_absent(const std::string &db_path, const std::string &model,
                  int status, int grace_ms = 1500) {
    usleep(grace_ms * 1000);
    return count_log(db_path, model, status) == 0;
}

}  // namespace

int main(int argc, char **argv) {
    assert(argc >= 2);
    const std::string schema_dir = argv[1];

    char dir_tpl[] = "/tmp/accounting-abort-XXXXXX";
    assert(mkdtemp(dir_tpl));
    const std::string db_path = std::string(dir_tpl) + "/abort.db";

    Database db;
    assert(db.open(db_path, schema_dir));

    // Seed the FK targets request_log needs: account, route_set, client_key.
    sqlite3 *seed = nullptr;
    assert(sqlite3_open(db_path.c_str(), &seed) == SQLITE_OK);
    const bool ok =
        exec_sql(seed, "INSERT INTO accounts(id,uuid,name,valid_from) "
                       "VALUES(1,'a','abort','2020-01-01')") &&
        exec_sql(seed, "INSERT INTO route_sets(id,uuid,account_id,name) "
                       "VALUES(1,'r',1,'abort')") &&
        exec_sql(seed, "INSERT INTO client_keys(id,uuid,key_value,label,"
                       "route_set_id) VALUES(1,'k','tb-abort','abort',1)");
    assert(ok);
    sqlite3_close(seed);  // commit the seed before any event is queued
    // open() already started the log writer (database_lifecycle); close()
    // stops and drains it.

    // 1. Started then destroyed abnormally -> internal_abort (599) row.
    {
        auto r = db.reserve_usage_event();
        assert(r);
        r->set_context(1, 1, "abort-model", false);
        r->mark_upstream_started();
        r.reset();
    }
    assert(wait_for_log(db_path, "abort-model", kInternalAbortStatus));
    printf("  1 OK: started-then-dropped -> internal_abort(%d) row\n",
           kInternalAbortStatus);

    // 2. Never started -> slot released, no row.
    {
        auto r = db.reserve_usage_event();
        assert(r);
        r->set_context(1, 1, "noabort-model", false);
        r.reset();
    }
    assert(stays_absent(db_path, "noabort-model", -1));
    printf("  2 OK: not-started drop releases the slot without a row\n");

    // 3. Normal completion consumes the reservation -> no abort row.
    {
        auto r = db.reserve_usage_event();
        assert(r);
        const bool accepted = db.log_request(
            1, 1, "consumed-model", 10, 20, 0, 30, 0.0, false, 200, 5,
            0, -1, -1, -1.0, -1, -1, 1, {}, 0, nullptr, r.get());
        assert(accepted);
        r.reset();
    }
    assert(wait_for_log(db_path, "consumed-model", 200));
    assert(stays_absent(db_path, "consumed-model", kInternalAbortStatus));
    printf("  3 OK: consumed event persists and adds no abort row\n");

    db.close();  // stops and drains the writer
    unlink(db_path.c_str());
    rmdir(dir_tpl);
    printf("OK: UsageReservation internal_abort contract\n");
    return 0;
}
