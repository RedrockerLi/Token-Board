#pragma once

#include <functional>
#include <map>
#include <memory>
#include <string>

#include "ir.h"

namespace ir {

/// Parses raw upstream SSE bytes into StreamEvents incrementally.  Buffers
/// only partial SSE blocks / UTF-8 tails — never the whole stream.
class StreamParser {
public:
    using EmitFn = std::function<bool(const StreamEvent &)>;  // false ⇒ client gone ⇒ abort
    virtual ~StreamParser() = default;
    /// Returns false when an EmitFn returned false (abort upstream).
    virtual bool feed(const char *data, size_t len, const EmitFn &emit) = 0;
    virtual bool finish(const EmitFn &emit) = 0;  // flush partial event, pending ToolCallDone
};

/// Serializes StreamEvents into harness SSE chunks written to a Sink.
class StreamEmitter {
public:
    using Sink = std::function<bool(const std::string &chunk)>;  // wraps sink.write
    virtual ~StreamEmitter() = default;
    virtual bool emit(const StreamEvent &ev, const Sink &sink) = 0;
    virtual bool finish(const Sink &sink) = 0;  // terminal event(s) + final usage
};

}  // namespace ir

/// A stateless codec for one wire format: JSON ↔ IR in both directions,
/// plus error-envelope conversion and stream parse/emit factories.
class FormatCodec {
public:
    virtual ~FormatCodec() = default;
    ir::ApiFormat format() const { return fmt_; }

    // Request: wire JSON → IR ; IR → wire JSON.
    virtual bool parse_request(const json &in, ir::ChatRequest &out,
                               std::string &err) const = 0;
    virtual json serialize_request(
        const ir::ChatRequest &in,
        const ir::ConversionContext *context = nullptr) const = 0;

    // Response (non-streaming): wire JSON → IR ; IR → wire JSON.
    virtual bool parse_response(const json &in, ir::ChatResponse &out,
                                std::string &err,
                                const ir::ConversionContext *context = nullptr) const = 0;
    virtual json serialize_response(
        const ir::ChatResponse &in,
        const ir::ConversionContext *context = nullptr) const = 0;

    // Error bodies: upstream wire error → normalized {message,type,code} →
    // harness wire error.
    virtual json parse_error_body(const json &upstream_err) const = 0;
    virtual json serialize_error_body(const json &normalized) const = 0;

    // Streaming: per-request instances.
    virtual std::unique_ptr<ir::StreamParser> make_stream_parser(
        const ir::ConversionContext *context = nullptr) const = 0;
    virtual std::unique_ptr<ir::StreamEmitter> make_stream_emitter(
        const ir::ConversionContext *context = nullptr) const = 0;

protected:
    explicit FormatCodec(ir::ApiFormat f) : fmt_(f) {}

private:
    ir::ApiFormat fmt_;
};

/// Registry of the three codecs, owned by ProxyServer.
class CodecRegistry {
public:
    void add(std::unique_ptr<FormatCodec> c);
    const FormatCodec &get(ir::ApiFormat f) const;

private:
    std::map<ir::ApiFormat, std::unique_ptr<FormatCodec>> codecs_;
};

std::unique_ptr<FormatCodec> make_openai_codec();
std::unique_ptr<FormatCodec> make_anthropic_codec();
std::unique_ptr<FormatCodec> make_responses_codec();
