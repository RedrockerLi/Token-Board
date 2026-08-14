#pragma once

#include "json.hpp"

#include <string>

using json = nlohmann::json;

namespace fmt {

std::string encode_reasoning_envelope(const json &item, const char *prefix);
bool decode_reasoning_envelope(const std::string &value, const char *prefix,
                               json &item);
json anthropic_block_from_responses_reasoning(const json &item);
json responses_reasoning_from_anthropic_block(const json &block);

}
