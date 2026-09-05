#include "proxy_server_internal.h"

#include <array>
#include <stdexcept>

namespace {

struct ErrorEnvelopeMapping {
    ir::ApiFormat format;
    const char *root_key;
};

// The codec still owns serialization. This table freezes the three supported
// wire roots in one place so a new endpoint cannot silently use the wrong
// envelope while reusing the internal classification.
constexpr std::array<ErrorEnvelopeMapping, 3> kErrorEnvelopeMappings{{
    {ir::ApiFormat::OpenAI, "error"},
    {ir::ApiFormat::OpenAIResponses, "error"},
    {ir::ApiFormat::Anthropic, "error"},
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
    }
    if (result.code.is_null()) result.code = status;
    return result;
}

}  // namespace

int no_upstream_status(const UpstreamClient::ForwardResult &result,
                       const std::vector<Database::AttemptInfo> &attempts) {
    if (result.is_timeout) return 504;
    if (attempts.empty()) return 429;
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
    return json{{"message", fwd.error.empty() ? "upstream error" : fwd.error},
                {"type", "upstream_error"},
                {"code", default_status}};
}

TerminalError render_terminal_error(
    const FormatCodec &harness_codec, const FormatCodec *upstream_codec,
    const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts,
    const TerminalErrorOptions &opts) {
    TerminalError out;
    if (!opts.used) {
        out.status = no_upstream_status(fwd, attempts);
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
        // Every candidate was busy/cooling before any forward — a busy
        // signal, not an upstream failure.
        out.status = 429;
        normalized = from_body(out.status, json{
            {"message", opts.busy_message ? opts.busy_message
                                          : "All upstream accounts are busy, cooling down, or failed"},
            {"type", "rate_limit_error"}, {"code", 429}});
    } else if (opts.used && opts.passthrough && !fwd.body.empty()) {
        // Same-format upstream error: preserve its structured body verbatim.
        out.body = fwd.body;
        return out;
    } else {
        normalized = from_body(out.status,
                               normalize_upstream_error(upstream_codec, fwd,
                                                        out.status));
    }
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
    const json &in_stream_error, int last_status, const char *busy_message) {
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
        // No candidate was acquired (all keys are cooling or busy). The
        // transport result is still default-initialized with status 0, which
        // is not a valid HTTP status line; normalize this branch to 429.
        out = from_body(429,
                        json{{"message", busy_message},
                             {"type", "rate_limit_error"}, {"code", 429}});
    } else {
        out = from_body(out.status,
                        normalize_upstream_error(upstream_codec, fwd,
                                                 out.status));
    }
    return out;
}
