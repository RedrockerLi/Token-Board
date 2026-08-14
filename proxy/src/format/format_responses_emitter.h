#pragma once

#include "format_responses_internal.h"
#include <map>
#include <set>

class ResponsesStreamEmitter final : public ir::StreamEmitter {
public:
    explicit ResponsesStreamEmitter(const ir::ConversionContext *ctx);
    bool emit(const ir::StreamEvent &, const Sink &) override;
    bool finish(const Sink &) override;

private:
    struct ToolItem { std::string call_id, name, arguments, kind, item_id, namespace_name; };
    const ir::ConversionContext *context_ = nullptr;
    std::string id_, model_ = "unknown";
    bool started_ = false, finished_ = false, completed_emitted_ = false;
    std::string deferred_status_ = "completed";
    ir::Usage last_usage_;
    std::map<int, std::string> text_, reasoning_text_, item_ids_;
    std::map<int, std::string> reasoning_signatures_, reasoning_redacted_data_;
    std::map<int, json> raw_items_;
    std::map<int, ToolItem> tools_;
    std::map<int, int> item_kind_;
    std::map<std::pair<int, int>, int> out_index_;
    std::set<int> text_started_, text_finished_;
    int next_output_index_ = 0;
    enum { kReasoningStream = 0, kTextStream = 1, kToolStream = 2 };

    std::string item_id(int) const;
    static std::string frame(const std::string &, const json &);
    int out_index_for(int, int);
    bool emit_response_created(const Sink &);
    bool emit_text_item_start(const Sink &, int, const ir::StreamEvent &);
    bool emit_reasoning_item_start(const Sink &, int, const ir::StreamEvent &);
    bool close_item(const Sink &, int);
    bool close_open_items(const Sink &);
    json build_output();
    bool emit_completed(const Sink &);
};
