#include "database_internal.h"

#include <cstdlib>
#include <unistd.h>

bool Database::append_log_spool_locked(const std::string &payload) {
    if (log_spool_fd_ < 0 || payload.empty() ||
        payload.size() > kLogRecordMaxBytes)
        return false;

    std::string frame;
    frame.reserve(kSpoolHeaderBytes + payload.size());
    append_u32_le(frame, static_cast<std::uint32_t>(payload.size()));
    append_u32_le(frame, spool_checksum(payload.data(), payload.size()));
    frame.append(payload);

    // Reclaim the file before applying the hard limit. The read and write
    // offsets may both be large even when no frames remain pending.
    if (log_spool_read_offset_ == log_spool_write_offset_ &&
        log_spool_write_offset_ >= kLogCompactThreshold)
        compact_log_spool_locked(true);
    // Bound total spool size. Admission reserves a worst-case frame before a
    // request reaches the upstream, so this path is a sticky disk failure,
    // never a normal queue-overflow drop.
    if (log_spool_write_offset_ + frame.size() > kLogSpoolHardLimit) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        TB_LOG_ERROR(
                "[DB] request-log spool hard limit reached (%llu bytes); "
                "writer degraded and admission is closed\n",
                static_cast<unsigned long long>(log_spool_write_offset_));
        return false;
    }

    const std::uint64_t original_offset = log_spool_write_offset_;
    if (!write_exact(log_spool_fd_, frame.data(), frame.size())) {
        const int saved_errno = errno;
        if (::ftruncate(log_spool_fd_, static_cast<off_t>(original_offset)) != 0)
            TB_LOG_ERROR( "[DB] CRITICAL: failed to remove torn spool tail: %s\n",
                    std::strerror(errno));
        errno = saved_errno;
        TB_LOG_ERROR( "[DB] request-log spool append error: %s\n",
                std::strerror(errno));
        return false;
    }
    log_spool_write_offset_ += frame.size();
    return true;
}

bool Database::read_log_spool_batch_locked(std::vector<SpoolRecord> &batch) {
    batch.clear();
    std::uint64_t offset = log_spool_read_offset_;
    std::size_t batch_bytes = 0;
    while (offset < log_spool_write_offset_ && batch.size() < kLogBatchSize) {
        char header[kSpoolHeaderBytes];
        if (!pread_exact(log_spool_fd_, header, sizeof(header), offset)) return false;
        const std::uint32_t payload_size = read_u32_le(header);
        const std::uint32_t checksum = read_u32_le(header + 4);
        if (payload_size == 0 || payload_size > kLogRecordMaxBytes) return false;
        const std::size_t frame_size = kSpoolHeaderBytes + payload_size;
        if (!batch.empty() && batch_bytes + frame_size > kLogBatchBytes) break;
        if (offset + frame_size > log_spool_write_offset_) return false;

        std::string payload(payload_size, '\0');
        if (!pread_exact(log_spool_fd_, payload.data(), payload.size(),
                         offset + kSpoolHeaderBytes) ||
            spool_checksum(payload.data(), payload.size()) != checksum)
            return false;

        SpoolRecord item;
        if (!deserialize_log_record(payload, item.record)) return false;
        offset += frame_size;
        item.end_offset = offset;
        item.frame_bytes = frame_size;
        batch_bytes += frame_size;
        batch.push_back(std::move(item));
    }
    return !batch.empty();
}

bool Database::compact_log_spool_locked(bool force) {
    if (log_spool_fd_ < 0 ||
        log_spool_read_offset_ != log_spool_write_offset_)
        return false;
    if (!force && log_spool_write_offset_ < kLogCompactThreshold) return true;
    if (::ftruncate(log_spool_fd_, 0) != 0) {
        TB_LOG_ERROR( "[DB] request-log spool truncate error: %s\n",
                std::strerror(errno));
        return false;
    }
    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = 0;
    if (::fdatasync(log_spool_fd_) != 0) {
        TB_LOG_ERROR( "[DB] request-log spool truncate sync error: %s\n",
                std::strerror(errno));
        return false;
    }
    return true;
}

