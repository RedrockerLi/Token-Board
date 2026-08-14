#include <cassert>

#include "endpoint_policy.h"

int main() {
    const auto &embeddings = endpoint_policy(EndpointKind::Embeddings);
    assert(embeddings.http_method == HttpMethod::Post);
    assert(embeddings.default_path == std::string("/embeddings"));
    assert(embeddings.records_usage);
    assert(!embeddings.allows_streaming);

    const auto &models = endpoint_policy(EndpointKind::Models);
    assert(models.http_method == HttpMethod::Get);
    assert(models.default_path == std::string("/models"));
    assert(!models.records_usage);
    assert(models.local_response_mode == LocalResponseMode::Catalog);

    assert(&endpoint_policy_for_path("/v1/embeddings") == &embeddings);
    assert(&endpoint_policy_for_path("/v1/models") == &models);

    std::string path;
    bool path_is_full = false;
    resolve_upstream_path(embeddings, "openai", "http://upstream/v1", "",
                          path, path_is_full);
    assert(path == "/embeddings");
    assert(!path_is_full);
    resolve_upstream_path(models, "anthropic", "http://upstream/v1", "",
                          path, path_is_full);
    assert(path == "/models");
    assert(!path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Messages), "anthropic",
                          "http://upstream/v1", "https://custom/messages",
                          path, path_is_full);
    assert(path == "https://custom/messages");
    assert(path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Messages), "openai",
                          "http://upstream/v1", "", path, path_is_full);
    assert(path == "/chat/completions");
    assert(!path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Responses),
                          "openai_responses", "http://upstream/v1/", "",
                          path, path_is_full);
    assert(path == "/responses");
    assert(!path_is_full);
    return 0;
}
