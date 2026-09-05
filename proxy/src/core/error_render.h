#pragma once

#include "attempt_executor.h"
#include "codec.h"
#include "db.h"
#include "upstream_client.h"

#include <string>
#include <vector>

// Terminal status when no candidate could be used by a non-streaming request:
// 504 on timeout, 429 only for explicit provider quota cooldown, 503 for
// local capacity, otherwise the last upstream status when it is >= 400.
int no_upstream_status(const UpstreamClient::ForwardResult &result,
                       const std::vector<Database::AttemptInfo> &attempts,
                       NoCandidateReason reason);

// Unified upstream-timeout error object: {message, type:"timeout_error", code:504}.
// `timeout_secs` (when > 0) names the configured timeout that fired.
json timeout_error_body(int timeout_secs = 0);

// Normalize an upstream HTTP error body to {message,type,code}.  Parses
// `fwd.body` through `upstream_codec` when both are available (every codec uses
// fmt::normalize_error_body); otherwise the body is opaque and `fwd.error`
// (or "upstream error") becomes the message with code `default_status`.
json normalize_upstream_error(const FormatCodec *upstream_codec,
                              const UpstreamClient::ForwardResult &fwd,
                              int default_status);

struct TerminalErrorOptions {
    bool used = false;             // a candidate produced this failure (vs fail-all)
    bool passthrough = false;      // same-format: keep a non-empty raw fwd.body
    int used_failure_status = 0;   // chat-converted floor: a sub-400 failure lifts to this
    const char *busy_message = nullptr;  // local-capacity message (models override)
    NoCandidateReason no_candidate_reason = NoCandidateReason::kNone;
};

struct TerminalError {
    int status = 500;
    std::string body;                  // harness-enveloped, dumped JSON
    bool close_connection = false;     // timeout → caller sends Connection: close
    int retry_after_seconds = 0;
};

/// Internal error classification shared by terminal and streaming paths.
/// `passthrough` means the caller already owns the original structured body;
/// no retry hint or other user-visible field is added here.
struct NormalizedError {
    int status = 500;
    std::string type = "upstream_error";
    std::string message = "upstream error";
    json code = nullptr;
    bool close_connection = false;
    bool passthrough = false;
    int retry_after_seconds = 0;
};

json normalized_error_body(const NormalizedError &error);
json serialize_normalized_error(const FormatCodec &client_codec,
                                const NormalizedError &error);

// Render the terminal failure response (status + body) for one request.  Shared
// by chat (fail-all, used passthrough, used converted), embeddings and models so
// the status decision (no_upstream_status), the error normalization
// (parse_error_body) and the harness envelope (serialize_error_body) live in one
// place instead of three divergent inline blocks.
TerminalError render_terminal_error(
    const FormatCodec &harness_codec, const FormatCodec *upstream_codec,
    const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts,
    const TerminalErrorOptions &opts = {});

// Streaming-tail companion: returns status + the normalized {message,type,code}
// (no harness envelope) so the SSE path can emit it through make_stream_emitter().
NormalizedError render_stream_error(
    const FormatCodec *upstream_codec, const UpstreamClient::ForwardResult &fwd,
    const std::vector<Database::AttemptInfo> &attempts, bool last_timeout,
    const json &in_stream_error, int last_status,
    NoCandidateReason no_candidate_reason,
    const char *busy_message = "All upstream key concurrency slots are occupied");
