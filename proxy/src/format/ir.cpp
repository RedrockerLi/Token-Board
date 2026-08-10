#include "ir.h"

#include <algorithm>
#include <cctype>

namespace ir {

ApiFormat parse_api_format(const std::string &s) {
    // Routing snapshots store canonical spellings. Avoid allocating a
    // temporary for the overwhelmingly common hot-path values; retain the
    // normalized fallback for configuration files written by older clients.
    if (s == "openai" || s.empty()) return ApiFormat::OpenAI;
    if (s == "anthropic") return ApiFormat::Anthropic;
    if (s == "openai_responses") return ApiFormat::OpenAIResponses;
    std::string normalized = s;
    std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                   [](unsigned char value) {
                       return static_cast<char>(std::tolower(value));
                   });
    if (normalized == "openai_responses") return ApiFormat::OpenAIResponses;
    if (normalized == "anthropic") return ApiFormat::Anthropic;
    return ApiFormat::OpenAI;  // "openai" and any unknown value → OpenAI
}

std::string to_string(ApiFormat f) {
    switch (f) {
        case ApiFormat::OpenAIResponses: return "openai_responses";
        case ApiFormat::Anthropic: return "anthropic";
        case ApiFormat::OpenAI: return "openai";
    }
    return "openai";
}

json merge_preserving(const json &extras, const json &generated) {
    json out = extras.is_object() ? extras : json::object();
    if (generated.is_object()) {
        for (auto it = generated.begin(); it != generated.end(); ++it)
            out[it.key()] = it.value();
    }
    return out;
}

void usage_merge(Usage &dst, const Usage &src) {
    dst.prompt_tokens = src.prompt_tokens;
    dst.completion_tokens = src.completion_tokens;
    dst.cache_read_tokens = src.cache_read_tokens;
    dst.cache_creation_tokens = src.cache_creation_tokens;
    dst.total_tokens = src.total_tokens;
    if (src.extra.is_object()) dst.extra = src.extra;
}

}  // namespace ir
