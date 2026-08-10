#include "proxy_server_internal.h"

thread_local std::shared_ptr<UsageReservation>
    ProxyServer::accounting_reservation_;

void ProxyServer::mark_accounting_upstream_started(
    int account_id, int local_key_id, const std::string &model, bool streaming) {
    if (!accounting_reservation_) return;
    accounting_reservation_->set_context(account_id, local_key_id, model,
                                         streaming);
    accounting_reservation_->mark_upstream_started();
}

void ProxyServer::enqueue_log(
    int account_id, int local_key_id, const UsageTracker::UsageInfo &usage,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int ttft_ms, int generation_ms, double output_tps, int upstream_ttft_ms,
    int upstream_duration_ms, int attempt_count,
    const std::vector<Database::AttemptInfo> &attempts) {
    double cost = 0.0;
    const bool accepted = tracker_.log_request(
            account_id, local_key_id, usage, is_streaming, status_code,
            duration_ms, upstream_key_id, ttft_ms, generation_ms, output_tps,
            upstream_ttft_ms, upstream_duration_ms, attempt_count, attempts,
            current_request_queue_delay_ms(), &cost,
            accounting_reservation_.get());
    if (!accepted) {
        // The token remains live when record construction or queue admission
        // fails, so resetting it releases exactly the reserved capacity.
        accounting_reservation_.reset();
        const std::uint64_t n =
            accounting_dropped_.fetch_add(1, std::memory_order_relaxed) + 1;
        if (n == 1 || n % 1000 == 0)
            TB_LOG_ERROR(
                    "[Proxy] usage event writer rejected %llu request-log "
                    "entries; health remains degraded\n",
                    static_cast<unsigned long long>(n));
            return;
    }
    // Database::log_request consumed the token when it moved the event into
    // the writer queue.  Resetting the empty shared_ptr is intentionally
    // harmless and makes the ownership transition explicit.
    accounting_reservation_.reset();
    if (upstream_key_id != 0 && cost > 0.0)
        cost_ledger_.add(upstream_key_id, cost);
}

void ProxyServer::enqueue_zero_usage(
    int account_id, int local_key_id, const std::string &model,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int attempt_count, const std::vector<Database::AttemptInfo> &attempts,
    int upstream_duration_ms) {
    UsageTracker::UsageInfo zero;
    zero.model = model;
    enqueue_log(account_id, local_key_id, zero, is_streaming, status_code,
                duration_ms, upstream_key_id, -1, -1, -1.0, -1,
                upstream_duration_ms, attempt_count, attempts);
}

// ── cooldown probe ──────────────────────────────────────────────────────
// While a plan key is inside its 5h cooldown (in-memory, per key slot), the
// background probe thread asks the upstream every `cooldown_probe_interval_secs_`
// whether quota has recovered.  A 2xx GET /models clears the cooldown early so
// the key rejoins the candidate pool before the 5h window ends.  Discipline:
// the probe writes NO request_log, does NOT acquire a gate slot (never counted
// in max_concurrency) and touches no gate failure counters — it only observes
// cooldown state and clears it.

void ProxyServer::start_cooldown_probe() {
    std::call_once(cooldown_probe_once_, [this] {
        cooldown_probe_thread_ =
            std::thread(&ProxyServer::cooldown_probe_loop, this);
    });
}

void ProxyServer::cooldown_probe_loop() {
    // Sleep in 200ms slices so shutdown is prompt; one cycle runs per interval
    // (interval * 5 slices).  No work is done while nothing is cooling.
    while (!cooldown_probe_stop_.load()) {
        for (int waited = 0; waited < cooldown_probe_interval_secs_ * 5;
             ++waited) {
            if (cooldown_probe_stop_.load()) return;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        run_cooldown_probe_cycle();
    }
}

void ProxyServer::run_cooldown_probe_cycle() {
    const auto now = std::chrono::steady_clock::now();
    const auto cooling = gate_.cooling_keys(now);
    if (cooling.empty()) return;
    for (const int key_slot_id : cooling) {
        const auto target = db_.lookup_probe_target(key_slot_id);
        if (!target) continue;  // key/account deleted meanwhile — cooldown is
                                // in-memory and will expire on its own.
        ForwardOptions opts;
        opts.non_streaming_timeout = 10;
        opts.non_streaming_total_timeout = 10;
        opts.auth_scheme = resolve_auth_scheme(target->api_format,
                                               target->auth_header);
        UpstreamClient::ForwardResult fwd;
        try {
            fwd = upstream_.forward("GET", target->base_url, target->key_value,
                                    "/models", "", "application/json", nullptr,
                                    opts);
        } catch (const std::exception &e) {
            TB_LOG_WARN("[Proxy] cooldown probe error (key=%d): %s\n",
                    key_slot_id, e.what());
            continue;  // keep cooling; retry next cycle
        }
        if (fwd.status_code >= 200 && fwd.status_code < 300) {
            // A successful health probe is a real credential success.  Clear
            // both the extended quota cooldown and any short circuit-breaker
            // state left by a racing/transient attempt; otherwise the key can
            // remain ineligible even after the probe declared it healthy.
            gate_.mark_success(key_slot_id);
            gate_.clear_cooldown(key_slot_id);
            TB_LOG_DEBUG(
                    "[Proxy] cooldown probe: key %d healthy, cooldown cleared "
                    "early (status %d)\n",
                    key_slot_id, fwd.status_code);
        } else {
            TB_LOG_DEBUG(
                    "[Proxy] cooldown probe: key %d still cooling (status %d)\n",
                    key_slot_id, fwd.status_code);
        }
    }
}