bool Database::start_log_writer() {
    std::unique_lock<std::mutex> lock(log_queue_mutex_);
    if (log_writer_thread_.joinable()) {
        TB_LOG_ERROR( "[DB] request-log writer is already running\n");
        return false;
    }

    const std::string spool_path = db_path_ + ".request-log.spool";
    log_spool_fd_ = ::open(spool_path.c_str(),
                           O_CREAT | O_RDWR | O_APPEND | O_CLOEXEC, 0600);
    if (log_spool_fd_ < 0) {
        TB_LOG_ERROR( "[DB] cannot open request-log spool %s: %s\n",
                spool_path.c_str(), std::strerror(errno));
        return false;
    }
    if (::fchmod(log_spool_fd_, 0600) != 0) {
        TB_LOG_ERROR( "[DB] cannot secure request-log spool permissions: %s\n",
                std::strerror(errno));
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }
    if (flock(log_spool_fd_, LOCK_EX | LOCK_NB) != 0) {
        TB_LOG_ERROR(
                "[DB] request-log spool is already owned by another proxy: %s\n",
                std::strerror(errno));
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }

    struct stat st {};
    if (::fstat(log_spool_fd_, &st) != 0 || st.st_size < 0) {
        TB_LOG_ERROR( "[DB] cannot stat request-log spool: %s\n",
                std::strerror(errno));
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }

    const std::uint64_t file_size = static_cast<std::uint64_t>(st.st_size);
    std::uint64_t valid_end = 0;
    while (valid_end + kSpoolHeaderBytes <= file_size) {
        char header[kSpoolHeaderBytes];
        if (!pread_exact(log_spool_fd_, header, sizeof(header), valid_end)) break;
        const std::uint32_t payload_size = read_u32_le(header);
        const std::uint32_t checksum = read_u32_le(header + 4);
        if (payload_size == 0 || payload_size > kLogRecordMaxBytes ||
            valid_end + kSpoolHeaderBytes + payload_size > file_size)
            break;
        std::string payload(payload_size, '\0');
        if (!pread_exact(log_spool_fd_, payload.data(), payload.size(),
                         valid_end + kSpoolHeaderBytes) ||
            spool_checksum(payload.data(), payload.size()) != checksum) break;
        LogRecord probe;
        if (!deserialize_log_record(payload, probe)) break;
        valid_end += kSpoolHeaderBytes + payload_size;
    }
    if (valid_end != file_size) {
        TB_LOG_ERROR(
                "[DB] trimming %llu byte(s) from an incomplete/corrupt spool tail\n",
                static_cast<unsigned long long>(file_size - valid_end));
        if (::ftruncate(log_spool_fd_, static_cast<off_t>(valid_end)) != 0 ||
            ::fdatasync(log_spool_fd_) != 0) {
            TB_LOG_ERROR( "[DB] failed to repair request-log spool: %s\n",
                    std::strerror(errno));
            flock(log_spool_fd_, LOCK_UN);
            ::close(log_spool_fd_);
            log_spool_fd_ = -1;
            return false;
        }
    }

    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = valid_end;
    log_reservations_ = 0;
    log_recovering_ = valid_end != 0;
    log_stop_ = false;
    log_accepting_ = !log_recovering_;
    log_persist_failures_.store(0, std::memory_order_relaxed);
    try {
        log_writer_thread_ = std::thread(&Database::log_writer_loop, this);
    } catch (const std::exception &e) {
        log_accepting_ = false;
        log_stop_ = true;
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        TB_LOG_ERROR( "[DB] request-log writer start error: %s\n", e.what());
        return false;
    } catch (...) {
        log_accepting_ = false;
        log_stop_ = true;
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        TB_LOG_ERROR( "[DB] request-log writer start error: unknown exception\n");
        return false;
    }
    if (valid_end != 0) log_queue_cv_.notify_one();
    return true;
}

