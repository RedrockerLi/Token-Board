#pragma once

#include "json.hpp"

#include <string>

using json = nlohmann::json;

namespace fmt {

/// Carriers this service owns.  `signature` / `redacted_thinking.data` on the
/// Anthropic wire and `encrypted_content` on the Responses wire are opaque
/// fields: an envelope put there travels client ↔ proxy only and must never be
/// handed to an upstream that verifies the field (audit 01-P-17 ②⑤).
extern const char kOpenAIReasoningPrefix[];
extern const char kAnthropicThinkingPrefix[];

/// True when `value` carries either envelope prefix, i.e. this service wrote
/// it.  Used to decide what has to be stripped before forwarding.
bool is_reasoning_envelope(const std::string &value);

/// Wrap `item` as `<prefix><base64url(no padding)>` (audit 01-P-17 ②).  With
/// the OpenAI prefix only the replayable fields survive (`type`, `id`,
/// `summary`, `encrypted_content`); with the Anthropic prefix the whole block is
/// kept, because its vendor signature is the payload.
std::string encode_reasoning_envelope(const json &item, const char *prefix);

/// Decode an envelope, validating by re-encoding: a value that does not
/// reproduce itself was not written by this service and must not be replayed
/// (audit 01-P-17 ④).  Envelopes produced before the field whitelist existed
/// still decode, with the whitelist applied on read.
bool decode_reasoning_envelope(const std::string &value, const char *prefix,
                               json &item);

// Responses reasoning item -> Anthropic content block (audit 01-P-17 ③):
//   * `encrypted_content` holding our Anthropic envelope -> the captured block,
//     vendor signature included;
//   * `encrypted_content` otherwise -> `thinking` with the summary text and our
//     OpenAI envelope as `signature` (never `redacted_thinking`, never dropped);
//   * summary only -> unsigned `thinking` carrying the text;
//   * neither -> null, and the caller drops the item.
json anthropic_block_from_responses_reasoning(const json &item);

// Anthropic content block -> Responses reasoning item (audit 01-P-17 ④).
// A block carrying our OpenAI envelope restores the original item; any other
// signed block (or redacted_thinking with data) keeps the whole block in
// `encrypted_content` under our Anthropic prefix.
json responses_reasoning_from_anthropic_block(const json &block);

/// Remove this service's envelopes from an Anthropic **request** body
/// (audit 01-P-17 ⑤): a signed thinking block becomes unsigned, and a
/// redacted_thinking block that only carried our envelope becomes an empty
/// unsigned thinking block.  Called for upstreams that verify signatures.
void strip_reasoning_envelopes(json &body);

}
