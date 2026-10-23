# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Hosted CI speed study: pip vs uv installs, and pytest-xdist load vs worksteal.

One job (one VM) runs one repeat. Windows: per installer one excluded primer, then
the two measured arms in seeded random order (venv, tool bootstrap, wheel build,
unlocked wheel install, installed smoke, hash-locked install, two test files).
Linux: per installer one excluded install-only primer, then four measured
conditions (pip|uv) x (load|worksteal) in seeded random order, each with a fresh
source archive and venv, running the real CI pytest command. uv lives in its own
tools env, installed with --require-hashes from the uv block of
requirements-ci.lock.txt; its provisioning time is recorded and must be counted
against uv. Caches are per installer and warmed by the primers. Scope: clean
environments, not the global-Python CI jobs; the Windows wheel install is a pinned loose
resolve, and only the Windows smoke path and the Linux test job are covered, not all
workflows. Every child runs in its own process group and its tree is killed on timeout.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import stat
import subprocess  # nosec B404 - fixed argv lists only, never a shell
import sys
import tarfile
import threading
import time
import xml.etree.ElementTree as ET  # nosec B405 - own pytest output, size-checked
from collections import Counter
from pathlib import Path

BASE = "92d92d3a7337851540dcce969a2095df5b83b93f"
BENCH_FILES = {".github/workflows/ci.yml", "dokimi/scripts/ci_speed_study.py"}
UV_VERSION = "0.13.4"
WIN_TOOLS = ["pip==26.2.2", "build==1.6.1", "setuptools==84.2.1", "wheel==0.48.0",
             "packaging==26.3", "pyproject-hooks==1.3.3", "colorama==0.4.6"]
WIN_TESTS = ["tests/test_cli_help.py", "tests/test_console_encoding.py"]
INPUTS = ["pyproject.toml", "requirements.lock.txt", "requirements-ci.lock.txt",
          "scripts/smoke_installed.py", *WIN_TESTS]
SEED = "424242"
MAX_XML = 64 << 20
LIMITS = {"install": 1200, "lock_install": 1200, "tests": 1500, "default": 600}
DROP_ENV = {"GITHUB_TOKEN", "GH_TOKEN", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"}
WIN = os.name == "nt"
BIN = "Scripts" if WIN else "bin"
EXE = ".exe" if WIN else ""
DEADLINE = time.monotonic() + 48 * 60
LOG_TAIL = 200_000
CLEANUP_LIMIT = 90  # seconds per condition; DEADLINE + 3 min is the hard end of all cleanup
# The latest exact-base normal CI run (base-linux-test-log.txt, base-windows-test-log.txt).
BASE_LINUX = {"items": 7408, "passed": 7387, "skipped": 21}
BASE_WIN_TESTS = 90
# Tests that skip without a git work tree. Whole module (None) or named tests; none may skip.
GIT_TESTS = [("test_growth_ratchet", None), ("test_repo_eval_task_selection", None),
             ("test_stack_health", None),
             ("test_agent_profiles", "test_over_cap_bundled_resources_are_known"),
             ("test_docs_review_triggers", "test_every_depends_on_glob_matches_a_tracked_file"),
             ("test_advisory_lists", "test_hypothesis_example_database_is_git_ignored"),
             ("test_cli_surface", "test_workflow_ignores_base_branch_changes_made_after_the_merge_commit"),
             ("test_cli_surface", "test_workflow_reports_error_and_passes_when_the_head_cannot_be_dumped"),
             ("test_cli_surface", "test_workflow_reports_error_and_passes_when_the_diff_crashes")]


class StudyError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink() and ".git" not in p.relative_to(root).parts}


def git_blob_sha(data: bytes) -> str:
    """The Git object id of a blob (SHA-1 over 'blob N\\0' + data); identity, not security."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # The runner environment as CI tests see it, minus anything token-like.
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in DROP_ENV and not k.upper().startswith(("ACTIONS_", "INPUT_"))}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.update(extra or {})
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and its descendants: its own process group on POSIX (it was started
    as a session leader), `taskkill /T /F` on Windows (descendants still linked to it)."""
    try:
        if WIN:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"],  # nosec B603 - fixed argv, integer pid
                           capture_output=True, timeout=30, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def spawn(cmd: list[str], *, cwd: Path, env: dict[str, str] | None, timeout: float,
          stdout, stderr) -> tuple[int, bytes | None, bytes | None]:
    """One child in its own process group; on timeout or interrupt the whole tree is killed."""
    kw = {} if WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr, **kw)  # nosec B603 - fixed argv lists, no shell
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException as exc:
        kill_tree(proc)
        if isinstance(exc, subprocess.TimeoutExpired):
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        raise
    return proc.returncode, out, err


