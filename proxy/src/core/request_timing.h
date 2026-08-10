#pragma once

inline thread_local int request_queue_delay_ms = 0;

inline void set_request_queue_delay_ms(int value) noexcept {
    request_queue_delay_ms = value;
}

inline int current_request_queue_delay_ms() noexcept {
    return request_queue_delay_ms;
}
