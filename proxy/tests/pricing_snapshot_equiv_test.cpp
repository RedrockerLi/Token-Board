// Money-equivalence gate (2/2): C++ snapshot twin vs the v18 SQL trigger.
//
// Opens a fresh DB through Database::open() (applies schema/proxy 0001..0018,
// installing v_pricing_rate + the view-based tr_request_log_insert), seeds
// identical pricing data, then for each vector computes the C++ enqueue-time
// snapshot (cost_frozen=1 path, snapshot_request_cost → stmt_snapshot_price_
// reading v_pricing_rate) and independently lets the SQLite trigger price the
// same row (cost_frozen=0 path).  The two tracks must agree bit-for-bit.
//
// Builds on the pure-SQL gate (pricing_equivalence_test.py, v17==v18): this
// one proves the C++ twin and the trigger consume the view identically.
//
// Usage: pricing_snapshot_equiv_test <schema_dir>

#include "store/db.h"

#include <assert.h>
#include <math.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <unistd.h>
#include <vector>

#include "sqlite3.h"

namespace {

struct Case {
    const char *label;
    const char *model;
    int prompt;
    int cache;
    int completion;
    long long ts;  // unix seconds (UTC)
};

// ts choices pin the minute-of-day and the date the trigger derives:
//   epoch anchors 37800=10:30 / 36000=10:00 / 43200=12:00 / 1800=00:30
//   (minute 630/600/720/30) for slot tests; 2026-08-06/05/01 anchors for fx.
const long long kDay = 86400;
const long long kAug6_08 = 1786003200;              // 2026-08-06 08:00 UTC
const long long kAug5_08 = kAug6_08 - kDay;         // 2026-08-05 08:00 UTC
const long long kAug1_08 = kAug6_08 - 5 * kDay;     // 2026-08-01 08:00 UTC
const long long kAug6_1230 = kAug6_08 + 4 * 3600 + 30 * 60;  // 12:30 UTC

const Case CASES[] = {
    {"exact",              "gpt-4o",      1000,    200,     500,     37800},
    {"wildcard-only",      "gpt-x9",      100,     0,       50,      37800},
    {"cache-with-price",   "gpt-x9",      100,     40,      50,      37800},
    {"slot-mid",           "slot-model",  100,     0,       100,     37800},
    {"slot-boundary-start","slot-model",  100,     0,       100,     36000},
    {"slot-boundary-end",  "slot-model",  100,     0,       100,     43200},
    {"cross-midnight-in",  "slot-model",  100,     0,       100,     1800},
    {"usd-fx-sameday",     "usd-model",   1000,    0,       0,       kAug6_08},
    {"usd-fx-older",       "usd-model",   1000,    0,       0,       kAug5_08},
    {"usd-no-fx",          "usd-model",   1000,    0,       0,       kAug1_08},
    {"cny-fx-ignored",     "slot-model",  100,     0,       100,     kAug6_1230},
    {"zero-tokens",        "gpt-4o",      0,       0,       0,       37800},
    {"million-scale",      "high-token",  1234567, 987654,  12345678, 37800},
    {"no-match",           "no-such-model", 1000,  0,       500,     37800},
    {"negative-uncached",  "gpt-4o",      100,     200,     50,      37800},
};

bool exec_sql(sqlite3 *db, const std::string &sql) {
    char *err = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
        fprintf(stderr, "sqlite error: %s\n%s\n", err ? err : "?", sql.c_str());
        if (err) sqlite3_free(err);
        return false;
    }
    return true;
}

}  // namespace

int main(int argc, char **argv) {
    assert(argc >= 2);
    const std::string schema_dir = argv[1];

    char dir_tpl[] = "/tmp/pricing-snapshot-XXXXXX";
    assert(mkdtemp(dir_tpl));
    const std::string db_path = std::string(dir_tpl) + "/pricing.db";

    Database db;
    assert(db.open(db_path, schema_dir));

    // Seed pricing data (must COMMIT before either track reads it).
    sqlite3 *seed = nullptr;
    assert(sqlite3_open(db_path.c_str(), &seed) == SQLITE_OK);
    bool ok =
        exec_sql(seed, "INSERT INTO model_pricing "
                       "(id, model_pattern, input_price, output_price, "
                       " cache_read_price, currency) VALUES "
                       "(1,'gpt-4o',10,30,NULL,'CNY'),"
                       "(2,'gpt-*',1,2,0.5,'CNY'),"
                       "(3,'usd-model',3,6,NULL,'USD'),"
                       "(4,'slot-model',5,5,NULL,'CNY'),"
                       "(5,'high-token',0.001,0.002,NULL,'CNY')") &&
        exec_sql(seed, "INSERT INTO pricing_slots "
                       "(id, pricing_id, start_minute, end_minute, multiplier) VALUES "
                       "(1,4,600,720,2.0),(2,4,1430,90,3.0)") &&
        exec_sql(seed, "INSERT INTO fx_rate (base,quote,date,rate) VALUES "
                       "('USD','CNY','2026-08-05',7.1),"
                       "('USD','CNY','2026-08-06',7.2)");
    assert(ok);

    // Helper: fire the trigger for one row (cost_frozen=0) and read api_cost.
    auto trigger_cost = [&](const Case &c) -> double {
        char sql[512];
        snprintf(sql, sizeof(sql),
                 "INSERT INTO request_log "
                 "(model, prompt_tokens, cache_read_tokens, completion_tokens, "
                 " total_tokens, status_code, requested_at, cost_frozen) "
                 "VALUES ('%s',%d,%d,%d,%d,200,"
                 "datetime(%lld,'unixepoch'),0)",
                 c.model, c.prompt, c.cache, c.completion,
                 c.prompt + c.completion, c.ts);
        if (!exec_sql(seed, sql)) return NAN;
        sqlite3_stmt *st = nullptr;
        double cost = NAN;
        if (sqlite3_prepare_v2(seed, "SELECT api_cost FROM request_log "
                                     "ORDER BY id DESC LIMIT 1",
                               -1, &st, nullptr) == SQLITE_OK &&
            sqlite3_step(st) == SQLITE_ROW) {
            cost = sqlite3_column_double(st, 0);
        }
        sqlite3_finalize(st);
        return cost;
    };

    int n = 0;
    for (const auto &c : CASES) {
        double frozen = -1.0;
        bool snap_ok = db.snapshot_request_cost(
            c.model, c.prompt, c.completion, c.cache, c.ts, frozen);
        double trig = trigger_cost(c);
        assert(snap_ok);
        assert(!isnan(trig));
        assert(fabs(frozen - trig) < 1e-9);
        printf("  %-22s snapshot=%.12g trigger=%.12g\n",
               c.label, frozen, trig);
        ++n;
    }

    sqlite3_close(seed);
    db.close();
    unlink(db_path.c_str());
    rmdir(dir_tpl);
    printf("OK: C++ snapshot == v18 trigger across %d pricing cases\n", n);
    return 0;
}
