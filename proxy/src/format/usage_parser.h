#pragma once

#include "ir.h"

#include <optional>
#include <string>

namespace fmt {

/// Protocol-level usage extracted from one complete non-streaming response
/// body. It deliberately stays in the format layer: the store must receive
/// only the explicit UsageAccounting projection.
struct WireUsage {
    ir::Usage usage;
    std::string model;
};

std::optional<WireUsage> parse_openai_usage_json(const std::string &body);
std::optional<WireUsage> parse_anthropic_usage_json(const std::string &body);
std::optional<WireUsage> parse_responses_usage_json(const std::string &body);

/// Dispatch non-streaming usage extraction by upstream wire format.
std::optional<WireUsage> parse_usage_for_format(ir::ApiFormat format,
                                                const std::string &body);

}  // namespace fmt
