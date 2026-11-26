# Vendored dependency patches

`proxy/third_party/` holds vendored copies of third-party code. Every local
edit to a vendored file carries a `// TB-PATCH:` marker and is registered
below, so a future upstream upgrade can re-apply it instead of silently
losing the fix.

Find every registered patch:

```sh
grep -rn 'TB-PATCH' proxy/third_party/
```

## httplib.h

- Upstream: cpp-httplib 0.18.1 (`CPPHTTPLIB_VERSION "0.18.1"` in the header,
  <https://github.com/yhirose/cpp-httplib>)
- Vendored at: `proxy/third_party/httplib.h`

### TB-PATCH-1 — bound chunked and EOF-delimited request bodies

- Location: `proxy/third_party/httplib.h:4386-4406`, `detail::read_content`:
  the `limited_out` counting receiver plus the two call sites that now pass
  it to `read_content_chunked()` and `read_content_without_length()`.
- Upstream behaviour: `payload_max_length` is compared against the
  `Content-Length` header only. `read_content_chunked()` and
  `read_content_without_length()` take no limit at all, so a
  `Transfer-Encoding: chunked` body or a body without any length header is
  read to its terminator (or to EOF) with no bound. `Server::set_payload_max_length`
  therefore only protected one of the three framings.
- Why: CVE-class unbounded read. The whole body is accumulated in `req.body`
  before any handler runs, so a single client can exhaust proxy memory
  regardless of the configured limit. Audit: `04-C-14` (32 MB cap main entry)
  and `05-TB05-F01` (test coverage of the same bypass). Measured on the
  unpatched build: chunked 40 MiB → `200 OK` (fully read, forwarded
  end-to-end); a single 128 MiB chunked upload moved VmHWM up by 138 MiB.
- Local behaviour: the bytes actually handed to the receiver are counted; if
  the next buffer would exceed `payload_max_length`, the receiver returns
  `false`, the read stops immediately and `detail::read_content` sets
  `StatusCode::PayloadTooLarge_413`. A body exactly at the cap still passes
  (the check is `n > cap - received`, not `>=`). Counting happens after
  `Content-Encoding` handling, i.e. it bounds the bytes kept in memory even
  when a compressed body is decompressed on the fly. Because the read stops
  mid-stream the socket still holds unread body bytes, so the 413 is
  followed by a connection close.
- Regression: CTest `payload_limit` (`proxy/tests/payload_limit_test.py`),
  cases `chunked_40MiB_413`, `content_length_exactly_32MiB_200`,
  `chunked_exactly_32MiB_200`, `content_length_32MiB_plus_1_413`,
  `chunked_32MiB_plus_1_413`, `no_length_eof_413`,
  `no_length_without_eof_413`, `VmHWM_chunked_32MiB_plus_1_413`,
  `VmHWM_chunked_128MiB_413`, `VmHWM_bounded_for_32MiB_plus_1`,
  `VmHWM_bounded_for_128MiB`. The 32 MiB value itself comes from
  `proxy/src/main.cpp` (`set_payload_max_length(32u * 1024u * 1024u)`) and is
  not part of this patch.
- Re-apply on upgrade: keep upstream's three-way branch order and route the
  chunked and no-length branches through a counting receiver as above. Keep
  the cap driven by the `payload_max_length` argument instead of a constant:
  `detail::read_content` is shared with the client response reader
  (`ClientImpl`, which passes `SIZE_MAX`), so a hard-coded limit in the two
  helpers would cap response decoding too.

## json.hpp, sqlite3.c, sqlite3.h

No local patch is registered for these files. Any future local edit must add
a `// TB-PATCH:` marker and a section here.
