# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Git and content identity for CI, with candidate files treated as data."""

from __future__ import annotations

import io
import os
import re
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dokimi.external.catalog_git import _git


def revision(root: Path, ref: str = "HEAD") -> str:
    done = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    value = done.stdout.strip()
    if done.returncode or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError(f"cannot resolve source revision {ref!r}")
    return value


def _repository(root: Path) -> str:
    supplied = os.environ.get("GITHUB_REPOSITORY", "")
    if supplied:
        return supplied
    for remote in ("fork", "origin"):
        done = _git(root, "config", "--get", f"remote.{remote}.url")
        match = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", done.stdout.strip())
        if match:
            return match.group(1)
    return "local/local"


def source_identity(root: Path, *, base: str | None = None, source_repository: str | None = None,
                    expected_head: str | None = None, expected_base: str | None = None,
                    clean: bool = False) -> dict[str, Any]:
    checked = revision(root)
    head = expected_head or checked
    if head != checked:
        parent = _git(root, "rev-parse", "--verify", "HEAD^2")
        if parent.returncode or parent.stdout.strip() != head:
            raise ValueError("expected head differs from the checked commit and merge head")
    base_sha = revision(root, base or expected_base or ("HEAD^1" if head != checked else head))
    if head != checked and revision(root, "HEAD^1") != base_sha:
        raise ValueError("comparison base differs from the checked merge base")
    if expected_base and base_sha != expected_base:
        raise ValueError("expected base differs from the resolved comparison base")
    if clean:
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        if status.returncode or status.stdout:
            raise ValueError("live CI requires a clean committed source checkout")
    repository = source_repository or _repository(root)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("source repository must be owner/name")
    return {"repository": repository, "head_sha": head, "base_sha": base_sha, "checked_sha": checked}


@contextmanager
def base_tree(root: Path, ref: str) -> Iterator[Path]:
    """Read a pinned base without running hooks, filters, installers or checkout scripts."""
    sha = revision(root, ref)
    done = _git(root, "archive", "--format=tar", sha)
    if done.returncode:
        raise ValueError("cannot export the trusted comparison base")
    if len(done.stdout_bytes) > 256 * 1024 * 1024:
        raise ValueError("trusted base archive exceeds 256 MiB")
    with tempfile.TemporaryDirectory(prefix="dokimi-ci-base-") as tmp:
        target = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(done.stdout_bytes)) as archive:
            archive.extractall(target, filter="data")
        yield target


@contextmanager
def trusted_base(root: Path, sha: str, checkout: Path | None = None) -> Iterator[Path]:
    """Use a clean checkout of the exact base, or export its committed data."""
    if checkout is None:
        with base_tree(root, sha) as target:
            yield target
        return
    checkout = checkout.resolve()
    if revision(checkout) != sha:
        raise ValueError("trusted base checkout does not match the pinned base revision")
    dirty = _git(checkout, "status", "--porcelain", "--untracked-files=all", "--ignored")
    if dirty.returncode or dirty.stdout:
        raise ValueError("trusted base checkout must be clean")
    yield checkout
