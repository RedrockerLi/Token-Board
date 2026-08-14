#include "format_responses_internal.h"
#include "format_responses_emitter.h"

#include <map>

using namespace ir;
namespace {
class ResponsesStreamParser final : public StreamParser {
public:
    explicit ResponsesStreamParser(const ConversionContext *ctx) : context_(ctx) {}
    bool feed(const char *data, size_t len, const EmitFn &emit) override { return frames(data, len, emit, false); }
    bool finish(const EmitFn &emit) override { return frames(nullptr, 0, emit, true); }
private:
    const ConversionContext *context_ = nullptr;
    fmt::SseFrameBuffer sse_;
    std::map<int, json> pending_items_;

    const ToolMapping *mapping_for(const std::string &wire_name,
                                   const std::string &namespace_name) const {
        if (!context_) return nullptr;
        for (const auto &mapping : context_->tools.mappings) {
            if (mapping.flat_name == wire_name) return &mapping;
            if (mapping.original_name == wire_name &&
                mapping.namespace_name == namespace_name) return &mapping;
        }
        return nullptr;
    }

    void restore_tool_identity(StreamEvent &event, const json &item,
                               const std::string &wire_kind,
                               bool terminal) const {
        // A Responses target still needs the flattened wire name so the
        // Responses emitter can apply the reverse mapping itself.  Restoring
        // here would make its flat-name lookup miss the namespace/custom map.
        if (context_ && context_->target == ApiFormat::OpenAIResponses)
            return;
        const std::string wire_name = item.value("name", "");
        const std::string ns = item.value("namespace", "");
        const ToolMapping *mapping = mapping_for(wire_name, ns);
        if (!mapping) return;
        event.namespace_name = mapping->namespace_name;
        event.item.name = mapping->original_name;
        event.item.namespace_name = mapping->namespace_name;
        if (!terminal) event.arguments = mapping->original_name;
        if (mapping->kind == ToolKind::Custom) {
            event.item.item_kind = ItemKind::CustomToolCall;
            event.extra["tool_kind"] = "custom_tool_call";
        } else if (mapping->kind == ToolKind::ToolSearch) {
            event.item.item_kind = ItemKind::ToolSearchCall;
            event.extra["tool_kind"] = "tool_search_call";
        }
        (void)wire_kind;
    }

