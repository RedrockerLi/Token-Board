#include "think_filter.h"

#include "json.hpp"

#include <regex>
#include <sstream>
#include <string_view>

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

bool sse_chunk_has_reasoning(const char *data, size_t len) {
    // Lightweight pre-check: does the chunk mention reasoning fields at all?
    std::string_view sv(data, len);
    if (sv.find("\"reasoning_content\"") == std::string::npos &&
        sv.find("\"reasoning\"") == std::string::npos)
        return false;

    // Parse SSE lines to verify the reasoning field is non-empty
    std::istringstream iss(std::string(data, len));
    std::string sse_line;
    while (std::getline(iss, sse_line)) {
        if (sse_line.rfind("data:", 0) != 0) continue;
        std::string js = sse_line.substr(sse_line.find(':') + 1);
        if (!js.empty() && js[0] == ' ') js.erase(0, 1);
        if (js == "[DONE]") continue;
        try {
            auto j = json::parse(js);
            if ((j.contains("reasoning_content") &&
                 j["reasoning_content"].is_string() &&
                 !j["reasoning_content"].get<std::string>().empty()) ||
                (j.contains("reasoning") &&
                 j["reasoning"].is_string() &&
                 !j["reasoning"].get<std::string>().empty())) {
                return true;
            }
        } catch (...) {}
    }
    return false;
}

// ── ThinkStreamFilter ──────────────────────────────────────────────────────

std::string ThinkStreamFilter::feed(const char *data, size_t len) {
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

std::string ThinkStreamFilter::process_line(const std::string &line) {
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
        return line;

    try {
        json j = json::parse(json_str);
        if (!j.contains("choices") || !j["choices"].is_array())
            return line;

        bool modified = false;
        for (auto &choice : j["choices"]) {
            if (!choice.contains("delta") || !choice["delta"].is_object())
                continue;
            auto &delta = choice["delta"];
            if (!delta.contains("content") || !delta["content"].is_string())
                continue;

            std::string content = delta["content"].get<std::string>();

            if (state == NORMAL) {
                size_t think_start = content.find("<think>");
                if (think_start == std::string::npos) {
                    continue;  // no think tag, pass through unchanged
                }

                std::string before = content.substr(0, think_start);
                std::string after_think = content.substr(think_start + 7);

                size_t close = after_think.find("</think>");
                if (close != std::string::npos) {
                    // Complete <think>...</think> in one chunk
                    std::string inner = after_think.substr(0, close);
                    std::string after = after_think.substr(close + 8);

                    delta["content"] = before + after;
                    delta["reasoning_content"] = inner;
                    // Strip any additional <think> blocks from content
                    std::string c = delta["content"].get<std::string>();
                    delta["content"] = strip_think_tags(c);
                    modified = true;
                } else {
                    // <think> opens here, doesn't close
                    delta["content"] = before;
                    delta["reasoning_content"] = after_think;
                    modified = true;
                    state = IN_THINK;
                }
            } else {  // IN_THINK
                size_t close = content.find("</think>");
                if (close != std::string::npos) {
                    // Found closing tag
                    std::string inner = content.substr(0, close);
                    std::string after = content.substr(close + 8);

                    delta["reasoning_content"] = inner;
                    delta["content"] = after;
                    // Strip any additional <think> blocks from content
                    std::string c = delta["content"].get<std::string>();
                    delta["content"] = strip_think_tags(c);
                    modified = true;
                    state = NORMAL;
                } else {
                    // Still inside think — all content is reasoning
                    delta["reasoning_content"] = content;
                    delta.erase("content");
                    modified = true;
                }
            }
        }

        if (modified) {
            return prefix + j.dump() + "\n";
        }
    } catch (const json::exception &) {
        // Malformed JSON — pass through unchanged
    }

    return line;
}
