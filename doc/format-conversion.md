# 格式转换

代理支持三种线格式:OpenAI Chat Completions(`/v1/chat/completions`)、OpenAI Responses(`/v1/responses`)、Anthropic Messages(`/v1/messages`)。客户端(harness)与上游各用任意一种,代理在中间转换。转换逻辑全部收敛在 `proxy/src/` 的 IR + codec 层,三个 chat 端点共用同一条管线。

## 中间表示 IR

`ir.h` 定义了一组平台无关的结构:`ChatRequest`(messages、tools、tool_choice、reasoning、max_tokens 等)、`ChatResponse`(content blocks、stop_reason、usage)、`StreamEvent`(文本/思考/tool 增量、消息起止、usage、错误事件)、`ContentBlock`(Text / Image / ToolUse / ToolResult / Thinking 五种)、`Usage`。

三种格式的 codec 都做同一件事:把自己的线格式解析成 IR,再把 IR 序列化回自己的线格式。因此任意格式互转就是组合:

```
parse(harness 格式) → IR → serialize(上游格式)
```

`codec.h` 的 `FormatCodec` 抽象接口包括:请求解析/序列化、响应解析/序列化、流式 parser / emitter(按 `StreamEvent` 喂入喂出)、错误体归一化。三个 codec(`format_openai.cpp`、`format_anthropic.cpp`、`format_responses.cpp`)在 `main.cpp` 里注册进 `CodecRegistry`。

## 格式识别与透传

客户端格式由请求 URL 路径识别(`harness_format_from_path`):`/v1/messages` → Anthropic,`/v1/responses` → Responses,其余按 OpenAI。客户端 base URL 若已带 `/v1` 再拼一次端点会产生 `/v1/v1/...` 双前缀,代理同时注册了 `/v1/v1/*` 别名兼容这类客户端。

同格式走透传快速路径:body 只做 `[1m]` 后缀剥离与模型名改写,不经 IR,流式时最多过一个 think 过滤器。跨格式才走完整的 parse → IR → serialize。流式请求是一次性选择候选,头一旦发出就无法回退;非流式请求可以按 429/5xx 回退下一个候选,见 [proxy-internals.md](proxy-internals.md)。

## 流式处理

流式路径用 httplib 的 chunked provider。透传时上游 chunk 原样转发(OpenAI 上游额外过 `ThinkStreamFilter`);转换时上游字节喂给 `upstream_codec` 的 stream parser,产出的 `StreamEvent` 逐个交给 harness codec 的 emitter 转成客户端格式的 SSE 帧。

usage 的解析,非流式直接 parse 响应体;流式从累积的 SSE 中取最后带 usage 的帧。`usage_tracker.cpp` 为三种格式各准备了非流式与 SSE 两套解析器,并额外兼容 opencode.ai 的非标准 `x-opencode-type: inference-cost` 帧(它携带 normalizedUsage 输入/输出/缓存读写 token,优先级最高)。Anthropic 上游把 `cache_read_input_tokens` / `cache_creation_input_tokens` 折进 `prompt_tokens`,保证 `prompt - cache_read` 在任意上游口径下都等于未命中输入。

## think 内容抽取

DeepSeek 这类厂商会在 assistant 消息里输出 `<think>...</think>` 推理块。代理把它抽取到 `reasoning_content` 字段,而不是丢弃:

- 非流式:透传响应经过 `sanitize_response_body`,把 `<think>` 内容从 `content` 移到 `reasoning_content`。
- 流式:`ThinkStreamFilter` 是个按行处理的状态机,区分 NORMAL / IN_THINK 两态,把 `<think>` 开合跨多个 chunk 的推理片段逐步累积到 `reasoning_content`,同时清掉 content 里残留的 think 标签。

`is_reasoning_vendor` 按模型名判断(deepseek / kimi / moonshot / mimo)。带 tool_calls 的 assistant 消息若没有 `reasoning_content`,会注入占位文本,满足上游对结构的要求。

## 转换中的边界处理

`format_common.cpp` 集中了几个跨格式的坑:

- `strip_one_m_suffix_for_upstream`:剥掉 Claude Code 的 `[1m]` / `[1M]` 上下文窗口后缀,上游不认这个标记。
- `normalize_tool_choice_to_openai` / `_to_anthropic`:OpenAI 的 `{"type":"function",...}` 与 Anthropic 的 `{"type":"tool","name":...}`、字符串形式的 auto/required/none 互相换算。
- `normalize_function_parameters`:强制 function 工具的 `parameters` 为 `{"type":"object", "properties":{...}}`,opencode.ai 等严格校验的上游会拒绝 null / 空对象。
- `parse_data_uri` / `build_data_uri`:图片 content 在 data URI 与 base64 字段之间互转。
- `parse_sse_frame`:SSE 分帧,兼容 `data:` 有无空格、事件名、注释行。
- `read_cache_hit_tokens`:从各格式的缓存字段里取命中 token 数(deepseek 的 `prompt_cache_hit_tokens`、OpenAI 的 `prompt_tokens_details.cached_tokens`、Responses 的 `input_tokens_details.cached_tokens`,或由 miss 推导),minimax 这类不报缓存的返回 0。
- 超时错误帧:上游读超时(100s 无数据)时按客户端格式发终止错误帧——Anthropic `event: error`、Responses `response.failed`、OpenAI `data: {error}` + `[DONE]`,而不是静默断连。

## 自测

codec 层不依赖 sqlite / http,单独编译成 `format_conv_test`,见 [development.md](development.md)。
