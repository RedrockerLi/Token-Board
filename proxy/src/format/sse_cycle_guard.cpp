#include "sse_cycle_guard.h"

#include <string_view>

#include "json.hpp"

namespace fmt {
namespace {

using json = nlohmann::json;

std::size_t find_sep(const std::string &s) {
    const std::size_t a = s.find("\r\n\r\n");
    const std::size_t b = s.find("\n\n");
    if (a == std::string::npos) return b;
    if (b == std::string::npos) return a;
    return a < b ? a : b;
}

}  // namespace

void SseCycleGuard::feed(const char *data, std::size_t len,
                         std::string &out) {
    if (terminal_) {
        if (len != 0) {
            dropped_ += len;
            suppressed_ = true;
        }
        return;
    }
    if (failed_) {
        out.append(data, len);
        return;
    }
    if (len == 0) return;
    buf_.append(data, len);
    std::size_t pos = 0;
    while ((pos = find_sep(buf_)) != std::string::npos) {
        const std::size_t sep_len =
            buf_.compare(pos, 4, "\r\n\r\n") == 0 ? 4 : 2;
        const std::size_t raw_len = pos + sep_len;
        std::string raw = buf_.substr(0, raw_len);
        std::string frame = raw.substr(0, pos);
        buf_.erase(0, raw_len);
        if (!frame.empty() && frame.back() == '\r') frame.pop_back();
        const FrameKind kind = classify(frame);
        if (kind == FrameKind::kSecondCycle) {
            terminal_ = true;
            // OpenAI's finish_reason already completed the first cycle; an
            // Anthropic/Responses first cycle that reaches this branch never
            // saw its terminal frame.
            if (openai_finish_seen_) terminal_frame_seen_ = true;
            suppressed_ = true;
            dropped_ += raw_len + buf_.size();
            buf_.clear();
            return;
        }
        out += raw;
        if (kind == FrameKind::kTerminal) {
            terminal_ = true;
            terminal_frame_seen_ = true;
            if (!buf_.empty()) {
                dropped_ += buf_.size();
                suppressed_ = true;
                buf_.clear();
            }
            return;
        }
    }
    if (buf_.size() > max_frame_bytes_) {
        // Fail open: a frame this large is not a normal SSE event, so stop
        // guarding rather than holding the stream hostage.
        out += buf_;
        buf_.clear();
        failed_ = true;
    }
}

void SseCycleGuard::finish(std::string &out) {
    if (terminal_ || failed_) return;
    if (!buf_.empty()) {
        std::string raw = std::move(buf_);
        buf_.clear();
        std::string frame = raw;
        if (!frame.empty() && frame.back() == '\r') frame.pop_back();
        const FrameKind kind = classify(frame);
        if (kind == FrameKind::kSecondCycle) {
            terminal_ = true;
            if (openai_finish_seen_) terminal_frame_seen_ = true;
            suppressed_ = true;
            dropped_ += raw.size();
            return;
        }
        out += raw;
        if (kind == FrameKind::kTerminal) {
            terminal_ = true;
            terminal_frame_seen_ = true;
        }
    }
    // OpenAI streams normally end with `[DONE]`; a `finish_reason` that is
    // followed by EOF still identifies a complete cycle.
    if (!terminal_ && openai_finish_seen_ &&
        format_ == ir::ApiFormat::OpenAI) {
        terminal_ = true;
        terminal_frame_seen_ = true;
    }
}

SseCycleGuard::FrameKind SseCycleGuard::classify(const std::string &frame) {
    std::string event_name;
    std::string data;
    const bool has_data = parse_sse_frame(frame, &event_name, &data);
    json parsed;
    bool has_json = false;
    if (has_data && !data.empty() && data.front() == '{') {
        parsed = json::parse(data, nullptr, false);
        has_json = !parsed.is_discarded() && parsed.is_object();
    }
    const std::string type =
        has_json && parsed.contains("type") && parsed["type"].is_string()
            ? parsed["type"].get<std::string>()
            : std::string();

    switch (format_) {
        case ir::ApiFormat::Anthropic: {
            if (event_name == "message_stop" || type == "message_stop")
                return FrameKind::kTerminal;
            if (event_name == "message_start" || type == "message_start") {
                if (saw_start_) return FrameKind::kSecondCycle;
                saw_start_ = true;
            }
            return FrameKind::kPass;
        }
        case ir::ApiFormat::OpenAI: {
            std::string_view payload = data;
            const auto first = payload.find_first_not_of(" \t\r\n");
            if (first != std::string_view::npos) payload.remove_prefix(first);
            const auto last = payload.find_last_not_of(" \t\r\n");
            if (last != std::string_view::npos) payload = payload.substr(0, last + 1);
            if (payload == "[DONE]") return FrameKind::kTerminal;
            if (has_json) {
                if (parsed.contains("id") && parsed["id"].is_string()) {
                    const std::string id = parsed["id"].get<std::string>();
                    if (!id.empty()) {
                        if (first_id_.empty()) {
                            first_id_ = id;
                            saw_start_ = true;
                        } else if (id != first_id_) {
                            return FrameKind::kSecondCycle;
                        }
                    }
                }
                const json *choices = nullptr;
                if (parsed.contains("choices") && parsed["choices"].is_array())
                    choices = &parsed["choices"];
                if (choices != nullptr && !choices->empty() &&
                    (*choices)[0].is_object()) {
                    const json &choice = (*choices)[0];
                    if (openai_finish_seen_ && choice.contains("delta") &&
                        choice["delta"].is_object() &&
                        choice["delta"].value("role", "") == "assistant")
                        return FrameKind::kSecondCycle;
                    if (choice.contains("finish_reason") &&
                        !choice["finish_reason"].is_null())
                        openai_finish_seen_ = true;
                }
            }
            return FrameKind::kPass;
        }
        case ir::ApiFormat::OpenAIResponses: {
            if (type == "response.completed" || type == "response.incomplete" ||
                type == "response.failed" || type == "response.error" ||
                event_name == "error")
                return FrameKind::kTerminal;
            if (type == "response.created") {
                if (saw_start_) return FrameKind::kSecondCycle;
                saw_start_ = true;
            }
            return FrameKind::kPass;
        }
        case ir::ApiFormat::Google: {
            if (!has_json) return FrameKind::kPass;
            // A fresh responseId marks a new generation; the model reports one
            // id per response.
            const std::string id = parsed.value("responseId", "");
            if (!id.empty()) {
                if (first_id_.empty()) {
                    first_id_ = id;
                    saw_start_ = true;
                } else if (id != first_id_) {
                    return FrameKind::kSecondCycle;
                }
            }
            if (parsed.contains("candidates") && parsed["candidates"].is_array()) {
                for (const auto &candidate : parsed["candidates"])
                    if (candidate.is_object() &&
                        candidate.contains("finishReason"))
                        return FrameKind::kTerminal;
            }
            return FrameKind::kPass;
        }
    }
    return FrameKind::kPass;
}

}  // namespace fmt
