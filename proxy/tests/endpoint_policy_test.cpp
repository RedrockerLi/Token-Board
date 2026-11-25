#include <cassert>
#include <string>

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
    const auto &compact = endpoint_policy(EndpointKind::ResponsesCompact);
    assert(compact.client_format == ir::ApiFormat::OpenAIResponses);
    assert(compact.default_path == std::string("/responses/compact"));
    assert(&endpoint_policy_for_path("/v1/v1/responses/compact") == &compact);

    std::string path;
    bool path_is_full = false;
    resolve_upstream_path(embeddings, "openai", "http://upstream/v1", "", "",
                          false, path, path_is_full);
    assert(path == "/embeddings");
    assert(!path_is_full);
    resolve_upstream_path(models, "anthropic", "http://upstream/v1", "", "",
                          false, path, path_is_full);
    assert(path == "/models");
    assert(!path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Messages), "anthropic",
                          "http://upstream/v1", "https://custom/messages", "",
                          false, path, path_is_full);
    assert(path == "https://custom/messages");
    assert(path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Messages), "openai",
                          "http://upstream/v1", "", "", false, path,
                          path_is_full);
    assert(path == "/chat/completions");
    assert(!path_is_full);
    resolve_upstream_path(endpoint_policy(EndpointKind::Responses),
                          "openai_responses", "http://upstream/v1/", "", "",
                          false, path, path_is_full);
    assert(path == "/responses");
    assert(!path_is_full);
    resolve_upstream_path(compact, "openai_responses", "http://upstream/v1/", "",
                          "", false, path, path_is_full);
    assert(path == "/responses/compact");
    assert(!path_is_full);

    // ── Gemini: the model and the RPC are path variables ──────────────────
    const auto &generate = endpoint_policy(EndpointKind::GenerateContent);
    assert(generate.client_format == ir::ApiFormat::Google);
    assert(generate.allows_streaming);
    assert(generate.records_usage);
    assert(generate.http_method == HttpMethod::Post);
    assert(&endpoint_policy_for_path(
               "/v1beta/models/gemini-2.5-pro:generateContent") == &generate);
    assert(&endpoint_policy_for_path(
               "/v1beta/models/gemini-2.5-pro:streamGenerateContent") == &generate);
    assert(&endpoint_policy_for_path(
               "/v1/v1beta/models/gemini-2.5-pro:generateContent") == &generate);
    assert(&endpoint_policy_for_path(
               "/v1beta/v1beta/models/x:generateContent") == &generate);
    assert(&chat_endpoint_policy(ir::ApiFormat::Google) == &generate);

    assert(google_model_from_path(
               "/v1beta/models/gemini-2.5-pro:generateContent") ==
           "gemini-2.5-pro");
    assert(google_model_from_path(
               "/v1beta/models/models/gemini-3-pro:streamGenerateContent") ==
           "gemini-3-pro");
    assert(google_model_from_path("/v1beta/models").empty());
    assert(google_model_from_path("/v1/chat/completions").empty());
    assert(!google_stream_from_path(
        "/v1beta/models/x:generateContent"));
    assert(google_stream_from_path(
        "/v1beta/models/x:streamGenerateContent"));

    // A base URL that already names a version keeps its own prefix; a bare
    // host gets the documented /v1beta prefix.
    resolve_upstream_path(generate, "google",
                          "https://generativelanguage.googleapis.com/v1beta", "",
                          "gemini-2.5-pro", false, path, path_is_full);
    assert(path == "/models/gemini-2.5-pro:generateContent");
    assert(!path_is_full);
    resolve_upstream_path(generate, "google",
                          "https://generativelanguage.googleapis.com", "",
                          "gemini-2.5-pro", true, path, path_is_full);
    assert(path == "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse");
    assert(!path_is_full);
    resolve_upstream_path(generate, "google", "https://host/v1/", "", "m",
                          false, path, path_is_full);
    assert(path == "/models/m:generateContent");
    // An explicit override can keep the model in the path abstractly.
    resolve_upstream_path(generate, "google", "https://host/v1beta",
                          "/v1beta/models/{model}:{method}", "m", true, path,
                          path_is_full);
    assert(path == "/v1beta/models/m:streamGenerateContent");
    assert(path_is_full);
    // The target model names the resource even for a client on another wire.
    resolve_upstream_path(endpoint_policy(EndpointKind::Messages), "google",
                          "https://host/v1beta", "", "gemini-2.5-pro", false,
                          path, path_is_full);
    assert(path == "/models/gemini-2.5-pro:generateContent");
    return 0;
}
