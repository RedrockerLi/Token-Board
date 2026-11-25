#pragma once

#include "format_common.h"
#include "format_google.h"
#include "format_media.h"
#include "core/logging.h"

#include <map>

/// Google Gemini GenerateContent codec.
///
/// The model is a path variable on this wire (`models/{model}:generateContent`),
/// never a request-body field, and streaming is selected by the method suffix
/// plus `alt=sse`.  The codec therefore only owns JSON ↔ IR; the HTTP layer
/// resolves the model and the stream flag from the URL.
class GoogleCodec final : public FormatCodec {
public:
    GoogleCodec() : FormatCodec(ir::ApiFormat::Google) {}
    bool parse_request(const json &, ir::ChatRequest &,
                       std::string &) const override;
    json serialize_request(const ir::ChatRequest &,
                           const ir::ConversionContext * = nullptr) const override;
    bool parse_response(const json &, ir::ChatResponse &, std::string &,
                        const ir::ConversionContext * = nullptr) const override;
    json serialize_response(const ir::ChatResponse &,
                            const ir::ConversionContext * = nullptr) const override;
    json parse_error_body(const json &value) const override;
    json serialize_error_body(const json &value) const override;
    std::unique_ptr<ir::StreamParser> make_stream_parser(
        const ir::ConversionContext * = nullptr) const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter(
        const ir::ConversionContext * = nullptr) const override;
};

namespace gfmt {

/// Model id without the `models/` resource prefix (`models/x` → `x`).
std::string bare_model_id(const std::string &model);
/// True when the model requires thought signatures (Gemini 3 and later).
bool requires_thought_signature(const std::string &model);

/// IR stop reason ↔ Gemini `finishReason`.  `nullptr` means "omit the field".
const char *finish_reason_to_wire(ir::StopReason reason);
ir::StopReason finish_reason_from_wire(const json &candidate,
                                       bool has_tool_calls);

/// `usageMetadata` ↔ IR usage.
void usage_from_wire(const json &usage, ir::Usage &out);
json usage_to_wire(const ir::Usage &usage);

/// `generationConfig` for a request (thinking, limits, structured output).
json generation_config(const ir::ChatRequest &request);

/// `functionDeclarations[]` entry for one IR tool.
json function_declaration(const ir::Tool &tool);
/// Gemini `Schema` (upper-case type enums) → plain JSON Schema.
json schema_from_wire(const json &schema);
/// `toolConfig.functionCallingConfig` for an IR tool_choice ({} = omit).
json function_calling_config(const json &tool_choice);

/// IR content blocks ↔ Gemini `parts`.
json parts_from_blocks(const std::vector<ir::ContentBlock> &blocks,
                       bool require_signature);
void blocks_from_parts(const json &parts, std::vector<ir::ContentBlock> &out);

/// Restore the client's own tool name for a flattened upstream name.
std::string client_tool_name(const ir::ConversionContext *context,
                             const std::string &upstream_name);

/// Tool-call-id → tool-name index used to name tool results (Gemini requires
/// `functionResponse.name`, OpenAI tool messages only carry the call id).
std::map<std::string, std::string> tool_call_names(
    const std::vector<ir::Message> &items);

std::unique_ptr<ir::StreamParser> make_stream_parser_impl(
    const ir::ConversionContext * = nullptr);
std::unique_ptr<ir::StreamEmitter> make_stream_emitter_impl(
    const ir::ConversionContext * = nullptr);

}  // namespace gfmt
