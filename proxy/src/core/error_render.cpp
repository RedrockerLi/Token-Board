#include "proxy_server_internal.h"

#include <array>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <mutex>
#include <stdexcept>

namespace {

// Wall-clock ISO-8601 stamp (UTC, milliseconds) for a recorded observation.
std::string iso8601_utc_ms(std::int64_t wall_ms) {
    const std::time_t seconds = static_cast<std::time_t>(wall_ms / 1000);
    std::tm utc{};
    gmtime_r(&seconds, &utc);
    char stamp[40];
    std::snprintf(stamp, sizeof stamp, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                  utc.tm_year + 1900, utc.tm_mon + 1, utc.tm_mday,
                  utc.tm_hour, utc.tm_min, utc.tm_sec,
                  static_cast<int>(wall_ms % 1000));
    return stamp;
}

// What the upstream actually said: its response body when it sent one, the
// transport/classification text otherwise.
std::string upstream_payload(const UpstreamClient::ForwardResult &fwd) {
    return fwd.body.empty() ? fwd.error : fwd.body;
}

// Last observed upstream error, process-wide.  One proxy serves one database,
// so a single slot is the honest scope; only the most recent failure is kept
// (this is an explanation for the next all-candidates-skipped response, not an
// event log — the attempt journal keeps the history).
struct UpstreamErrorLog {
    std::mutex mutex;
    UpstreamErrorSnapshot last;
    bool seen = false;
};

UpstreamErrorLog &upstream_error_log() {
    static UpstreamErrorLog log;
    return log;
}

struct ErrorEnvelopeMapping {
    ir::ApiFormat format;
    const char *root_key;
};

// The codec still owns serialization. This table freezes the supported wire
// roots in one place so a new endpoint cannot silently use the wrong envelope
// while reusing the internal classification.
constexpr std::array<ErrorEnvelopeMapping, 4> kErrorEnvelopeMappings{{
    {ir::ApiFormat::OpenAI, "error"},
    {ir::ApiFormat::OpenAIResponses, "error"},
    {ir::ApiFormat::Anthropic, "error"},
    {ir::ApiFormat::Google, "error"},
}};

const ErrorEnvelopeMapping &error_envelope_mapping(ir::ApiFormat format) {
    for (const auto &mapping : kErrorEnvelopeMappings)
        if (mapping.format == format) return mapping;
    return kErrorEnvelopeMappings.front();
}

NormalizedError from_body(int status, const json &body,
                          bool close_connection = false,
                          bool passthrough = false) {
    NormalizedError result;
    result.status = status;
    result.close_connection = close_connection;
    result.passthrough = passthrough;
    if (body.is_object()) {
        if (body.contains("type") && body["type"].is_string())
            result.type = body["type"].get<std::string>();
        if (body.contains("message") && body["message"].is_string())
            result.message = body["message"].get<std::string>();
        if (body.contains("code")) result.code = body["code"];
        // A codec may surface the provider's own retry hint (Gemini's
        // google.rpc.RetryInfo, for example) alongside the normalized error.
        if (body.contains("retry_after_seconds") &&
            body["retry_after_seconds"].is_number_integer()) {
            const int seconds = body["retry_after_seconds"].get<int>();
            if (seconds > 0) result.retry_after_seconds = seconds;
        }
    }
    if (result.code.is_null()) result.code = status;
    return result;
}

// One ladder for "no candidate was attempted": the terminal and the streaming
// renderers must tell the client the same story for the same reason.
const char *no_candidate_message(NoCandidateReason reason,
                                 const char *busy_message) {
    switch (reason) {
        case NoCandidateReason::kProviderQuotaCooldown:
            return "All upstream keys are in provider quota cooldown";
        case NoCandidateReason::kKeyRateLimited:
            return "All upstream keys are rate limited";
        case NoCandidateReason::kKeyUnavailable:
            return "All upstream keys are temporarily unavailable";
        case NoCandidateReason::kSubscriptionLapsed:
            return "All upstream keys have an inactive subscription";
        case NoCandidateReason::kLocalCapacity:
        case NoCandidateReason::kNone:
            break;
    }
    return busy_message ? busy_message
                        : "All upstream key concurrency slots are occupied";
}

}  // namespace

