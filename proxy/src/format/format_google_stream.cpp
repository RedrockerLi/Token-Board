#include "format_google_internal.h"

#include <limits>

using namespace ir;

namespace {

/// Gemini's prompt count already includes cached content, so only an Anthropic
/// upstream (whose input bucket is net of both cache buckets) needs folding to
/// make the client-visible numbers use this wire's convention.
ir::Usage client_usage_view(const ir::Usage &usage,
                           const ir::ConversionContext *context) {
    ir::Usage view = usage;
    if (context && context->source == ir::ApiFormat::Anthropic) {
        const long long prompt = static_cast<long long>(view.prompt_tokens) +
                                 view.cache_read_tokens +
                                 view.cache_creation_tokens;
        view.prompt_tokens = prompt >= 0 &&
                prompt <= std::numeric_limits<int>::max()
            ? static_cast<int>(prompt) : 0;
        const long long total = static_cast<long long>(view.prompt_tokens) +
                                view.completion_tokens;
        view.total_tokens = total >= 0 &&
                total <= std::numeric_limits<int>::max()
            ? static_cast<int>(total) : 0;
    }
    return view;
}

const json *first_candidate(const json &payload) {
    if (!payload.contains("candidates") || !payload["candidates"].is_array() ||
        payload["candidates"].empty() || !payload["candidates"][0].is_object())
        return nullptr;
    return &payload["candidates"][0];
}

class GoogleStreamParser final : public StreamParser {
public:
    explicit GoogleStreamParser(const ConversionContext *context)
        : context_(context) {}

    bool feed(const char *data, size_t len, const EmitFn &emit) override {
        aborted_ = false;
        const bool ok = frames_.feed(data, len, [&](const std::string &frame) {
            if (!aborted_) handle_frame(frame, emit);
        });
        return ok && !aborted_;
    }

    bool finish(const EmitFn &emit) override {
        aborted_ = false;
        frames_.finish([&](const std::string &frame) {
            if (!aborted_) handle_frame(frame, emit);
        });
        if (!terminated_) {
            // The upstream closed without a finishReason: report the absence
            // truthfully instead of inventing a completion reason.
            StreamEvent finish;
            finish.type = StreamEventType::MessageFinish;
            finish.stop_reason = StopReason::Absent;
            if (!prompt_feedback_.is_null())
                finish.extra["prompt_feedback"] = prompt_feedback_;
            terminated_ = true;
            emit(finish);
        }
        return !aborted_;
    }

private:
    void handle_frame(const std::string &frame, const EmitFn &emit) {
        std::string event_name;
        std::string data;
        if (!fmt::parse_sse_frame(frame, &event_name, &data) || data.empty())
            return;
        const json payload = json::parse(data, nullptr, false);
        if (payload.is_discarded() || !payload.is_object()) return;
        handle_payload(payload, emit);
    }

    void ensure_started(const EmitFn &emit) {
        if (started_) return;
        started_ = true;
        StreamEvent start;
        start.type = StreamEventType::MessageStart;
        start.extra["model"] = model_;
        start.extra["id"] = response_id_;
        if (!emit(start)) aborted_ = true;
    }

    void handle_payload(const json &payload, const EmitFn &emit) {
        if (payload.contains("responseId") && payload["responseId"].is_string())
            response_id_ = payload["responseId"].get<std::string>();
        if (payload.contains("modelVersion") && payload["modelVersion"].is_string())
            model_ = payload["modelVersion"].get<std::string>();
        if (payload.contains("promptFeedback") &&
            payload["promptFeedback"].is_object())
            prompt_feedback_ = payload["promptFeedback"];

        const json *candidate = first_candidate(payload);
        if (candidate != nullptr) emit_candidate(*candidate, emit);
        if (aborted_) return;

        if (payload.contains("usageMetadata") &&
            payload["usageMetadata"].is_object()) {
            ensure_started(emit);
            if (aborted_) return;
            StreamEvent usage;
            usage.type = StreamEventType::UsageEvent;
            gfmt::usage_from_wire(payload["usageMetadata"], usage.usage);
            if (!emit(usage)) aborted_ = true;
        }
    }

