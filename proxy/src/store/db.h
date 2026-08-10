#pragma once

#include <atomic>
#include <condition_variable>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "usage_reservation.h"

struct sqlite3;
struct sqlite3_stmt;

class Database {
public:
    Database() = default;
    ~Database();

    Database(const Database &) = delete;
    Database &operator=(const Database &) = delete;
    Database(Database &&) = delete;
    Database &operator=(Database &&) = delete;

    bool open(const std::string &path, const std::string &schema_dir);
    int schema_major() const noexcept { return schema_major_; }
    int schema_minor() const noexcept { return schema_minor_; }

    void close();

    struct KeyInfo {
        int id;
        std::string key_value;
        int account_id;
        std::string label;
    };
    std::optional<KeyInfo> lookup_local_key(const std::string &key_value);

    struct AccountInfo {
        int id;
        std::string name;
        std::string base_url;
        std::string api_format;
        std::string endpoint_path;
        std::string auth_header;
        int upstream_id = 0;
        bool is_aggregate = false;
        std::string account_type;
        bool extended_usage_limit_cooldown = false;
        int max_concurrency = 0;
        bool deleted = false;
    };
    std::optional<AccountInfo> get_account(int account_id);

    struct RouteInfo {
        KeyInfo key;
        AccountInfo account;
    };
    std::optional<RouteInfo> lookup_route(const std::string &key_value);

    struct KeySlot {
        int id;
        std::string key_value;
        int position = 0;
    };
    std::vector<KeySlot> get_upstream_keys(int account_id);

    struct ProbeTarget {
        std::string base_url;
        std::string key_value;
        std::string api_format;
        std::string auth_header;
        bool valid = false;
    };
    std::optional<ProbeTarget> lookup_probe_target(int key_slot_id);

    struct RoutingTarget {
        // Immutable snapshot owns one copy of each account, credential and
        // target model.  Route rules only keep references into that storage;
        // request candidates never copy URLs or secrets.
        std::shared_ptr<const AccountInfo> account_ref;
        std::vector<std::shared_ptr<const KeySlot>> key_refs;
        std::shared_ptr<const std::string> upstream_model_ref;
        int priority_group = 0;

        const AccountInfo &account() const { return *account_ref; }
        const std::string &upstream_model() const { return *upstream_model_ref; }
    };
    struct RoutingRule {
        int route_set_id = 0;
        std::string model_pattern;
        RoutingTarget target;
    };
    struct TimeoutConfig {
        int streaming_first_byte_timeout = 60;
        int streaming_idle_timeout = 120;
        int non_streaming_timeout = 600;
    };
    struct RoutingConfig {
        std::uint64_t generation = 0;
        std::vector<RouteInfo> routes;
        std::vector<RoutingRule> rules;
        std::unordered_map<std::string, TimeoutConfig> timeouts;
    };
    bool load_routing_config(RoutingConfig &config);
    std::uint64_t routing_config_generation();
    TimeoutConfig get_timeout_config(const std::string &app_type);
    struct AggregateEntry {
        std::string pattern;
        int upstream_account_id = 0;
        std::string upstream_model;
    };
    std::vector<AggregateEntry> resolve_aggregate(int account_id,
                                                  const std::string &model);
    std::vector<std::string> get_aggregate_model_patterns(int account_id);
    struct AttemptInfo {
        int account_id = 0;
        int upstream_id = 0;
        int upstream_key_id = 0;
        int status_code = 0;
        int duration_ms = 0;
        int dns_ms = 0;
        int connect_ms = 0;
        int tls_ms = 0;
        int lease_wait_ms = 0;
        int first_byte_ms = 0;
        bool connection_reused = false;
        int ttft_ms = -1;
        bool is_timeout = false;
        std::string error;
    };

    bool log_request(int account_id, int local_key_id, const std::string &model,
                     int prompt_tokens, int completion_tokens,
                     int cache_read_tokens, int total_tokens,
                     double cost, bool is_streaming, int status_code,
                     int duration_ms, int upstream_key_id = 0,
                     int ttft_ms = -1, int generation_ms = -1,
                     double output_tps = -1.0,
                     int upstream_ttft_ms = -1, int upstream_duration_ms = -1,
                     int attempt_count = 1,
                     const std::vector<AttemptInfo> &attempts = {},
                     int queue_ms = 0,
                     double *out_cost = nullptr,
                     UsageReservation *reservation = nullptr);
    struct PricingEntry {
        int id;
        std::string model_pattern;
        double input_price;
        double output_price;
    };

