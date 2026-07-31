#include "ir.h"

namespace ir {

ApiFormat parse_api_format(const std::string &s) {
    if (s == "openai_responses") return ApiFormat::OpenAIResponses;
    if (s == "anthropic") return ApiFormat::Anthropic;
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

HarnessFormat parse_harness_format(const std::string &s) {
    if (s == "openai") return HarnessFormat::OpenAI;
    if (s == "openai_responses") return HarnessFormat::OpenAIResponses;
    if (s == "anthropic") return HarnessFormat::Anthropic;
    return HarnessFormat::Unset;  // "" or unknown → fall back to account format
}

std::string to_string(HarnessFormat f) {
    switch (f) {
        case HarnessFormat::OpenAI: return "openai";
        case HarnessFormat::OpenAIResponses: return "openai_responses";
        case HarnessFormat::Anthropic: return "anthropic";
        case HarnessFormat::Unset: return "";
    }
    return "";
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
