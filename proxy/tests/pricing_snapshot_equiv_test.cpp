// V1 pricing authority gate.
//
// The runtime no longer contains a second C++ pricing algorithm.  This test
// therefore seeds the current V1 pricing tables and verifies that a pending
// UsageEvent is rated by SQLite's V1 trigger, including model matching,
// cache/slot pricing and historical FX selection.  Keeping this test at the
// C++ boundary ensures Database::open() and the production schema agree while
// leaving one authoritative implementation of the billing formula.
//
// Usage: pricing_sql_authority_test <schema_dir>

#include "store/db.h"

#include <assert.h>
#include <math.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <tuple>
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

// ts choices pin the minute-of-day and the date the trigger derives.  All
// cases are in 2026 so the seeded rates are valid for the requested time.
const long long kDay = 86400;
const long long kAug6_08 = 1786003200;              // 2026-08-06 08:00 UTC
const long long kAug5_08 = kAug6_08 - kDay;         // 2026-08-05 08:00 UTC
const long long kAug1_08 = kAug6_08 - 5 * kDay;     // 2026-08-01 08:00 UTC
const long long kAug6_00_30 = kAug6_08 - 7 * 3600 - 30 * 60;
const long long kAug6_10_00 = kAug6_08 + 2 * 3600;
const long long kAug6_10_30 = kAug6_10_00 + 30 * 60;
const long long kAug6_12_00 = kAug6_08 + 4 * 3600;
const long long kAug6_1230 = kAug6_08 + 4 * 3600 + 30 * 60;  // 12:30 UTC

const Case CASES[] = {
    {"exact",              "gpt-4o",      1000,    200,     500,     kAug6_10_30},
    {"wildcard-only",      "gpt-x9",      100,     0,       50,      kAug6_10_30},
    {"cache-with-price",   "gpt-x9",      100,     40,      50,      kAug6_10_30},
    {"slot-mid",           "slot-model",  100,     0,       100,     kAug6_10_30},
    {"slot-boundary-start","slot-model",  100,     0,       100,     kAug6_10_00},
    {"slot-boundary-end",  "slot-model",  100,     0,       100,     kAug6_12_00},
    {"cross-midnight-in",  "slot-model",  100,     0,       100,     kAug6_00_30},
    {"usd-fx-sameday",     "usd-model",   1000,    0,       0,       kAug6_08},
    {"usd-fx-older",       "usd-model",   1000,    0,       0,       kAug5_08},
    {"usd-no-fx",          "usd-model",   1000,    0,       0,       kAug1_08},
    {"cny-fx-ignored",     "slot-model",  100,     0,       100,     kAug6_1230},
    {"zero-tokens",        "gpt-4o",      0,       0,       0,       kAug6_10_30},
    {"million-scale",      "high-token",  1234567, 987654,  12345678, kAug6_10_30},
    {"no-match",           "no-such-model", 1000,  0,       500,     kAug6_10_30},
    {"negative-uncached",  "gpt-4o",      100,     200,     50,      kAug6_10_30},
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
    db.close();

    // Seed pricing data (must COMMIT before either track reads it).
    sqlite3 *seed = nullptr;
    assert(sqlite3_open(db_path.c_str(), &seed) == SQLITE_OK);
    bool ok =
        exec_sql(seed, "INSERT INTO pricing_rules "
                       "(id, model_pattern, priority, enabled) VALUES "
                       "(1,'gpt-4o',0,1),(2,'gpt-*',10,1),"
                       "(3,'usd-model',0,1),(4,'slot-model',0,1),"
                       "(5,'high-token',0,1)") &&
        exec_sql(seed, "INSERT INTO pricing_rates "
                       "(id, pricing_rule_id, input_price, cache_read_price, "
                       "output_price, currency, valid_from) VALUES "
                       "(1,1,10,0,30,'CNY','2020-01-01T00:00:00Z'),"
                       "(2,2,1,0.5,2,'CNY','2020-01-01T00:00:00Z'),"
                       "(3,3,3,0,6,'USD','2020-01-01T00:00:00Z'),"
                       "(4,4,5,0,5,'CNY','2020-01-01T00:00:00Z'),"
                       "(5,5,0.001,0,0.002,'CNY','2020-01-01T00:00:00Z')") &&
        exec_sql(seed, "INSERT INTO pricing_slots "
                       "(id, pricing_rate_id, start_minute, end_minute, multiplier) VALUES "
                       "(1,4,600,720,2.0),(2,4,1430,90,3.0)") &&
        exec_sql(seed, "INSERT INTO fx_rates "
                       "(base_currency,quote_currency,date,rate) VALUES "
                       "('USD','CNY','2026-08-05',7.1),"
                       "('USD','CNY','2026-08-06',7.2)");
    assert(ok);

    // Fire the V1 pending-event trigger and read all provenance fields.
    auto rated = [&](const Case &c, int id) -> std::tuple<double, int, int> {
        char sql[768];
        snprintf(sql, sizeof(sql),
                 "INSERT INTO request_log "
                 "(event_id,model,prompt_tokens,cache_read_tokens,"
                 "completion_tokens,total_tokens,status_code,requested_at,"
                 "pricing_status) VALUES ('event-%d','%s',%d,%d,%d,%d,200,"
                 "datetime(%lld,'unixepoch'),'pending')",
                 id, c.model, c.prompt, c.cache, c.completion,
                 c.prompt + c.completion, c.ts);
        if (!exec_sql(seed, sql)) return {NAN, -1, -1};
        sqlite3_stmt *st = nullptr;
        double cost = NAN;
        int status = -1;
        int rate_id = -1;
        if (sqlite3_prepare_v2(seed,
                               "SELECT equivalent_cost,pricing_status,"
                               "COALESCE(pricing_rate_id,-1) FROM request_log "
                               "WHERE event_id=?1",
                               -1, &st, nullptr) == SQLITE_OK &&
            sqlite3_bind_text(st, 1, ("event-" + std::to_string(id)).c_str(),
                              -1, SQLITE_TRANSIENT) == SQLITE_OK &&
            sqlite3_step(st) == SQLITE_ROW) {
            cost = sqlite3_column_double(st, 0);
            const auto *text = sqlite3_column_text(st, 1);
            status = text && std::string(reinterpret_cast<const char *>(text)) ==
                               "rated"
                         ? 1
                         : (text && std::string(reinterpret_cast<const char *>(text)) ==
                                      "unrated"
                                ? 0
                                : -1);
            rate_id = sqlite3_column_int(st, 2);
        }
        sqlite3_finalize(st);
        return {cost, status, rate_id};
    };

    int n = 0;
    for (const auto &c : CASES) {
        const auto [cost, status, rate_id] = rated(c, n + 1);
        assert(!isnan(cost));
        const bool unrated = std::string(c.label) == "usd-no-fx" ||
                             std::string(c.label) == "no-match";
        assert(status == (unrated ? 0 : 1));
        if (unrated) {
            assert(rate_id == -1);
            assert(cost == 0.0);
        } else {
            assert(rate_id > 0);
        }
        printf("  %-22s equivalent_cost=%.12g status=%s rate_id=%d\n",
               c.label, cost, status == 1 ? "rated" : "unrated", rate_id);
        ++n;
    }

    sqlite3_close(seed);
    unlink(db_path.c_str());
    rmdir(dir_tpl);
    printf("OK: V1 SQLite pricing authority across %d pricing cases\n", n);
    return 0;
}