void Database::stop_log_writer() {
    std::unique_lock<std::mutex> lock(log_queue_mutex_);
    log_accepting_ = false;
    log_stop_ = true;
    log_queue_cv_.notify_all();
    const bool joinable = log_writer_thread_.joinable();
    lock.unlock();
    if (joinable) log_writer_thread_.join();

    lock.lock();
    if (log_spool_read_offset_ != log_spool_write_offset_) {
        const auto pending = log_spool_write_offset_ - log_spool_read_offset_;
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        TB_LOG_ERROR(
                "[DB] CRITICAL: shutdown retained %llu byte(s) of durable "
                "request-log spool for next startup\n",
                static_cast<unsigned long long>(pending));
    }
    if (!log_memory_queue_.empty()) {
        const auto lost = static_cast<std::uint64_t>(log_memory_queue_.size());
        log_lost_events_.fetch_add(lost, std::memory_order_relaxed);
        log_persist_failures_.fetch_add(lost, std::memory_order_relaxed);
        TB_LOG_ERROR(
                "[DB] CRITICAL: shutdown discarded %llu unsynced request-log "
                "events; health remains degraded\n",
                static_cast<unsigned long long>(lost));
        log_memory_queue_.clear();
    }
    if (log_spool_fd_ >= 0) {
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
    }
    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = 0;
    log_reservations_ = 0;
    log_recovering_ = false;
    log_stop_ = false;
}

