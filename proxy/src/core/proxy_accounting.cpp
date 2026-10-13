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
    int account_id, int local_key_id, const UsageAccounting &usage,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int ttft_ms, int generation_ms, double output_tps, int upstream_ttft_ms,
    int upstream_duration_ms, int attempt_count,
    const std::vector<Database::AttemptInfo> &attempts) {
    const bool accepted = recorder_.log_request(
            account_id, local_key_id, usage, is_streaming, status_code,
            duration_ms, upstream_key_id, ttft_ms, generation_ms, output_tps,
            upstream_ttft_ms, upstream_duration_ms, attempt_count, attempts,
            current_request_queue_delay_ms(), nullptr,
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
}

void ProxyServer::enqueue_zero_usage(
    int account_id, int local_key_id, const std::string &model,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int attempt_count, const std::vector<Database::AttemptInfo> &attempts,
    int upstream_duration_ms) {
    UsageAccounting zero;
    zero.model = model;
    enqueue_log(account_id, local_key_id, zero, is_streaming, status_code,
                duration_ms, upstream_key_id, -1, -1, -1.0, -1,
                upstream_duration_ms, attempt_count, attempts);
}

// ── cooldown probe ──────────────────────────────────────────────────────
// While a plan key is inside its 5h cooldown (in-memory, per key slot), the
// background probe thread watches for quota recovery and clears the cooldown
// early so the key rejoins the candidate pool before the 5h window ends.
// Two recovery channels share the thread:
//   - usage-watched sub-types (opencode-go) read the usage snapshot the
//     dashboard poller refreshes every ~5 minutes — pure local reads, no
//     HTTP at all.  Their gateway's catalog endpoint answers 200 for any
//     (or no) key, so an HTTP probe could not witness recovery; the usage
//     windows can.
//   - every other plan key gets one minimal chat completion ("你好") per
//     cooldown_probe_interval_secs_; 2xx means the key can serve again.  A
//     completed call is the ground truth; a catalog 2xx would only prove
//     the list is readable.  Accounts without a known model ("更新模型"
//     never ran) fall back to the free GET catalog probe.
// Discipline: neither channel writes request_log, acquires a gate slot
// (never counted in max_concurrency) nor touches gate failure counters — they
// only observe cooldown state and clear it.

namespace {

// Minimal chat body per upstream wire format: one "你好" user turn, one
// output token.  Plan calls are free, so the probe costs nothing.
std::string cooldown_probe_body(ir::ApiFormat format,
                                const std::string &model) {
    using nlohmann::json;
    if (format == ir::ApiFormat::OpenAIResponses) {
        return json{{"model", model}, {"input", "你好"},
                    {"max_output_tokens", 1}}.dump();
    }
    return json{{"model", model}, {"max_tokens", 1},
                {"messages", json::array({{{"role", "user"},
                                           {"content", "你好"}}})}}.dump();
}

}  // namespace

void ProxyServer::start_cooldown_probe() {
    std::call_once(cooldown_probe_once_, [this] {
        cooldown_probe_thread_ =
            std::thread(&ProxyServer::cooldown_probe_loop, this);
    });
}

void ProxyServer::cooldown_probe_loop() {
    // Sleep in 200ms slices so shutdown is prompt; three independent cadences
    // share the thread — the frequent ones are pure local reads.
    auto next_usage_watch = std::chrono::steady_clock::now();
    auto next_probe = next_usage_watch;
    auto next_ledger = next_usage_watch;
    while (!cooldown_probe_stop_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        const auto now = std::chrono::steady_clock::now();
        if (now >= next_usage_watch) {
            run_usage_watch_cycle();
            next_usage_watch = now +
                std::chrono::seconds(usage_watch_interval_secs_);
        }
        if (now >= next_probe) {
            run_cooldown_probe_cycle();
            next_probe = now +
                std::chrono::seconds(cooldown_probe_interval_secs_);
        }
        if (now >= next_ledger) {
            run_ledger_refresh_cycle();
            next_ledger = now +
                std::chrono::seconds(ledger_refresh_interval_secs_);
        }
    }
}

void ProxyServer::run_usage_watch_cycle() {
    const auto now = std::chrono::steady_clock::now();
    const auto cooling = gate_.cooling_keys(now);
    if (cooling.empty()) return;
    const std::int64_t wall_now = wall_now_epoch();
    for (const int key_slot_id : cooling) {
        const auto target = db_.lookup_probe_target(key_slot_id);
        if (!target) continue;  // key/account deleted meanwhile — cooldown is
                                // in-memory and will expire on its own.
        if (!target->usage_watch) continue;  // HTTP probe cycle handles it
        AccountGate::CooldownEvidence evidence;
        if (!gate_.cooldown_evidence(key_slot_id, &evidence)) continue;
        const auto usage =
            parse_credential_usage(key_slot_id, target->usage_json);
        // Recovery evidence must be fresh: a stale snapshot (or none) proves
        // nothing and keeps the key cooling until its deadline.
        if (!usage_snapshot_fresh(usage.get(), wall_now)) continue;
        // A reported-spent window whose trusted reset is still ahead keeps
        // the veto in force — nothing may lift a confirmed exhaustion while
        // the window it named has not reset.
        bool exhausted = false;
        for (const auto &window : usage->windows) {
            if (window.percent >= kExhaustedPercent &&
                window.resets_at > wall_now) {
                exhausted = true;
                break;
            }
        }
        if (exhausted) continue;
        bool clear = false;
        if (!evidence.window.empty() && evidence.resets_at > 0) {
            // Clear only on reset evidence: the evidence window must have
            // begun a new cycle (its reset stamp moved).  A percentage that
            // merely reads below 100 again — integer rounding — is not a
            // reset, and clearing on it would only produce a 429 loop.
            const auto *window =
                find_usage_window(usage.get(), evidence.window.c_str());
            clear = window != nullptr &&
                    window->resets_at != evidence.resets_at;
        } else if (usage->observed_at > evidence.marked_wall) {
            // No window evidence was recorded at marking time; an observation
            // completed after the mark that shows no exhausted window is the
            // best available evidence.
            clear = true;
        }
        TB_LOG_DEBUG(
            "[Proxy] cooldown watch: key %d evidence=(%s,%lld) observed=%lld "
            "clear=%d\n",
            key_slot_id, evidence.window.empty() ? "-" : evidence.window.c_str(),
            static_cast<long long>(evidence.resets_at),
            static_cast<long long>(usage->observed_at), clear ? 1 : 0);
        if (clear) {
            gate_.clear_cooldown(key_slot_id);
            TB_LOG_DEBUG(
                    "[Proxy] cooldown: key %d reset observed in the usage "
                    "snapshot, cooldown cleared\n",
                    key_slot_id);
        }
    }
}

void ProxyServer::run_ledger_refresh_cycle() {
    std::vector<Database::KeyCostTotals> totals;
    if (!db_.load_key_cost_totals(totals)) return;
    std::vector<KeyCostLedger::SlotTotals> slots;
    slots.reserve(totals.size());
    for (const auto &row : totals) {
        slots.push_back(KeyCostLedger::SlotTotals{
            row.key_slot_id, row.priced_cost, row.priced_tokens,
            row.unpriced_tokens, row.requests});
    }
    cost_ledger_.refresh(slots);
}

void ProxyServer::run_cooldown_probe_cycle() {
    const auto now = std::chrono::steady_clock::now();
    const auto cooling = gate_.cooling_keys(now);
    if (cooling.empty()) return;
    for (const int key_slot_id : cooling) {
        const auto target = db_.lookup_probe_target(key_slot_id);
        if (!target) continue;  // key/account deleted meanwhile — cooldown is
                                // in-memory and will expire on its own.
        if (target->usage_watch) continue;  // usage-watch cycle handles it
        ForwardOptions opts;
        opts.non_streaming_timeout = 10;
        opts.non_streaming_total_timeout = 10;
        opts.auth_scheme = resolve_auth_scheme(target->api_format,
                                               target->auth_header);
        // An opencode-go gateway rejects a session-less probe (400), which
        // would read as "still cooling" forever.  The probe carries no
        // conversation, so it identifies itself with a one-off session (the
        // handler-path helper takes a candidate, which a probe has none of).
        if (target->opencode_session) {
            opts.opencode_upstream = true;
            opts.opencode_session = opencode_random_session_value();
        }
        std::string method = "GET";
        std::string probe_path = "/models";
        std::string probe_body;
        if (!target->probe_model.empty()) {
            // Minimal chat probe on the account's own wire format, resolved
            // through the same endpoint policy as real forwarding.
            const auto format = ir::parse_api_format(target->api_format);
            const auto &policy = chat_endpoint_policy(format);
            bool path_is_full = false;
            resolve_upstream_path(policy, target->api_format,
                                  target->base_url, "", probe_path,
                                  path_is_full);
            probe_body = cooldown_probe_body(format, target->probe_model);
            method = "POST";
        }
        UpstreamClient::ForwardResult fwd;
        try {
            fwd = upstream_.forward(method, target->base_url,
                                    target->key_value, probe_path, probe_body,
                                    "application/json", nullptr, opts);
        } catch (const std::exception &e) {
            TB_LOG_WARN("[Proxy] cooldown probe error (key=%d): %s\n",
                    key_slot_id, e.what());
            continue;  // keep cooling; retry next cycle
        }
        if (fwd.status_code >= 200 && fwd.status_code < 300) {
            // A successful probe is explicit evidence that the provider's
            // quota cooldown can be cleared.
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
