# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Hosted Windows CI-step experiment: pip vs uv pip in clean virtualenvs.

One matrix job runs one excluded primer per installer (pip, then uv) and one
measured pair in a seeded random order. Every run: fresh venv, bootstrap pip
tools, build, wheel install (loose deps), installed-wheel smoke from outside the
source, hash-locked lock install, two Windows test files. The uv binary lives in
its own tools env, never in the tested venv; its download is a one-off overhead
that the timed stages exclude. Not a reproduction of the global-Python
windows-smoke job and no queue or end-to-end speedup claim. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

PINNED = "506f7ba962ba719d110d48e92fb2c0f28703702a"
BENCH_FILES = {".github/workflows/ci.yml", "dokimi/scripts/ci_installer_experiment.py"}
UV_VERSION = "0.13.5"
UV_URL = (
    f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
    "uv-x86_64-pc-windows-msvc.zip"
)
# Digest of this release asset from the GitHub REST release metadata for the tag
# above; empty fails closed.
UV_ZIP_SHA256 = "a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a"
TOOLS = ["pip==26.2.2", "build==1.6.1", "setuptools==84.2.1", "wheel==0.48.0",
         "packaging==26.3", "pyproject-hooks==1.3.3", "colorama==0.4.6"]
TEST_FILES = ["tests/test_cli_help.py", "tests/test_console_encoding.py"]
INPUTS = ["pyproject.toml", "requirements.lock.txt", "scripts/smoke_installed.py", *TEST_FILES]
SEED = "424242"
TIMED = ["venv", "bootstrap", "build", "wheel_install", "smoke", "lock_install", "tests"]
LIMITS = {"venv": 300, "bootstrap": 300, "build": 600, "wheel_install": 600, "smoke": 300,
          "lock_install": 900, "tests": 600, "validate": 300}
ENV_KEEP = ["SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"]
DEADLINE = time.monotonic() + 22 * 60


class StageError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(f"{p.relative_to(root).as_posix()}\0{sha256(p)}\n".encode())
    return h.hexdigest()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.update(extra or {})
    return env


