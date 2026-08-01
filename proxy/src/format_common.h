#pragma once

#include "ir.h"

/// Shared helpers for the three wire-format codecs.
namespace fmt {

/// Parse an OpenAI-style `{type:"image_url", image_url:{url}}` part into an
/// Image block.  Handles both http(s) URLs and `data:` URIs.
ir::ContentBlock openai_image_part_to_block(const json &part);

/// Serialize an Image block into an OpenAI chat-completions content part.
json image_block_to_openai_part(const ir::ContentBlock &b);

/// Split a `data:<media>;base64,<data>` URI; returns false if not a data URI.
bool parse_data_uri(const std::string &uri, std::string &media_type,
                    std::string &b64);
std::string build_data_uri(const std::string &media_type,
                           const std::string &b64);

/// Stop-reason mappings.
const char *stop_reason_to_openai(ir::StopReason r);   // finish_reason
const char *stop_reason_to_anthropic(ir::StopReason r); // stop_reason
const char *stop_reason_to_responses(ir::StopReason r); // status: completed/incomplete
ir::StopReason openai_finish_reason_to_stop(const std::string &fr);
ir::StopReason anthropic_stop_reason_to_stop(const std::string &sr);
ir::StopReason responses_status_to_stop(const std::string &st);

/// Normalize an IR tool_choice (raw, may be either format's shape) into the
/// OpenAI chat-completions tool_choice shape:
///   "auto"/"required"/"none" string, or {"type":"function","function":{...}}.
/// Anthropic shapes are translated; anything already OpenAI-shaped passes through.
json normalize_tool_choice_to_openai(const json &tc);

/// Normalize an IR tool_choice into the Anthropic tool_choice shape:
///   {"type":"auto"|"any"|"none"} or {"type":"tool","name":...}.
/// OpenAI shapes are translated; anything already Anthropic-shaped passes through.
json normalize_tool_choice_to_anthropic(const json &tc);

/// Normalize an arbitrary error object to {message, type, code} (json object).
json normalize_error_body(const json &body);

/// Copy only the listed keys from `src` into a new object (preserves order).
/// Used to forward format-specific extra params when serializing.
json filter_keys(const json &src, std::initializer_list<const char *> keep);

/// Claude Code appends a `[1m]`/`[1M]` context-window marker to model names
/// (e.g. `deepseek-v4-flash[1m]`).  Upstream APIs don't accept this local
/// capability marker, so strip it (case-insensitively) before forwarding —
/// mirroring cc-switch's `strip_one_m_suffix_for_upstream`.
std::string strip_one_m_suffix_for_upstream(const std::string &model);

/// True if the model name suggests a reasoning-vendor upstream (DeepSeek,
/// Moonshot/Kimi, Mimo, …).  Such upstreams require a `reasoning_content`
/// field on assistant messages that carry `tool_calls`, otherwise they reject
/// the request with a 400 — mirroring cc-switch's `preserve_reasoning_content`.
bool is_reasoning_vendor(const std::string &model);

/// Incremental SSE frame splitter.  Frames are separated by "\n\n" (LF) or
/// "\r\n\r\n" (CRLF).  Emits complete frame bodies (sans the trailing blank
/// line).  Safe because SSE payloads never contain a literal blank line.
class SseFrameBuffer {
public:
    template <typename F>
    void feed(const char *data, size_t len, F &&on_frame) {
        buf_.append(data, len);
        size_t pos;
        while ((pos = find_sep(buf_)) != std::string::npos) {
            bool crlf = (buf_.compare(pos, 4, "\r\n\r\n") == 0);
            std::string frame = buf_.substr(0, pos);
            buf_.erase(0, pos + (crlf ? 4 : 2));
            if (!frame.empty() && frame.back() == '\r') frame.pop_back();
            on_frame(frame);
        }
    }
    template <typename F>
    void finish(F &&on_frame) {
        if (!buf_.empty()) {
            if (buf_.back() == '\r') buf_.pop_back();
            on_frame(buf_);
            buf_.clear();
        }
    }

private:
    static size_t find_sep(const std::string &s) {
        size_t a = s.find("\r\n\r\n");
        size_t b = s.find("\n\n");
        if (a == std::string::npos) return b;
        if (b == std::string::npos) return a;
        return a < b ? a : b;
    }
    std::string buf_;
};

/// Parse a raw SSE frame into its optional event name and data payload.
/// Returns false for comment/heartbeat frames with no data.
bool parse_sse_frame(const std::string &frame, std::string *event_name,
                     std::string *data);

/// Parse a wire `usage` object (OpenAI/Responses or Anthropic shape) into IR.
ir::Usage parse_usage_json(const json &u);

}  // namespace fmt
