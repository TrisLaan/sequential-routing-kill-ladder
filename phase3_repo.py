"""Phase 3 — per-repo operations for the code-agent smoke test.

Self-contained repo lifecycle used by both task selection (phase3_tasks.py) and
grading (phase3_grade.py), running on this Windows host with NO Docker:

  fetch_checkout -> make_venv -> pip_install -> apply_patch -> run_pytest_nodeids

Design notes
------------
* Work dirs live under C:\\p3 (SHORT path, OUTSIDE OneDrive) to dodge MAX_PATH
  limits and OneDrive sync churn on multi-thousand-file venvs.
* A single commit is fetched (`git fetch --depth 1 origin <sha>`) — GitHub allows
  fetching an arbitrary reachable SHA, so we never clone full history.
* Tests are run by EXACT FAIL_TO_PASS node-id (argv list, no shell) so bracketed
  parametrized ids with spaces are passed verbatim and safely. pytest output is
  parsed from the `-rA` short summary lines (matches dataset log_parser='pytest').
* resolve signal for the smoke test = all FAIL_TO_PASS node-ids PASS. Full
  PASS_TO_PASS regression checking is intentionally out of scope (documented).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WORK_ROOT = Path("C:/p3")               # short, non-OneDrive, non-synced
COMMON_TEST_DEPS = [                      # best-effort; many suites import these
    "pytest", "pytest-mock", "pytest-asyncio", "freezegun", "responses",
]
_PYTEST_SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(.+?)\s*$")


@dataclass
class RepoResult:
    ok: bool
    detail: str = ""
    statuses: dict[str, str] = field(default_factory=dict)


def testbed_dir(instance_id: str) -> Path:
    return WORK_ROOT / instance_id / "testbed"


def cleanup(instance_id: str) -> None:
    """Remove a candidate's whole workdir (testbed+venv) to reclaim disk — used
    for SKIPPED candidates during long selection walks."""
    import shutil
    d = WORK_ROOT / instance_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def venv_python(instance_id: str) -> Path:
    return WORK_ROOT / instance_id / "venv" / "Scripts" / "python.exe"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, timeout=timeout,
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env,
    )


def fetch_checkout(instance_id: str, repo: str, base_commit: str) -> RepoResult:
    """git fetch a single commit of github.com/<repo> into a fresh testbed."""
    tb = testbed_dir(instance_id)
    if tb.exists():
        # already materialised; assume good (idempotent re-runs)
        if (tb / ".git").exists():
            return RepoResult(True, "exists")
    tb.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", url],
        ["git", "fetch", "-q", "--depth", "1", "origin", base_commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for cmd in steps:
        p = _run(cmd, cwd=tb, timeout=600)
        if p.returncode != 0:
            return RepoResult(False, f"{' '.join(cmd)} failed: {p.stdout[-500:]}")
    return RepoResult(True, "fetched")


def make_venv(instance_id: str) -> RepoResult:
    vdir = WORK_ROOT / instance_id / "venv"
    if venv_python(instance_id).exists():
        return RepoResult(True, "venv exists")
    p = _run([sys.executable, "-m", "venv", str(vdir)], timeout=300)
    if p.returncode != 0:
        return RepoResult(False, f"venv create failed: {p.stdout[-300:]}")
    return RepoResult(True, "venv created")


def pip_install(instance_id: str) -> RepoResult:
    """pip install the repo (editable) + a small set of common test deps.

    Best-effort and deliberately minimal: -e . then common pytest plugins. If a
    repo needs exotic build/test deps we let it surface as a collection error in
    run_pytest and SKIP the task there (mechanical rule), rather than guessing.
    """
    py = str(venv_python(instance_id))
    tb = testbed_dir(instance_id)
    _run([py, "-m", "pip", "install", "-q", "-U", "pip", "setuptools", "wheel"], timeout=300)
    # 300s install cutoff IS the objective "pip-installable on this host within the
    # smoke-test budget" filter — heavy ML repos (torch etc.) exceed it and are skipped.
    try:
        p = _run([py, "-m", "pip", "install", "-q", "-e", "."], cwd=tb, timeout=300)
    except subprocess.TimeoutExpired:
        return RepoResult(False, "pip install -e . exceeded 300s budget (too heavy)")
    if p.returncode != 0:
        # try a couple of common editable extras before giving up
        for extra in (".[test]", ".[dev]", ".[tests]"):
            try:
                q = _run([py, "-m", "pip", "install", "-q", "-e", extra], cwd=tb, timeout=300)
            except subprocess.TimeoutExpired:
                continue
            if q.returncode == 0:
                p = q
                break
        else:
            return RepoResult(False, f"pip install -e . failed: {p.stdout[-800:]}")
    _run([py, "-m", "pip", "install", "-q", *COMMON_TEST_DEPS], timeout=900)
    return RepoResult(True, "installed")


def git_clean_reset(instance_id: str, base_commit: str) -> RepoResult:
    """Hard-reset testbed back to base_commit and drop untracked files."""
    tb = testbed_dir(instance_id)
    _run(["git", "checkout", "-q", "-f", base_commit], cwd=tb)
    _run(["git", "reset", "-q", "--hard", base_commit], cwd=tb)
    _run(["git", "clean", "-qfdx", "-e", "venv"], cwd=tb)
    return RepoResult(True, "reset")


def apply_patch(instance_id: str, patch_text: str, label: str = "patch") -> RepoResult:
    """Apply a unified diff to the testbed via `git apply` (3-way fallback)."""
    if not patch_text or not patch_text.strip():
        return RepoResult(True, "empty patch (noop)")
    tb = testbed_dir(instance_id)
    pf = WORK_ROOT / instance_id / f"{label}.diff"
    pf.write_text(patch_text, encoding="utf-8", newline="\n")
    for args in (["git", "apply", "--whitespace=nowarn", str(pf)],
                 ["git", "apply", "--3way", "--whitespace=nowarn", str(pf)]):
        p = _run(args, cwd=tb, timeout=120)
        if p.returncode == 0:
            return RepoResult(True, f"applied {label}")
    return RepoResult(False, f"apply {label} failed: {p.stdout[-500:]}")


def git_diff(instance_id: str) -> str:
    """Tracked-file diff of the testbed vs HEAD (the agent's source changes)."""
    tb = testbed_dir(instance_id)
    p = _run(["git", "diff"], cwd=tb, timeout=120)
    return p.stdout if p.returncode == 0 else ""


def parse_pytest(output: str) -> dict[str, str]:
    """Map node-id -> status from `pytest -rA` short-summary lines.

    FAILED/ERROR summary lines look like ``FAILED <nodeid> - <exc oneline>``; we
    strip the `` - <msg>`` suffix to recover the bare node id. (Node-id truncation
    by terminal width is prevented upstream by setting COLUMNS wide.)
    """
    out: dict[str, str] = {}
    for line in output.splitlines():
        m = _PYTEST_SUMMARY.match(line.strip())
        if m:
            status, rest = m.group(1), m.group(2).strip()
            nodeid = rest.split(" - ", 1)[0].strip()
            out[nodeid] = status
    return out


def run_pytest_nodeids(instance_id: str, node_ids: list[str],
                       timeout: int = 1200) -> tuple[dict[str, str], str]:
    """Run pytest -rA on exact node ids. Returns (statuses, raw_tail)."""
    py = str(venv_python(instance_id))
    tb = testbed_dir(instance_id)
    env = os.environ | {"PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1",
                        "PYTHONUTF8": "1", "COLUMNS": "1000", "MPLBACKEND": "Agg"}
    cmd = [py, "-m", "pytest", "-rA", "-p", "no:cacheprovider", "--no-header",
           "-q", *node_ids]
    p = _run(cmd, cwd=tb, timeout=timeout, env=env)
    statuses = parse_pytest(p.stdout)
    return statuses, p.stdout[-1500:]


def f2p_all_pass(statuses: dict[str, str], f2p: list[str]) -> bool:
    """True iff every FAIL_TO_PASS node id is present and PASSED."""
    for nid in f2p:
        if statuses.get(nid) != "PASSED":
            return False
    return True


def f2p_any_fail(statuses: dict[str, str], f2p: list[str]) -> bool:
    """True iff at least one FAIL_TO_PASS is FAILED/ERROR (well-formedness@base)."""
    return any(statuses.get(nid) in ("FAILED", "ERROR") for nid in f2p)
