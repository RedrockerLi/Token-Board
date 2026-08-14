#include "format_common.h"
#include "format_responses.h"
#include "format_openai.h"
#include "reasoning_bridge.h"

#include <cassert>
#include <cctype>
#include <vector>

int main() {
    auto responses = make_responses_codec();
    json request{
        {"model", "gpt-test"},
        {"input", json::array({
            json{{"type", "message"}, {"id", "in_1"}, {"role", "user"},
                 {"content", json::array({json{{"type", "input_text"}, {"text", "hello"}}})}},
            json{{"type", "reasoning"}, {"id", "rs_1"}, {"status", "completed"},
                 {"summary", json::array({json{{"type", "summary_text"}, {"text", "think"}}})}},
            json{{"type", "mystery_item"}, {"id", "opaque_1"}},
            json{{"type", "function_call"}, {"id", "fc_1"}, {"call_id", "call_1"},
                 {"name", "f"}, {"arguments", "{\"x\":1}"}}
        })},
        {"text", json{{"format", json{{"type", "json_object"}}}}}
    };
    ir::ChatRequest parsed;
    std::string error;
    assert(responses->parse_request(request, parsed, error));
    assert(parsed.items.size() == 4);
    assert(parsed.items[0].id == "in_1" && parsed.items[1].item_kind == ir::ItemKind::Reasoning);
    assert(parsed.items[2].item_kind == ir::ItemKind::Opaque);
    json roundtrip = responses->serialize_request(parsed);
    assert(roundtrip["input"].size() == 4);
    for (std::size_t i = 0; i < 4; ++i) assert(roundtrip["input"][i].value("id", "") == request["input"][i].value("id", ""));
    assert(roundtrip["text"]["format"]["type"] == "json_object");
    json reasoning_request = request;
    reasoning_request["reasoning"] = json{{"effort", "high"}, {"custom_control", true}};
    reasoning_request["include"] = json::array({"foo"});
    ir::ChatRequest parsed_reasoning_request;
    assert(responses->parse_request(reasoning_request, parsed_reasoning_request, error));
    json reasoning_roundtrip = responses->serialize_request(parsed_reasoning_request);
    assert(reasoning_roundtrip["reasoning"]["custom_control"] == true);
    assert(reasoning_roundtrip["reasoning"]["effort"] == "high");
    assert(reasoning_roundtrip["include"].size() == 2);
    assert(reasoning_roundtrip["include"][0] == "foo");
    assert(reasoning_roundtrip["include"][1] == "reasoning.encrypted_content");
    auto chat = make_openai_codec();
    ir::ConversionContext chat_context;
    json chat_request = chat->serialize_request(parsed, &chat_context);
    assert(chat_request["response_format"]["type"] == "json_object");
    assert(!chat_request.contains("text"));

    ir::ChatRequest chat_reasoning;
    assert(chat->parse_request(
        json{{"model", "gpt-test"}, {"messages", json::array({
            json{{"role", "assistant"}, {"reasoning_content", "visible plan"},
                 {"content", "answer"}}
        })}}, chat_reasoning, error));
    json responses_reasoning_request = responses->serialize_request(chat_reasoning);
    assert(responses_reasoning_request["input"].size() == 2);
    assert(responses_reasoning_request["input"][0]["type"] == "reasoning");
    assert(responses_reasoning_request["input"][0]["summary"][0]["text"] == "visible plan");
    assert(responses_reasoning_request["input"][1]["type"] == "message");
    assert(!responses_reasoning_request["input"][0].contains("encrypted_content"));

    json schema_request{
        {"model", "gpt-test"},
        {"text", json{{"format", json{
            {"type", "json_schema"}, {"name", "answer"},
            {"description", "An answer"},
            {"schema", json{{"type", "object"}, {"properties", json{{"ok", json{{"type", "boolean"}}}}}}},
            {"strict", true}
        }}}}
    };
    ir::ChatRequest schema_ir;
    assert(responses->parse_request(schema_request, schema_ir, error));
    json schema_chat = chat->serialize_request(schema_ir);
    assert(schema_chat["response_format"]["type"] == "json_schema");
    assert(schema_chat["response_format"]["json_schema"]["name"] == "answer");
    assert(schema_chat["response_format"]["json_schema"]["schema"]["type"] == "object");
    assert(schema_chat["response_format"]["json_schema"]["strict"] == true);
    ir::ChatRequest schema_chat_ir;
    assert(chat->parse_request(schema_chat, schema_chat_ir, error));
    json schema_responses = responses->serialize_request(schema_chat_ir);
    assert(schema_responses["text"]["format"]["type"] == "json_schema");
    assert(schema_responses["text"]["format"]["name"] == "answer");
    assert(schema_responses["text"]["format"]["schema"]["type"] == "object");

    ir::Tool ns;
    ns.kind = ir::ToolKind::Namespace; ns.name = "namespace_with_a_very_long_name_that_exceeds_the_limit";
    ir::Tool child; child.kind = ir::ToolKind::Function; child.name = "child_with_a_very_long_name_that_exceeds_the_limit";
    ns.children.push_back(child);
    ir::ToolContext tools; assert(fmt::build_tool_context({ns}, tools, error));
    assert(tools.target_tools.size() == 1 && tools.target_tools[0].name.size() == 64);
    assert(tools.target_tools[0].name.find("__") != std::string::npos);
    ir::Tool collision; collision.name = tools.target_tools[0].name;
    ir::ToolContext ignored; assert(!fmt::build_tool_context({ns, collision}, ignored, error));

    json reasoning{{"type", "reasoning"}, {"id", "rs"}, {"status", "completed"},
                   {"encrypted_content", "cipher"},
                   {"summary", json::array({json{{"type", "summary_text"}, {"text", "visible"}}})}};
    json block = fmt::anthropic_block_from_responses_reasoning(reasoning);
    assert(block["type"] == "thinking" && block["signature"].get<std::string>().find("token-board-openai-reasoning-v1:") == 0);
    json restored = fmt::responses_reasoning_from_anthropic_block(block);
    assert(restored == reasoning);

    ir::ChatResponse response;
    response.model = "gpt-test";
    ir::AgentItem item; item.item_kind = ir::ItemKind::FunctionCall; item.name = "f"; item.payload = "{\"x\":1}";
    response.output_items.push_back(item);
    ir::ConversionContext context; context.generated_response_id = "resp_tb_test";
    json serialized = responses->serialize_response(response, &context);
    assert(serialized["id"] == "resp_tb_test");
    assert(serialized["output"][0]["id"].get<std::string>().find("resp_tb_test_item_") == 0);
    assert(serialized["output"][0]["call_id"].get<std::string>().find("call_tb_") == 0);
    assert(serialized["output"][0]["arguments"].is_string());

    ir::ChatRequest grouped;
    ir::AgentItem thinking; thinking.role = "assistant"; thinking.group_id = 7;
    thinking.item_kind = ir::ItemKind::Reasoning;
    thinking.content.push_back({ir::ContentKind::Thinking, "plan"});
    ir::AgentItem text_item; text_item.role = "assistant"; text_item.group_id = 7;
    text_item.content.push_back({ir::ContentKind::Text, "answer"});
    ir::AgentItem grouped_call; grouped_call.role = "assistant";
    grouped_call.group_id = 7; grouped_call.item_kind = ir::ItemKind::FunctionCall;
    ir::ContentBlock call_block; call_block.kind = ir::ContentKind::ToolUse;
    call_block.tool_call_id = "call-group"; call_block.tool_name = "f";
    call_block.tool_input = json{{"x", 1}};
    grouped_call.content.push_back(call_block);
    grouped.items = {thinking, text_item, grouped_call};
    json grouped_chat = chat->serialize_request(grouped);
    assert(grouped_chat["messages"].size() == 1);
    assert(grouped_chat["messages"][0]["reasoning_content"] == "plan");
    assert(grouped_chat["messages"][0]["tool_calls"].size() == 1);
    auto anthropic = make_anthropic_codec();
    json grouped_anthropic = anthropic->serialize_request(grouped);
    assert(grouped_anthropic["messages"].size() == 1);
    assert(grouped_anthropic["messages"][0]["content"].size() == 3);

    json adapter_request{
        {"model", "gpt-test"},
        {"tools", json::array({
            json{{"type", "custom"}, {"name", "patch"}, {"description", "p"}},
            json{{"type", "namespace"}, {"name", "ns"},
                 {"tools", json::array({json{{"type", "function"},
                                               {"name", "run"}}})}}
        })}
    };
    ir::ChatRequest adapter_ir;
    assert(responses->parse_request(adapter_request, adapter_ir, error));
    ir::ConversionContext adapter_context;
    adapter_context.source = ir::ApiFormat::OpenAIResponses;
    adapter_context.target = ir::ApiFormat::OpenAIResponses;
    assert(fmt::build_tool_context(adapter_ir.tools, adapter_context.tools, error));
    adapter_ir.tools = adapter_context.tools.target_tools;
    json adapted = responses->serialize_request(adapter_ir, &adapter_context);
    assert(adapted["tools"][0]["type"] == "custom");
    assert(adapted["tools"][1]["type"] == "function");
    assert(adapted["tools"][1]["name"] == "ns__run");
    ir::ChatResponse adapted_response;
    assert(responses->parse_response(
        json{{"id", "upstream"}, {"status", "completed"},
             {"output", json::array({json{{"type", "function_call"},
                                             {"id", "fc"}, {"call_id", "cc"},
                                             {"name", "ns__run"},
                                             {"arguments", "{\"x\":1}"}}})}},
        adapted_response, error, &adapter_context));
    json restored_response = responses->serialize_response(adapted_response,
                                                             &adapter_context);
    assert(restored_response["output"][0]["name"] == "run");
    assert(restored_response["output"][0]["namespace"] == "ns");

    ir::ToolContext stream_tools;
    stream_tools.mappings.push_back({"lookup", "lookup", "", ir::ToolKind::ToolSearch});
    stream_tools.mappings.push_back({"patch", "patch", "", ir::ToolKind::Custom});
    ir::ConversionContext stream_tool_context;
    stream_tool_context.source = ir::ApiFormat::OpenAI;
    stream_tool_context.target = ir::ApiFormat::OpenAIResponses;
    stream_tool_context.tools = stream_tools;
    auto tool_emitter = responses->make_stream_emitter(&stream_tool_context);
    std::vector<std::string> tool_frames;
    auto tool_sink = [&](const std::string &frame) { tool_frames.push_back(frame); return true; };
    ir::StreamEvent tool_start; tool_start.type = ir::StreamEventType::ToolCallStart;
    tool_start.index = 1; tool_start.text = "search-id"; tool_start.arguments = "lookup";
    tool_emitter->emit(tool_start, tool_sink);
    ir::StreamEvent tool_delta; tool_delta.type = ir::StreamEventType::ToolCallArgumentDelta;
    tool_delta.index = 1; tool_delta.arguments = "{\"query\":\"q\"}";
    tool_emitter->emit(tool_delta, tool_sink);
    ir::StreamEvent tool_done; tool_done.type = ir::StreamEventType::ToolCallDone;
    tool_done.index = 1; tool_done.arguments = "{\"query\":\"q\"}";
    tool_emitter->emit(tool_done, tool_sink);
    tool_emitter->finish(tool_sink);
    bool saw_search_delta = false, saw_search_item = false;
    for (const auto &frame : tool_frames) {
        saw_search_delta = saw_search_delta || frame.find("function_call_arguments.delta") != std::string::npos;
        saw_search_item = saw_search_item || frame.find("tool_search_call") != std::string::npos;
    }
    assert(!saw_search_delta && saw_search_item);

    auto custom_emitter = responses->make_stream_emitter(&stream_tool_context);
    std::vector<std::string> custom_frames;
    auto custom_sink = [&](const std::string &frame) { custom_frames.push_back(frame); return true; };
    ir::StreamEvent custom_start; custom_start.type = ir::StreamEventType::ToolCallStart;
    custom_start.index = 2; custom_start.text = "custom-id"; custom_start.arguments = "patch";
    custom_emitter->emit(custom_start, custom_sink);
    ir::StreamEvent custom_delta; custom_delta.type = ir::StreamEventType::ToolCallArgumentDelta;
    custom_delta.index = 2; custom_delta.arguments = "{\"input\":\"abc\"}";
    custom_emitter->emit(custom_delta, custom_sink);
    ir::StreamEvent custom_done; custom_done.type = ir::StreamEventType::ToolCallDone;
    custom_done.index = 2; custom_done.arguments = "{\"input\":\"abc\"}";
    custom_emitter->emit(custom_done, custom_sink);
    custom_emitter->finish(custom_sink);
    bool saw_custom_input = false, saw_custom_result = false;
    for (const auto &frame : custom_frames) {
        saw_custom_input = saw_custom_input || frame.find("custom_tool_call_input.delta") != std::string::npos;
        saw_custom_result = saw_custom_result || frame.find("\"input\":\"abc\"") != std::string::npos;
    }
    assert(saw_custom_input && saw_custom_result);

    ir::ConversionContext stream_context;
    stream_context.generated_response_id = "resp_tb_stream";
    auto emitter = responses->make_stream_emitter(&stream_context);
    std::vector<std::string> frames;
    auto sink = [&](const std::string &frame) { frames.push_back(frame); return true; };
    ir::StreamEvent start; start.type = ir::StreamEventType::MessageStart;
    start.extra["id"] = "upstream-id"; emitter->emit(start, sink);
    ir::StreamEvent delta; delta.type = ir::StreamEventType::ContentTextDelta; delta.text = "x";
    emitter->emit(delta, sink);
    ir::StreamEvent finish; finish.type = ir::StreamEventType::MessageFinish;
    finish.stop_reason = ir::StopReason::Stop; emitter->emit(finish, sink);
    emitter->finish(sink);
    assert(frames.size() >= 5);
    assert(frames[0].find("resp_tb_stream") != std::string::npos);
    assert(frames[2].find("resp_tb_stream_0") != std::string::npos);
    assert(frames.back().find("resp_tb_stream_0") != std::string::npos);

    // A Responses upstream's explicit output indexes and item ids must survive
    // a same-protocol adapter even when a later item emits its delta first.
    ir::ConversionContext native_stream_context;
    native_stream_context.source = ir::ApiFormat::OpenAIResponses;
    native_stream_context.target = ir::ApiFormat::OpenAIResponses;
    auto native_emitter = responses->make_stream_emitter(&native_stream_context);
    std::vector<std::string> native_frames;
    auto native_sink = [&](const std::string &frame) {
        native_frames.push_back(frame); return true;
    };
    ir::StreamEvent native_start;
    native_start.type = ir::StreamEventType::MessageStart;
    native_start.extra["id"] = "native-response";
    native_emitter->emit(native_start, native_sink);
    ir::StreamEvent native_tool;
    native_tool.type = ir::StreamEventType::ToolCallStart;
    native_tool.index = 3; native_tool.item_id = "native-tool-item";
    native_tool.text = "call-native"; native_tool.arguments = "f";
    native_tool.item.status = "in_progress";
    native_emitter->emit(native_tool, native_sink);
    ir::StreamEvent native_text;
    native_text.type = ir::StreamEventType::ContentTextDelta;
    native_text.index = 1; native_text.output_index = 1;
    native_text.item_id = "native-text-item"; native_text.text = "text";
    native_text.item.status = "in_progress";
    native_emitter->emit(native_text, native_sink);
    ir::StreamEvent native_finish;
    native_finish.type = ir::StreamEventType::MessageFinish;
    native_finish.stop_reason = ir::StopReason::Stop;
    native_emitter->emit(native_finish, native_sink);
    native_emitter->finish(native_sink);
    bool preserved_tool = false, preserved_text = false;
    for (const auto &frame : native_frames) {
        preserved_tool = preserved_tool ||
            frame.find("native-tool-item") != std::string::npos &&
            frame.find("\"output_index\":3") != std::string::npos;
        preserved_text = preserved_text || frame.find("native-text-item") != std::string::npos &&
            frame.find("\"output_index\":1") != std::string::npos;
    }
    assert(preserved_tool && preserved_text);

    auto empty_emitter = responses->make_stream_emitter(&native_stream_context);
    std::vector<std::string> empty_frames;
    auto empty_sink = [&](const std::string &frame) {
        empty_frames.push_back(frame); return true;
    };
    ir::StreamEvent empty_start;
    empty_start.type = ir::StreamEventType::ToolCallStart;
    empty_start.index = 5; empty_start.item_id = "empty-message";
    empty_start.item.item_kind = ir::ItemKind::Message;
    empty_start.item.extra["raw_item"] = json{{"type", "message"}};
    empty_emitter->emit(empty_start, empty_sink);
    ir::StreamEvent empty_done = empty_start;
    empty_done.type = ir::StreamEventType::ToolCallDone;
    empty_done.item.status = "completed";
    empty_emitter->emit(empty_done, empty_sink);
    empty_emitter->finish(empty_sink);
    bool empty_added = false, empty_done_seen = false;
    for (const auto &frame : empty_frames) {
        empty_added = empty_added || frame.find("response.output_item.added") != std::string::npos;
        empty_done_seen = empty_done_seen || frame.find("response.output_item.done") != std::string::npos;
    }
    assert(empty_added && empty_done_seen);

    auto opaque_emitter = responses->make_stream_emitter(&native_stream_context);
    std::vector<std::string> opaque_frames;
    auto opaque_sink = [&](const std::string &frame) {
        opaque_frames.push_back(frame); return true;
    };
    ir::StreamEvent opaque_start;
    opaque_start.type = ir::StreamEventType::ToolCallStart;
    opaque_start.index = 8; opaque_start.item_id = "opaque-stream";
    opaque_start.item.item_kind = ir::ItemKind::Opaque;
    opaque_start.item.extra["raw_item"] = json{{"type", "future_item"},
                                               {"id", "opaque-stream"},
                                               {"status", "in_progress"},
                                               {"payload", "keep"}};
    opaque_emitter->emit(opaque_start, opaque_sink);
    ir::StreamEvent opaque_done = opaque_start;
    opaque_done.type = ir::StreamEventType::ToolCallDone;
    opaque_done.item.status = "completed";
    opaque_emitter->emit(opaque_done, opaque_sink);
    opaque_emitter->finish(opaque_sink);
    bool opaque_added = false, opaque_completed = false;
    for (const auto &frame : opaque_frames) {
        opaque_added = opaque_added || frame.find("future_item") != std::string::npos &&
            frame.find("response.output_item.added") != std::string::npos;
        opaque_completed = opaque_completed || frame.find("future_item") != std::string::npos &&
            frame.find("response.output_item.done") != std::string::npos;
    }
    assert(opaque_added && opaque_completed);

    // Anthropic content blocks become ordered Items instead of being grouped
    // by kind (text must not move behind thinking/tool_use).
    ir::ChatResponse anthropic_response;
    assert(anthropic->parse_response(
        json{{"id", "anthropic-id"}, {"content", json::array({
            json{{"type", "text"}, {"text", "before"}},
            json{{"type", "thinking"}, {"thinking", "plan"}, {"signature", "sig"}},
            json{{"type", "tool_use"}, {"id", "tool-id"}, {"name", "f"},
                 {"input", json{{"x", 1}}}},
            json{{"type", "text"}, {"text", "after"}}
        })}, {"stop_reason", "end_turn"}},
        anthropic_response, error));
    assert(anthropic_response.output_items.size() == 4);
    assert(anthropic_response.output_items[0].item_kind == ir::ItemKind::Message);
    assert(anthropic_response.output_items[1].item_kind == ir::ItemKind::Reasoning);
    assert(anthropic_response.output_items[2].item_kind == ir::ItemKind::FunctionCall);
    assert(anthropic_response.output_items[3].item_kind == ir::ItemKind::Message);

    ir::ChatRequest bad; auto openai = make_openai_codec();
    json bad_call{{"id", "c"}, {"type", "function"},
                  {"function", json{{"name", "f"}, {"arguments", "{"}}}};
    json bad_message{{"role", "assistant"},
                     {"tool_calls", json::array({bad_call})}};
    json bad_request{{"model", "x"}, {"messages", json::array({bad_message})}};
    assert(!openai->parse_request(bad_request, bad, error));
    return 0;
}
