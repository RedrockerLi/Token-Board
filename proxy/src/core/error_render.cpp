#include "proxy_server_internal.h"

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
    json normalized;
    if (fwd.is_timeout) {
        out.close_connection = true;
        normalized = timeout_error_body(fwd.timeout_secs);
    } else if (!opts.used && attempts.empty()) {
        // Every candidate was busy/cooling before any forward — a busy
        // signal, not an upstream failure.
        out.status = 429;
        normalized = json{
            {"message", opts.busy_message ? opts.busy_message
                                          : "All upstream accounts are busy, cooling down, or failed"},
            {"type", "rate_limit_error"}, {"code", 429}};
    } else if (opts.used && opts.passthrough && !fwd.body.empty()) {
        // Same-format upstream error: preserve its structured body verbatim.
        out.body = fwd.body;
        return out;
    } else {
        normalized = normalize_upstream_error(upstream_codec, fwd, out.status);
    }
    out.body = harness_codec.serialize_error_body(normalized).dump();
    return out;
}

NormalizedError render_stream_error(
    const FormatCodec *upstream_codec, const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts, bool last_timeout,
    const json &in_stream_error, int last_status, const char *busy_message) {
    NormalizedError out;
    out.status = last_timeout ? 504 : last_status;
    if (!in_stream_error.is_null()) {
        // Already parsed by the metrics observer; emit it verbatim.
        out.body = in_stream_error;
    } else if (last_timeout) {
        out.body = timeout_error_body(fwd.timeout_secs);
        out.close_connection = true;
    } else if (attempts.empty()) {
        out.body = json{{"message", busy_message},
                        {"type", out.status == 429 ? "rate_limit_error"
                                                   : "upstream_error"},
                        {"code", out.status}};
    } else {
        out.body = normalize_upstream_error(upstream_codec, fwd, out.status);
    }
    return out;
}
