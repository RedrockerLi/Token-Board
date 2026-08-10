#include "format_responses_internal.h"

std::unique_ptr<ir::StreamParser> ResponsesCodec::make_stream_parser() const {
    return make_responses_stream_parser_impl();
}
std::unique_ptr<ir::StreamEmitter> ResponsesCodec::make_stream_emitter() const {
    return make_responses_stream_emitter_impl();
}

std::unique_ptr<FormatCodec> make_responses_codec() {
    return std::make_unique<ResponsesCodec>();
}
