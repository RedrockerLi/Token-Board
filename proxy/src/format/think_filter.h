#pragma once

#include <map>
#include <string>

// Forward-declare json from nlohmann
#include "json.hpp"
using json = nlohmann::json;

// ── Think-tag sanitize helpers ────────────────────────────────────────────

/// Check whether the message already has reasoning content populated.
/// If so, we should NOT strip <think> tags — the provider is doing it right.
bool has_reasoning_field(const json &message);

/// Strip <think>...</think> tags (and trailing whitespace) from a string.
std::string strip_think_tags(const std::string &text);

/// Sanitize a single message object: strip think tags from content
/// iff no reasoning field is already populated.
void sanitize_message(json &msg);

/// Sanitize a full JSON response body (non-streaming).
/// Parses JSON, strips <think> tags from all choices[].message.content
/// (unless reasoning_content/reasoning is already populated),
/// and returns the cleaned JSON string.
/// On parse error, returns the original body unchanged.
std::string sanitize_response_body(const std::string &body);

// ── ThinkStreamFilter ─────────────────────────────────────────────────────

/// State machine that filters <think>...</think> blocks from SSE streams.
/// Handles tags that span multiple SSE delta chunks.
struct ThinkStreamFilter {
    /// Feed raw bytes from upstream, return filtered bytes ready to forward.
    /// Returns empty when data is being buffered or suppressed.
    std::string feed(const char *data, size_t len);

    /// Flush a final SSE line when the upstream closes without a trailing
    /// newline. The returned bytes have gone through the same think filter.
    std::string finish();

    bool ok() const noexcept { return error_.empty(); }
    const std::string &error() const noexcept { return error_; }

private:
    enum class State { Normal, InThink };
    struct ChoiceState {
        State state = State::Normal;
        // At most sizeof("</think>")-2 bytes: a suffix which may become a tag
        // when the next content delta arrives.
        std::string pending;
        json frame_template = json::object();
        json choice_template = json::object();
        bool has_template = false;
    };

    std::string buf_;  // buffer for partial / incomplete SSE lines
    bool bypass_ = false;  // upstream already supplies native reasoning fields
    bool finished_ = false;
    std::string error_;
    std::map<int, ChoiceState> choices_;

    std::string process_line(const std::string &line, bool terminal_hint = false);
    std::string flush_pending();
    static void filter_fragment(ChoiceState &choice, const std::string &input,
                                std::string &content, std::string &reasoning,
                                bool allow_pending);
    void fail(const std::string &message);
};
