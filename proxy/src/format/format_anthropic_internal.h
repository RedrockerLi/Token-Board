#pragma once

#include "format_anthropic.h"
#include "format_common.h"
#include "format_media.h"
#include "core/logging.h"

class AnthropicCodec final : public FormatCodec {
public:
    AnthropicCodec() : FormatCodec(ir::ApiFormat::Anthropic) {}
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
        return json{{"type", "error"}, {"error", value}};
    }
    std::unique_ptr<ir::StreamParser> make_stream_parser(const ir::ConversionContext * = nullptr) const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter(const ir::ConversionContext * = nullptr) const override;
};

bool anthropic_request_key_consumed(const std::string &key);
int anthropic_effort_budget(const std::string &effort);
void parse_anthropic_blocks(const json &value,
                            std::vector<ir::ContentBlock> &output);
json serialize_anthropic_blocks(
    const std::vector<ir::ContentBlock> &blocks);
std::unique_ptr<ir::StreamParser> make_anthropic_stream_parser_impl(
    const ir::ConversionContext * = nullptr);
std::unique_ptr<ir::StreamEmitter> make_anthropic_stream_emitter_impl();
