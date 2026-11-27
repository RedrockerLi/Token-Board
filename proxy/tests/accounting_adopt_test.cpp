/// 04-C-12 regression: adopting a second accounting reservation onto the same
/// thread must trip the premise check in ProxyServer::adopt_accounting_reservation
/// (proxy/src/core/proxy_server.h), not silently overwrite the hand-off slot.
///
/// The thread_local reservation is a carrier, never a second slot: adopting
/// twice would release or account against the wrong request.  The assertion is
/// therefore expected to abort the process.  This test runs the double adopt in
/// a child process so the assertion cannot take CTest down with it: the parent
/// inspects the child's exit status (134, i.e. SIGABRT) and stderr (the assert
/// text naming accounting_reservation_ in proxy_server.h).
///
/// Usage: accounting_adopt_test            (parent, exits 0 when guarded)
///        accounting_adopt_test <arg>      (child, aborts on the second adopt)
#include "proxy_server.h"

#include "upstream_client.h"

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace {

/// Only path that reaches the assertion: two non-null reservations adopted on
/// one thread.  UsageReservation's default constructor is public; no database
/// slot is needed because the second adopt aborts before any use.
[[noreturn]] void run_double_adopt_child() {
    Database db;
    Router router(db);
    UpstreamClient upstream;
    UsageRecorder recorder(db);
    CodecRegistry codecs;
    ProxyServer server(db, router, upstream, recorder, codecs);

    auto first = std::make_shared<UsageReservation>();
    auto second = std::make_shared<UsageReservation>();
    server.adopt_accounting_reservation(first);
    // Pre-fix this silently replaced `first`; the guard asserts instead.
    server.adopt_accounting_reservation(second);

    std::fprintf(stderr,
                 "accounting_adopt_test: double adopt did not abort\n");
    std::_Exit(97);
}

std::string read_all(int fd) {
    std::string out;
    char buffer[512];
    for (;;) {
        const ssize_t got = ::read(fd, buffer, sizeof buffer);
        if (got == 0) break;
        if (got < 0) {
            if (errno == EINTR) continue;
            break;
        }
        out.append(buffer, static_cast<std::size_t>(got));
    }
    return out;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc > 1) run_double_adopt_child();

    int pipe_fds[2];
    assert(::pipe(pipe_fds) == 0);

    const pid_t child = ::fork();
    assert(child >= 0);
    if (child == 0) {
        ::close(pipe_fds[0]);
        ::dup2(pipe_fds[1], STDERR_FILENO);
        ::close(pipe_fds[1]);
        // The child is expected to abort; never let it leave a core file.
        struct rlimit no_core{0, 0};
        ::setrlimit(RLIMIT_CORE, &no_core);
        ::execl("/proc/self/exe", "accounting_adopt_test", "--double-adopt",
                static_cast<char *>(nullptr));
        std::fprintf(stderr, "accounting_adopt_test: exec failed: %s\n",
                     std::strerror(errno));
        std::_Exit(127);
    }

    ::close(pipe_fds[1]);
    const std::string child_stderr = read_all(pipe_fds[0]);
    ::close(pipe_fds[0]);

    int status = 0;
    assert(::waitpid(child, &status, 0) == child);

    int shell_code = -1;
    if (WIFEXITED(status)) shell_code = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) shell_code = 128 + WTERMSIG(status);

    std::printf("double adopt child exit=%d (expected 134), stderr=%zu bytes\n",
                shell_code, child_stderr.size());
    std::fflush(stdout);
    if (shell_code != 134)
        std::fprintf(stderr, "child stderr:\n%s\n", child_stderr.c_str());

    // Exit 134 is SIGABRT (128+6) as reported by a shell; a direct exit(134)
    // is accepted too, so the check is on the observable process contract.
    assert(shell_code == 134);
    assert(child_stderr.find("accounting_reservation_") != std::string::npos);
    assert(child_stderr.find("proxy_server.h") != std::string::npos);
    assert(child_stderr.find("Assertion") != std::string::npos);

    std::printf("double adopt aborted in the child at the proxy_server.h "
                "assert; parent stayed healthy\n");
    return 0;
}
