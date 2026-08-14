#pragma once

#include "format_common.h"

#include <string>
#include <vector>

namespace fmt {

/// Parse provider-native content blocks, including blocks nested inside a
/// tool result. Unknown objects are retained as raw text by the caller.
void parse_media_content(const json &value,
                         std::vector<ir::ContentBlock> &output,
                         bool parse_json_strings = true);

/// Parse the value of a Responses function_call_output or an Anthropic
/// tool_result. Media is represented as native IR blocks; only non-media
/// scalar/unknown values remain text.
void parse_tool_result_content(const json &value,
                               ir::ContentBlock &result);

json serialize_openai_file_part(const ir::ContentBlock &block);
json serialize_openai_audio_part(const ir::ContentBlock &block);
json serialize_responses_file_part(const ir::ContentBlock &block);
json serialize_responses_audio_part(const ir::ContentBlock &block);
json serialize_anthropic_file_part(const ir::ContentBlock &block);
json serialize_openai_tool_media_parts(const ir::ContentBlock &result);
json serialize_openai_content_blocks(const std::vector<ir::ContentBlock> &blocks);
json serialize_responses_tool_result_value(const ir::ContentBlock &result);
json serialize_anthropic_tool_result_content(const ir::ContentBlock &result);

/// Return all text nested in a tool result. This is the only value allowed in
/// an OpenAI Chat `role:"tool"` message; media is emitted as a native user
/// content part by the Chat bridge.
std::string tool_result_text(const ir::ContentBlock &result);
bool tool_result_has_media(const ir::ContentBlock &result);

struct MediaRequirements {
    bool image = false;
    bool file = false;
    bool audio = false;
    bool file_id = false;
    bool file_url = false;
    bool file_unresolved = false;
    bool audio_invalid = false;
};

MediaRequirements request_media_requirements(const ir::ChatRequest &request);

/// Check target-format support before account-gate acquisition. A false result
/// is a deterministic incompatibility (not an upstream retryable failure).
bool target_supports_media(ir::ApiFormat target,
                           const MediaRequirements &requirements,
                           std::string &reason);

}  // namespace fmt