def run(cmd: list[str], log: Path, *, cwd: Path, env: dict[str, str], limit: int) -> str:
    """Run one command, stdout+stderr to `log`; raise StageError on failure or timeout."""
    remaining = int(DEADLINE - time.monotonic())
    if remaining <= 5:
        raise StageError("job deadline reached")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        fh.write(("$ " + " ".join(cmd) + "\n").encode())
        fh.flush()
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=min(limit, remaining), check=False)
        except subprocess.TimeoutExpired as exc:
            raise StageError(f"timeout after {exc.timeout}s: {cmd[0]}") from exc
        except OSError as exc:
            raise StageError(f"cannot start {cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise StageError(f"exit {proc.returncode}: {' '.join(cmd[:4])}")
    return log.read_text(encoding="utf-8", errors="replace")


def capture(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                         timeout=120, check=True)
    return out.stdout


def rmtree(path: Path) -> bool:
    def onexc(func, p, _exc):  # read-only files on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    for _ in range(3):
        if not path.exists():
            return True
        try:
            shutil.rmtree(path, onexc=onexc)
        except OSError:
            time.sleep(1)
    return not path.exists()


def prepare_source(repo: Path, lab: Path) -> dict:
    """git archive of the pinned commit, extracted with the data filter, validated."""
    head = capture(["git", "rev-parse", "HEAD"], repo).strip()
    parent = capture(["git", "rev-parse", "HEAD^"], repo).strip()
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", PINNED, "HEAD"],
                              cwd=repo, capture_output=True, text=True, timeout=120, check=False)
    if ancestor.returncode != 0:
        raise StageError(f"pinned {PINNED} is not an ancestor of HEAD {head}: "
                         f"exit {ancestor.returncode} {ancestor.stderr.strip()}")
    changed = set(capture(["git", "diff", "--name-only", PINNED, "HEAD"], repo).split())
    if changed != BENCH_FILES:
        raise StageError(f"benchmark commit changes unexpected files: {sorted(changed)}")
    epoch = capture(["git", "log", "-1", "--format=%ct", PINNED], repo).strip()
    tree = capture(["git", "rev-parse", f"{PINNED}^{{tree}}"], repo).strip()
    tarball, src = lab / "source.tar", lab / "source"
    capture(["git", "archive", "--format=tar", "-o", str(tarball), PINNED], repo)
    src.mkdir(parents=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(src, filter="data")
    pkg = src / "dokimi"
    for rel in INPUTS:
        if not (pkg / rel).is_file():
            raise StageError(f"extracted source lacks dokimi/{rel}")
    return {"head": head, "parent": parent, "source_sha": PINNED, "tree": tree,
            "source_date_epoch": epoch,
            "origin": "git archive of pinned commit via subprocess git", "src": str(src),
            "tarball_sha256": sha256(tarball), "extracted_tree_sha256": tree_hash(src),
            "input_sha256": {r: sha256(pkg / r) for r in INPUTS}}


def prepare_tools(lab: Path, log: Path, env: dict[str, str]) -> dict:
    """One-off: dedicated uv tools dir and the shared bootstrap wheelhouse. Untimed overhead."""
    t0 = time.perf_counter()
    if len(UV_ZIP_SHA256) != 64:
        raise StageError("UV_ZIP_SHA256 is not set")
    tools = lab / "uv-tools"
    tools.mkdir(parents=True)
    zpath = tools / "uv.zip"
    with urllib.request.urlopen(UV_URL, timeout=120) as resp, zpath.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)
    if sha256(zpath) != UV_ZIP_SHA256:
        raise StageError("uv archive sha256 mismatch")
    with zipfile.ZipFile(zpath) as zf:
        (tools / "uv.exe").write_bytes(zf.read("uv.exe"))
    uv_version = capture([str(tools / "uv.exe"), "--version"], tools)
    wheelhouse = lab / "wheelhouse"
    run([sys.executable, "-m", "pip", "download", "--dest", str(wheelhouse), *TOOLS],
        log, cwd=lab, env=env, limit=300)
    return {"uv_exe": str(tools / "uv.exe"), "uv_version": uv_version.strip(),
            "uv_zip_sha256": UV_ZIP_SHA256, "wheelhouse": str(wheelhouse),
            "wheelhouse_sha256": {p.name: sha256(p) for p in sorted(wheelhouse.glob("*.whl"))},
            "overhead_seconds": round(time.perf_counter() - t0, 3)}

MANIFEST_PY = (
    "import importlib.metadata as m,json;"
    "print(json.dumps(sorted([(d.metadata['Name'] or '').lower().replace('_','-')"
    ".replace('.','-'),d.version] for d in m.distributions())))"
)
ORIGIN_PY = "import dokimi,json;print(json.dumps(dokimi.__file__))"


