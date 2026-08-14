#pragma once

#include "format_openai.h"
#include "format_common.h"
#include "format_media.h"

class OpenAICodec final : public FormatCodec {
public:
    OpenAICodec() : FormatCodec(ir::ApiFormat::OpenAI) {}
    bool parse_request(const json &, ir::ChatRequest &,
                       std::string &) const override;
    json serialize_request(const ir::ChatRequest &, const ir::ConversionContext * = nullptr) const override;
    bool parse_response(const json &, ir::ChatResponse &,
                        std::string &, const ir::ConversionContext * = nullptr) const override;
    json serialize_response(const ir::ChatResponse &, const ir::ConversionContext * = nullptr) const override;
    json parse_error_body(const json &value) const override {
        return fmt::normalize_error_body(value);
    }
    json serialize_error_body(const json &value) const override {
        return json{{"error", value}};
    }
    std::unique_ptr<ir::StreamParser> make_stream_parser(const ir::ConversionContext * = nullptr) const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter(const ir::ConversionContext * = nullptr) const override;
};

bool openai_request_key_consumed(const std::string &key);
void collapse_openai_system_messages(json &messages);
void parse_openai_namespace_children(const json &tool_json, ir::Tool &tool);
void append_openai_content_part(json &parts, json &tool_calls, bool &has_tools,
                                std::string &reasoning_text,
                                const ir::ContentBlock &block);
std::unique_ptr<ir::StreamParser> make_openai_stream_parser_impl(
    const ir::ConversionContext * = nullptr);
std::unique_ptr<ir::StreamEmitter> make_openai_stream_emitter_impl();
