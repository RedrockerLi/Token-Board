#include "codec.h"
#include "core/logging.h"

#include <cstdio>
#include <cstdlib>

void CodecRegistry::add(std::unique_ptr<FormatCodec> c) {
    codecs_[c->format()] = std::move(c);
}

const FormatCodec &CodecRegistry::get(ir::ApiFormat f) const {
    auto it = codecs_.find(f);
    if (it == codecs_.end() || !it->second) {
        // Unreachable with a registry built at startup from the four
        // `make_*_codec()` factories: every value `parse_api_format()` can
        // return has a codec.  It therefore reports a programming error, not a
        // request condition, and the process must not continue with a
        // half-configured registry — the ERROR line makes the abort observable
        // (audit 01-P-14).  Recovering per-request instead would need an error
        // path through the 22 get() call sites in core/net.
        TB_LOG_ERROR("[Codec] FATAL: no codec registered for format %d\n",
                static_cast<int>(f));
        std::abort();
    }
    return *it->second;
}