int no_upstream_status(const UpstreamClient::ForwardResult &result,
                       const std::vector<Database::AttemptInfo> &attempts,
                       NoCandidateReason reason) {
    if (result.is_timeout) return 504;
    if (attempts.empty())
        return reason == NoCandidateReason::kProviderQuotaCooldown ||
                       reason == NoCandidateReason::kKeyRateLimited
                   ? 429
                   : 503;
    return result.status_code >= 400 ? result.status_code : 429;
}

json timeout_error_body(int timeout_secs) {
    std::string msg = timeout_secs > 0
        ? "Upstream timeout: no response within " +
              std::to_string(timeout_secs) + "s. Please retry."
        : "Upstream timeout: no response within the configured "
          "timeout. Please retry.";
    return json{{"message", msg},
                {"type", "timeout_error"},
                {"code", 504}};
}

json normalize_upstream_error(const FormatCodec *upstream_codec,
                              const UpstreamClient::ForwardResult &fwd,
                              int default_status) {
    if (upstream_codec && !fwd.body.empty()) {
        try {
            return upstream_codec->parse_error_body(json::parse(fwd.body));
        } catch (...) {}
    }
    // No codec could normalize the body (opaque text, an unknown envelope, a
    // non-JSON page): the classification text alone would hide the only
    // actionable part, so the escaped excerpt rides along (04-C-19).
    std::string message = fwd.error.empty() ? "upstream error" : fwd.error;
    if (!fwd.body.empty()) {
        const std::string excerpt = upstream_text_excerpt(fwd.body);
        if (!excerpt.empty()) message += ": " + excerpt;
    }
    return json{{"message", message},
                {"type", "upstream_error"},
                {"code", default_status}};
}

std::string upstream_text_excerpt(const std::string &raw, std::size_t limit) {
    static constexpr char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(limit == 0 ? raw.size() : limit + 24);
    std::size_t consumed = 0;
    for (const unsigned char ch : raw) {
        std::string piece;
        switch (ch) {
            case '\\': piece = "\\\\"; break;
            case '"': piece = "\\\""; break;
            case '\r': piece = "\\r"; break;
            case '\n': piece = "\\n"; break;
            case '\t': piece = "\\t"; break;
            default:
                if (ch < 0x20 || ch == 0x7f) {
                    piece = "\\x";
                    piece.push_back(hex[ch >> 4]);
                    piece.push_back(hex[ch & 0x0f]);
                } else {
                    piece.push_back(static_cast<char>(ch));
                }
        }
        if (limit > 0 && out.size() + piece.size() > limit) break;
        out += piece;
        ++consumed;
    }
    if (consumed < raw.size()) {
        out += "...(+";
        out += std::to_string(raw.size() - consumed);
        out += " bytes)";
    }
    return out;
}

void record_upstream_error(int status_code, const std::string &raw) {
    if (raw.empty()) return;
    UpstreamErrorSnapshot snapshot;
    snapshot.status_code = status_code;
    snapshot.excerpt = upstream_text_excerpt(raw);
    snapshot.wall_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::system_clock::now().time_since_epoch())
                           .count();
    auto &log = upstream_error_log();
    std::lock_guard<std::mutex> lock(log.mutex);
    log.last = std::move(snapshot);
    log.seen = true;
}

bool last_upstream_error(UpstreamErrorSnapshot *out) {
    auto &log = upstream_error_log();
    std::lock_guard<std::mutex> lock(log.mutex);
    if (!log.seen) return false;
    if (out != nullptr) *out = log.last;
    return true;
}

