#include "codec.h"

#include <cstdio>

void CodecRegistry::add(std::unique_ptr<FormatCodec> c) {
    codecs_[c->format()] = std::move(c);
}

const FormatCodec &CodecRegistry::get(ir::ApiFormat f) const {
    auto it = codecs_.find(f);
    if (it == codecs_.end() || !it->second) {
        fprintf(stderr, "[Codec] FATAL: no codec registered for format %d\n",
                static_cast<int>(f));
        std::abort();
    }
    return *it->second;
}