def run(cmd: list[str], log: Path, *, cwd: Path, env: dict[str, str], limit: int) -> str:
    """Run one bounded command, output to `log`; return the log tail. Raises StudyError."""
    remaining = int(DEADLINE - time.monotonic())
    if remaining <= 5:
        raise StudyError("job deadline reached")
    log.parent.mkdir(parents=True, exist_ok=True)
    problem = None
    rc = -1
    with log.open("ab") as fh:
        fh.write(("$ " + " ".join(cmd) + "\n").encode())
        fh.flush()
        try:
            rc, _, _ = spawn(cmd, cwd=cwd, env=env, timeout=min(limit, remaining),
                             stdout=fh, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired as exc:
            problem = f"timeout after {exc.timeout}s, process tree killed: {cmd[0]}"
        except OSError as exc:
            problem = f"cannot start {cmd[0]}: {exc}"
    data = log.read_bytes()
    if len(data) > LOG_TAIL:  # keep the head note and the tail so artifacts stay bounded
        log.write_bytes(data[:LOG_TAIL // 4] + b"\n...[truncated]...\n" + data[-3 * LOG_TAIL // 4:])
    if problem:
        raise StudyError(problem)
    if rc != 0:
        raise StudyError(f"exit {rc}: {' '.join(cmd[:4])}")
    return log.read_text(encoding="utf-8", errors="replace")


def capture(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = 120) -> str:
    """Stdout of a short command (stderr kept apart). Raises StudyError on failure or timeout."""
    try:
        rc, out, err = spawn(cmd, cwd=cwd, env=env, timeout=timeout,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        raise StudyError(f"timeout after {timeout}s, process tree killed: {' '.join(cmd[:3])}") from exc
    except OSError as exc:
        raise StudyError(f"cannot start {cmd[0]}: {exc}") from exc
    if rc != 0:
        tail = (err or b"").decode("utf-8", "replace")[-300:]
        raise StudyError(f"exit {rc}: {' '.join(cmd[:3])}: {tail}")
    return (out or b"").decode("utf-8", "replace")


def rmtree(path: Path) -> tuple[bool, float]:
    """Remove `path` within a time bound; return (gone, seconds). A removal that outlives the
    bound is abandoned (daemon thread) and reported as not clean."""
    def onexc(func, p, _exc):  # read-only files on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    def work(box: list[bool]) -> None:
        for _ in range(3):
            if not path.exists():
                break
            try:
                shutil.rmtree(path, onexc=onexc)
            except OSError:
                time.sleep(1)
        box.append(not path.exists())

    t0, box = time.perf_counter(), []
    limit = max(1.0, min(CLEANUP_LIMIT, DEADLINE + 180 - time.monotonic()))
    th = threading.Thread(target=work, args=(box,), daemon=True)
    th.start()
    th.join(limit)
    return bool(box and box[0]), round(time.perf_counter() - t0, 3)


def parse_ls(text: str, tree: bool) -> dict[str, tuple[str, str]]:
    """`git ls-tree -r -z` or `git ls-files -s -z` output as {path: (mode, blob sha)}."""
    out = {}
    for rec in filter(None, text.split("\0")):
        meta, path = rec.split("\t", 1)
        parts = meta.split(" ")
        out[path] = (parts[0], parts[2] if tree else parts[1])
    return out


def anchor_to_base(root: Path, files: dict[str, str], repo: Path) -> tuple[dict, list, list]:
    """Tie the extracted archive to the Git blobs of BASE: same regular-file paths, and each
    file's blob id equals BASE's. On Windows git archive may convert LF to CRLF; a file that
    matches only after CRLF->LF is listed as converted (accounted for), anything else fails."""
    tree = parse_ls(capture(["git", "ls-tree", "-r", "-z", BASE], repo), tree=True)
    regular = {p: sha for p, (mode, sha) in tree.items() if mode in ("100644", "100755")}
    if set(regular) != set(files):
        diff = sorted(set(regular) ^ set(files))
        raise StudyError(f"archive paths differ from {BASE} tree: {diff[:5]} ({len(diff)})")
    blobs, crlf, bad = {}, [], []
    for p in files:
        data = (root / p).read_bytes()
        blobs[p] = git_blob_sha(data)
        if blobs[p] != regular[p]:
            (crlf if git_blob_sha(data.replace(b"\r\n", b"\n")) == regular[p] else bad).append(p)
    return blobs, crlf, bad


def index_blobs(root: Path) -> dict[str, str]:
    """Tracked paths and blob ids of the synthetic repository's index."""
    return {p: sha for p, (_, sha) in parse_ls(capture(["git", "ls-files", "-s", "-z"], root),
                                                tree=False).items()}


def tree_after_tests(root: Path, before: dict[str, str]) -> dict:
    """Did the tests change the tracked tree? Index ids and worktree status, against before."""
    status = [x for x in capture(["git", "status", "--porcelain", "-z", "--untracked-files=no"],
                                 root).split("\0") if x]
    untracked = [x for x in capture(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                                    root).split("\0") if x]
    same_index = index_blobs(root) == before
    return {"unchanged": same_index and not status, "index_same": same_index,
            "modified": status[:20], "untracked_count": len(untracked), "untracked": untracked[:20]}


def prepare_source(repo: Path, lab: Path) -> dict:
    """git archive of BASE; fail closed unless HEAD differs from BASE in the two study files only."""
    try:
        capture(["git", "merge-base", "--is-ancestor", BASE, "HEAD"], repo)
    except StudyError as exc:
        raise StudyError(f"{BASE} is not an ancestor of HEAD") from exc
    changed = set(capture(["git", "diff", "--name-only", BASE, "HEAD"], repo).split())
    if changed != BENCH_FILES:
        raise StudyError(f"study commit changes unexpected files: {sorted(changed)}")
    tarball = lab / "source.tar"
    capture(["git", "archive", "--format=tar", "-o", str(tarball), BASE], repo)
    pristine = lab / "pristine"
    pristine.mkdir(parents=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(pristine, filter="data")
    for rel in INPUTS:
        if not (pristine / "dokimi" / rel).is_file():
            raise StudyError(f"archive lacks dokimi/{rel}")
    files = tree_manifest(pristine)
    blobs, crlf, bad = anchor_to_base(pristine, files, repo)
    if bad:
        raise StudyError(f"archive differs from {BASE} git blobs: {bad[:5]} ({len(bad)})")
    return {"base": BASE, "head": capture(["git", "rev-parse", "HEAD"], repo).strip(),
            "epoch": capture(["git", "log", "-1", "--format=%ct", BASE], repo).strip(),
            "base_tree": capture(["git", "rev-parse", f"{BASE}^{{tree}}"], repo).strip(),
            "blobs": blobs, "crlf_converted": crlf[:20], "crlf_converted_count": len(crlf),
            "tarball_sha256": sha256(tarball), "file_count": len(files),
            "manifest_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "lock_sha256": files["dokimi/requirements.lock.txt"],
            "ci_lock_sha256": files["dokimi/requirements-ci.lock.txt"],
            "files": files, "dir": str(pristine)}


def fresh_source(src: dict, dest: Path, epoch: str) -> tuple[Path, dict[str, str]]:
    """Clean copy of the archive, verified file by file. Several tests ask git (ls-files,
    check-ignore, the growth baseline at origin/main), so a one-commit repository of the same
    files is made with a fixed identity and date; without it they skip and the run would not
    be the CI test set. Returns the package dir and the index (path -> blob id) before tests."""
    shutil.copytree(src["dir"], dest)
    if tree_manifest(dest) != src["files"]:
        raise StudyError("copied source differs from the archive manifest")
    env = base_env({"GIT_AUTHOR_NAME": "study", "GIT_AUTHOR_EMAIL": "study@example.invalid",
                    "GIT_COMMITTER_NAME": "study", "GIT_COMMITTER_EMAIL": "study@example.invalid",
                    "GIT_AUTHOR_DATE": f"{epoch} +0000", "GIT_COMMITTER_DATE": f"{epoch} +0000"})
    # No CRLF/safecrlf rewriting; -f so an ignored-but-archived file is still tracked.
    git = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
           "-c", "commit.gpgsign=false"]
    for args in (["init", "-q", "-b", "main"], ["add", "-f", "-A"], ["commit", "-q", "-m", "base"],
                 # CI checks out with full history, so origin/main exists there.
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        capture([*git, *args], dest, env, timeout=300)
    index = index_blobs(dest)
    if set(index) != set(src["files"]) or index != src["blobs"]:
        raise StudyError("tracked index paths/blob ids differ from the archive manifest")
    return dest / "dokimi", index


def uv_requirement(lock: Path, out: Path) -> int:
    """Write the uv block (pin and every hash) of the CI lock; fail closed if it is not uv==UV_VERSION."""
    lines, take = [], False
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line.startswith("uv=="):
            take = True
        elif take and not line.startswith("    --hash="):
            break
        if take:
            lines.append(line)
    hashes = [x for x in lines if "--hash=sha256:" in x]
    if not lines or lines[0].split()[0] != f"uv=={UV_VERSION}" or not hashes:
        raise StudyError(f"lock does not hold uv=={UV_VERSION} with hashes")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(hashes)


def provision_uv(lab: Path, src: dict, out: Path) -> dict:
    """uv in its own env from the hash-checked lock block. Its seconds count against uv."""
    t0 = time.perf_counter()
    req, env_dir, log = lab / "uv-only.txt", lab / "uv-tools", out / "logs" / "uv_provision.log"
    n = uv_requirement(Path(src["dir"]) / "dokimi" / "requirements-ci.lock.txt", req)
    run([sys.executable, "-m", "venv", str(env_dir)], log, cwd=lab, env=base_env(), limit=300)
    py = str(env_dir / BIN / f"python{EXE}")
    run([py, "-m", "pip", "install", "--require-hashes", "--no-cache-dir", "-r", str(req)],
        log, cwd=lab, env=base_env(), limit=300)
    exe = env_dir / BIN / f"uv{EXE}"
    version = capture([str(exe), "--version"], lab).strip()
    if not version.startswith(f"uv {UV_VERSION}"):
        raise StudyError(f"uv --version is {version!r}")
    return {"exe": str(exe), "version": version, "hashes_in_requirement": n,
            "tools_env_pip": capture([py, "-m", "pip", "--version"], lab).strip(),
            "exe_sha256": sha256(exe), "provision_seconds": round(time.perf_counter() - t0, 3)}

MANIFEST_PY = (
    "import importlib.metadata as m,json;"
    "print(json.dumps(sorted([(d.metadata['Name'] or '').lower().replace('_','-')"
    ".replace('.','-'),d.version] for d in m.distributions())))"
)


def read_xml(path: Path) -> ET.Element:
    data = path.read_bytes()
    if len(data) > MAX_XML or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise StudyError(f"{path.name}: too large or declares a DTD/entity")
    return ET.fromstring(data)  # nosec B314 - local pytest/coverage output, checked above


def parse_junit(path: Path) -> dict:
    """{id: outcome}; a duplicate id would hide a case, so it is rejected (cases == raw count)."""
    cases, dups = {}, []
    for tc in read_xml(path).iter("testcase"):
        bad = [c.tag for c in tc if c.tag in ("failure", "error", "skipped")]
        key = f"{tc.get('classname')}.{tc.get('name')}"
        if key in cases:
            dups.append(key)
        cases[key] = bad[0] if bad else "passed"
    if dups:
        raise StudyError(f"duplicate JUnit ids: {dups[:3]} ({len(dups)})")
    if not cases or any(v in ("failure", "error") for v in cases.values()):
        raise StudyError("JUnit has no cases or a failing case")
    return dict(sorted(cases.items()))


def parse_coverage(path: Path) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for cls in read_xml(path).iter("class"):
        rows = [(int(ln.get("number")), int(ln.get("hits"))) for ln in cls.iter("line")]
        d = out.setdefault(cls.get("filename"), {"executable": [], "covered": []})
        d["executable"] += [n for n, _ in rows]
        d["covered"] += [n for n, h in rows if h > 0]
    return {f: {k: sorted(set(v)) for k, v in d.items()} for f, d in sorted(out.items())}


def canonical(path: Path) -> dict:
    """The report JSON without its one allowed run-specific field, provenance.date."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.get("provenance", {}).pop("date", None)
    return data


class Cond:
    """One condition: fresh source copy, fresh venv, one installer (and one xdist mode on Linux)."""

    def __init__(self, cid: str, kind: str, installer: str, dist: str, ctx: dict):
        self.id, self.kind, self.installer, self.dist, self.ctx = cid, kind, installer, dist, ctx
        self.out: Path = ctx["out"]
        self.work: Path = ctx["lab"] / "runs" / cid
        self.venv, self.py = self.work / "venv", self.work / "venv" / BIN / f"python{EXE}"
        self.cache = str(ctx["lab"] / "cache" / installer)
        self.env = base_env({"VIRTUAL_ENV": str(self.venv), "SOURCE_DATE_EPOCH": ctx["src"]["epoch"],
                             "PATH": str(self.venv / BIN) + os.pathsep + os.environ.get("PATH", "")})
        self.rec: dict = {"id": cid, "kind": kind, "installer": installer, "dist": dist,
                          "status": "running", "stages": {}, "commands": {}}

    def step(self, name: str, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
        """Run one timed stage; the record is on disk before and after it."""
        self.rec["commands"][name] = argv
        write_json(self.out / "runs" / f"{self.id}.json", self.rec)
        t0 = time.perf_counter()
        try:
            text = run(argv, self.out / "logs" / self.id / f"{name}.log", cwd=cwd,
                       env=env or self.env, limit=LIMITS.get(name, LIMITS["default"]))
        finally:
            self.rec["stages"][name] = round(time.perf_counter() - t0, 3)
        print(f"[{self.id}] {name} {self.rec['stages'][name]}s", flush=True)
        return text

    def install_cmd(self, args: list[str]) -> list[str]:
        if self.installer == "pip":
            return [str(self.py), "-m", "pip", "install", "--cache-dir", self.cache, *args]
        return [self.ctx["uv"]["exe"], "pip", "install", "--no-config", "--no-progress",
                "--python", str(self.py), "--cache-dir", self.cache, "--compile-bytecode", *args]

    def manifest(self, name: str, cwd: Path) -> list:
        # stdout only: a stderr warning must not break the JSON line
        rows = [x for x in capture([str(self.py), "-I", "-c", MANIFEST_PY], cwd, self.env).splitlines()
                if x.startswith("[")]
        if not rows:
            raise StudyError(f"manifest {name}: no JSON line in stdout")
        self.rec.setdefault("manifests", {})[name] = json.loads(rows[-1])
        return self.rec["manifests"][name]

    def pip_version(self, label: str) -> None:
        self.rec.setdefault("pip", {})[label] = capture(
            [str(self.py), "-m", "pip", "--version"], self.work / "outside", self.env).strip()

    def after_tests(self, before: dict[str, str]) -> None:
        self.rec["tree_after_tests"] = tree_after_tests(self.work / "src", before)

    def common(self) -> tuple[Path, dict[str, str]]:
        ctx = self.ctx
        pkg, index = fresh_source(ctx["src"], self.work / "src", ctx["src"]["epoch"])
        (self.work / "outside").mkdir(parents=True, exist_ok=True)
        self.step("venv", [sys.executable, "-m", "venv", str(self.venv)], self.work / "outside", base_env())
        self.pip_version("venv")
        return pkg, index

    def lock_install(self, pkg: Path) -> None:
        self.step("lock_install", self.install_cmd(["--require-hashes", "-r", "requirements.lock.txt"]),
                  pkg, {**self.env, "PIP_CACHE_DIR": self.cache})
        self.manifest("post_lock", self.work / "outside")
        self.step("pip_check", [str(self.py), "-m", "pip", "check"], self.work / "outside")
        if sha256(pkg / "requirements.lock.txt") != self.ctx["src"]["lock_sha256"]:
            raise StudyError("requirements.lock.txt changed")

    def linux(self) -> None:
        pkg, index = self.common()
        self.lock_install(pkg)
        if self.kind == "primer":
            return
        junit, pytest = self.out / "junit" / f"{self.id}.xml", str(self.venv / BIN / "pytest")
        cmd = [pytest, "-n", "auto", "--outcomes", "--outcomes-report=outcomes-result.json",
               "--journeys-report=journeys-result.json", "--cov=dokimi", "--cov-branch",
               "--cov-report=term", "--cov-report=xml", f"--randomly-seed={SEED}",
               f"--junitxml={junit}", "--durations=25"]
        if self.dist == "worksteal":
            cmd += ["--dist", "worksteal"]
        text = self.step("tests", cmd, pkg)
        m = re.search(r"(\d+) workers \[(\d+) items\]", text)
        self.rec["workers"], self.rec["items"] = (int(m[1]), int(m[2])) if m else (None, None)
        self.after_tests(index)
        cases = parse_junit(junit)
        if self.rec["items"] != len(cases):
            raise StudyError(f"xdist items {self.rec['items']} != JUnit cases {len(cases)}")
        self.rec["result"] = {"junit": cases, "coverage": parse_coverage(pkg / "coverage.xml"),
                              "outcomes": canonical(pkg / "outcomes-result.json"),
                              "journeys": canonical(pkg / "journeys-result.json")}
        shutil.copy2(pkg / "coverage.xml", self.out / "junit" / f"{self.id}.coverage.xml")

    def windows(self) -> None:
        ctx, py = self.ctx, str(self.py)
        (pkg, index), outside = self.common(), self.work / "outside"
        constraints = ctx["lab"] / "constraints.txt"
        bld = {**self.env, "PIP_CACHE_DIR": str(ctx["lab"] / "cache" / "build"),
               "PIP_CONSTRAINT": str(ctx["lab"] / "build-constraints.txt")}
        self.step("bootstrap", [py, "-m", "pip", "install", "--no-index", "--no-cache-dir",
                                "--find-links", ctx["wheelhouse"], *WIN_TOOLS], outside)
        self.pip_version("after_bootstrap")
        self.manifest("initial", outside)
        dist = self.work / "dist"
        self.step("build", [py, "-m", "build", "--wheel", "--outdir", str(dist), str(pkg)], pkg, bld)
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise StudyError(f"expected one wheel, found {len(wheels)}")
        self.rec["wheel"] = {"name": wheels[0].name, "sha256": sha256(wheels[0])}
        cargs = ["--constraint", str(constraints)] if constraints.is_file() else []
        self.step("wheel_install", self.install_cmd([*cargs, str(wheels[0])]), outside,
                  {**self.env, "PIP_CACHE_DIR": self.cache})
        pre = self.manifest("pre_lock", outside)
        if self.id == "primer-pip":  # the unlocked resolve is pinned for every later arm
            constraints.write_text("".join(f"{n}=={v}\n" for n, v in pre if n != "dokimi"),
                                   encoding="utf-8")
        self.step("smoke", [py, str(pkg / "scripts" / "smoke_installed.py")], outside)
        self.lock_install(pkg)
        junit = self.out / "junit" / f"{self.id}.xml"
        self.step("tests", [str(self.venv / BIN / "pytest"), "-q", f"--randomly-seed={SEED}",
                            f"--junitxml={junit}", *WIN_TESTS], pkg)
        self.after_tests(index)
        self.rec["result"] = {"junit": parse_junit(junit)}

    def execute(self) -> dict:
        t0 = time.perf_counter()
        try:
            self.windows() if self.ctx["family"] == "windows" else self.linux()
            self.rec["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - recorded; the study keeps its other data
            self.rec["status"], self.rec["error"] = "failed", str(exc)
        self.rec["wall_seconds"] = round(time.perf_counter() - t0, 3)
        self.rec["cleanup_ok"], self.rec["cleanup_seconds"] = rmtree(self.work)
        write_json(self.out / "runs" / f"{self.id}.json", self.rec)
        return self.rec

STAGE_SUM = ["venv", "bootstrap", "build", "wheel_install", "smoke", "lock_install", "tests"]
INSTALL_STAGES = ["wheel_install", "lock_install"]


def same(runs: list[dict], get) -> bool:
    vals = [json.dumps(get(r), sort_keys=True) for r in runs]
    return bool(vals) and len(set(vals)) == 1 and vals[0] != "null"


def coverage_diff(runs: list[dict], limit: int = 20) -> list[str]:
    """Files whose executable or covered line sets differ from the first run (bounded list)."""
    ref, diffs = runs[0]["result"]["coverage"], []
    for r in runs[1:]:
        cov = r["result"]["coverage"]
        for f in sorted(set(ref) | set(cov)):
            for k in ("executable", "covered"):
                a, b = set(ref.get(f, {}).get(k, [])), set(cov.get(f, {}).get(k, []))
                if a != b:
                    diffs.append(f"{r['id']} {f} {k}: only-ref {sorted(a - b)[:5]} only-run {sorted(b - a)[:5]}")
    return diffs[:limit]


def git_tests_skipped(junit: dict) -> list[str]:
    """Ids of GIT_TESTS cases that did not pass, plus specs that matched no case at all."""
    bad = []
    for module, name in GIT_TESTS:
        hits = [(k, v) for k, v in junit.items()
                if module in k.split(".") and (name is None or k.split(".")[-1].split("[")[0] == name)]
        bad += [f"{k}={v}" for k, v in hits if v != "passed"]
        if not hits:
            bad.append(f"{module}.{name or '*'}: not collected")
    return bad


def validate(runs: list[dict], family: str) -> tuple[dict, list[str]]:
    ok = [r for r in runs if r["status"] == "ok"]
    g = {"all_runs_ok": bool(runs) and len(ok) == len(runs),
         "cleanup_ok": bool(runs) and all(r.get("cleanup_ok") is True for r in runs),
         "tracked_tree_unchanged_by_tests": all(
             r["tree_after_tests"]["unchanged"] for r in ok if "result" in r),
         "manifest_post_lock": same(ok, lambda r: r.get("manifests", {}).get("post_lock")),
         "junit_ids_and_results": same([r for r in ok if "result" in r],
                                       lambda r: r["result"]["junit"])}
    notes: list[str] = []
    if family == "windows":
        g.update(wheel=same(ok, lambda r: r.get("wheel")),
                 manifest_initial=same(ok, lambda r: r.get("manifests", {}).get("initial")),
                 manifest_pre_lock=same(ok, lambda r: r.get("manifests", {}).get("pre_lock")),
                 ninety_tests_all_passed=bool(ok) and all(
                     len(r["result"]["junit"]) == BASE_WIN_TESTS
                     and set(r["result"]["junit"].values()) == {"passed"} for r in ok))
    else:
        meas = [r for r in ok if r["kind"] == "measured"]
        counts = [Counter(r["result"]["junit"].values()) for r in meas]
        g.update(four_measured=len(meas) == 4,
                 workers_and_items=same(meas, lambda r: [r.get("workers"), r.get("items")]),
                 # tied to the latest exact-base normal CI: 7408 items = 7387 passed + 21 skipped
                 items_equal_base_ci=bool(meas) and all(
                     r["items"] == len(r["result"]["junit"]) == BASE_LINUX["items"] for r in meas),
                 pass_skip_counts_equal_base_ci=bool(meas) and all(
                     c["passed"] == BASE_LINUX["passed"] and c["skipped"] == BASE_LINUX["skipped"]
                     and sum(c.values()) == BASE_LINUX["items"] for c in counts),
                 git_dependent_tests_not_skipped=bool(meas) and not any(
                     git_tests_skipped(r["result"]["junit"]) for r in meas),
                 outcomes_report=same(meas, lambda r: r["result"]["outcomes"]),
                 journeys_report=same(meas, lambda r: r["result"]["journeys"]),
                 outcomes_verdict_pass=bool(meas) and all(
                     r["result"]["outcomes"].get("verdict") == "pass" for r in meas))
        notes = coverage_diff(meas) if meas else []
        g["coverage_line_sets"] = bool(meas) and not notes
        notes += [f"{r['id']} git test not passed: {x}" for r in meas
                  for x in git_tests_skipped(r["result"]["junit"])][:20]
    return g, notes


def metadata(args: argparse.Namespace) -> dict:
    keys = ["ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_REF"]
    out = {"env": {k: os.environ.get(k) for k in keys}, "platform": platform.platform(),
           "cpu_count": os.cpu_count(), "python": sys.version, "family": args.family,
           "repeat": args.repeat, "seed": SEED}
    out["pip"] = capture([sys.executable, "-m", "pip", "--version"], Path.cwd()).strip()
    out["git"] = capture(["git", "--version"], Path.cwd()).strip()
    return out


def summarize(runs: list[dict], tools: dict, family: str) -> dict:
    def row(r: dict) -> dict:
        s = r["stages"]
        return {"id": r["id"], "installer": r["installer"], "dist": r["dist"], "status": r["status"],
                "stages": s, "install_seconds": round(sum(s.get(k, 0) for k in INSTALL_STAGES), 3),
                "job_seconds": round(sum(s.get(k, 0) for k in STAGE_SUM), 3)}

    meas = [row(r) for r in runs if r["kind"] == "measured" and r["status"] == "ok"]
    out: dict = {"measured": meas, "primers_excluded": [row(r) for r in runs if r["kind"] == "primer"],
                 "uv_provision_seconds": tools["provision_seconds"]}

    def avg(inst: str, dist: str | None, key: str) -> float | None:
        v = [m[key] for m in meas if m["installer"] == inst and dist in (None, m["dist"])]
        return round(sum(v) / len(v), 3) if v else None

    for key in ("install_seconds", "job_seconds"):
        pip_s, uv_s = avg("pip", None, key), avg("uv", None, key)
        if pip_s is not None and uv_s is not None:
            out[f"{key}_pip_mean"], out[f"{key}_uv_mean"] = pip_s, uv_s
            out[f"{key}_saved_by_uv_after_provisioning"] = round(
                pip_s - (uv_s + tools["provision_seconds"]), 3)
    if family == "linux":
        for inst in ("pip", "uv"):
            tl = [m["stages"].get("tests") for m in meas if m["installer"] == inst and m["dist"] == "load"]
            tw = [m["stages"].get("tests") for m in meas if m["installer"] == inst and m["dist"] == "worksteal"]
            if tl and tw:
                out[f"tests_seconds_{inst}"] = {"load": tl[0], "worksteal": tw[0],
                                                "worksteal_saved": round(tl[0] - tw[0], 3)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--family", choices=["windows", "linux"], required=True)
    ap.add_argument("--repeat", type=int, required=True)
    ap.add_argument("--python", required=True, help="expected major.minor, e.g. 3.14")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    args = ap.parse_args(argv)
    out, lab, repo = args.out.resolve(), args.lab.resolve(), args.repo.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if lab.exists():
        raise SystemExit(f"lab dir already exists: {lab}")
    lab.mkdir(parents=True)
    t_job = time.perf_counter()
    summary: dict = {"status": "setup", "limits": [
        "clean-environment step comparison, not the global-Python CI jobs; no queue or end-to-end claim",
        "each installer has its own warmed download cache from an excluded primer",
        "uv provisioning seconds must be added to the uv side (see *_saved_by_uv_after_provisioning)",
        "one repeat per VM; no significance claim from one VM",
        "Windows wheel_install is a pinned, loose resolve (constraints from primer-pip), controlled "
        "for pip-vs-uv equality; it excludes the fresh-resolve cost CI pays and is not the CI canary cost",
        "scope is the Windows smoke path and the Linux Tests (py3.14, coverage) job only; the other "
        "workflows (static, typecheck, package-smoke, docs, ...) are not measured",
        "Linux skipped ids are not matched to the base run: base CI does not emit them; only the "
        "21-skip count, identical ids/results across the four conditions and the git-dependent tests "
        "passing are checked",
        "job_seconds sums installer-independent stages and is noise; the headline is install_seconds",
        "source prep, uv provisioning and the Windows wheelhouse are setup costs, recorded, not in stage sums",
        "process-tree kill covers descendants still linked to the child (Windows taskkill /T)"]}
    try:
        if f"{sys.version_info.major}.{sys.version_info.minor}" != args.python:
            raise StudyError(f"python is {sys.version_info[:2]}, expected {args.python}")
        summary["metadata"] = metadata(args)
        t_src = time.perf_counter()
        src = prepare_source(repo, lab)
        summary["source_prepare_seconds_excluded"] = round(time.perf_counter() - t_src, 3)
        summary["source"] = {k: v for k, v in src.items() if k not in ("dir", "files", "blobs")}
        write_json(out / "source-manifest.json", {"sha256": src["files"], "git_blob": src["blobs"]})
        tools = provision_uv(lab, src, out)
        summary["uv"] = tools
        ctx = {"out": out, "lab": lab, "src": src, "uv": tools, "family": args.family}
        if args.family == "windows":
            (lab / "build-constraints.txt").write_text("setuptools==84.2.1\nwheel==0.48.0\n")
            t0 = time.perf_counter()
            run([sys.executable, "-m", "pip", "download", "--dest", str(lab / "wheelhouse"), *WIN_TOOLS],
                out / "logs" / "wheelhouse.log", cwd=lab, env=base_env(), limit=300)
            ctx["wheelhouse"] = str(lab / "wheelhouse")
            summary["wheelhouse_seconds_excluded"] = round(time.perf_counter() - t0, 3)
    except Exception as exc:  # noqa: BLE001
        summary.update(status="failed", error=f"setup: {exc}")
        write_json(out / "summary.json", summary)
        print(f"setup FAILED: {exc}", flush=True)
        return 1
    # nosec B311 - seeded shuffle of the run order, not a security use
    rng = random.Random(f"{os.environ.get('GITHUB_RUN_ID', '0')}-{args.family}-{args.repeat}-{SEED}")  # nosec B311
    dists = ["load"] if args.family == "windows" else ["load", "worksteal"]
    plan = rng.sample([(i, d) for i in ("pip", "uv") for d in dists], len(dists) * 2)
    summary["plan"] = {"primers": ["pip", "uv"], "measured_order": plan}
    summary["status"] = "running"
    write_json(out / "summary.json", summary)
    runs = [Cond(f"primer-{i}", "primer", i, "load", ctx).execute() for i in ("pip", "uv")]
    if all(r["status"] == "ok" for r in runs):
        runs += [Cond(f"measured-{i}-{d}", "measured", i, d, ctx).execute() for i, d in plan]
    guards, notes = validate(runs, args.family)
    guards["source_unchanged"] = tree_manifest(Path(src["dir"])) == src["files"]
    summary.update(runs={r["id"]: {"status": r["status"], "error": r.get("error")} for r in runs},
                   summary=summarize(runs, tools, args.family), guards=guards, coverage_differences=notes,
                   job_wall_seconds=round(time.perf_counter() - t_job, 3))
    summary["status"] = "complete" if all(guards.values()) else "failed"
    write_json(out / "summary.json", summary)
    print(f"status: {summary['status']} guards: {guards}", flush=True)
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
