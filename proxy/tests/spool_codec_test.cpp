#include "store/database_internal.h"

#include <atomic>
#include <cassert>
#include <chrono>
#include <csignal>
#include <fcntl.h>
#include <string>
#include <thread>
#include <vector>

#include <unistd.h>

namespace {

volatile std::sig_atomic_t alarm_seen = 0;

void remember_alarm(int) {
    alarm_seen = 1;
}

std::string frame_for(const std::string &payload) {
    std::string frame;
    append_u32_le(frame, static_cast<std::uint32_t>(payload.size()));
    append_u32_le(frame, spool_checksum(payload.data(), payload.size()));
    frame += payload;
    return frame;
}

void write_frame(int fd, const std::string &frame, std::size_t size) {
    assert(::ftruncate(fd, 0) == 0);
    assert(::lseek(fd, 0, SEEK_SET) == 0);
    assert(write_exact(fd, frame.data(), size));
}

}  // namespace

int main() {
    const std::string payload = "{\"request_id\":\"spool-fixture\"}";
    const std::string frame = frame_for(payload);
    char path[] = "/tmp/token-board-spool-codec-XXXXXX";
    const int fd = ::mkstemp(path);
    assert(fd >= 0);
    ::unlink(path);

    // Every truncation boundary must be rejected, while the complete frame
    // must remain readable. This is the frozen on-disk framing contract.
    for (std::size_t cut = 0; cut < frame.size(); ++cut) {
        write_frame(fd, frame, frame.size());
        assert(::ftruncate(fd, static_cast<off_t>(cut)) == 0);
        char header[kSpoolHeaderBytes]{};
        const bool header_ok = pread_exact(fd, header, sizeof(header), 0);
        if (cut < kSpoolHeaderBytes) {
            assert(!header_ok);
            continue;
        }
        assert(header_ok);
        const std::uint32_t length = read_u32_le(header);
        assert(length == payload.size());
        std::string body(length, '\0');
        assert(!pread_exact(fd, body.data(), body.size(), kSpoolHeaderBytes) ||
               cut == frame.size());
    }
    write_frame(fd, frame, frame.size());
    char header[kSpoolHeaderBytes]{};
    assert(pread_exact(fd, header, sizeof(header), 0));
    std::string body(payload.size(), '\0');
    assert(pread_exact(fd, body.data(), body.size(), kSpoolHeaderBytes));
    assert(read_u32_le(header + 4) == spool_checksum(body.data(), body.size()));

    // A checksum mismatch is not a valid frame even when its bytes are
    // complete.
    header[4] ^= 0x01;
    assert(read_u32_le(header + 4) != spool_checksum(body.data(), body.size()));

    // The production reader rejects an overlong length before allocating or
    // reading a payload. Keep the same bound in this format fixture.
    std::string overlong;
    append_u32_le(overlong, 256 * 1024 + 1);
    append_u32_le(overlong, 0);
    write_frame(fd, overlong, overlong.size());
    assert(pread_exact(fd, header, sizeof(header), 0));
    assert(read_u32_le(header) > 256 * 1024);
    assert(bounded_string("0123456789", 4) == "0123");

    // Exercise the EINTR retry path deterministically: keep a pipe full,
    // interrupt the blocked write, then release one read chunk.
    int pipe_fds[2]{};
    assert(::pipe(pipe_fds) == 0);
    const int flags = ::fcntl(pipe_fds[1], F_GETFL, 0);
    assert(flags >= 0);
    assert(::fcntl(pipe_fds[1], F_SETFL, flags | O_NONBLOCK) == 0);
    std::string filler(4096, 'x');
    while (::write(pipe_fds[1], filler.data(), filler.size()) > 0) {}
    assert(errno == EAGAIN || errno == EWOULDBLOCK);
    assert(::fcntl(pipe_fds[1], F_SETFL, flags) == 0);

    std::atomic<bool> write_finished{false};
    std::thread reader([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        char buffer[4096]{};
        assert(::read(pipe_fds[0], buffer, sizeof(buffer)) > 0);
        while (!write_finished.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
    });
    auto previous = std::signal(SIGALRM, remember_alarm);
    alarm_seen = 0;
    ::ualarm(100000, 0);
    const char marker = 'm';
    assert(write_exact(pipe_fds[1], &marker, 1));
    ::ualarm(0, 0);
    std::signal(SIGALRM, previous);
    write_finished = true;
    reader.join();
    assert(alarm_seen == 1);
    ::close(pipe_fds[0]);
    ::close(pipe_fds[1]);
    ::close(fd);
    return 0;
}