std::string with_upstream_context(
    const std::string &message,
    const std::vector<Database::AttemptInfo> &attempts,
    const UpstreamClient::ForwardResult &fwd) {
    if (!attempts.empty()) {
        // This request reached the upstream: quote its own last error.
        const std::string excerpt =
            upstream_text_excerpt(upstream_payload(fwd));
        if (excerpt.empty()) return message;
        return message + ". Last upstream error: " + excerpt;
    }
    // Every candidate was skipped locally: quote the last upstream answer the
    // proxy did see, with the time it arrived, so "all keys rate limited" can
    // be told apart from an exhausted quota or a disabled model.
    UpstreamErrorSnapshot last;
    if (!last_upstream_error(&last))
        return message + ". No upstream response has been recorded yet";
    return message + ". Last upstream response (" +
           std::to_string(last.status_code) + ") at " +
           iso8601_utc_ms(last.wall_ms) + ": " + last.excerpt;
}

TerminalError render_terminal_error(
    const FormatCodec &harness_codec, const FormatCodec *upstream_codec,
    const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts,
    const TerminalErrorOptions &opts) {
    TerminalError out;
    if (!opts.used) {
        out.status = no_upstream_status(fwd, attempts,
                                        opts.no_candidate_reason);
    } else {
        out.status = fwd.status_code;
        // Chat converted-failure rule: a sub-400 failure is impossible, but
        // coerce it to 502 rather than forwarding it.  Statuses >= 400 keep
        // their truthful upstream value.
        if (opts.used_failure_status > 0 && out.status < 400)
            out.status = opts.used_failure_status;
    }
    NormalizedError normalized;
    if (fwd.is_timeout) {
        normalized = from_body(out.status, timeout_error_body(fwd.timeout_secs),
                               true);
    } else if (!opts.used && attempts.empty()) {
        const bool provider_cooldown =
            opts.no_candidate_reason == NoCandidateReason::kProviderQuotaCooldown;
        const bool rate_limited =
            opts.no_candidate_reason == NoCandidateReason::kKeyRateLimited;
        const bool local_capacity =
            opts.no_candidate_reason == NoCandidateReason::kLocalCapacity;
        const bool throttled = provider_cooldown || rate_limited;
        out.status = throttled ? 429 : 503;
        // A cooling candidate recovers at its recorded avoid deadline, so the
        // client is told the real wait instead of retrying every second.
        // Local capacity has no avoid entry (a slot may free at any moment)
        // and keeps the historical one-second hint.
        out.retry_after_seconds = provider_cooldown
            ? 0
            : rate_limited ? std::max(1, opts.no_candidate_retry_after)
            : local_capacity ? 1
            : opts.no_candidate_retry_after;
        const char *message =
            no_candidate_message(opts.no_candidate_reason, opts.busy_message);
        // A skipped candidate is still an upstream fact: quote what the
        // upstream last said so the client can tell a transient rate limit
        // from an exhausted quota or a disabled model (04-C-19).  Local
        // capacity is a proxy-resource condition with no upstream answer to
        // quote, so it keeps the plain message.
        const bool upstream_conditioned =
            provider_cooldown || rate_limited ||
            opts.no_candidate_reason == NoCandidateReason::kKeyUnavailable ||
            opts.no_candidate_reason == NoCandidateReason::kSubscriptionLapsed;
        const std::string annotated = upstream_conditioned
            ? with_upstream_context(message, attempts, fwd)
            : std::string(message);
        normalized = from_body(out.status, json{
            {"message", annotated},
            {"type", throttled ? "rate_limit_error" : "service_unavailable"},
            {"code", out.status}});
    } else if (!opts.used) {
        // Attempts were made but no candidate owns this failure (the retry
        // budget ended mid-chain): keep the normalized upstream message and
        // append this request's raw upstream error.
        NormalizedError base = from_body(
            out.status,
            normalize_upstream_error(upstream_codec, fwd, out.status));
        base.message = with_upstream_context(base.message, attempts, fwd);
        normalized = std::move(base);
    } else if (opts.used && opts.passthrough && !fwd.body.empty()) {
        // Same-format upstream error: preserve its structured body verbatim.
        out.body = fwd.body;
        return out;
    } else {
        normalized = from_body(out.status,
                               normalize_upstream_error(upstream_codec, fwd,
                                                        out.status));
    }
    // A provider-supplied retry hint outranks the all-candidates default, but
    // the cooldown ladder above already reported the authoritative wait.
    if (normalized.retry_after_seconds > 0)
        out.retry_after_seconds = normalized.retry_after_seconds;
    out.close_connection = normalized.close_connection;
    out.body = serialize_normalized_error(harness_codec, normalized).dump();
    return out;
}

