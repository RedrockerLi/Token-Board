#pragma once

#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

using json = nlohmann::json;

/// Intermediate representation (IR) for chat requests/responses/streams.
///
/// Every wire format (OpenAI chat completions, OpenAI Responses, Anthropic
/// Messages) parses into this IR and serializes from it, so any harness
/// format can be converted to any upstream format by composing
///   parse(harness) → IR → serialize(upstream).
namespace ir {

enum class ApiFormat { OpenAI, OpenAIResponses, Anthropic };
ApiFormat parse_api_format(const std::string &s);
std::string to_string(ApiFormat f);

enum class ContentKind { Text, Image, File, Audio, ToolUse, ToolResult, Thinking };
enum class StopReason { Stop, Length, ToolUse, ContentFilter, Unknown };

/// A single normalized content block (request and response share this).
/// Only the fields relevant to the block's kind are populated.
struct ContentBlock {
    ContentKind kind = ContentKind::Text;
    std::string text;             // Text / Thinking
    std::string image_url;        // Image: http(s) URL
    std::string image_data_b64;   // Image: base64 payload
    std::string media_type;       // Image: "image/png", ...
    std::string file_id;          // File: provider-managed file id
    std::string file_url;         // File: remote URL (Responses/Anthropic)
    std::string file_data_b64;    // File: base64 payload (without data URI)
    std::string filename;         // File: optional display name
    std::string audio_data_b64;   // Audio: base64 payload
    std::string audio_format;     // Audio: wav/mp3/...
    std::string tool_use_id;      // ToolResult: the tool_use block being answered
    std::string tool_call_id;     // ToolUse: this call's id
    std::string tool_name;        // ToolUse
    json tool_input = json::object();  // ToolUse arguments (object form)
    std::vector<ContentBlock> nested;   // ToolResult text/media blocks
    json extra = json::object();       // per-block extra (thinking signature, raw part, ...)
};

struct Message {
    std::string role;                   // "user" | "assistant" | "system" | "tool"
    std::vector<ContentBlock> content;  // normalized block array
    json extra = json::object();
};

struct Tool {
    std::string name;
    std::string description;
    json input_schema = json::object();  // OpenAI "parameters" / Anthropic input_schema / Responses parameters
    json extra = json::object();
};

struct ReasoningConfig {
    bool enabled = false;
    std::optional<int> budget_tokens;  // Anthropic thinking.budget_tokens
    std::string effort;                // OpenAI reasoning_effort / Responses reasoning.effort
    json extra = json::object();
};

struct Usage {
    int prompt_tokens = 0;
    int completion_tokens = 0;
    int cache_read_tokens = 0;
    int cache_creation_tokens = 0;
    int total_tokens = 0;  // prompt + completion (computed by callers)
    json extra = json::object();
};

struct ChatRequest {
    std::string model;
    std::vector<ContentBlock> system;  // system prompt as blocks
    std::vector<Message> messages;
    std::vector<Tool> tools;
    json tool_choice = json::object();  // raw; enum shapes differ per format
    bool stream = false;
    ReasoningConfig reasoning;
    std::optional<int> max_tokens;
    std::optional<double> temperature;
    std::vector<std::string> stop_sequences;
    json extras = json::object();  // format-specific keys not in the union
};

struct ChatResponse {
    std::string id;
    std::string model;
    std::vector<ContentBlock> content;  // text / thinking / tool_use
    StopReason stop_reason = StopReason::Unknown;
    std::optional<std::string> stop_sequence;
    Usage usage;
    json extras = json::object();
};

enum class StreamEventType {
    MessageStart,          // model, id, initial usage (Anthropic message_start)
    ContentTextDelta,      // text fragment
    ContentThinkingDelta,  // reasoning/thinking fragment
    ToolCallStart,         // index + id + name
    ToolCallArgumentDelta, // index + arguments fragment (fragmented JSON)
    ToolCallDone,          // index + final complete arguments string
    MessageFinish,         // stop_reason (+ stop_sequence in extra)
    UsageEvent,            // usage snapshot (last wins)
    ErrorEvent,            // in-stream upstream error; envelope in `extra`
};

struct StreamEvent {
    StreamEventType type;
    int index = 0;                    // content-block / tool index
    std::string text;                 // text/thinking delta, or tool id / tool name
    std::string arguments;            // tool-call arguments (delta or final)
    StopReason stop_reason = StopReason::Unknown;
    Usage usage;
    json extra = json::object();
};

/// Start from `extras` and overwrite with `generated` keys, so serialization
/// preserves unknown format-specific fields while emitting canonical ones.
json merge_preserving(const json &extras, const json &generated);

/// Accumulate a usage snapshot into `dst` (for streaming, last snapshot wins).
void usage_merge(Usage &dst, const Usage &src);

}  // namespace ir
