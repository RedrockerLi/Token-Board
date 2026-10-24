/// Standalone checks for the UpstreamProfile dispatch table.
/// Exit code 0 = every case passed.

#include <cstdio>
#include <string>

#include "upstream_profile.h"

namespace {

int g_failures = 0;

void check(bool ok, const char *what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++g_failures;
    }
}

}  // namespace

int main() {
    const UpstreamProfile &fallback = default_upstream_profile();
    check(std::string(fallback.name) == "default", "default profile name");
    check(!fallback.needs_session, "default profile needs no session");
    check(fallback.cooldown_recovery == CooldownRecoveryChannel::kHttpProbe,
          "default profile probes over HTTP");
    check(fallback.decorate_headers != nullptr,
          "default decorator is callable, never null");

    UpstreamHeaderList none;
    fallback.decorate_headers(none, "tb-session");
    check(none.empty(), "default decorator adds no headers");

    check(upstream_profile_for_subtype("") == &fallback,
          "empty sub-type falls back silently");

    const UpstreamProfile *go = upstream_profile_for_subtype("opencode-go");
    check(go != nullptr, "opencode-go resolves");
    check(go == upstream_profile_for_subtype("opencode-go"),
          "opencode-go resolution is stable");
    check(std::string(go->name) == "opencode-go", "opencode-go profile name");
    check(go->needs_session, "opencode-go needs the session header");
    check(go->cooldown_recovery == CooldownRecoveryChannel::kUsageSnapshot,
          "opencode-go recovers from the usage snapshot");

    UpstreamHeaderList headers;
    go->decorate_headers(headers, "tb-session");
    check(headers.size() == 2, "opencode-go adds two headers");
    check(headers[0].first == "x-opencode-session" &&
              headers[0].second == "tb-session",
          "opencode-go forwards the session id");
    check(headers[1].first == "User-Agent" &&
              headers[1].second == "token-board/1.0",
          "opencode-go pins the proxy User-Agent");

    UpstreamHeaderList no_session;
    go->decorate_headers(no_session, "");
    check(no_session.size() == 1 && no_session[0].first == "User-Agent",
          "opencode-go omits an empty session header");

    check(upstream_profile_for_subtype("not-a-subtype") == &fallback,
          "unknown sub-type degrades to the default profile");

    if (g_failures != 0) {
        std::fprintf(stderr, "%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("upstream_profile: all checks passed\n");
    return 0;
}
