#pragma once

#include "ir.h"

#include <algorithm>
#include <cctype>
#include <string>

namespace fmt {

enum class ContextManagementAction { None, Preserve, Drop, Reject };

struct ContextManagementDecision {
    ContextManagementAction action = ContextManagementAction::None;
    std::string reason;
    // A bounded, value-free summary suitable for logs. Never include the
    // request's context-management JSON itself in diagnostics.
    std::string edit_types = "none";
};

inline std::string context_management_safe_type(const std::string &value) {
    std::string out;
    out.reserve(std::min<std::size_t>(value.size(), 64));
    for (const unsigned char byte : value) {
        if (out.size() >= 64) break;
        if (std::isalnum(byte) || byte == '_' || byte == '-' || byte == '.')
            out.push_back(static_cast<char>(byte));
        else
            out.push_back('_');
    }
    return out.empty() ? "unknown" : out;
}

inline std::string context_management_edit_types(const json &value) {
    std::string out;
    auto append = [&](const json &entry) {
        if (!entry.is_object() || !entry.contains("type") ||
            !entry["type"].is_string()) return;
        const std::string type =
            context_management_safe_type(entry["type"].get<std::string>());
        if (!out.empty()) out.push_back(',');
        out += type;
    };
    if (value.is_object() && value.contains("edits") &&
        value["edits"].is_array()) {
        for (const auto &entry : value["edits"]) append(entry);
    } else if (value.is_array()) {
        for (const auto &entry : value) append(entry);
    }
    if (out.empty()) return "unknown";
    constexpr std::size_t kMaxBytes = 160;
    if (out.size() > kMaxBytes) {
        out.resize(kMaxBytes - 3);
        out += "...";
    }
    return out;
}

/// Decide whether the source protocol's context-management control can cross
/// the conversion seam. The wire schemas are deliberately not treated as
/// interchangeable merely because both protocols use the same JSON key.
inline ContextManagementDecision context_management_decision(
    ir::ApiFormat source, ir::ApiFormat target, const json &value) {
    ContextManagementDecision decision;
    decision.edit_types = context_management_edit_types(value);

    if (source == target) {
        decision.action = ContextManagementAction::Preserve;
        decision.reason = "native context_management is preserved";
        return decision;
    }

    if (target == ir::ApiFormat::OpenAI) {
        decision.action = ContextManagementAction::Drop;
        decision.reason =
            "context_management is not representable by OpenAI Chat Completions; "
            "the control field is dropped";
        return decision;
    }

    if (target == ir::ApiFormat::OpenAIResponses) {
        if (source == ir::ApiFormat::OpenAIResponses) {
            decision.action = ContextManagementAction::Preserve;
            decision.reason = "native Responses context_management is preserved";
        } else {
            decision.action = ContextManagementAction::Drop;
            decision.reason =
                "the source context_management schema is not copied into OpenAI "
                "Responses; the control field is dropped";
        }
        return decision;
    }

    decision.action = ContextManagementAction::Reject;
    decision.reason =
        "context_management cannot be represented by Anthropic Messages from "
        "the source protocol";
    return decision;
}

inline const char *context_management_action_name(
    ContextManagementAction action) {
    switch (action) {
        case ContextManagementAction::None: return "none";
        case ContextManagementAction::Preserve: return "preserve";
        case ContextManagementAction::Drop: return "drop";
        case ContextManagementAction::Reject: return "reject";
    }
    return "unknown";
}

} // namespace fmt
