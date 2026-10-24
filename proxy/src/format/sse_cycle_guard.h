#pragma once

#include <cstddef>
#include <string>

#include "format_common.h"
#include "ir.h"

namespace fmt {

/// Frame-level guard that lets exactly one message cycle through.
///
/// Some gateways (observed on opencode.ai Zen/Go's Anthropic endpoint) emit
/// two complete cycles — `message_start … message_stop` twice, with different
/// message ids — inside a single HTTP response.  The proxy's passthrough would
/// forward both and the client would render two replies.
///
/// The guard buffers only the current *incomplete* frame and emits complete
/// frames with their original separator, so the forwarded bytes stay
/// byte-exact and a second cycle can never be half-written.  The first
/// terminal frame is delivered; everything after it — including a next-cycle
/// start that arrives when the upstream omitted its terminal — is dropped.
class SseCycleGuard {
public:
    explicit SseCycleGuard(ir::ApiFormat upstream_format,
                           std::size_t max_frame_bytes = kMaxSseFrameBytes)
        : format_(upstream_format), max_frame_bytes_(max_frame_bytes) {}

    /// Append the bytes that may be forwarded to `out`.  After the first
    /// terminal frame `out` is left untouched and the input is counted as
    /// dropped.
    void feed(const char *data, std::size_t len, std::string &out);

    /// Flush the trailing partial frame at EOF.  A frame without its blank
    /// line is classified like any other, so a terminal without a trailing
    /// separator still counts as a terminal.
    void finish(std::string &out);

    /// The guard stopped: a terminal frame was delivered, or a second-cycle
    /// start was cut away.
    bool terminal_seen() const noexcept { return terminal_; }
    /// A real terminal frame was delivered.  A cut at a second-cycle start
    /// leaves the first cycle unterminated, so callers must not treat that as
    /// a complete response.
    bool terminal_frame_seen() const noexcept { return terminal_frame_seen_; }
    /// Bytes after the first cycle were detected and dropped.
    bool suppressed_after_terminal() const noexcept { return suppressed_; }
    std::size_t dropped_bytes() const noexcept { return dropped_; }
    /// A single frame exceeded the bound: the guard emitted what it had and
    /// now passes bytes through unchanged rather than stalling the stream.
    bool failed() const noexcept { return failed_; }

private:
    enum class FrameKind { kPass, kTerminal, kSecondCycle };

    FrameKind classify(const std::string &frame);

    ir::ApiFormat format_;
    std::string buf_;          // bytes of the current incomplete frame
    std::string first_id_;     // OpenAI: message id of the first cycle
    bool saw_start_ = false;
    bool openai_finish_seen_ = false;
    bool terminal_ = false;
    bool terminal_frame_seen_ = false;
    bool suppressed_ = false;
    bool failed_ = false;
    std::size_t dropped_ = 0;
    std::size_t max_frame_bytes_;
};

}  // namespace fmt
