#include "format_anthropic_internal.h"

std::unique_ptr<ir::StreamParser> AnthropicCodec::make_stream_parser() const {
    return make_anthropic_stream_parser_impl();
}
std::unique_ptr<ir::StreamEmitter> AnthropicCodec::make_stream_emitter() const {
    return make_anthropic_stream_emitter_impl();
}

std::unique_ptr<FormatCodec> make_anthropic_codec() {
    return std::make_unique<AnthropicCodec>();
}
