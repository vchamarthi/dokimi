# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Hosted CI speed study: pip vs uv installs, and pytest-xdist load vs worksteal.

One job (one VM) runs one repeat, in two measured blocks with the condition order of
block 2 the reverse of block 1 (every condition's mean position is then equal, and the
block 1 order follows a Williams square over the repeats). Each condition has a fresh source
archive and venv. Windows: per installer one excluded primer, then (pip|uv) x 2 blocks
(venv, tool bootstrap, wheel build, unlocked wheel install, installed smoke, hash-locked
install, two test files). Linux: per installer one excluded install-only primer, then
(pip|uv) x (load|worksteal) x 2 blocks running the real CI pytest command. Each condition
therefore has a same-condition second run in the same VM; the two-value range is reported as
a descriptive observation to read the comparisons against, not as a variability estimate. uv lives in its own tools env, installed
with --require-hashes from the uv block of requirements-ci.lock.txt; its provisioning time
is recorded and charged once per hypothetical CI job. Caches are per installer and warmed by
the primers. Scope: clean environments, not the global-Python CI jobs; the Windows wheel
install is a pinned loose resolve, and only the Windows smoke path and the Linux test job are
covered, not all workflows. Every child runs in its own process group and its tree is killed
on timeout. Claims are gated one by one: a failed guard leaves the numbers it covers marked
unvalidated. Standard library only. `--self-check` runs synthetic checks of these guards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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

BASE = "94e3eb12ac17b6f714d5519dc910f64d08dfff3f"
BENCH_FILES = {".github/workflows/ci.yml", "dokimi/scripts/ci_speed_study.py"}
UV_VERSION = "0.13.4"
# Pins read from the CI lock of BASE (requirements-ci.lock.txt) and checked against these
# at run time; each is installed with the lock's own hashes. wheel is not in the lock.
LOCK_PINS = {"uv": UV_VERSION, "pip": "26.2.1", "build": "1.6.1", "setuptools": "84.0.0",
             "packaging": "26.3", "pyproject-hooks": "1.3.3", "colorama": "0.4.6"}
WIN_LOCK_TOOLS = ["pip", "build", "setuptools", "packaging", "pyproject-hooks", "colorama"]
WHEEL_PIN = "wheel==0.48.0"
WIN_TOOLS = [f"{n}=={LOCK_PINS[n]}" for n in WIN_LOCK_TOOLS] + [WHEEL_PIN]
WIN_TESTS = ["tests/test_cli_help.py", "tests/test_console_encoding.py"]
INPUTS = ["pyproject.toml", "requirements.lock.txt", "requirements-ci.lock.txt",
          "scripts/smoke_installed.py", *WIN_TESTS]
