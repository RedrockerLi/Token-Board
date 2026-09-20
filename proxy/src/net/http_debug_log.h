#pragma once

#include "logging.h"

#ifndef CPPHTTPLIB_OPENSSL_SUPPORT
#define CPPHTTPLIB_OPENSSL_SUPPORT
#endif
#include "httplib.h"

#include <cctype>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace tb_http_debug {

inline std::string escape(std::string_view value) {
    static constexpr char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(value.size() + 2);
    for (const unsigned char ch : value) {
        switch (ch) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\r': out += "\\r"; break;
            case '\n': out += "\\n"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20 || ch == 0x7f) {
                    out += "\\x";
                    out += hex[ch >> 4];
                    out += hex[ch & 0x0f];
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    return out;
}

inline bool sensitive_header(std::string name) {
    for (char &ch : name)
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return name == "authorization" || name == "proxy-authorization" ||
           name == "cookie" || name == "set-cookie" ||
           name.find("api-key") != std::string::npos ||
           name.find("token") != std::string::npos;
}

inline std::string headers(const httplib::Headers &value) {
    std::string out = "{";
    bool first = true;
    for (const auto &header : value) {
        if (!first) out += ", ";
        first = false;
        out += header.first;
        out += ": ";
        out += sensitive_header(header.first) ? "<redacted>"
                                              : "\"" + escape(header.second) + "\"";
    }
    out += "}";
    return out;
}

inline void emit(const std::string &line) {
    if (!log_enabled(LogLevel::Debug)) return;
    TB_LOG_DEBUG("%s\n", line.c_str());
}

inline void request(const char *side, const std::string &method,
                    const std::string &target, const httplib::Headers &header_map,
                    const std::string &body) {
    if (!log_enabled(LogLevel::Debug)) return;
    emit(std::string("[HTTP_DEBUG][") + side + "] request method=" +
         escape(method) + " target=\"" + escape(target) + "\" headers=" +
         headers(header_map) + " body=\"" + escape(body) + "\"");
}

inline void response_headers(const char *side, const std::string &target,
                             int status, const httplib::Headers &header_map) {
    if (!log_enabled(LogLevel::Debug)) return;
    emit(std::string("[HTTP_DEBUG][") + side + "] response_headers status=" +
         std::to_string(status) + " target=\"" + escape(target) +
         "\" headers=" + headers(header_map));
}

inline void response_body(const char *side, const std::string &target,
                          const std::string &body) {
    if (!log_enabled(LogLevel::Debug)) return;
    emit(std::string("[HTTP_DEBUG][") + side + "] response_body bytes=" +
         std::to_string(body.size()) + " target=\"" + escape(target) +
         "\" payload=\"" + escape(body) + "\"");
}

inline void response_chunk(const char *side, const std::string &target,
                           std::size_t index, const char *data, std::size_t size) {
    if (!log_enabled(LogLevel::Debug)) return;
    emit(std::string("[HTTP_DEBUG][") + side + "] response_chunk index=" +
         std::to_string(index) + " bytes=" + std::to_string(size) +
         " target=\"" + escape(target) + "\" payload=\"" +
         escape(std::string_view(data, size)) + "\"");
}

inline void exchange_result(const char *side, const std::string &target,
                            int status, bool transport_ok,
                            const std::string &error, int duration_ms,
                            std::size_t body_bytes) {
    if (!log_enabled(LogLevel::Debug)) return;
    emit(std::string("[HTTP_DEBUG][") + side + "] exchange_result status=" +
         std::to_string(status) + " transport=" +
         (transport_ok ? "ok" : "failed") + " duration_ms=" +
         std::to_string(duration_ms) + " body_bytes=" +
         std::to_string(body_bytes) + " target=\"" + escape(target) +
         "\" error=\"" + escape(error) + "\"");
}

inline thread_local bool downstream_request_logged = false;

inline void reset_downstream_request_state() {
    downstream_request_logged = false;
}

inline void downstream_request(const httplib::Request &req) {
    request("downstream", req.method, req.path, req.headers, req.body);
    downstream_request_logged = log_enabled(LogLevel::Debug);
}

inline void downstream_request_if_missing(const httplib::Request &req) {
    if (!downstream_request_logged) downstream_request(req);
    downstream_request_logged = false;
}

inline void downstream_response(const httplib::Request &req,
                                const httplib::Response &res) {
    response_headers("downstream", req.path, res.status, res.headers);
    response_body("downstream", req.path, res.body);
}

}  // namespace tb_http_debug
