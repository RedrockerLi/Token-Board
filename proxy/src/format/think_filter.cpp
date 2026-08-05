#include "think_filter.h"

#include "format_common.h"
#include "json.hpp"

#include <algorithm>
#include <regex>

using json = nlohmann::json;

// ── Sanitize helpers ───────────────────────────────────────────────────────

bool has_reasoning_field(const json &message) {
    if (message.contains("reasoning_content") &&
        message["reasoning_content"].is_string() &&
        !message["reasoning_content"].get<std::string>().empty())
        return true;
    if (message.contains("reasoning") &&
        message["reasoning"].is_string() &&
        !message["reasoning"].get<std::string>().empty())
        return true;
    return false;
}

std::string strip_think_tags(const std::string &text) {
    // Still used as a fallback to remove any remaining <think> blocks
    static const std::regex re(R"(<think>[\s\S]*?</think>\s*)");
    return std::regex_replace(text, re, "");
}

void sanitize_message(json &msg) {
    if (!msg.contains("content") || !msg["content"].is_string()) return;
    if (has_reasoning_field(msg)) return;

    std::string content = msg["content"].get<std::string>();
    std::string reasoning;
    std::string cleaned;

    size_t pos = 0;
    while (true) {
        size_t start = content.find("<think>", pos);
        if (start == std::string::npos) {
            cleaned += content.substr(pos);
            break;
        }
        // Text before <think> goes to content
        cleaned += content.substr(pos, start - pos);

        size_t inner_start = start + 7;  // past "<think>"
        size_t end = content.find("</think>", inner_start);
        if (end == std::string::npos) {
            // No closing tag — leave rest unchanged (malformed)
            cleaned += content.substr(start);
            break;
        }

        // Extract inner text into reasoning
        if (!reasoning.empty()) reasoning += "\n";
        reasoning += content.substr(inner_start, end - inner_start);

        pos = end + 8;  // past "</think>"
        // Skip one trailing newline/space after </think>（常见格式：</think>\n）
        if (pos < content.size() && content[pos] == '\n')
            pos++;
        else if (pos < content.size() && content[pos] == '\r')
            pos++;
        if (pos < content.size() && content[pos] == '\n')
            pos++;
    }

    if (!reasoning.empty()) {
        msg["content"] = cleaned;
        msg["reasoning_content"] = reasoning;
    }
}

std::string sanitize_response_body(const std::string &body) {
    try {
        json resp = json::parse(body);
        if (resp.contains("choices") && resp["choices"].is_array()) {
            for (auto &choice : resp["choices"]) {
                if (choice.contains("message")) {
                    sanitize_message(choice["message"]);
                }
            }
        }
        return resp.dump();
    } catch (const json::exception &) {
        // If JSON is malformed, pass through unchanged
        return body;
    }
}

// ── ThinkStreamFilter ──────────────────────────────────────────────────────

std::string ThinkStreamFilter::feed(const char *data, size_t len) {
    if (finished_ || !ok() || len == 0) return {};
    if (len > fmt::kMaxSseFrameBytes ||
        buf_.size() > fmt::kMaxSseFrameBytes - len) {
        fail("unterminated SSE line exceeds streaming limit");
        return {};
    }
    buf_.append(data, len);
    std::string output;
    size_t pos;
    while ((pos = buf_.find('\n')) != std::string::npos) {
        std::string line = buf_.substr(0, pos + 1);
        buf_.erase(0, pos + 1);
        std::string filtered = process_line(line);
        if (!filtered.empty())
            output += filtered;
    }
    return output;
}

std::string ThinkStreamFilter::finish() {
    if (finished_ || !ok()) return {};
    finished_ = true;
    std::string output;
    if (!buf_.empty()) {
        std::string line = std::move(buf_);
        buf_.clear();
        output = process_line(line, true);
    }
    output += flush_pending();
    return output;
}

namespace {

size_t suffix_prefix_length(const std::string &value,
                            const std::string &tag) {
    const size_t max_len = std::min(value.size(), tag.size() - 1);
    for (size_t len = max_len; len > 0; --len) {
        if (value.compare(value.size() - len, len, tag, 0, len) == 0)
            return len;
    }
    return 0;
}

}  // namespace

void ThinkStreamFilter::filter_fragment(ChoiceState &choice,
                                        const std::string &input,
                                        std::string &content,
                                        std::string &reasoning,
                                        bool allow_pending) {
    std::string value = std::move(choice.pending);
    choice.pending.clear();
    value += input;

    size_t pos = 0;
    while (pos < value.size()) {
        const bool normal = choice.state == State::Normal;
        const std::string tag = normal ? "<think>" : "</think>";
        const size_t found = value.find(tag, pos);
        std::string *out = normal ? &content : &reasoning;
        if (found != std::string::npos) {
            out->append(value, pos, found - pos);
            pos = found + tag.size();
            choice.state = normal ? State::InThink : State::Normal;
            continue;
        }

        const std::string tail = value.substr(pos);
        const size_t hold = allow_pending ? suffix_prefix_length(tail, tag) : 0;
        out->append(tail, 0, tail.size() - hold);
        if (hold != 0)
            choice.pending.assign(tail, tail.size() - hold, hold);
        break;
    }
}

