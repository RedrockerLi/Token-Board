#pragma once

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

/// Check whether raw SSE chunk data contains non-empty reasoning_content
/// or reasoning fields. Used by streaming path to decide whether to skip
/// think-tag filtering.
bool sse_chunk_has_reasoning(const char *data, size_t len);

// ── ThinkStreamFilter ─────────────────────────────────────────────────────

/// State machine that filters <think>...</think> blocks from SSE streams.
/// Handles tags that span multiple SSE delta chunks.
struct ThinkStreamFilter {
    enum State { NORMAL, IN_THINK };
    State state = NORMAL;

    /// Feed raw bytes from upstream, return filtered bytes ready to forward.
    /// Returns empty when data is being buffered or suppressed.
    std::string feed(const char *data, size_t len);

private:
    std::string buf_;  // buffer for partial / incomplete SSE lines

    std::string process_line(const std::string &line);
};
