#pragma once

#include "codec.h"

/// Google Gemini GenerateContent codec
/// (`models/{model}:generateContent`, `:streamGenerateContent?alt=sse`).
std::unique_ptr<FormatCodec> make_google_codec();
