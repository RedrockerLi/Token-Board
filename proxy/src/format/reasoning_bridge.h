#pragma once

#include "json.hpp"

#include <string>

using json = nlohmann::json;

namespace fmt {

std::string encode_reasoning_envelope(const json &item, const char *prefix);
bool decode_reasoning_envelope(const std::string &value, const char *prefix,
                               json &item);

// Responses reasoning item -> Anthropic content block.
// Returns an *unsigned* `thinking` block carrying the visible summary text,
// or a null json when the item has no text carrier (encrypted_content only).
// Callers must drop the null case: this service never mints the Anthropic
// `signature` / `redacted_thinking.data` fields (audit 01-P-17).
json anthropic_block_from_responses_reasoning(const json &item);
// Anthropic content block -> Responses reasoning item.  Decodes envelopes
// written by earlier releases, but never produces one.
json responses_reasoning_from_anthropic_block(const json &block);

}