    void emit_candidate(const json &candidate, const EmitFn &emit) {
        if (candidate.contains("content") &&
            candidate["content"].is_object() &&
            candidate["content"].contains("parts") &&
            candidate["content"]["parts"].is_array()) {
            for (const auto &part : candidate["content"]["parts"]) {
                if (aborted_ || !part.is_object()) continue;
                emit_part(part, emit);
            }
        }
        if (aborted_) return;
        if (!candidate.contains("finishReason") ||
            !candidate["finishReason"].is_string())
            return;
        ensure_started(emit);
        if (aborted_) return;
        StreamEvent finish;
        finish.type = StreamEventType::MessageFinish;
        finish.stop_reason =
            gfmt::finish_reason_from_wire(candidate, saw_tool_call_);
        if (candidate.contains("safetyRatings"))
            finish.extra["safetyRatings"] = candidate["safetyRatings"];
        if (candidate.contains("finishMessage") &&
            candidate["finishMessage"].is_string())
            finish.extra["finish_message"] = candidate["finishMessage"];
        if (!prompt_feedback_.is_null())
            finish.extra["prompt_feedback"] = prompt_feedback_;
        terminated_ = true;
        if (!emit(finish)) aborted_ = true;
    }

    void emit_part(const json &part, const EmitFn &emit) {
        if (part.contains("functionCall") && part["functionCall"].is_object()) {
            emit_tool_call(part, emit);
            return;
        }
        if (!part.contains("text") || !part["text"].is_string()) {
            // Streamed inlineData/fileData and provider-executed parts have no
            // delta representation; a completed response carries them instead.
            return;
        }
        const std::string text = part["text"].get<std::string>();
        if (text.empty()) return;
        const bool thought = part.value("thought", false);
        ensure_started(emit);
        if (aborted_) return;
        StreamEvent delta;
        delta.type = thought ? StreamEventType::ContentThinkingDelta
                             : StreamEventType::ContentTextDelta;
        delta.text = text;
        if (part.contains("thoughtSignature") &&
            part["thoughtSignature"].is_string())
            delta.extra["thoughtSignature"] = part["thoughtSignature"];
        if (!emit(delta)) aborted_ = true;
    }

    void emit_tool_call(const json &part, const EmitFn &emit) {
        const json &call = part["functionCall"];
        std::string id;
        if (call.contains("id") && call["id"].is_string())
            id = call["id"].get<std::string>();
        if (id.empty()) id = fmt::generate_call_id();
        const std::string name =
            gfmt::client_tool_name(context_, call.value("name", ""));
        json arguments = json::object();
        if (call.contains("args") && call["args"].is_object())
            arguments = call["args"];

        ensure_started(emit);
        if (aborted_) return;
        saw_tool_call_ = true;
        const int index = tool_index_++;

        StreamEvent start;
        start.type = StreamEventType::ToolCallStart;
        start.index = index;
        start.text = id;
        start.arguments = name;
        start.call_id = id;
        start.item.name = name;
        start.item.item_kind = ItemKind::FunctionCall;
        if (part.contains("thoughtSignature") &&
            part["thoughtSignature"].is_string())
            start.extra["thoughtSignature"] = part["thoughtSignature"];
        if (!emit(start)) { aborted_ = true; return; }

        StreamEvent delta;
        delta.type = StreamEventType::ToolCallArgumentDelta;
        delta.index = index;
        delta.arguments = arguments.dump();
        if (!emit(delta)) { aborted_ = true; return; }

        StreamEvent done;
        done.type = StreamEventType::ToolCallDone;
        done.index = index;
        done.text = id;
        done.call_id = id;
        done.arguments = arguments.dump();
        if (!emit(done)) aborted_ = true;
    }

    const ConversionContext *context_ = nullptr;
    fmt::SseFrameBuffer frames_;
    bool started_ = false;
    bool terminated_ = false;
    bool aborted_ = false;
    bool saw_tool_call_ = false;
    int tool_index_ = 0;
    std::string model_;
    std::string response_id_;
    json prompt_feedback_ = json();
};

class GoogleStreamEmitter final : public StreamEmitter {
public:
    explicit GoogleStreamEmitter(const ConversionContext *context)
        : context_(context) {}

