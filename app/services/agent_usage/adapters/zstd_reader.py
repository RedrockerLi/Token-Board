"""Zstandard decoding for compressed agent usage logs.

Codex compresses rollouts older than a week into ``<name>.jsonl.zst`` and
deletes the plaintext, so the cold archive is the only copy left.  Decoding
goes through a library only: the standard-library ``compression.zstd`` module
(Python 3.14+), the ``backports.zstd`` backport, or ``zstandard``.  No external
``zstd`` binary is required or invoked.

A cold archive is decoded as one complete file: concatenated frames are all
decoded, and an incomplete or corrupt stream raises :class:`ZstdDecodeError`
instead of silently returning the part that happened to decode.
"""

from __future__ import annotations

import importlib
from types import ModuleType

# 按优先级尝试的解码库:标准库(3.14+)、backport、第三方绑定。
# ``compression.zstd.decompress`` 与 ``backports.zstd.decompress`` 都会解完
# 拼接在一起的每一帧;``zstandard`` 没有同名函数,由 ``_decode_zstandard``
# 逐帧解码。
_BACKENDS = ("compression.zstd", "backports.zstd", "zstandard")


class ZstdUnavailable(RuntimeError):
    """No Zstandard decoder library is importable."""


class ZstdDecodeError(ValueError):
    """The payload is not a complete Zstandard stream."""


def _import_backend(name: str) -> ModuleType | None:
    """Import one decoder library; tests replace this to fake capabilities."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _backend() -> tuple[str, ModuleType] | None:
    """Return the highest-priority importable decoder, or None."""
    for name in _BACKENDS:
        module = _import_backend(name)
        if module is not None:
            return name, module
    return None


def available() -> bool:
    """Whether any supported Zstandard decoder library is importable."""
    return _backend() is not None


def _decode_zstandard(module: ModuleType, raw: bytes) -> bytes:
    """Decode every frame of a concatenated stream with ``zstandard``.

    ``zstandard``'s one-shot ``ZstdDecompressor.decompress`` stops after the
    first frame, and its stream reader silently drops an incomplete trailing
    frame.  Walking ``decompressobj`` keeps both behaviours out: each frame is
    decoded, and a frame that does not reach its end-of-stream marker raises.
    """
    decompressor = module.ZstdDecompressor()
    chunks = []
    remaining = raw
    while remaining:
        frame = decompressor.decompressobj()
        chunks.append(frame.decompress(remaining))
        chunks.append(frame.flush())
        if not frame.eof:
            raise ZstdDecodeError("incomplete Zstandard frame")
        remaining = frame.unused_data or b""
    return b"".join(chunks)


def decode_bytes(raw: bytes) -> bytes:
    """Decode a complete Zstandard archive into its uncompressed bytes.

    Raises :class:`ZstdUnavailable` when no decoder library is importable and
    :class:`ZstdDecodeError` when the payload is empty, truncated, or not a
    Zstandard stream.
    """
    if not raw:
        raise ZstdDecodeError("empty Zstandard archive")
    backend = _backend()
    if backend is None:
        raise ZstdUnavailable(
            "no Zstandard decoder library available "
            "(compression.zstd, backports.zstd, zstandard)")
    name, module = backend
    try:
        if name == "zstandard":
            return _decode_zstandard(module, raw)
        return module.decompress(raw)
    except ZstdDecodeError:
        raise
    except Exception as exc:  # each backend has its own error type
        raise ZstdDecodeError(str(exc)) from exc
