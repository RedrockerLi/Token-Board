#include "request_context.h"

#include "endpoint_policy.h"
#include "request_timing.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include <atomic>
#include <openssl/rand.h>
#include <openssl/sha.h>

namespace {
std::string session_id(const httplib::Request &request,
                       const nlohmann::json &body) {
    // Claude Code's own session header is the most specific identity a client
    // sends, so it outranks the generic (and more inventable) session and
    // conversation headers.
    std::string value = request.get_header_value("x-claude-code-session-id");
    if (value.empty()) value = request.get_header_value("x-session-id");
    if (value.empty()) value = request.get_header_value("x-conversation-id");
    if (!value.empty()) return value;
    if (body.contains("user") && body["user"].is_string())
        return body["user"].get<std::string>();
    if (body.contains("metadata") && body["metadata"].is_object()) {
        const auto &metadata = body["metadata"];
        if (metadata.contains("user_id") && metadata["user_id"].is_string())
            return metadata["user_id"].get<std::string>();
    }
    return {};
}

bool blank(const std::string &value) {
    return value.find_first_not_of(" \t\r\n") == std::string::npos;
}

// A session id is echoed into an upstream request header.  httplib writes
// header values verbatim, so a value carrying control characters (CR/LF first
// among them) could terminate the header block and inject a smuggled request
// into the pooled upstream connection.  Length is bounded for the same reason
// a header line is: a session id is a short opaque token.
bool header_value_safe(const std::string &value) {
    if (blank(value) || value.size() > 255) return false;
    for (unsigned char ch : value)
        if (ch < 0x20 || ch == 0x7f) return false;
    return true;
}

std::string hex32(const unsigned char *bytes) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string out;
    out.reserve(32);
    for (int i = 0; i < 16; ++i) {
        out.push_back(digits[bytes[i] >> 4]);
        out.push_back(digits[bytes[i] & 0x0f]);
    }
    return out;
}

// Remove every "cache_control" key at any depth.  Prompt-cache markers are
// transport hints, not conversation content: the same conversation must
// fingerprint identically with and without them.
void strip_cache_control(nlohmann::json &value) {
    if (value.is_object()) {
        value.erase("cache_control");
        for (auto &entry : value.items()) strip_cache_control(entry.value());
    } else if (value.is_array()) {
        for (auto &entry : value) strip_cache_control(entry);
    }
}

// True when a JSON part carries actual conversation text.  An absent, null or
// empty `system` must not turn a prompt into an identity of its own.
bool json_has_content(const nlohmann::json &value) {
    if (value.is_string()) return !value.get_ref<const std::string &>().empty();
    if (value.is_array()) return !value.empty();
    if (value.is_object()) return !value.empty();
    return false;
}

// Re-serialized form of one part with its prompt-cache markers removed.
std::string fingerprint_part(const nlohmann::json &value) {
    nlohmann::json stripped = value;
    strip_cache_control(stripped);
    return stripped.dump();
}

// Conversation fingerprint: the identity of the prompt itself, for clients
// that send no session header at all.  Empty when the body carries neither a
// system prompt nor a user message (nothing stable to identify).
std::string conversation_fingerprint(const RequestContext &context) {
    const auto &body = context.parsed_json;
    std::string material;
    bool has_content = false;
    const auto system = body.find("system");
    if (system != body.end() && json_has_content(*system)) {
        material += fingerprint_part(*system);
        has_content = true;
    }
    const auto messages = body.find("messages");
    if (messages != body.end() && messages->is_array()) {
        for (const auto &message : *messages) {
            if (!message.is_object()) continue;
            const auto role = message.find("role");
            if (role == message.end() || !role->is_string()) continue;
            if (role->get_ref<const std::string &>() != "user") continue;
            material += fingerprint_part(message);
            has_content = true;
            break;
        }
    }
    if (!has_content) return {};
    unsigned char digest[SHA256_DIGEST_LENGTH] = {};
    SHA256(reinterpret_cast<const unsigned char *>(material.data()),
           material.size(), digest);
    return "tb-" + hex32(digest);
}
}

bool parse_request_context(const httplib::Request &request,
                           RequestContext &context, std::string &error) {
    const auto &policy = endpoint_policy_for_path(request.path);
    context.endpoint_kind = policy.kind;
    context.client_format = policy.client_format;
    context.raw_body = std::make_shared<const std::string>(request.body);
    context.queue_ms = current_request_queue_delay_ms();
    context.content_type = request.has_header("Content-Type")
        ? request.get_header_value("Content-Type") : "application/json";
    try {
        context.parsed_json = nlohmann::json::parse(request.body);
    } catch (...) {
        error = "invalid JSON body";
        return false;
    }
    if (!context.parsed_json.is_object()) {
        error = "request body must be a JSON object";
        return false;
    }
    if (!context.parsed_json.contains("model") ||
        !context.parsed_json["model"].is_string() ||
        context.parsed_json["model"].get_ref<const std::string &>().empty()) {
        error = "model must be a non-empty string";
        return false;
    }
    context.model = context.parsed_json["model"].get<std::string>();
    if (context.parsed_json.contains("stream") &&
        !context.parsed_json["stream"].is_boolean()) {
        error = "stream must be a boolean";
        return false;
    }
    context.streaming = context.parsed_json.value("stream", false);
    if (context.streaming && !policy.allows_streaming) {
        error = "streaming is not supported for this endpoint";
        return false;
    }
    context.session_id = session_id(request, context.parsed_json);
    if (context.parsed_json.contains("previous_response_id") &&
        !context.parsed_json["previous_response_id"].is_string()) {
        error = "previous_response_id must be a string";
        return false;
    }
    if (context.parsed_json.contains("previous_response_id"))
        context.previous_response_id = context.parsed_json["previous_response_id"].get<std::string>();
    return true;
}

bool ensure_request_ir(const CodecRegistry &codecs, RequestContext &context,
                       std::string &error) {
    if (context.ir_ready) return true;
    try {
        if (!codecs.get(context.client_format).parse_request(
                context.parsed_json, context.parsed_ir, error))
            return false;
    } catch (const std::exception &e) {
        error = e.what();
        return false;
    } catch (...) {
        error = "invalid request fields";
        return false;
    }
    context.ir_ready = true;
    return true;
}

std::string opencode_random_session_value() {
    unsigned char bytes[16] = {};
    if (RAND_bytes(bytes, sizeof(bytes)) != 1) {
        static std::atomic<unsigned long long> fallback{1};
        return "tb-" + std::to_string(
            fallback.fetch_add(1, std::memory_order_relaxed));
    }
    return "tb-" + hex32(bytes);
}

std::string opencode_session_value(const httplib::Request &request,
                                   const RequestContext &context) {
    if (request.has_header("x-opencode-session")) {
        const std::string value = request.get_header_value("x-opencode-session");
        if (header_value_safe(value)) return value;
    }
    // The request session id may be derived from the client body, so it is
    // validated the same way before it can reach an upstream header.
    if (header_value_safe(context.session_id)) return context.session_id;
    // Reached only for clients that send no usable session identity, so the
    // hash of the request body is paid once per request at most.
    std::string fingerprint = conversation_fingerprint(context);
    if (!fingerprint.empty()) return fingerprint;
    return opencode_random_session_value();
}
