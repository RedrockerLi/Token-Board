#include "format_openai_internal.h"

std::unique_ptr<ir::StreamParser> OpenAICodec::make_stream_parser(
    const ir::ConversionContext *context) const {
    return make_openai_stream_parser_impl(context);
}
std::unique_ptr<ir::StreamEmitter> OpenAICodec::make_stream_emitter(
    const ir::ConversionContext *context) const {
    return make_openai_stream_emitter_impl(context);
}

std::unique_ptr<FormatCodec> make_openai_codec() {
    return std::make_unique<OpenAICodec>();
}
