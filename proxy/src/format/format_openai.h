#pragma once

#include "codec.h"

/// OpenAI Chat Completions (/v1/chat/completions) codec.
std::unique_ptr<FormatCodec> make_openai_codec();
