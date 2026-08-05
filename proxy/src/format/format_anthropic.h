#pragma once

#include "codec.h"

/// Anthropic Messages (/v1/messages) codec.
std::unique_ptr<FormatCodec> make_anthropic_codec();