    bool emit(const StreamEvent &event, const Sink &sink) override {
        switch (event.type) {
            case StreamEventType::MessageStart:
                remember_model(event);
                return true;
            case StreamEventType::ContentTextDelta:
                return write_part(json{{"text", event.text}}, sink);
            case StreamEventType::ContentThinkingDelta: {
                json part;
                part["text"] = event.text;
                part["thought"] = true;
                if (event.extra.contains("thoughtSignature"))
                    part["thoughtSignature"] = event.extra["thoughtSignature"];
                return write_part(std::move(part), sink);
            }
            case StreamEventType::ToolCallStart: {
                PendingCall &call = pending_[event.index];
                call.id = event.text;
                call.name = event.item.name.empty() ? event.arguments
                                                    : event.item.name;
                if (event.extra.contains("thoughtSignature"))
                    call.signature = event.extra["thoughtSignature"];
                return true;
            }
            case StreamEventType::ToolCallArgumentDelta: {
                pending_[event.index].arguments += event.arguments;
                return true;
            }
            case StreamEventType::ToolCallDone:
                return write_tool_call(event, sink);
            case StreamEventType::MessageFinish:
                finish_reason_ = event.stop_reason;
                if (event.extra.contains("safetyRatings"))
                    safety_ratings_ = event.extra["safetyRatings"];
                if (event.extra.contains("finish_message"))
                    finish_message_ = event.extra["finish_message"];
                return true;
            case StreamEventType::UsageEvent:
                usage_merge(usage_, event.usage);
                return true;
            case StreamEventType::ErrorEvent: {
                json error = event.extra.contains("error")
                    ? event.extra["error"] : json::object();
                const json frame = json{{"error", std::move(error)}};
                terminal_ = true;
                return sink("data: " + frame.dump() + "\n\n");
            }
        }
        return true;
    }

    bool finish(const Sink &sink) override {
        if (terminal_) return true;
        terminal_ = true;
        json candidate;
        candidate["index"] = 0;
        if (const char *reason = gfmt::finish_reason_to_wire(finish_reason_))
            candidate["finishReason"] = reason;
        if (!finish_message_.empty())
            candidate["finishMessage"] = finish_message_;
        if (!safety_ratings_.is_null())
            candidate["safetyRatings"] = safety_ratings_;
        json frame;
        frame["candidates"] = json::array({candidate});
        write_metadata(frame);
        if (usage_.prompt_tokens > 0 || usage_.completion_tokens > 0 ||
            usage_.cache_read_tokens > 0)
            frame["usageMetadata"] =
                gfmt::usage_to_wire(client_usage_view(usage_, context_));
        return sink("data: " + frame.dump() + "\n\n");
    }

private:
    struct PendingCall {
        std::string id;
        std::string name;
        std::string arguments;
        std::string signature;
    };

    void remember_model(const StreamEvent &event) {
        if (event.extra.contains("model") && event.extra["model"].is_string())
            model_ = event.extra["model"].get<std::string>();
        if (event.extra.contains("id") && event.extra["id"].is_string())
            response_id_ = event.extra["id"].get<std::string>();
    }

    void write_metadata(json &frame) const {
        if (!model_.empty()) frame["modelVersion"] = model_;
        if (!response_id_.empty()) frame["responseId"] = response_id_;
    }

    bool write_part(json part, const Sink &sink) {
        json candidate;
        candidate["index"] = 0;
        candidate["content"] = json{
            {"role", "model"}, {"parts", json::array({std::move(part)})}};
        json frame;
        frame["candidates"] = json::array({std::move(candidate)});
        write_metadata(frame);
        return sink("data: " + frame.dump() + "\n\n");
    }

    bool write_tool_call(const StreamEvent &event, const Sink &sink) {
        PendingCall &call = pending_[event.index];
        std::string payload = event.arguments.empty() ? call.arguments
                                                     : event.arguments;
        json arguments = json::parse(payload, nullptr, false);
        if (arguments.is_discarded() || !arguments.is_object())
            arguments = json{{"input", payload}};
        json function_call;
        if (!call.id.empty()) function_call["id"] = call.id;
        function_call["name"] = call.name;
        function_call["args"] = std::move(arguments);
        json part;
        part["functionCall"] = std::move(function_call);
        if (!call.signature.empty())
            part["thoughtSignature"] = call.signature;
        pending_.erase(event.index);
        return write_part(std::move(part), sink);
    }

    const ConversionContext *context_ = nullptr;
    bool terminal_ = false;
    StopReason finish_reason_ = StopReason::Absent;
    ir::Usage usage_;
    std::string model_;
    std::string response_id_;
    std::string finish_message_;
    json safety_ratings_ = json();
    std::map<int, PendingCall> pending_;
};

}  // namespace

std::unique_ptr<ir::StreamParser> gfmt::make_stream_parser_impl(
    const ir::ConversionContext *context) {
    return std::make_unique<GoogleStreamParser>(context);
}

std::unique_ptr<ir::StreamEmitter> gfmt::make_stream_emitter_impl(
    const ir::ConversionContext *context) {
    return std::make_unique<GoogleStreamEmitter>(context);
}
