#pragma once

#include "format_responses.h"
#include "format_common.h"

class ResponsesCodec final : public FormatCodec {
public:
    ResponsesCodec() : FormatCodec(ir::ApiFormat::OpenAIResponses) {}
    bool parse_request(const json &, ir::ChatRequest &,
                       std::string &) const override;
    json serialize_request(const ir::ChatRequest &) const override;
    bool parse_response(const json &, ir::ChatResponse &,
                        std::string &) const override;
    json serialize_response(const ir::ChatResponse &) const override;
    json parse_error_body(const json &value) const override {
        return fmt::normalize_error_body(value);
    }
    json serialize_error_body(const json &value) const override {
        return json{{"error", value}};
    }
    std::unique_ptr<ir::StreamParser> make_stream_parser() const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter() const override;
};

bool responses_request_key_consumed(const std::string &key);
json parse_responses_arguments(const std::string &arguments);
void parse_responses_content(const json &content,
                             std::vector<ir::ContentBlock> &output);
json serialize_responses_content(const std::vector<ir::ContentBlock> &blocks,
                                 bool output_style);
std::unique_ptr<ir::StreamParser> make_responses_stream_parser_impl();
std::unique_ptr<ir::StreamEmitter> make_responses_stream_emitter_impl();

