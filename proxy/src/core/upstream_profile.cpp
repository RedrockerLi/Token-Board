#include "upstream_profile.h"

#include "request_context.h"
#include "upstream_client.h"

void apply_upstream_profile(ForwardOptions &opts,
                            const UpstreamProfile &profile,
                            const std::string &session) {
    opts.upstream_profile = &profile;
    opts.upstream_session =
        profile.needs_session
            ? (session.empty() ? opencode_random_session_value() : session)
            : std::string();
}
