#pragma once

#include <limits>
#include <string>
#include <utility>

#include "../format/ir.h"

/// Database-compatible usage projection.
///
/// ``prompt_tokens`` is the input bucket used by the existing billing schema:
/// it includes cache-read tokens, while ``cache_read_tokens`` remains a
/// separate pricing bucket.  Cache creation is folded into prompt for the
/// database projection because the schema has no cache-creation column; the
/// wire/IR value still retains it separately.  This is a compatibility rule,
/// not an invitation to add a second schema column during refactoring.
///
/// ``total_tokens`` is recomputed as the stored prompt plus completion rather
/// than forwarded from the IR.  Some decoders build the IR total over the
/// provider's mutually-exclusive net input (Anthropic streaming is one), so
/// forwarding it would omit every cache-read and cache-creation token from
/// the stored row.  Recomputing keeps the row's total consistent with its
/// cache-inclusive prompt for every format.
struct UsageAccounting {
    std::string model;
    int prompt_tokens = 0;
    int completion_tokens = 0;
    int total_tokens = 0;
    int cache_read_tokens = 0;
    int cache_creation_tokens = 0;

    static UsageAccounting from_ir(const ir::Usage &usage,
                                   ir::ApiFormat upstream_format,
                                   std::string model = {}) {
        UsageAccounting result;
        result.model = std::move(model);
        result.prompt_tokens = usage.prompt_tokens;
        result.completion_tokens = usage.completion_tokens;
        result.cache_read_tokens = usage.cache_read_tokens;
        result.cache_creation_tokens = usage.cache_creation_tokens;
        if (upstream_format == ir::ApiFormat::Anthropic) {
            result.prompt_tokens += usage.cache_read_tokens
                                  + usage.cache_creation_tokens;
        }
        // Storage rule: total = stored prompt + completion (see the struct
        // comment).  Sum in 64-bit and fall back to 0 out of range, matching
        // the usage parsers' treatment of untrusted provider counters.
        const long long total = static_cast<long long>(result.prompt_tokens)
                              + result.completion_tokens;
        result.total_tokens = total >= 0
            && total <= std::numeric_limits<int>::max()
            ? static_cast<int>(total) : 0;
        return result;
    }
};