class Arm:
    """One run: fresh copied source tree and fresh venv for one installer."""

    def __init__(self, run_id: str, installer: str, kind: str, ctx: dict):
        self.id, self.installer, self.kind, self.ctx = run_id, installer, kind, ctx
        self.out: Path = ctx["out"]
        self.work: Path = ctx["lab"] / "runs" / run_id
        self.venv = self.work / "venv"
        self.py = self.venv / "Scripts" / "python.exe"
        self.src = self.work / "src"
        self.pkg = self.src / "dokimi"
        self.outside = self.work / "outside"
        self.rec: dict = {"id": run_id, "installer": installer, "kind": kind,
                          "pair": ctx["pair"], "status": "running", "stages": {},
                          "validation": {}}
        self.venv_env = base_env({
            "VIRTUAL_ENV": str(self.venv), "SOURCE_DATE_EPOCH": ctx["src"]["source_date_epoch"],
            "PATH": str(self.venv / "Scripts") + os.pathsep + os.environ.get("PATH", "")})

    def save(self) -> None:
        write_json(self.out / "runs" / f"{self.id}.json", self.rec)

    def step(self, name: str, fn, section: str = "stages"):
        """Record the step as running first, so a crash still leaves its partial record."""
        self.rec["current"] = name
        self.rec[section][name] = {"status": "running"}
        self.save()
        print(f"[{self.id}] {name} ...", flush=True)
        t0 = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            self.rec[section][name] = {"status": "failed", "error": str(exc),
                                       "seconds": round(time.perf_counter() - t0, 3)}
            self.save()
            print(f"[{self.id}] {name} FAILED: {exc}", flush=True)
            raise
        secs = round(time.perf_counter() - t0, 3)
        self.rec[section][name] = {"status": "ok", "seconds": secs}
        self.save()
        print(f"[{self.id}] {name} ok {secs}s", flush=True)
        return result

    def cmd(self, name: str, argv: list[str], cwd: Path, env: dict[str, str]) -> str:
        log = self.out / "logs" / self.id / f"{name}.log"
        return run(argv, log, cwd=cwd, env=env, limit=LIMITS.get(name, LIMITS["validate"]))

    def manifest(self, name: str) -> list:
        def go():
            text = self.cmd(f"manifest_{name}", [str(self.py), "-I", "-c", MANIFEST_PY],
                            self.outside, self.venv_env)
            self.rec.setdefault("manifests", {})[name] = json.loads(text.splitlines()[-1])
        self.step(f"manifest_{name}", go, "validation")
        return self.rec["manifests"][name]

    def check_origin(self, name: str) -> None:
        def go():
            text = self.cmd(f"origin_{name}", [str(self.py), "-I", "-c", ORIGIN_PY],
                            self.outside, self.venv_env)
            where = Path(json.loads(text.splitlines()[-1])).resolve()
            site = (self.venv / "Lib" / "site-packages").resolve()
            if site not in where.parents or self.src.resolve() in where.parents:
                raise StageError(f"dokimi imported from {where}, not venv site-packages")
            self.rec.setdefault("origins", {})[name] = str(where)
        self.step(f"origin_{name}", go, "validation")

    def install(self, args: list[str], cache: str, cwd: Path) -> list[str]:
        """Installer command line shared by wheel and lock installs."""
        if self.installer == "pip":
            return [str(self.py), "-m", "pip", "install", "--cache-dir", cache, *args]
        return [self.ctx["tools"]["uv_exe"], "pip", "install", "--no-config", "--no-progress",
                "--python", str(self.py), "--cache-dir", cache, "--compile-bytecode", *args]

    def stages(self) -> None:
        ctx, constraints = self.ctx, self.ctx["lab"] / "constraints.txt"
        cache = str(ctx["lab"] / "cache" / self.installer)
        for p in (self.outside, self.out / "logs" / self.id):
            p.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ctx["src"]["src"], self.src)
        if {r: sha256(self.pkg / r) for r in INPUTS} != ctx["src"]["input_sha256"]:
            raise StageError("copied source inputs differ from pinned input hashes")
        lock = self.pkg / "requirements.lock.txt"
        lock_before = sha256(lock)
        py = str(self.py)
        build_env = {**self.venv_env, "PIP_CACHE_DIR": str(ctx["lab"] / "cache" / "build"),
                     "PIP_CONSTRAINT": str(ctx["lab"] / "build-constraints.txt")}
        self.step("venv", lambda: self.cmd("venv", [sys.executable, "-m", "venv", str(self.venv)],
                                           self.outside, base_env()))
        self.step("bootstrap", lambda: self.cmd(
            "bootstrap", [py, "-m", "pip", "install", "--no-index", "--no-cache-dir",
                          "--find-links", ctx["tools"]["wheelhouse"], *TOOLS],
            self.outside, self.venv_env))
        self.manifest("initial")
        dist = self.work / "dist"
        self.step("build", lambda: self.cmd(
            "build", [py, "-m", "build", "--wheel", "--outdir", str(dist), str(self.pkg)],
            self.pkg, build_env))
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise StageError(f"expected one wheel, found {len(wheels)}")
        self.rec["wheel"] = {"name": wheels[0].name, "sha256": sha256(wheels[0])}
        cargs = ["--constraint", str(constraints)] if constraints.is_file() else []
        self.rec["constrained_wheel_install"] = bool(cargs)
        self.step("wheel_install", lambda: self.cmd(
            "wheel_install", self.install([*cargs, str(wheels[0])], cache, self.outside),
            self.outside, {**self.venv_env, "PIP_CACHE_DIR": cache}))
        pre = self.manifest("pre_lock")
        if self.installer == "pip" and self.kind == "primer":
            constraints.write_text("".join(f"{n}=={v}\n" for n, v in pre if n != "dokimi"),
                                   encoding="utf-8")
        self.step("smoke", lambda: self.cmd(
            "smoke", [py, str(self.pkg / "scripts" / "smoke_installed.py")],
            self.outside, self.venv_env))
        self.check_origin("after_smoke")
        self.step("lock_install", lambda: self.cmd(
            "lock_install", self.install(["--require-hashes", "-r", "requirements.lock.txt"],
                                         cache, self.pkg),
            self.pkg, {**self.venv_env, "PIP_CACHE_DIR": cache}))
        self.manifest("post_lock")
        self.check_origin("after_lock")
        self.step("pip_check", lambda: self.cmd("pip_check", [py, "-m", "pip", "check"],
                                                self.outside, self.venv_env), "validation")
        if sha256(lock) != lock_before:
            raise StageError("requirements.lock.txt bytes changed")
        pytest = str(self.venv / "Scripts" / "pytest.exe")
        sel = [f"--randomly-seed={SEED}", *TEST_FILES]
        text = self.step("collect", lambda: self.cmd(
            "collect", [pytest, "--collect-only", "-q", *sel], self.pkg, self.venv_env),
            "validation")
        junit = self.out / "junit" / f"{self.id}.xml"
        junit.parent.mkdir(parents=True, exist_ok=True)
        self.step("tests", lambda: self.cmd(
            "tests", [pytest, "-q", f"--junitxml={junit}", *sel], self.pkg, self.venv_env))
        self.rec["tests"] = self.parse_tests(text, junit)

    @staticmethod
    def parse_tests(collect_text: str, junit: Path) -> dict:
        import xml.etree.ElementTree as ET  # noqa: S405 - our own pytest output

        collected = sorted(line.strip() for line in collect_text.splitlines() if "::" in line)
        cases = {}
        for tc in ET.parse(junit).getroot().iter("testcase"):
            bad = [c.tag for c in tc if c.tag in ("failure", "error", "skipped")]
            cases[f"{tc.get('classname')}.{tc.get('name')}"] = bad[0] if bad else "passed"
        executed = len(cases)
        if not collected or executed != len(collected):
            raise StageError(f"collected {len(collected)} ids but executed {executed}")
        if any(v != "passed" for v in cases.values()):
            raise StageError("not every JUnit case passed")
        return {"collected_ids": collected, "outcomes": dict(sorted(cases.items())),
                "executed": executed}

    def execute(self) -> dict:
        t0 = time.perf_counter()
        self.rec["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.stages()
            self.rec["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - recorded, matrix keeps going
            self.rec["status"], self.rec["error"] = "failed", str(exc)
        self.rec["wall_seconds"] = round(time.perf_counter() - t0, 3)
        self.rec["timed_stage_sum_seconds"] = round(sum(
            s.get("seconds", 0) for n, s in self.rec["stages"].items() if n in TIMED), 3)
        self.rec["cleanup_ok"] = rmtree(self.work)
        self.save()
        return self.rec


def metadata() -> dict:
    keys = ["ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_REF"]
    return {"env": {k: os.environ.get(k) for k in keys}, "platform": platform.platform(),
            "machine": platform.machine(), "cpu_count": os.cpu_count(),
            "python": sys.version, "python_executable": sys.executable,
            "python_implementation": platform.python_implementation()}


def equivalence(runs: list[dict]) -> dict[str, bool]:
    """Every run must agree on these; any mismatch fails the experiment closed."""
    def same(get) -> bool:
        vals = [json.dumps(get(r), sort_keys=True) for r in runs]
        return bool(vals) and len(set(vals)) == 1

    return {
        "all_runs_ok": bool(runs) and all(r["status"] == "ok" for r in runs),
        "wheel_name_and_sha": same(lambda r: r.get("wheel")),
        "manifest_initial": same(lambda r: r.get("manifests", {}).get("initial")),
        "manifest_pre_lock": same(lambda r: r.get("manifests", {}).get("pre_lock")),
        "manifest_post_lock": same(lambda r: r.get("manifests", {}).get("post_lock")),
        "test_ids_and_outcomes": same(lambda r: r.get("tests")),
        "constraints_used_after_primer": all(
            r.get("constrained_wheel_install") for r in runs if r["id"] != "primer-pip"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pair", type=int, required=True)
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
    summary: dict = {"status": "setup", "pair": args.pair, "metadata": metadata(),
                     "selected_commands": {"installers": ["pip install", "uv pip install"],
                                           "tests": TEST_FILES, "randomly_seed": SEED},
                     "limits": ["hosted CI-step experiment in clean virtualenvs; not the "
                                "global-Python windows-smoke job and no queue or end-to-end "
                                "speedup claim",
                                "uv download/setup is a one-off overhead excluded from the timed "
                                "arms; it must be counted in any net assessment",
                                "timed stages exclude collection, manifests, pip check and "
                                "origin checks; wall_seconds includes them",
                                "no statistical significance claim; one measured pair per job"]}
    write_json(out / "summary.json", summary)
    try:
        log = out / "logs" / "setup.log"
        src = prepare_source(repo, lab)
        summary["source"] = {k: v for k, v in src.items() if k != "src"}
        summary["source_date_epoch"] = src["source_date_epoch"]
        write_json(out / "summary.json", summary)
        (lab / "build-constraints.txt").write_text("setuptools==84.2.1\nwheel==0.48.0\n")
        tools = prepare_tools(lab, log, base_env())
        summary["uv_tool_bootstrap"] = tools
    except Exception as exc:  # noqa: BLE001
        summary.update(status="failed", error=f"setup: {exc}")
        write_json(out / "summary.json", summary)
        print(f"setup FAILED: {exc}", flush=True)
        return 1
    rng = random.Random(f"{os.environ.get('GITHUB_RUN_ID', '0')}-{args.pair}-{SEED}")
    order = rng.sample(["pip", "uv"], 2)
    summary["plan"] = {"primers": ["pip", "uv"], "measured_order": order}
    summary["status"] = "running"
    write_json(out / "summary.json", summary)  # plan is on disk before any measurement
    print(f"plan: primers pip,uv; measured order {order}", flush=True)
    ctx = {"out": out, "lab": lab, "src": src, "tools": tools, "pair": args.pair}
    runs: list[dict] = []
    for kind, name in [("primer", "pip"), ("primer", "uv"), *[("measured", n) for n in order]]:
        rec = Arm(f"{kind}-{name}", name, kind, ctx).execute()
        runs.append(rec)
        if rec["status"] != "ok" and rec["id"] == "primer-pip":
            break  # no constraints manifest exists; later runs would not be comparable
    guards = equivalence(runs) if len(runs) == 4 else {"all_four_runs_executed": False}
    guards["source_tree_unchanged"] = tree_hash(Path(src["src"])) == src["extracted_tree_sha256"]
    measured = {r["installer"]: r for r in runs if r["kind"] == "measured"}
    summary["runs"] = {r["id"]: {"status": r["status"], "error": r.get("error")} for r in runs}
    if len(measured) == 2:
        summary["measured"] = {
            "pair": args.pair, "order": order,
            **{n: {"stages": {s: r["stages"].get(s, {}).get("seconds") for s in TIMED},
                   "timed_stage_sum_seconds": r["timed_stage_sum_seconds"],
                   "wall_seconds": r["wall_seconds"]} for n, r in measured.items()},
            "timed_sum_delta_uv_minus_pip_seconds": round(
                measured["uv"]["timed_stage_sum_seconds"]
                - measured["pip"]["timed_stage_sum_seconds"], 3)}
    summary["guards"] = guards
    summary["status"] = "complete" if guards and all(guards.values()) else "failed"
    summary["job_wall_seconds"] = round(time.perf_counter() - t_job, 3)
    write_json(out / "summary.json", summary)
    print(f"status: {summary['status']} guards: {guards}", flush=True)
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
