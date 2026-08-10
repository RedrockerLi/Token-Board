#pragma once

#include "codec.h"

#include <string>
#include <memory>

namespace httplib { struct Request; }

struct RequestContext {
    std::shared_ptr<const std::string> raw_body;
    ir::ApiFormat client_format = ir::ApiFormat::OpenAI;
    std::string model;
    bool streaming = false;
    std::string session_id;
    std::string content_type = "application/json";
    int queue_ms = 0;
    nlohmann::json parsed_json;
    ir::ChatRequest parsed_ir;
    bool ir_ready = false;
};

bool parse_request_context(const httplib::Request &request,
                           RequestContext &context, std::string &error);
bool ensure_request_ir(const CodecRegistry &codecs, RequestContext &context,
                       std::string &error);
