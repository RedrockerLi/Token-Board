#pragma once

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
        result.total_tokens = usage.total_tokens;
        return result;
    }
};