void Database::log_writer_loop() {
    try {
        for (;;) {
            std::vector<SpoolRecord> recovery_batch;
            std::vector<LogRecord> records;
            std::uint64_t durable_end = 0;
            bool recovered = false;
            {
                std::unique_lock<std::mutex> lock(log_queue_mutex_);
                log_queue_cv_.wait(lock, [this] {
                    return log_stop_ || !log_memory_queue_.empty() ||
                           log_spool_read_offset_ < log_spool_write_offset_;
                });
                if (log_spool_read_offset_ < log_spool_write_offset_) {
                    recovery_batch.reserve(kLogBatchSize);
                    if (!read_log_spool_batch_locked(recovery_batch)) {
                        TB_LOG_ERROR(
                                "[DB] CRITICAL: cannot decode durable request-log "
                                "spool at offset %llu\n",
                                static_cast<unsigned long long>(
                                    log_spool_read_offset_));
                        log_persist_failures_.fetch_add(
                            1, std::memory_order_relaxed);
                        log_accepting_ = false;
                        if (log_stop_) break;
                        log_queue_cv_.wait_for(lock, std::chrono::seconds(1));
                        continue;
                    }
                    recovered = true;
                    durable_end = recovery_batch.back().end_offset;
                    records.reserve(recovery_batch.size());
                    for (auto &item : recovery_batch)
                        records.push_back(std::move(item.record));
                } else if (!log_memory_queue_.empty()) {
                    if (!log_stop_)
                        log_queue_cv_.wait_for(
                            lock, std::chrono::milliseconds(5));
                    const auto count = std::min(
                        kLogBatchSize, log_memory_queue_.size());
                    records.reserve(count);
                    while (!log_memory_queue_.empty() &&
                           records.size() < count) {
                        records.push_back(
                            std::move(log_memory_queue_.front()));
                        log_memory_queue_.pop_front();
                    }
                } else if (log_stop_) {
                        compact_log_spool_locked(true);
                        break;
                }
            }

            if (!recovered) {
                std::vector<std::string> payloads;
                payloads.reserve(records.size());
                std::size_t bytes = 0;
                std::size_t accepted = 0;
                bool encoding_error = false;
                for (; accepted < records.size(); ++accepted) {
                    std::string payload;
                    try {
                        payload = serialize_log_record(records[accepted]);
                    } catch (const std::exception &e) {
                        TB_LOG_ERROR( "[DB] request-log encode error: %s\n",
                                e.what());
                        encoding_error = true;
                        break;
                    } catch (...) {
                        TB_LOG_ERROR( "[DB] request-log encode error\n");
                        encoding_error = true;
                        break;
                    }
                    const auto frame_bytes = kSpoolHeaderBytes + payload.size();
                    if (payload.empty() || payload.size() > kLogRecordMaxBytes) {
                        encoding_error = true;
                        break;
                    }
                    if (accepted != 0 && bytes + frame_bytes > kLogBatchBytes)
                        break;
                    bytes += frame_bytes;
                    payloads.push_back(std::move(payload));
                }
                if (accepted < records.size()) {
                    std::lock_guard<std::mutex> lock(log_queue_mutex_);
                    for (std::size_t i = records.size(); i > accepted; --i)
                        log_memory_queue_.push_front(std::move(records[i - 1]));
                    records.resize(accepted);
                }
                if (encoding_error) {
                    std::lock_guard<std::mutex> lock(log_queue_mutex_);
                    for (std::size_t i = records.size(); i > 0; --i)
                        log_memory_queue_.push_front(std::move(records[i - 1]));
                    log_accepting_ = false;
                    log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
                    log_queue_cv_.notify_all();
                    continue;
                }
                bool spool_error = false;
                std::size_t appended = 0;
                {
                    std::lock_guard<std::mutex> lock(log_queue_mutex_);
                    for (const auto &payload : payloads) {
                        if (!append_log_spool_locked(payload)) {
                            spool_error = true;
                            break;
                        }
                        ++appended;
                    }
                    if (spool_error) {
                        // Frames already appended remain recoverable from the
                        // spool. Requeue only records whose frame was not
                        // appended; event_id makes a later mixed recovery
                        // batch exactly-once even if the process stops now.
                        for (std::size_t i = records.size(); i > appended; --i)
                            log_memory_queue_.push_front(std::move(records[i - 1]));
                        records.resize(appended);
                        log_accepting_ = false;
                        log_queue_cv_.notify_all();
                    }
                    durable_end = log_spool_write_offset_;
                }
                if (spool_error) {
                    std::unique_lock<std::mutex> lock(log_queue_mutex_);
                    log_queue_cv_.wait_for(lock, std::chrono::seconds(1),
                                           [this] { return log_stop_; });
                    continue;
                }
            }

            int shutdown_failures = 0;
            int retry = 0;
            bool spool_synced = false;
            log_last_batch_size_.store(records.size(),
                                       std::memory_order_release);
            for (;;) {
                if (!spool_synced) {
                    int sync_rc;
                    do {
                        sync_rc = ::fdatasync(log_spool_fd_);
                    } while (sync_rc != 0 && errno == EINTR);
                    spool_synced = sync_rc == 0;
                    if (!spool_synced)
                        TB_LOG_ERROR(
                                "[DB] request-log spool batch sync error: %s\n",
                                std::strerror(errno));
                }
                // Test-only crash point: prove that a frame which reached
                // durable spool before SQLite commit is replayed exactly once
                // after restart. Production never sets this variable.
                if (spool_synced && std::getenv("TB_TEST_CRASH_AFTER_SPOOL_SYNC")) {
                    TB_LOG_WARN("[DB] test crash after request-log spool sync\n");
                    ::_exit(86);
                }
                if (spool_synced &&
                    persist_log_records(records.data(), records.size()))
                    break;
                ++retry;
                bool stopping = false;
                {
                    std::unique_lock<std::mutex> lock(log_queue_mutex_);
                    stopping = log_stop_;
                    if (stopping && ++shutdown_failures >= kShutdownRetryLimit) {
                        log_persist_failures_.fetch_add(
                            records.size(), std::memory_order_relaxed);
                        TB_LOG_ERROR(
                                "[DB] CRITICAL: SQLite unavailable during "
                                "shutdown; durable spool retained\n");
                        return;
                    }
                    const int backoff_ms = std::min(1000, 25 << std::min(retry, 5));
                    log_queue_cv_.wait_for(
                        lock, std::chrono::milliseconds(backoff_ms),
                        [this] { return log_stop_; });
                }
            }

            {
                std::lock_guard<std::mutex> lock(log_queue_mutex_);
                log_spool_read_offset_ = durable_end;
                if (log_recovering_ &&
                    log_spool_read_offset_ == log_spool_write_offset_) {
                    log_recovering_ = false;
                    log_accepting_ = true;
                    log_queue_cv_.notify_all();
                }
                if (log_spool_read_offset_ == log_spool_write_offset_)
                    compact_log_spool_locked(log_stop_);
            }
        }
    } catch (const std::exception &e) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        TB_LOG_ERROR(
                "[DB] CRITICAL: request-log writer exception; spool retained: %s\n",
                e.what());
        // The writer thread is dead; stop accepting new spool records so the
        // spool cannot grow unbounded with nothing draining it.
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
    } catch (...) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        TB_LOG_ERROR(
                "[DB] CRITICAL: unknown request-log writer exception; spool retained\n");
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
    }
}