    std::vector<PricingEntry> get_all_pricing();
    std::uint64_t log_spool_bytes();
    std::size_t log_queue_depth();
    std::int64_t log_oldest_age_ms();
    std::shared_ptr<UsageReservation> reserve_usage_event();
    bool reserve_log_slot();
    void release_log_slot();
    bool log_writer_healthy();
    bool log_recovery_complete();
    using CostObserver = std::function<void(int key_slot_id, double cost)>;
    void set_cost_observer(CostObserver observer);
    std::size_t log_last_batch_size() const noexcept {
        return log_last_batch_size_.load(std::memory_order_acquire);
    }
    std::uint64_t log_persist_failures() const noexcept {
        return log_persist_failures_.load(std::memory_order_acquire);
    }
    std::uint64_t log_lost_events() const noexcept {
        return log_lost_events_.load(std::memory_order_acquire);
    }
    std::uint64_t log_last_accounting_ms() const noexcept {
        return log_last_accounting_ms_.load(std::memory_order_acquire);
    }
private:
    struct LogRecord {
        int account_id = 0;
        int local_key_id = 0;
        std::string model;
        int prompt_tokens = 0;
        int completion_tokens = 0;
        int cache_read_tokens = 0;
        int total_tokens = 0;
        double cost = 0.0;
        bool is_streaming = false;
        int status_code = 0;
        int duration_ms = 0;
        int upstream_key_id = 0;
        int ttft_ms = -1;
        int generation_ms = -1;
        double output_tps = -1.0;
        int upstream_ttft_ms = -1;
        int upstream_duration_ms = -1;
        int attempt_count = 1;
        std::vector<AttemptInfo> attempts;
        std::int64_t requested_at_unix = 0;
        std::string event_id;
        int queue_ms = 0;
        std::chrono::steady_clock::time_point enqueued_at;
    };

    struct SpoolRecord {
        LogRecord record;
        std::uint64_t end_offset = 0;
        std::size_t frame_bytes = 0;
    };

    bool run_migrations(const std::string &schema_dir);
    bool prepare_statements();
    void finalize_statements();

    bool start_log_writer();
    void stop_log_writer();
    void log_writer_loop();
    bool persist_log_records(const LogRecord *records, std::size_t count);
    bool write_log_record_in_transaction(const LogRecord &record,
                                         bool *inserted = nullptr);
    bool update_accounting_metrics(
        const std::vector<const LogRecord *> &records);
    void notify_rated_costs(
        const std::vector<const LogRecord *> &records);
    bool append_log_spool_locked(const std::string &payload);
    bool read_log_spool_batch_locked(std::vector<SpoolRecord> &batch);
    bool compact_log_spool_locked(bool force);
    static std::string serialize_log_record(const LogRecord &record);
    static bool deserialize_log_record(const std::string &payload,
                                       LogRecord &record);

    static constexpr std::size_t kLogBatchSize = 64;
    static constexpr std::size_t kLogQueueMax = 16384;
    static constexpr std::size_t kLogBatchBytes = 1024 * 1024;
    static constexpr std::size_t kLogRecordMaxBytes = 256 * 1024;
    static constexpr std::size_t kLogModelMaxBytes = 512;
    static constexpr std::size_t kLogErrorMaxBytes = 2048;
    static constexpr std::size_t kLogAttemptsMax = 64;
    static constexpr std::uint64_t kLogCompactThreshold = 8 * 1024 * 1024;
    static constexpr std::uint64_t kLogSpoolHardLimit = 256 * 1024 * 1024;
    static constexpr int kShutdownRetryLimit = 3;

    sqlite3 *write_db_ = nullptr;
    sqlite3 *read_db_ = nullptr;
    sqlite3 *pricing_db_ = nullptr;
    std::string db_path_;  // used to derive the "<db>.migrate.lock" filename
    int schema_major_ = 0;
    int schema_minor_ = 0;
    mutable std::shared_mutex lifecycle_mutex_;
    std::mutex write_mutex_;
    std::mutex read_mutex_;
    std::mutex pricing_mutex_;
    std::mutex cost_observer_mutex_;
    CostObserver cost_observer_;
    mutable std::mutex log_queue_mutex_;
    std::condition_variable log_queue_cv_;
    std::thread log_writer_thread_;
    bool log_accepting_ = false;
    bool log_recovering_ = false;
    bool log_stop_ = false;
    std::deque<LogRecord> log_memory_queue_;
    std::size_t log_reservations_ = 0;
    int log_spool_fd_ = -1;
    std::uint64_t log_spool_read_offset_ = 0;
    std::uint64_t log_spool_write_offset_ = 0;
    std::atomic<std::uint64_t> log_persist_failures_{0};
    std::atomic<std::uint64_t> log_lost_events_{0};
    std::atomic<std::size_t> log_last_batch_size_{0};
    std::atomic<std::uint64_t> log_last_accounting_ms_{0};
    sqlite3_stmt *stmt_lookup_key_ = nullptr;
    sqlite3_stmt *stmt_get_account_ = nullptr;
    sqlite3_stmt *stmt_lookup_route_ = nullptr;
    sqlite3_stmt *stmt_get_upstream_keys_ = nullptr;
    sqlite3_stmt *stmt_get_aggregate_entries_ = nullptr;
    sqlite3_stmt *stmt_get_pricing_ = nullptr;
    sqlite3_stmt *stmt_get_timeout_config_ = nullptr;
    sqlite3_stmt *stmt_lookup_probe_target_ = nullptr;
    sqlite3_stmt *stmt_insert_log_ = nullptr;
    sqlite3_stmt *stmt_find_log_event_ = nullptr;
    sqlite3_stmt *stmt_insert_attempt_ = nullptr;
    sqlite3_stmt *stmt_update_last_used_ = nullptr;
    sqlite3_stmt *stmt_update_accounting_ = nullptr;
};