    bool frames(const char *data, size_t len, const EmitFn &emit, bool final) {
        bool ok = true;
        auto handle = [&](const std::string &frame) {
            if (!ok) return;
            std::string event, payload;
            if (!fmt::parse_sse_frame(frame, &event, &payload) || payload.empty()) return;
            json j; try { j = json::parse(payload); } catch (...) { return; }
            EmitFn downstream = [&](const StreamEvent &ev) { ok = ok && emit(ev); return ok; };
            handle_frame(j, downstream);
        };
        if (final) sse_.finish(handle); else sse_.feed(data, len, handle);
        return ok;
    }
    void handle_frame(const json &j, const EmitFn &emit) {
        const std::string type = j.value("type", "");
        if (type == "response.created") {
            StreamEvent ev; ev.type = StreamEventType::MessageStart;
            if (j.contains("response") && j["response"].is_object()) {
                const auto &r = j["response"]; ev.extra["id"] = r.value("id", "");
                ev.extra["model"] = r.value("model", "");
            }
            emit(ev); return;
        }
        if (type == "response.output_item.added") {
            if (!j.contains("item") || !j["item"].is_object()) return;
            const auto &item = j["item"]; const std::string kind = item.value("type", "");
            if (kind == "message" || kind == "reasoning") {
                pending_items_[j.value("output_index", 0)] = item;
                // The Responses target is the only target that can represent
                // an empty output Item without inventing text/tool content.
                // Reuse the legacy lifecycle event with item_kind metadata so
                // existing Chat/Anthropic emitters remain unchanged.
                if (context_ && context_->target == ApiFormat::OpenAIResponses) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallStart;
                    ev.index = j.value("output_index", 0);
                    ev.output_index = ev.index;
                    ev.item_id = item.value("id", "");
                    ev.item.id = ev.item_id;
                    ev.item.status = item.value("status", "");
                    ev.item.phase = item.value("phase", "");
                    ev.item.extra["raw_item"] = item;
                    ev.item.item_kind = kind == "message"
                        ? ItemKind::Message : ItemKind::Reasoning;
                    emit(ev);
                } else if (kind == "reasoning" && context_ &&
                           context_->target == ApiFormat::Anthropic) {
                    // An encrypted/redacted reasoning Item may have no
                    // visible summary deltas.  Still open one IR block so the
                    // Anthropic emitter can carry the opaque envelope.
                    StreamEvent ev;
                    ev.type = StreamEventType::ContentThinkingDelta;
                    ev.index = j.value("output_index", 0);
                    ev.item_id = item.value("id", "");
                    ev.item.id = ev.item_id;
                    ev.item.status = item.value("status", "");
                    ev.item.phase = item.value("phase", "");
                    ev.item.item_kind = ItemKind::Reasoning;
                    ev.item.extra["raw_item"] = item;
                    emit(ev);
                }
                return;
            }
            if (kind != "function_call" && kind != "custom_tool_call" && kind != "tool_search_call") {
                if (context_ && context_->target == ApiFormat::OpenAIResponses) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallStart;
                    ev.index = j.value("output_index", 0);
                    ev.output_index = ev.index;
                    ev.item_id = item.value("id", "");
                    ev.item.id = ev.item_id;
                    ev.item.status = item.value("status", "");
                    ev.item.phase = item.value("phase", "");
                    ev.item.item_kind = ItemKind::Opaque;
                    ev.item.extra["raw_item"] = item;
                    emit(ev);
                    return;
                }
                StreamEvent error;
                error.type = StreamEventType::ErrorEvent;
                error.extra["error"] = json{
                    {"type", "unsupported_feature"},
                    {"code", "unsupported_item"},
                    {"message", "Responses output Item type cannot be converted"},
                    {"item_type", kind}};
                emit(error);
                return;
            }
            StreamEvent ev; ev.type = StreamEventType::ToolCallStart; ev.index = j.value("output_index", 0); ev.output_index = ev.index;
            ev.text = item.value("call_id", ""); ev.arguments = item.value("name", ""); ev.item_id = item.value("id", "");
            ev.call_id = ev.text;
            ev.item.id = ev.item_id; ev.item.call_id = ev.text; ev.item.name = ev.arguments;
            ev.item.status = item.value("status", "");
            ev.item.phase = item.value("phase", "");
            ev.item.namespace_name = item.value("namespace", "");
            ev.item.execution = item.value("execution", json::object());
            ev.item.item_kind = kind == "custom_tool_call" ? ItemKind::CustomToolCall : kind == "tool_search_call" ? ItemKind::ToolSearchCall : ItemKind::FunctionCall;
            ev.item.extra["raw_item"] = item; ev.extra["tool_kind"] = kind;
            restore_tool_identity(ev, item, kind, false);
            emit(ev); return;
        }
        if (type == "response.output_text.delta" || type == "response.refusal.delta") {
            StreamEvent ev; ev.type = StreamEventType::ContentTextDelta; ev.index = j.value("output_index", 0); ev.output_index = ev.index; ev.content_index = j.value("content_index", 0);
            ev.item_id = j.value("item_id", ""); ev.text = j.value("delta", "");
            if (const auto it = pending_items_.find(ev.output_index); it != pending_items_.end()) {
                ev.item.id = it->second.value("id", ev.item_id);
                ev.item.status = it->second.value("status", "");
                ev.item.phase = it->second.value("phase", "");
                ev.item.item_kind = ItemKind::Message;
                ev.item.extra["raw_item"] = it->second;
            }
            emit(ev); return;
        }
        if (type == "response.reasoning_text.delta" || type == "response.reasoning_summary_text.delta") {
            StreamEvent ev; ev.type = StreamEventType::ContentThinkingDelta; ev.index = j.value("output_index", 0); ev.output_index = ev.index; ev.summary_index = j.value("summary_index", 0);
            ev.item_id = j.value("item_id", ""); ev.text = j.value("delta", "");
            if (const auto it = pending_items_.find(ev.output_index); it != pending_items_.end()) {
                ev.item.id = it->second.value("id", ev.item_id);
                ev.item.status = it->second.value("status", "");
                ev.item.phase = it->second.value("phase", "");
                ev.item.item_kind = ItemKind::Reasoning;
                ev.item.extra["raw_item"] = it->second;
            }
            emit(ev); return;
        }
        if (type == "response.function_call_arguments.delta" || type == "response.custom_tool_call_input.delta") {
            // A native Responses custom input is a raw string, while Chat and
            // Anthropic compatibility functions require the {input:string}
            // envelope.  Hold native custom deltas and emit one valid JSON
            // fragment when the terminal Item arrives.
            if (type == "response.custom_tool_call_input.delta" && context_ &&
                context_->target != ApiFormat::OpenAIResponses)
                return;
            StreamEvent ev; ev.type = StreamEventType::ToolCallArgumentDelta; ev.index = j.value("output_index", 0); ev.output_index = ev.index;
            ev.item_id = j.value("item_id", ""); ev.arguments = j.value("delta", ""); emit(ev); return;
        }
        if (type == "response.output_item.done") {
            if (!j.contains("item") || !j["item"].is_object()) return;
            const auto &item = j["item"]; const std::string kind = item.value("type", "");
            if (kind == "message" || kind == "reasoning") {
                if (context_ && context_->target == ApiFormat::OpenAIResponses) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallDone;
                    ev.index = j.value("output_index", 0);
                    ev.output_index = ev.index;
                    ev.item_id = item.value("id", "");
                    ev.item.id = ev.item_id;
                    ev.item.status = item.value("status", "");
                    ev.item.phase = item.value("phase", "");
                    ev.item.extra["raw_item"] = item;
                    ev.item.item_kind = kind == "message"
                        ? ItemKind::Message : ItemKind::Reasoning;
                    emit(ev);
                }
                return;
            }
            if (kind != "function_call" && kind != "custom_tool_call" && kind != "tool_search_call") {
                if (context_ && context_->target == ApiFormat::OpenAIResponses) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallDone;
                    ev.index = j.value("output_index", 0);
                    ev.output_index = ev.index;
                    ev.item_id = item.value("id", "");
                    ev.item.id = ev.item_id;
                    ev.item.status = item.value("status", "");
                    ev.item.phase = item.value("phase", "");
                    ev.item.item_kind = ItemKind::Opaque;
                    ev.item.extra["raw_item"] = item;
                    emit(ev);
                    return;
                }
                StreamEvent error;
                error.type = StreamEventType::ErrorEvent;
                error.extra["error"] = json{
                    {"type", "unsupported_feature"},
                    {"code", "unsupported_item"},
                    {"message", "Responses output Item type cannot be converted"},
                    {"item_type", kind}};
                emit(error);
                return;
            }
            StreamEvent ev; ev.type = StreamEventType::ToolCallDone; ev.index = j.value("output_index", 0); ev.output_index = ev.index;
            ev.item_id = item.value("id", ""); ev.item.id = ev.item_id; ev.item.call_id = item.value("call_id", "");
            ev.call_id = ev.item.call_id;
            ev.item.name = item.value("name", ""); ev.item.item_kind = kind == "custom_tool_call" ? ItemKind::CustomToolCall : kind == "tool_search_call" ? ItemKind::ToolSearchCall : ItemKind::FunctionCall;
            ev.item.status = item.value("status", "");
            ev.item.phase = item.value("phase", "");
            ev.item.namespace_name = item.value("namespace", "");
            ev.item.execution = item.value("execution", json::object());
            ev.arguments = kind == "custom_tool_call" ? item.value("input", "") : item.value("arguments", "");
            const std::string payload = ev.arguments;
            ev.item.payload = payload; ev.item.extra["raw_item"] = item; ev.extra["tool_kind"] = kind;
            restore_tool_identity(ev, item, kind, true);
            if (context_ && context_->target != ApiFormat::OpenAIResponses &&
                kind == "custom_tool_call") {
                ev.arguments = json{{"input", payload}}.dump();
                StreamEvent delta;
                delta.type = StreamEventType::ToolCallArgumentDelta;
                delta.index = ev.index;
                delta.output_index = ev.output_index;
                delta.item_id = ev.item_id;
                delta.arguments = ev.arguments;
                emit(delta);
            } else {
                ev.arguments = payload;
            }
            ev.item.payload = ev.arguments;
            emit(ev); return;
        }
        if (type == "response.completed" || type == "response.incomplete") {
            if (!j.contains("response") || !j["response"].is_object()) return;
            const auto &r = j["response"]; StreamEvent fin; fin.type = StreamEventType::MessageFinish;
            fin.stop_reason = fmt::responses_status_to_stop(r.value("status", type)); emit(fin);
            if (r.contains("usage") && r["usage"].is_object()) { StreamEvent u; u.type = StreamEventType::UsageEvent; u.usage = fmt::parse_usage_json(r["usage"]); emit(u); }
            return;
        }
        if (type == "response.failed" || type == "response.error" || type == "error") {
            StreamEvent ev; ev.type = StreamEventType::ErrorEvent;
            if (j.contains("response") && j["response"].is_object()) ev.extra["error"] = j["response"].value("error", json{{"message", "upstream error"}});
            else ev.extra["error"] = j.value("error", json{{"message", "upstream error"}});
            emit(ev);
        }
    }
};
}

std::unique_ptr<ir::StreamParser> make_responses_stream_parser_impl(const ir::ConversionContext *ctx) { return std::make_unique<ResponsesStreamParser>(ctx); }
std::unique_ptr<ir::StreamEmitter> make_responses_stream_emitter_impl(const ir::ConversionContext *ctx) { return std::make_unique<ResponsesStreamEmitter>(ctx); }
