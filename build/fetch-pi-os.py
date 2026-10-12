#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fetch a Raspberry Pi OS Lite image and its sha256, idempotently.

Usage:
    fetch-pi-os.py <url> <cache-dir>

<url> may be the naked "latest" endpoint or a concrete .img.xz URL, in which case
the published <url>.sha256 is also fetched. The real file name (after any redirect)
is printed on stdout. An existing cache file whose hash matches the published one is
left untouched and no download happens.

Cached files, the expected hash and a temp download are tracked by file name.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request


def head_final(url: str) -> str:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as r:
        return r.geturl()


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as r:
        return r.read().decode()


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_image(url: str, cache_dir: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    final = head_final(url)
    name = final.rsplit("/", 1)[1]
    dest = os.path.join(cache_dir, name)

    # The published checksum is required: without it there is no way to know the
    # cache is still the current image, so refuse rather than download unchecked.
    try:
        expected = fetch_text(final + ".sha256").split()[0]
    except Exception as e:
        raise RuntimeError(f"no published sha256 for {final}: {e}")

    if os.path.exists(dest) and sha256_of(dest) == expected:
        print(f"already up to date: {dest}", file=sys.stderr)
        return name

    print(f"downloading {final}", file=sys.stderr)
    tmp = dest + ".part"
    with urllib.request.urlopen(final) as r, open(tmp, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    if sha256_of(tmp) != expected:
        os.remove(tmp)
        raise RuntimeError(f"checksum mismatch for {name}")
    os.replace(tmp, dest)
    with open(dest + ".sha256", "w") as f:
        f.write(f"{expected}  {name}\n")
    return name


def main() -> None:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <url> <cache-dir>", file=sys.stderr)
        return 2
    try:
        name = fetch_image(sys.argv[1], sys.argv[2])
    except Exception as e:
        sys.exit(f"error: {e}")
    print(name)


if __name__ == "__main__":
    sys.exit(main())
