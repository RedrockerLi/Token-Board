#pragma once

#include "origin_limiter.h"
#include "upstream_client.h"
#include "upstream_metrics_internal.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cctype>
#include <cstdint>
#include <deque>
#include <exception>
#include <list>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <poll.h>
#include <set>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#ifdef _WIN32
#include <process.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

namespace {
constexpr int NO_TIMEOUT_SECS = 24 * 3600;
constexpr int DEFAULT_CONNECT_TIMEOUT_SECS = 10;
constexpr int64_t DNS_SUCCESS_TTL_MS = 60 * 1000;
constexpr int64_t DNS_FAILURE_TTL_MS = 5 * 1000;
constexpr int64_t DNS_ADDRESS_BACKOFF_MS = 15 * 1000;
constexpr size_t DNS_CACHE_MAX_HOSTS = 256;
constexpr size_t DNS_QUEUE_MAX = 64;
constexpr size_t DNS_WORKER_COUNT = 4;

#include "origin_internal.h"
#include "stream_progress_internal.h"
#include "dns_resolver_internal.h"
#include "connection_pool_internal.h"
#include "forward_watchdog_internal.h"
#include "watchdog_service_internal.h"