SEED = "424242"
MAX_XML = 64 << 20
LIMITS = {"wheel_install": 1200, "lock_install": 1200, "tests": 1500, "default": 600}
DROP_ENV = {"GITHUB_TOKEN", "GH_TOKEN", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"}
WIN = os.name == "nt"
BIN = "Scripts" if WIN else "bin"
EXE = ".exe" if WIN else ""
DEADLINE = time.monotonic() + 48 * 60
LOG_TAIL = 200_000
CLEANUP_LIMIT = 90  # seconds per condition; DEADLINE + 3 min is the hard end of all cleanup
# Seconds a first measured condition is assumed to need (observed Linux 170-260, Windows 60-120).
FIRST_NEED = {"linux": 330, "windows": 150}
# The exact-base normal CI run 40010743546 (Tests py3.14: "4 workers [7739 items]",
# "7718 passed, 21 skipped"; Windows smoke: "90 passed"). Its coverage leg runs
# pytest -n auto --outcomes ... --cov=dokimi --cov-branch --cov-report=term --cov-report=xml.
BASE_LINUX = {"items": 7739, "passed": 7718, "skipped": 21}
BASE_WIN_TESTS = 90
# Pre-registered timing/race branches (reviewer finding P1, source read at BASE): the covered
# state of these lines depends on scheduling, not on installer or distribution. Keys are
# coverage.xml filenames (relative to the package dir); "blob" is the Git blob id of the file
# at BASE, and the class applies only while the archive's blob equals it. Only *covered*
# differences on these lines are classified; executable-line differences and every other
# covered difference stay fatal. The blob ids are the Git objects of the frozen BASE tree
# (git ls-tree, race-branch-blob-proof-94.json). A line covered by exactly one installer or
# distribution level is not tolerated (see coverage_report).
RACE_BRANCHES = {
    "process.py": {
        "path": "dokimi/dokimi/process.py", "blob": "c1d43706103ab06088d01296e5cb727cb01bb34d",
        "lines": {565: "except OSError: /proc entry exited during the scan",
                  566: "continue after that exit",
                  570: "return False when the scan finds no live group member"}},
    "harnesses/egress_proxy.py": {
        "path": "dokimi/dokimi/harnesses/egress_proxy.py",
        "blob": "145dfd931c1b490b8c1e1878b833f8c21310077a",
        "lines": {233: "except (socket.timeout, OSError): accept() timed out or the socket closed",
                  234: "continue after that"}}}
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


def lock_blocks(lock: Path, names: list[str]) -> dict[str, list[str]]:
    """The requirement block (pin line and every hash line) of each name in a lock file;
    fail closed unless each is pinned to LOCK_PINS[name] and carries sha256 hashes."""
    out: dict[str, list[str]] = {}
    cur: list[str] | None = None
    for line in lock.read_text(encoding="utf-8").splitlines():
        head = line.split("==", 1)[0] if "==" in line and not line.startswith(" ") else None
        if head is not None:
            cur = out.setdefault(head, []) if head in names else None
        elif cur is not None and not line.startswith("    --hash="):
            cur = None
        if cur is not None:
            cur.append(line)
    for name in names:
        block = out.get(name) or []
        if not block or block[0].split()[0] != f"{name}=={LOCK_PINS[name]}" \
                or not any("--hash=sha256:" in x for x in block):
            raise StudyError(f"lock does not hold {name}=={LOCK_PINS[name]} with hashes")
    return out


def write_lock_requirement(lock: Path, names: list[str], out: Path) -> dict:
    """Write the blocks of `names` as one requirements file; return their pins and sha256."""
    blocks = lock_blocks(lock, names)
    text = "\n".join(x for n in names for x in blocks[n]) + "\n"
    out.write_text(text, encoding="utf-8")
    return {"names": names, "pins": {n: LOCK_PINS[n] for n in names},
            "hashes": sum("--hash=sha256:" in x for n in names for x in blocks[n]),
            "block_sha256": hashlib.sha256(text.encode()).hexdigest()}


def provision_uv(lab: Path, src: dict, out: Path) -> dict:
    """uv in its own env from the hash-checked lock block. Its seconds count against uv."""
    t0 = time.perf_counter()
    req, env_dir, log = lab / "uv-only.txt", lab / "uv-tools", out / "logs" / "uv_provision.log"
    block = write_lock_requirement(Path(src["dir"]) / "dokimi" / "requirements-ci.lock.txt", ["uv"], req)
    n = block["hashes"]
    run([sys.executable, "-m", "venv", str(env_dir)], log, cwd=lab, env=base_env(), limit=300)
    py = str(env_dir / BIN / f"python{EXE}")
    run([py, "-m", "pip", "install", "--require-hashes", "--no-cache-dir", "-r", str(req)],
        log, cwd=lab, env=base_env(), limit=300)
    exe = env_dir / BIN / f"uv{EXE}"
    version = capture([str(exe), "--version"], lab).strip()
    if not version.startswith(f"uv {UV_VERSION}"):
        raise StudyError(f"uv --version is {version!r}")
    return {"exe": str(exe), "version": version, "hashes_in_requirement": n,
            "lock_block_sha256": block["block_sha256"],
            "ci_lock_sha256": src["ci_lock_sha256"],
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

    def __init__(self, cid: str, kind: str, installer: str, dist: str, ctx: dict,
                 block: int = 0, seq: int = 0):
        self.id, self.kind, self.installer, self.dist, self.ctx = cid, kind, installer, dist, ctx
        self.out: Path = ctx["out"]
        self.work: Path = ctx["lab"] / "runs" / cid
        self.venv, self.py = self.work / "venv", self.work / "venv" / BIN / f"python{EXE}"
        self.cache = str(ctx["lab"] / "cache" / installer)
        self.env = base_env({"VIRTUAL_ENV": str(self.venv), "SOURCE_DATE_EPOCH": ctx["src"]["epoch"],
                             "PATH": str(self.venv / BIN) + os.pathsep + os.environ.get("PATH", "")})
        # block/seq: 1-based measured block and 0-based position in the VM's executed sequence
        self.rec: dict = {"id": cid, "kind": kind, "installer": installer, "dist": dist,
                          "block": block, "seq": seq, "repeat_id": f"{ctx.get('repeat', 0)}:{cid}",
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


def cond_key(r: dict) -> str:
    return f"{r['installer']}-{r['dist']}"


def coverage_report(runs: list[dict], blobs: dict[str, str], limit: int = 200) -> dict:
    """All pairwise coverage comparisons of the measured runs, strict and classified.

    Strict: any executable or covered line-set difference between any two runs. Classified:
    a covered difference on a RACE_BRANCHES line whose file blob equals the pinned BASE blob is
    "preregistered"; every other covered difference is "unregistered". An executable-line
    difference is always fatal, whatever the line. Per differing line the run ids that do and
    do not cover it are listed (bounded), with the pairwise counts for same-condition replicates.

    The tolerance rests on the premise that the line varies with scheduling, not with the
    condition. A preregistered line whose covering set equals exactly one installer level or
    one distribution level, with no variation inside any same-condition pair, is therefore not
    tolerated: it is "condition-aligned", unresolved, and the coverage guard for it fails."""
    cov = {r["id"]: r["result"]["coverage"] for r in runs}
    ids = [r["id"] for r in runs]
    cond = {r["id"]: cond_key(r) for r in runs}
    levels = {f"{k}={v}": {r["id"] for r in runs if r[k] == v}
              for k in ("installer", "dist") for v in sorted({r[k] for r in runs})}
    files = sorted({f for c in cov.values() for f in c})
    lines: list[dict] = []
    for f in files:
        for kind in ("executable", "covered"):
            sets = {i: set(cov[i].get(f, {}).get(kind, [])) for i in ids}
            for n in sorted(set().union(*sets.values()) - set.intersection(*sets.values())):
                has = [i for i in ids if n in sets[i]]
                rb = RACE_BRANCHES.get(f)
                known = bool(rb) and n in rb["lines"] and blobs.get(rb["path"]) == rb["blob"]
                aligned = [name for name, members in levels.items() if set(has) == members]
                varies = any(0 < len(set(has) & m) < len(m) for m in
                             ({i for i in ids if cond[i] == c} for c in set(cond.values())))
                cls = "fatal-executable" if kind == "executable" else (
                    "unregistered" if not known else
                    "condition-aligned" if aligned and not varies else "preregistered")
                lines.append({"file": f, "line": n, "kind": kind, "class": cls, "covered_by": has,
                              "not_covered_by": [i for i in ids if i not in has],
                              "covered_by_conditions": dict(Counter(cond[i] for i in has)),
                              "aligned_with_level": aligned, "same_condition_varies": varies})
    pairs = []
    for x, a in enumerate(ids):
        for b in ids[x + 1:]:
            mine = [d for d in lines if (a in d["covered_by"]) != (b in d["covered_by"])]
            pairs.append({"a": a, "b": b, "same_condition": cond[a] == cond[b],
                          "strict_differing_lines": len(mine),
                          "classes": dict(Counter(d["class"] for d in mine))})
    by_class = Counter(d["class"] for d in lines)
    return {"runs": ids, "strict_identical": not lines, "differing_lines_total": len(lines),
            "by_class": dict(by_class), "pairs": pairs,
            "same_condition_pairs_differing": sum(1 for p in pairs
                                                  if p["same_condition"] and p["strict_differing_lines"]),
            "lines": lines[:limit], "lines_truncated": len(lines) > limit,
            "executable_equal": by_class["fatal-executable"] == 0,
            "no_unregistered_covered_differences": by_class["unregistered"] == 0,
            "no_condition_aligned_differences": by_class["condition-aligned"] == 0,
            "unresolved_condition_aligned": [
                {k: d[k] for k in ("file", "line", "aligned_with_level")}
                for d in lines if d["class"] == "condition-aligned"][:limit],
            "preregistered_rationale": {f: rb["lines"] for f, rb in RACE_BRANCHES.items()},
            "preregistered_blobs": {rb["path"]: rb["blob"] for rb in RACE_BRANCHES.values()}}


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


def pip_release(text: str) -> str | None:
    """The version of a `pip --version` line. The line also names the pip's install path, which
    holds the run's own venv directory and so differs between runs of the very same release."""
    m = re.match(r"pip (\d+(?:\.\d+)*\S*) from ", text)
    return m[1] if m else None


def pip_equal(runs: list[dict]) -> bool:
    """Every condition's venv (and Windows bootstrapped) pip is the same version, whichever
    run directory it was installed in. An unreadable pip line is never equal."""
    def releases(r: dict) -> dict[str, str | None]:
        return {label: pip_release(text) for label, text in r["pip"].items()}
    return same([r for r in runs if r.get("pip")], releases) and \
        None not in releases([r for r in runs if r.get("pip")][0]).values()


def factors(family: str) -> list[str]:
    """The measured conditions of a VM: installer x distribution (Linux), installer (Windows)."""
    return ["pip-load", "uv-load", "pip-worksteal", "uv-worksteal"] if family == "linux" \
        else ["pip-load", "uv-load"]


def order_balanced(meas: list[dict], expected: list[str]) -> bool:
    """Two blocks, each holding every expected condition exactly once, block 2 the reverse of
    block 1, run back to back; every condition's mean sequence position is then equal.
    `meas` is every measured condition that was started, in any status."""
    ordered = sorted(meas, key=lambda r: r["seq"])
    seqs = [r["seq"] for r in ordered]
    if len(ordered) != 2 * len(expected) or seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        return False
    blocks = {b: [cond_key(r) for r in ordered if r["block"] == b] for b in (1, 2)}
    if any(sorted(v) != sorted(expected) for v in blocks.values()) or blocks[2] != blocks[1][::-1]:
        return False
    return len({sum(r["seq"] for r in ordered if cond_key(r) == c) for c in expected}) == 1


def validate(runs: list[dict], family: str, blobs: dict[str, str]) -> tuple[dict, dict | None, list[str]]:
    """Every guard, by name. Which claim a guard gates is decided in claim_status()."""
    ok = [r for r in runs if r["status"] == "ok"]
    started = [r for r in runs if r["kind"] == "measured"]
    g = {"all_runs_ok": bool(runs) and len(ok) == len(runs),
         "blocks_complete": len([r for r in started if r["status"] == "ok"]) == 2 * len(factors(family)),
         "order_balanced": bool(started) and order_balanced(started, factors(family)),
         "pip_versions_equal": pip_equal(runs),
         "cleanup_ok": bool(runs) and all(r.get("cleanup_ok") is True for r in runs),
         "tracked_tree_unchanged_by_tests": all(
             r["tree_after_tests"]["unchanged"] for r in ok if "result" in r),
         "manifest_post_lock": same(ok, lambda r: r.get("manifests", {}).get("post_lock")),
         "junit_ids_and_results": same([r for r in ok if "result" in r],
                                       lambda r: r["result"]["junit"])}
    notes: list[str] = []
    cov = None
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
        g.update(workers_and_items=same(meas, lambda r: [r.get("workers"), r.get("items")]),
                 # tied to the exact-base normal CI: 7739 items = 7718 passed + 21 skipped
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
        cov = coverage_report(meas, blobs) if meas else None
        g["coverage_executable_lines_equal"] = bool(cov) and cov["executable_equal"]
        g["coverage_no_unregistered_differences"] = bool(cov) and cov["no_unregistered_covered_differences"]
        g["coverage_no_condition_aligned_differences"] = bool(cov) and cov["no_condition_aligned_differences"]
        notes += [f"{r['id']} git test not passed: {x}" for r in meas
                  for x in git_tests_skipped(r["result"]["junit"])][:20]
    return g, cov, notes


def metadata(args: argparse.Namespace) -> dict:
    keys = ["ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_REF"]
    out = {"env": {k: os.environ.get(k) for k in keys}, "platform": platform.platform(),
           "cpu_count": os.cpu_count(), "python": sys.version, "family": args.family,
           "repeat": args.repeat, "seed": SEED}
    out["pip"] = capture([sys.executable, "-m", "pip", "--version"], Path.cwd()).strip()
    out["git"] = capture(["git", "--version"], Path.cwd()).strip()
    return out


def summarize(runs: list[dict], tools: dict) -> dict:
    def row(r: dict) -> dict:
        s = r["stages"]
        return {"id": r["id"], "installer": r["installer"], "dist": r["dist"], "status": r["status"],
                "stages": s, "install_seconds": round(sum(s.get(k, 0) for k in INSTALL_STAGES), 3),
                "job_seconds": round(sum(s.get(k, 0) for k in STAGE_SUM), 3)}

    meas = [dict(row(r), block=r["block"], seq=r["seq"], repeat_id=r["repeat_id"])
            for r in sorted(runs, key=lambda r: r["seq"]) if r["kind"] == "measured" and r["status"] == "ok"]
    # Raw per-run times and per-condition replicates: measurements, not claims.
    reps: dict[str, dict] = {}
    for m in meas:
        c = reps.setdefault(f"{m['installer']}-{m['dist']}", {"install_seconds": [], "tests_seconds": []})
        c["install_seconds"].append(m["install_seconds"])
        c["tests_seconds"].append(m["stages"].get("tests"))
    for c in reps.values():
        c["install_range"] = round(max(c["install_seconds"]) - min(c["install_seconds"]), 3)
        tests = [t for t in c["tests_seconds"] if t is not None]
        c["tests_range"] = round(max(tests) - min(tests), 3) if tests else None
    return {"measured": meas, "primers_excluded": [row(r) for r in runs if r["kind"] == "primer"],
            "uv_provision_seconds": tools["provision_seconds"], "replicates_by_condition": reps}


def mean(v: list[float]) -> float:
    return round(sum(v) / len(v), 3)


def installer_value(raw: dict, provision: float) -> dict | None:
    """pip minus (uv + provisioning charged once per job), on the job's install seconds."""
    pip = [m["install_seconds"] for m in raw["measured"] if m["installer"] == "pip"]
    uv = [m["install_seconds"] for m in raw["measured"] if m["installer"] == "uv"]
    if not pip or not uv:
        return None
    saved = round(mean(pip) - (mean(uv) + provision), 3)
    spread = max(c["install_range"] for c in raw["replicates_by_condition"].values())
    by_block = {}
    for b in (1, 2):
        p = [m["install_seconds"] for m in raw["measured"] if m["installer"] == "pip" and m["block"] == b]
        u = [m["install_seconds"] for m in raw["measured"] if m["installer"] == "uv" and m["block"] == b]
        if p and u:
            by_block[str(b)] = round(mean(p) - (mean(u) + provision), 3)
    return {"pip_mean_install_seconds": mean(pip), "uv_mean_install_seconds": mean(uv),
            "uv_provision_seconds_charged_once": provision, "saved_seconds_after_provisioning": saved,
            "saved_by_block": by_block, "same_condition_range_max_seconds": spread,
            "exceeds_same_condition_range": abs(saved) > spread}


def distribution_value(raw: dict) -> dict:
    """Per installer: load minus worksteal test seconds, with the same-condition ranges."""
    def t(inst: str, dist: str, block: int | None = None) -> list[float]:
        return [m["stages"]["tests"] for m in raw["measured"] if m["installer"] == inst
                and m["dist"] == dist and block in (None, m["block"]) and "tests" in m["stages"]]

    out = {}
    for inst in ("pip", "uv"):
        if t(inst, "load") and t(inst, "worksteal"):
            blocks = {str(b): round(mean(t(inst, "load", b)) - mean(t(inst, "worksteal", b)), 3)
                      for b in (1, 2) if t(inst, "load", b) and t(inst, "worksteal", b)}
            saved = round(mean(t(inst, "load")) - mean(t(inst, "worksteal")), 3)
            spread = max(raw["replicates_by_condition"][f"{inst}-{d}"]["tests_range"] or 0
                         for d in ("load", "worksteal"))
            out[inst] = {"load_mean": mean(t(inst, "load")), "worksteal_mean": mean(t(inst, "worksteal")),
                         "worksteal_saved_seconds": saved, "saved_by_block": blocks,
                         # both blocks measured, and one strictly positive or strictly negative sign
                         "sign_consistent_across_blocks": len(blocks) == 2 and (
                             all(v > 0 for v in blocks.values()) or all(v < 0 for v in blocks.values())),
                         "same_condition_range_max_seconds": spread,
                         "exceeds_same_condition_range": abs(saved) > spread}
    return out


# Guards each claim needs. The installer comparison needs equal installed sets, equal pip,
# balanced order and successful runs; it does not need the test-run guards. The test-run
# equivalence and the distribution comparison do.
INSTALL_GUARDS = ["all_runs_ok", "blocks_complete", "order_balanced", "pip_versions_equal",
                  "cleanup_ok", "source_unchanged", "manifest_post_lock"]
WINDOWS_INSTALL_GUARDS = ["wheel", "manifest_initial", "manifest_pre_lock"]
WINDOWS_TEST_GUARDS = ["tracked_tree_unchanged_by_tests", "junit_ids_and_results", "ninety_tests_all_passed"]
LINUX_TEST_GUARDS = [
    "tracked_tree_unchanged_by_tests", "junit_ids_and_results", "workers_and_items",
    "items_equal_base_ci", "pass_skip_counts_equal_base_ci", "git_dependent_tests_not_skipped",
    "outcomes_report", "journeys_report", "outcomes_verdict_pass",
    "coverage_executable_lines_equal", "coverage_no_unregistered_differences",
    "coverage_no_condition_aligned_differences"]
VM_SCOPE = "single VM, one repeat of the five; no installer or distribution decision from one VM"


def claim_status(g: dict, raw: dict, provision: float, family: str) -> dict:
    """Per claim: its guards, whether all passed, and the number only when they did. A number
    from a claim with a failed guard is kept under `unvalidated_diagnostic`, never as `value`."""
    install = INSTALL_GUARDS + (WINDOWS_INSTALL_GUARDS if family == "windows" else [])
    tests = WINDOWS_TEST_GUARDS if family == "windows" else LINUX_TEST_GUARDS
    claims = {"installer_install_seconds": (install, installer_value(raw, provision)),
              "test_run_equivalence": (tests, None)}
    if family == "linux":
        claims["distribution_test_seconds"] = (install + tests, distribution_value(raw))
    out = {}
    for name, (need, value) in claims.items():
        failed = [k for k in need if g.get(k) is not True]
        ok = not failed
        out[name] = {"status": "vm_validated" if ok else "unvalidated", "scope": VM_SCOPE,
                     "guards": {k: g.get(k) is True for k in need}, "failed_guards": failed,
                     "value": value if ok else None,
                     "unvalidated_diagnostic": None if ok else value}
    return out


# Williams square over the four Linux conditions (indices into factors("linux")): every ordered
# pair of conditions is adjacent once across rows 1-4. Repeat r uses row (r-1) % 4, so repeat 5
# repeats row 1. Block 2 of every VM is block 1 reversed.
WILLIAMS = [(0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1)]


def block_plan(family: str, repeat: int) -> list[list[tuple[str, str]]]:
    """[block 1, block 2] as (installer, dist) lists; block 2 is block 1 reversed."""
    names = factors(family)
    if family == "linux":
        first = [names[i] for i in WILLIAMS[(repeat - 1) % 4]]
    else:  # two conditions: pip first on odd repeats, uv first on even ones
        first = names if repeat % 2 else names[::-1]
    pairs = [tuple(n.split("-", 1)) for n in first]
    return [pairs, pairs[::-1]]


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise StudyError(f"self-check: {msg}")


def _syn_run(cid: str, inst: str, dist: str, block: int, seq: int, cov: dict | None = None) -> dict:
    return {"id": cid, "installer": inst, "dist": dist, "block": block, "seq": seq,
            "result": {"coverage": cov or {}}}


def _syn_cov(path: str, executable: list[int], covered: list[int]) -> dict:
    return {path: {"executable": executable, "covered": covered}}


def _syn_plan_runs(family: str, repeat: int) -> list[dict]:
    runs, seq = [], 0
    for b, order in enumerate(block_plan(family, repeat), 1):
        for inst, dist in order:
            runs.append(_syn_run(f"m-{inst}-{dist}-b{b}", inst, dist, b, seq))
            seq += 1
    return runs


def _self_coverage() -> None:
    proc, blob = RACE_BRANCHES["process.py"], RACE_BRANCHES["process.py"]["blob"]
    blobs = {proc["path"]: blob}
    exe = list(range(560, 575))
    base = _syn_cov("process.py", exe, [n for n in exe if n not in (565, 566, 570)])
    racy = _syn_cov("process.py", exe, exe)

    def rep(a: dict, b: dict, known: dict[str, str]) -> dict:
        return coverage_report([_syn_run("a", "pip", "load", 1, 0, a), _syn_run("b", "uv", "load", 1, 1, b)], known)

    same_cov = rep(base, base, blobs)
    check(same_cov["strict_identical"] and same_cov["no_unregistered_covered_differences"], "identical rejected")
    def grid(racy_runs: set[tuple[str, str, int]], known: dict[str, str] = blobs) -> dict:
        """The eight real measured runs (two blocks of the four conditions); `racy_runs` cover the race lines."""
        conds = [(i, d) for i in ("pip", "uv") for d in ("load", "worksteal")]
        runs = [_syn_run(f"{i}-{d}-b{b}", i, d, b, s, racy if (i, d, b) in racy_runs else base)
                for s, (b, (i, d)) in enumerate((b, c) for b in (1, 2) for c in conds)]
        return coverage_report(runs, known)

    # documented same-condition variability: one racy run in each of two conditions
    reg = grid({("pip", "load", 1), ("uv", "worksteal", 2)})  # 565, 566 and 570 differ
    check(not reg["strict_identical"] and reg["by_class"] == {"preregistered": 3}
          and reg["no_unregistered_covered_differences"] and reg["no_condition_aligned_differences"]
          and reg["executable_equal"] and reg["same_condition_pairs_differing"] == 2,
          "pre-registered race lines with same-condition variability not classified")
    # every uv run covers the race lines, no pip run does: exact installer alignment
    inst = grid({("uv", d, b) for d in ("load", "worksteal") for b in (1, 2)})
    check(not inst["strict_identical"] and inst["by_class"] == {"condition-aligned": 3}
          and not inst["no_condition_aligned_differences"] and inst["executable_equal"]
          and inst["unresolved_condition_aligned"][0]["aligned_with_level"] == ["installer=uv"]
          and inst["same_condition_pairs_differing"] == 0, "exact installer alignment tolerated")
    # every worksteal run covers them, no load run does: exact distribution alignment
    dist = grid({(i, "worksteal", b) for i in ("pip", "uv") for b in (1, 2)})
    check(dist["by_class"] == {"condition-aligned": 3} and not dist["no_condition_aligned_differences"]
          and dist["unresolved_condition_aligned"][0]["aligned_with_level"] == ["dist=worksteal"],
          "exact distribution alignment tolerated")
    # all uv runs but one: not an exact level, and the uv-load pair varies inside the condition
    almost = grid({("uv", d, b) for d in ("load", "worksteal") for b in (1, 2)} - {("uv", "load", 2)})
    check(almost["by_class"] == {"preregistered": 3} and almost["no_condition_aligned_differences"],
          "partial installer coverage with same-condition variability rejected")
    # blob mismatch: the aligned line is unregistered, not tolerated
    check(not grid({("uv", d, b) for d in ("load", "worksteal") for b in (1, 2)}, {})
          ["no_unregistered_covered_differences"], "aligned line with an unbound blob accepted")
    check(not rep(base, racy, {})["no_unregistered_covered_differences"], "blob-unbound class accepted")
    check(not rep(base, racy, {proc["path"]: "0" * 40})["no_unregistered_covered_differences"],
          "changed source blob still accepted")
    other = _syn_cov("process.py", exe, [n for n in exe if n not in (565, 566, 570, 567)])
    check(not rep(other, racy, blobs)["no_unregistered_covered_differences"], "unregistered line 567 accepted")
    unknown = rep(_syn_cov("other.py", [1, 2], [1]), _syn_cov("other.py", [1, 2], [1, 2]), blobs)
    check(unknown["by_class"] == {"unregistered": 1} and not unknown["no_unregistered_covered_differences"],
          "unknown file difference accepted")
    changed = rep(_syn_cov("process.py", exe, exe), _syn_cov("process.py", exe[:-1], exe[:-1]), blobs)
    check(not changed["executable_equal"], "changed executable line accepted")
    reg_exec = rep(_syn_cov("process.py", exe, exe), _syn_cov("process.py", [n for n in exe if n != 565],
                                                              [n for n in exe if n != 565]), blobs)
    check(not reg_exec["executable_equal"], "executable difference on a registered line accepted")
    # pairwise, not first-reference: one odd run among four differs from three, others agree
    four = [_syn_run(f"r{i}", "pip", "load", 1, i, racy if i == 0 else base) for i in range(4)]
    pairs = coverage_report(four, blobs)["pairs"]
    check(sum(1 for p in pairs if p["strict_differing_lines"]) == 3 and len(pairs) == 6, "pairwise matrix wrong")


def _self_order() -> None:
    for fam in ("linux", "windows"):
        for rep in range(1, 6):
            runs = _syn_plan_runs(fam, rep)
            check(order_balanced(runs, factors(fam)), f"{fam} repeat {rep} plan not balanced")
    adj = {(a, b) for row in WILLIAMS for a, b in zip(row, row[1:], strict=False)}
    check(len(adj) == 12, "Williams rows do not cover every ordered pair once")
    runs = _syn_plan_runs("linux", 1)
    check(not order_balanced(runs[:-1], factors("linux")), "missing run accepted")
    b2 = [r for r in runs if r["block"] == 2]  # block 2 rotated by one: every condition, not reversed
    swapped = [dict(r, installer=b2[(i + 1) % 4]["installer"], dist=b2[(i + 1) % 4]["dist"])
               if r["block"] == 2 else r for r, i in zip(runs, range(8), strict=True)]
    check(not order_balanced(swapped, factors("linux")), "block 2 not the reverse of block 1 accepted")
    gap = [dict(r, seq=r["seq"] + (5 if r["seq"] > 3 else 0)) for r in runs]
    check(not order_balanced(gap, factors("linux")), "non-consecutive sequence accepted")


def _self_pip_and_lock(tmp: Path) -> None:
    def line(version: str, run: str) -> str:  # the real `pip --version` shape, own venv path included
        return f"pip {version} from D:\\a\\_temp\\study-lab\\runs\\{run}\\venv\\Lib\\site-packages\\pip (python 3.13)"
    a = {"id": "a", "pip": {"venv": line("26.2.1", "a"), "after_bootstrap": line("26.2.1", "a")}}
    b = {"id": "b", "pip": {"venv": line("26.2.1", "b"), "after_bootstrap": line("26.2.1", "b")}}
    check(pip_equal([a, b]), "equal pip release in different run directories rejected")
    check(not pip_equal([a, dict(b, pip={"venv": line("25.0", "b"), "after_bootstrap": line("26.2.1", "b")})]),
          "pip mismatch accepted")
    check(not pip_equal([a, dict(b, pip={"venv": line("26.2.1", "b"), "after_bootstrap": line("25", "b")})]),
          "bootstrap pip mismatch accepted")
    check(not pip_equal([a, dict(b, pip={"venv": "garbled", "after_bootstrap": line("26.2.1", "b")})]),
          "unreadable pip line accepted")
    check(not pip_equal([dict(a, pip={"venv": "garbled"}), dict(b, pip={"venv": "garbled"})]),
          "identical unreadable pip lines accepted")
    check(not pip_equal([a, dict(b, pip={"venv": line("26.2.1", "b")})]), "missing bootstrap pip accepted")
    lock = tmp / "ci.lock"
    lock.write_text("# header\nuv==0.13.4 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
                    "    # via x\nother==1 \\\n    --hash=sha256:cc\n", encoding="utf-8")
    check(lock_blocks(lock, ["uv"])["uv"][0].startswith("uv==0.13.4"), "uv block not read")
    for text, why in (("uv==0.13.5 \\\n    --hash=sha256:aa\n", "wrong pin"), ("uv==0.13.4\n", "no hashes")):
        lock.write_text(text, encoding="utf-8")
        try:
            lock_blocks(lock, ["uv"])
        except StudyError:
            continue
        raise StudyError(f"self-check: lock accepted with {why}")


def _self_claims() -> None:
    meas = [{"installer": i, "dist": d, "block": b, "install_seconds": s, "stages": {"tests": t}}
            for i, d, b, s, t in [("pip", "load", 1, 50, 120), ("uv", "load", 1, 14, 118), ("pip", "worksteal", 1, 49, 112),
                                  ("uv", "worksteal", 1, 14, 111), ("uv", "worksteal", 2, 15, 113),
                                  ("pip", "worksteal", 2, 51, 114), ("uv", "load", 2, 14, 121), ("pip", "load", 2, 50, 119)]]
    reps = {f"{i}-{d}": {"install_range": 2, "tests_range": 3} for i in ("pip", "uv") for d in ("load", "worksteal")}
    raw = {"measured": meas, "replicates_by_condition": reps}
    guards = {k: True for k in INSTALL_GUARDS + LINUX_TEST_GUARDS}
    good = claim_status(guards, raw, 5.0, "linux")
    check(all(c["status"] == "vm_validated" and c["value"] is not None
              for n, c in good.items() if n != "test_run_equivalence"), "validated claims lack a value")
    bad = claim_status({**guards, "coverage_no_unregistered_differences": False}, raw, 5.0, "linux")
    check(bad["installer_install_seconds"]["status"] == "vm_validated", "coverage guard gated the installer claim")
    for n in ("distribution_test_seconds", "test_run_equivalence"):
        check(bad[n]["status"] == "unvalidated" and bad[n]["value"] is None, f"{n} shows a number after a failed guard")
    check(bad["distribution_test_seconds"]["unvalidated_diagnostic"] is not None, "diagnostic dropped")
    aligned = claim_status({**guards, "coverage_no_condition_aligned_differences": False}, raw, 5.0, "linux")
    check(aligned["installer_install_seconds"]["status"] == "vm_validated", "alignment guard gated the installer claim")
    for n in ("distribution_test_seconds", "test_run_equivalence"):
        check(aligned[n]["status"] == "unvalidated" and aligned[n]["value"] is None
              and aligned[n]["failed_guards"] == ["coverage_no_condition_aligned_differences"],
              f"{n} validated despite condition-aligned coverage")
    # sign consistency needs both blocks and one strict sign; zero or one block is no evidence
    sign = distribution_value(raw)["pip"]["sign_consistent_across_blocks"]
    check(sign is True, "two positive blocks not consistent")
    for drop, why in ((lambda m: m["block"] == 2, "one block"),
                      (lambda m: m["block"] == 2 and m["dist"] == "load" and m["installer"] == "pip", "one block")):
        part = {**raw, "measured": [m for m in meas if not drop(m)]}
        check(not distribution_value(part)["pip"]["sign_consistent_across_blocks"], f"{why} counted as consistent")
    zero = [dict(m, stages={"tests": 100}) if m["installer"] == "pip" and m["block"] == 2 else m for m in meas]
    check(not distribution_value({**raw, "measured": zero})["pip"]["sign_consistent_across_blocks"],
          "a zero difference counted as consistent")
    flipped = [dict(m, stages={"tests": 100}) if m["installer"] == "pip" and m["block"] == 2
               and m["dist"] == "load" else m for m in meas]  # block 2 load 100 < worksteal 114: negative
    check(not distribution_value({**raw, "measured": flipped})["pip"]["sign_consistent_across_blocks"],
          "opposite block signs counted as consistent")
    worse = claim_status({**guards, "pip_versions_equal": False}, raw, 5.0, "linux")
    check(all(c["status"] == "unvalidated" for n, c in worse.items() if n != "test_run_equivalence")
          and worse["installer_install_seconds"]["value"] is None, "pip mismatch left a claim validated")
    # pip mean 50, uv mean 14.25, provisioning 5 charged once: 50 - (14.25 + 5)
    check(good["installer_install_seconds"]["value"]["saved_seconds_after_provisioning"] == 30.75,
          "installer arithmetic")


def _self_tree_kill(tmp: Path) -> None:
    beat = tmp / "beat.txt"
    child = "import time,sys\nwhile True:\n open(sys.argv[1],'a').write('x')\n time.sleep(0.2)\n"
    parent = "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]])\ntime.sleep(60)\n"
    try:
        spawn([sys.executable, "-c", parent, str(beat), child], cwd=tmp, env=base_env(), timeout=3,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise StudyError("self-check: the timeout did not fire")
    size = beat.stat().st_size if beat.exists() else 0
    check(size > 0, "the grandchild never ran")
    time.sleep(1.5)
    check(beat.stat().st_size == size, "the grandchild survived the process-tree kill")


def self_check() -> int:
    """Synthetic checks of the study's own guards; no repository code is used."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        steps = [("coverage classification", _self_coverage), ("order balance", _self_order),
                 ("pip and lock", lambda: _self_pip_and_lock(tmp)), ("claim gating", _self_claims),
                 ("process-tree kill", lambda: _self_tree_kill(tmp))]
        for name, fn in steps:
            try:
                fn()
            except StudyError as exc:
                print(f"self-check FAILED [{name}]: {exc}", flush=True)
                return 1
            print(f"self-check ok [{name}]", flush=True)
    return 0


def write_step_summary(summary: dict) -> None:
    """Claim status table into the job summary when running on GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = [f"### {summary['metadata']['family']} repeat {summary['metadata']['repeat']}: {summary['status']}",
            "", "| claim | status | failed guards |", "|---|---|---|"]
    for name, c in summary.get("claims", {}).items():
        rows.append(f"| {name} | {c['status']} | {', '.join(c['failed_guards']) or '-'} |")
    cov = summary.get("coverage")
    if cov:
        rows += ["", f"coverage: strict identical {cov['strict_identical']}, by class {cov['by_class']}"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-check", action="store_true", help="run the synthetic guard checks and exit")
    ap.add_argument("--family", choices=["windows", "linux"])
    ap.add_argument("--repeat", type=int)
    ap.add_argument("--python", help="expected major.minor, e.g. 3.14")
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--lab", type=Path)
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check()
    missing = [k for k in ("family", "repeat", "python", "repo", "out", "lab") if getattr(args, k) is None]
    if missing:
        ap.error(f"required: {', '.join('--' + k for k in missing)}")
    out, lab, repo = args.out.resolve(), args.lab.resolve(), args.repo.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if lab.exists():
        raise SystemExit(f"lab dir already exists: {lab}")
    lab.mkdir(parents=True)
    t_job = time.perf_counter()
    summary: dict = {"status": "setup", "limits": [
        "clean-environment step comparison, not the global-Python CI jobs; no queue or end-to-end claim",
        "each installer has its own warmed download cache from an excluded primer",
        "uv provisioning seconds are charged once per hypothetical CI job against the uv side, "
        "inside claims.installer_install_seconds only",
        "one repeat per VM; each claim is vm_validated or unvalidated; no decision from one VM; "
        "worksteal is not chosen from noise-sized differences (compare with the same-condition range)",
        "two blocks per VM, block 2 the reverse of block 1: 10 runs per VM, two per condition, so "
        "5 same-condition two-run comparisons per condition across the five VMs; equal mean position; "
        "the pytest seed is fixed (CI's is random)",
        "same_condition_range_max_seconds and exceeds_same_condition_range are descriptive "
        "observations of a two-value range per VM (partly adjacency, partly drift across positions), "
        "not a significance test or a general variability estimate; sign_consistent_across_blocks "
        "is true only with both blocks measured and one strictly positive or strictly negative sign",
        "coverage: executable-line equality is strict; covered-line differences are reported raw "
        "(strict) and classified, only the pre-registered RACE_BRANCHES lines (bound to BASE blob ids) "
        "are tolerated, everything else is fatal for test_run_equivalence and distribution claims; a "
        "pre-registered line whose covering set equals exactly one installer or distribution level, "
        "with no variation inside a same-condition pair, is condition-aligned: unresolved and "
        "unvalidated, never tolerated, and gates test_run_equivalence and distribution_test_seconds",
        "pip equality is the pip release (the path in `pip --version` is ignored: it names each "
        "run's own venv) equal across every run of a VM, not the lock's pip: on Linux "
        "the venv pip is the runner Python's ensurepip bootstrap (equal within a VM, not necessarily "
        "26.2.1); only the post-lock manifests are strict. The baseline is not re-bootstrapped "
        "differently from the original CI",
        "pip and uv installs are compared by package names and versions plus equal venv pip; built "
        "wheels (scikit-network sdist on Linux) are not compared byte for byte",
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
        ctx = {"out": out, "lab": lab, "src": src, "uv": tools, "family": args.family,
               "repeat": args.repeat}
        if args.family == "windows":
            (lab / "build-constraints.txt").write_text(
                f"setuptools=={LOCK_PINS['setuptools']}\n{WHEEL_PIN}\n")
            t0 = time.perf_counter()
            wreq = lab / "win-tools.txt"
            summary["win_tools_lock"] = write_lock_requirement(
                Path(src["dir"]) / "dokimi" / "requirements-ci.lock.txt", WIN_LOCK_TOOLS, wreq)
            wlog = out / "logs" / "wheelhouse.log"
            # The lock's tools with its hashes; wheel (not in the lock) by exact pin.
            run([sys.executable, "-m", "pip", "download", "--no-deps", "--require-hashes",
                 "--dest", str(lab / "wheelhouse"), "-r", str(wreq)],
                wlog, cwd=lab, env=base_env(), limit=300)
            run([sys.executable, "-m", "pip", "download", "--no-deps",
                 "--dest", str(lab / "wheelhouse"), WHEEL_PIN],
                wlog, cwd=lab, env=base_env(), limit=300)
            ctx["wheelhouse"] = str(lab / "wheelhouse")
            summary["wheelhouse_seconds_excluded"] = round(time.perf_counter() - t0, 3)
    except Exception as exc:  # noqa: BLE001
        summary.update(status="failed", error=f"setup: {exc}")
        write_json(out / "summary.json", summary)
        print(f"setup FAILED: {exc}", flush=True)
        return 1
    blocks = block_plan(args.family, args.repeat)
    summary["plan"] = {"primers": ["pip", "uv"], "blocks": blocks, "design": "Williams row, block 2 reversed"}
    summary["status"] = "running"
    write_json(out / "summary.json", summary)
    runs = [Cond(f"primer-{i}", "primer", i, "load", ctx).execute() for i in ("pip", "uv")]
    seq, wall = 0, [FIRST_NEED[args.family]]
    for b, order in enumerate(blocks, 1):
        for inst, dist in order:
            if not all(r["status"] == "ok" for r in runs if r["kind"] == "primer"):
                break  # a failed primer leaves the caches cold: no measured condition is comparable
            need = 1.25 * max(wall) + CLEANUP_LIMIT
            if DEADLINE - time.monotonic() < need:  # a started condition must be able to finish
                summary["deadline_stop"] = {"before": f"measured-{inst}-{dist}-b{b}", "need_seconds": round(need)}
                break
            r = Cond(f"measured-{inst}-{dist}-b{b}", "measured", inst, dist, ctx, block=b, seq=seq).execute()
            runs.append(r)
            wall.append(r["wall_seconds"])
            seq += 1
        else:
            continue
        break
    blobs = src["blobs"]
    guards, cov, notes = validate(runs, args.family, blobs)
    guards["source_unchanged"] = tree_manifest(Path(src["dir"])) == src["files"]
    raw = summarize(runs, tools)
    claims = claim_status(guards, raw, tools["provision_seconds"], args.family)
    summary.update(runs={r["id"]: {"status": r["status"], "error": r.get("error"), "block": r["block"],
                                   "seq": r["seq"], "repeat_id": r["repeat_id"]} for r in runs},
                   measures=raw, guards=guards, claims=claims, coverage=cov, notes=notes,
                   job_wall_seconds=round(time.perf_counter() - t_job, 3))
    clean = all(guards.values())
    summary["status"] = "complete" if clean else (
        "execution-failed" if not guards["all_runs_ok"] or "deadline_stop" in summary else "guard-failed")
    write_json(out / "summary.json", summary)
    write_step_summary(summary)
    print(f"status: {summary['status']} guards: {guards}", flush=True)
    for name, c in claims.items():
        print(f"claim {name}: {c['status']} failed={c['failed_guards']}", flush=True)
    if not clean:
        print("a validity guard failed: the claims above marked unvalidated carry no established "
              "number; this is not by itself a repository test failure", flush=True)
    return 0 if clean else (1 if summary["status"] == "execution-failed" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