json normalized_error_body(const NormalizedError &error) {
    json body = {{"message", error.message}, {"type", error.type}};
    if (!error.code.is_null()) body["code"] = error.code;
    return body;
}

json serialize_normalized_error(const FormatCodec &client_codec,
                                const NormalizedError &error) {
    // The root key is intentionally data, not a format if/else scattered
    // through handlers. The codec still owns the actual wire serialization;
    // this assertion only catches a codec that violates the frozen mapping.
    const auto &mapping = error_envelope_mapping(client_codec.format());
    const auto serialized = client_codec.serialize_error_body(
        normalized_error_body(error));
    if (!serialized.is_object() || !serialized.contains(mapping.root_key)) {
        throw std::logic_error("client codec violated error envelope mapping");
    }
    return serialized;
}

NormalizedError render_stream_error(
    const FormatCodec *upstream_codec, const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts, bool last_timeout,
    const json &in_stream_error, int last_status,
    NoCandidateReason no_candidate_reason, const char *busy_message,
    int no_candidate_retry_after) {
    NormalizedError out;
    out.status = last_timeout ? 504 : last_status;
    if (!in_stream_error.is_null()) {
        // Already parsed by the metrics observer; emit it verbatim.
        out.passthrough = true;
        if (in_stream_error.is_object()) {
            if (in_stream_error.contains("type") &&
                in_stream_error["type"].is_string())
                out.type = in_stream_error["type"].get<std::string>();
            if (in_stream_error.contains("message") &&
                in_stream_error["message"].is_string())
                out.message = in_stream_error["message"].get<std::string>();
            if (in_stream_error.contains("code"))
                out.code = in_stream_error["code"];
        }
    } else if (last_timeout) {
        out = from_body(out.status, timeout_error_body(fwd.timeout_secs), true);
    } else if (attempts.empty()) {
        const bool provider_cooldown =
            no_candidate_reason == NoCandidateReason::kProviderQuotaCooldown;
        const bool rate_limited =
            no_candidate_reason == NoCandidateReason::kKeyRateLimited;
        const bool local_capacity =
            no_candidate_reason == NoCandidateReason::kLocalCapacity;
        const bool throttled = provider_cooldown || rate_limited;
        const char *message =
            no_candidate_message(no_candidate_reason, busy_message);
        // Same rule as the terminal renderer: an upstream-conditioned skip
        // quotes the last upstream answer (04-C-19), local capacity does not.
        const bool upstream_conditioned =
            provider_cooldown || rate_limited ||
            no_candidate_reason == NoCandidateReason::kKeyUnavailable ||
            no_candidate_reason == NoCandidateReason::kSubscriptionLapsed;
        const std::string annotated = upstream_conditioned
            ? with_upstream_context(message, attempts, fwd)
            : std::string(message);
        out = from_body(throttled ? 429 : 503,
                        json{{"message", annotated},
                             {"type", throttled
                                          ? "rate_limit_error"
                                          : "service_unavailable"},
                             {"code", throttled ? 429 : 503}});
        // Same rule as the terminal renderer: report the real avoid deadline
        // for auth/provider skips; local capacity keeps the one-second hint.
        out.retry_after_seconds = provider_cooldown
            ? 0
            : rate_limited ? std::max(1, no_candidate_retry_after)
            : local_capacity ? 1
            : no_candidate_retry_after;
    } else {
        out = from_body(out.status,
                        normalize_upstream_error(upstream_codec, fwd,
                                                 out.status));
    }
    return out;
}