std::string ThinkStreamFilter::flush_pending() {
    std::string output;
    for (auto &entry : choices_) {
        auto &choice = entry.second;
        if (choice.pending.empty()) continue;
        if (!choice.has_template) {
            // Defensive fallback: this cannot happen because pending bytes only
            // originate in a parsed content delta. Never silently swallow them.
            fail("cannot flush pending think-tag prefix safely");
            return {};
        }

        json frame = choice.frame_template;
        json ch = choice.choice_template;
        json delta = json::object();
        if (choice.state == State::Normal)
            delta["content"] = choice.pending;
        else
            delta["reasoning_content"] = choice.pending;
        ch["delta"] = std::move(delta);
        ch["finish_reason"] = nullptr;
        frame["choices"] = json::array({std::move(ch)});
        output += "data: " + frame.dump() + "\n\n";
        choice.pending.clear();
    }
    return output;
}

std::string ThinkStreamFilter::process_line(const std::string &line,
                                            bool terminal_hint) {
    // Pass through non-data lines, comments, and [DONE]
    if (line.empty() || line[0] == ':' || line == "\r" || line == "\n")
        return line;

    std::string prefix;
    std::string json_str;

    if (line.rfind("data: ", 0) == 0) {
        prefix = "data: ";
        json_str = line.substr(6);
    } else if (line.rfind("data:", 0) == 0) {
        prefix = "data:";
        json_str = line.substr(5);
    } else {
        return line;
    }

    while (!json_str.empty() && (json_str.back() == '\r' || json_str.back() == '\n'))
        json_str.pop_back();

    if (json_str == "[DONE]")
        return flush_pending() + line;

    if (bypass_) return line;

    try {
        json j = json::parse(json_str);
        if (!j.contains("choices") || !j["choices"].is_array())
            return line;

        bool terminal = terminal_hint || j.contains("error");
        for (const auto &choice : j["choices"]) {
            if (choice.is_object() && choice.contains("finish_reason") &&
                !choice["finish_reason"].is_null()) {
                terminal = true;
                break;
            }
        }

        // Providers that expose a native reasoning field are already
        // separating thought from content. Switch to byte-preserving bypass
        // at a complete SSE-line boundary; chunk splitting cannot make us
        // lose or reorder the line that enabled bypass.
        for (const auto &choice : j["choices"]) {
            if (!choice.is_object() || !choice.contains("delta") ||
                !choice["delta"].is_object())
                continue;
            const auto &delta = choice["delta"];
            if ((delta.contains("reasoning_content") &&
                 delta["reasoning_content"].is_string() &&
                 !delta["reasoning_content"].get_ref<const std::string &>().empty()) ||
                (delta.contains("reasoning") && delta["reasoning"].is_string() &&
                 !delta["reasoning"].get_ref<const std::string &>().empty())) {
                std::string pending = flush_pending();
                if (!ok()) return {};
                bypass_ = true;
                return pending + line;
            }
        }

        bool modified = false;
        for (auto &choice : j["choices"]) {
            if (!choice.is_object() || !choice.contains("delta") ||
                !choice["delta"].is_object())
                continue;
            auto &delta = choice["delta"];
            if (!delta.contains("content") || !delta["content"].is_string())
                continue;

            const int index = choice.value("index", 0);
            auto &choice_state = choices_[index];
            choice_state.frame_template = j;
            choice_state.choice_template = choice;
            choice_state.has_template = true;
            std::string content = delta["content"].get<std::string>();
            std::string filtered_content;
            std::string filtered_reasoning;
            filter_fragment(choice_state, content, filtered_content,
                            filtered_reasoning, !terminal);

            delta["content"] = filtered_content;
            if (!filtered_reasoning.empty())
                delta["reasoning_content"] = filtered_reasoning;
            else
                delta.erase("reasoning_content");
            modified = filtered_content != content ||
                       !filtered_reasoning.empty() ||
                       !choice_state.pending.empty() || modified;
        }

        // Content in the terminal frame first gets a chance to resolve a prefix
        // retained from its previous delta. Any other choice's unresolved
        // prefix is then emitted as a literal synthetic delta before terminal.
        std::string before_terminal;
        if (terminal) {
            before_terminal = flush_pending();
            if (!ok()) return {};
        }

        if (modified) {
            return before_terminal + prefix + j.dump() +
                   (!line.empty() && line.back() == '\n' ? "\n" : "");
        }
        return before_terminal + line;
    } catch (const json::exception &) {
        // Malformed JSON — pass through unchanged
    }

    return line;
}

void ThinkStreamFilter::fail(const std::string &message) {
    if (!error_.empty()) return;
    error_ = message;
    buf_.clear();
    choices_.clear();
}
