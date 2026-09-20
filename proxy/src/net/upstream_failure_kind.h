#pragma once

#include "httplib.h"
#include "upstream_client.h"

inline UpstreamFailureKind classify_transport_failure(
    httplib::Error error, bool timeout, bool streaming) noexcept {
    if (timeout) return UpstreamFailureKind::Timeout;
    switch (error) {
        case httplib::Error::SSLConnection:
        case httplib::Error::SSLLoadingCerts:
        case httplib::Error::SSLServerVerification:
        case httplib::Error::SSLServerHostnameVerification:
            return UpstreamFailureKind::Tls;
        case httplib::Error::Connection:
        case httplib::Error::ProxyConnection:
            return UpstreamFailureKind::Connect;
        case httplib::Error::Write:
            return UpstreamFailureKind::Write;
        case httplib::Error::Read:
            return UpstreamFailureKind::Read;
        case httplib::Error::Canceled:
            return streaming ? UpstreamFailureKind::StreamProtocol
                             : UpstreamFailureKind::Transport;
        default:
            return UpstreamFailureKind::Transport;
    }
}
