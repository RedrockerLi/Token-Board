#pragma once

#include "codec.h"

/// OpenAI Responses (/v1/responses) codec.
std::unique_ptr<FormatCodec> make_responses_codec();
