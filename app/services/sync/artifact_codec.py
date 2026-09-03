"""Streaming codecs for SQLite synchronization artifacts.

The public interface deliberately works with filesystem paths rather than
SQLite connections.  Callers can therefore keep the database transaction and
the wire transfer separate while this module owns temporary files, bounded
copying, gzip framing, SQLite validation, and atomic replacement.
"""

from __future__ import annotations

import gzip
import os
import tempfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


CHUNK_SIZE = 1024 * 1024
MAX_DECOMPRESSED_BYTES = 1 << 30
SQLITE_HEADER = b"SQLite format 3\x00"
GZIP_SUFFIX = ".db.gz"
SQLITE_SUFFIX = ".db"


class ArtifactCodecError(Exception):
    """Raised when an artifact cannot be encoded or materialized safely."""


def is_gzip_artifact(name: str) -> bool:
    """Return whether *name* uses the explicit gzip artifact suffix."""
    return str(name).endswith(GZIP_SUFFIX)


def _temporary_path(directory: Path, *, name: str, suffix: str) -> tuple[int, Path]:
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{name}.", suffix=suffix, dir=directory)
    return fd, Path(raw_path)


def _copy_bounded(source, destination, *, limit: int) -> int:
    copied = 0
    while True:
        block = source.read(CHUNK_SIZE)
        if not block:
            return copied
        copied += len(block)
        if copied > limit:
            raise ArtifactCodecError(
                f"decompressed artifact exceeds {limit} byte limit")
        destination.write(block)


@contextmanager
def encode_sqlite_artifact(source: str | Path) -> Iterator[Path]:
    """Yield a deterministic, temporary gzip representation of *source*.

    The source is read in bounded chunks and is never modified.  The yielded
    path remains valid until the context exits, after which it is removed even
    if the upload fails.
    """
    source_path = Path(source)
    fd: int | None = None
    encoded_path: Path | None = None
    try:
        fd, encoded_path = _temporary_path(
            source_path.parent, name=source_path.name, suffix=GZIP_SUFFIX)
        with os.fdopen(fd, "wb") as output:
            fd = None
            with gzip.GzipFile(
                    filename="", mode="wb", fileobj=output,
                    compresslevel=6, mtime=0) as compressed:
                with source_path.open("rb") as input_file:
                    while True:
                        block = input_file.read(CHUNK_SIZE)
                        if not block:
                            break
                        compressed.write(block)
            output.flush()
            os.fsync(output.fileno())
        yield encoded_path
    except (OSError, gzip.BadGzipFile) as exc:
        raise ArtifactCodecError(f"unable to encode SQLite artifact: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if encoded_path is not None:
            encoded_path.unlink(missing_ok=True)


def decode_sqlite_artifact(payload: str | Path, remote_name: str,
                           destination: str | Path) -> None:
    """Decode a `.db` or `.db.gz` payload into *destination* atomically.

    A gzip payload is fully consumed so its CRC and end-of-stream marker are
    checked before the destination is replaced.  The destination is also
    checked for the SQLite file header, and remains unchanged on every error.
    """
    payload_path = Path(payload)
    destination_path = Path(destination)
    if not (is_gzip_artifact(remote_name) or str(remote_name).endswith(SQLITE_SUFFIX)):
        raise ArtifactCodecError(f"unsupported artifact name: {remote_name}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    decoded_path: Path | None = None
    try:
        fd, decoded_path = _temporary_path(
            destination_path.parent, name=destination_path.name, suffix=".decoded")
        with os.fdopen(fd, "wb") as output:
            fd = None
            if is_gzip_artifact(remote_name):
                with gzip.open(payload_path, "rb") as input_file:
                    _copy_bounded(input_file, output, limit=MAX_DECOMPRESSED_BYTES)
            else:
                with payload_path.open("rb") as input_file:
                    _copy_bounded(input_file, output, limit=MAX_DECOMPRESSED_BYTES)
            output.flush()
            os.fsync(output.fileno())

        with decoded_path.open("rb") as input_file:
            if input_file.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise ArtifactCodecError("decoded artifact is not a SQLite database")
        os.replace(decoded_path, destination_path)
        decoded_path = None
    except ArtifactCodecError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise ArtifactCodecError(
            f"unable to decode SQLite artifact {remote_name}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if decoded_path is not None:
            decoded_path.unlink(missing_ok=True)


__all__ = [
    "ArtifactCodecError", "CHUNK_SIZE", "GZIP_SUFFIX",
    "MAX_DECOMPRESSED_BYTES", "SQLITE_HEADER", "decode_sqlite_artifact",
    "encode_sqlite_artifact", "is_gzip_artifact",
]
