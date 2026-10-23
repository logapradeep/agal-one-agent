"""Managed OTA (Over-The-Air) self-update for the Agal One agent, with
health-gated rollback — and nothing to do on the board's terminal.

The hard problem: the process that triggers an update *dies* when it restarts
the service, so rollback cannot live in that process. This module solves it with
an on-disk state marker plus an INDEPENDENT systemd verify-timer.

Flow (see Agal/contracts/enums.yaml#OtaStatus + firmware-rollout.schema.json):

  1. perform_update(target, source=, sha256=, outputs_on=) — runs on its own thread
     (main.start_firmware_update), never in the MQTT callback:
     a. the command is checked: a release version no older than MIN_OTA_VERSION (the first
        agent that updates from the release archive) and no older than the running one, and —
        when it names an archive — an archive URL on the allow-list (ARCHIVE_SOURCE_RE: the
        GitHub release archive of logapradeep/agal-one-agent for exactly that tag) with a sha256;
     b. the node waits while anything it drives is ON (outputs_on): a restart cuts every
        output, so it is not restarted under a running pump. Up to BUSY_WAIT_S, then the
        update ends `failed` with an error that starts "busy:". Agent 0.4.4 (contracts v1.26.0,
        nodes/README.md §13): the wait SAYS SO — wait_report() is the heartbeat's `otaWait`
        (phase waiting_idle, what is on, since, until), and after a wait that ran out (phase busy,
        what stayed on) until the next update; one heartbeat is asked for at once when the wait
        begins, when what is on changes and when it ends busy (on_wait_change);
     c. firmware_update_started; the archive is downloaded over HTTPS into the state
        directory (size cap, timeouts, redirects only to GitHub over HTTPS) and its
        sha256 checked BEFORE anything is installed;
     d. a SNAPSHOT of the running version is taken (every file its pip RECORD lists:
        the agal_one_agent package, its dist-info and its console scripts) and flushed to the
        SD card, so the way back needs no network, no git and no archive of the old version,
        and survives a power cut during the install;
     e. the verify-timer units are written and enabled if the board lacks them (the join
        script installs only the main service), and every module of the running version is
        imported, so this process never loads a file of the new version;
     f. ota_state.json → pending_verify; `pip install --no-deps --require-hashes -r` of
        the local archive — the flags the join script uses (backend enroll.ts
        installerScript); a requirement of the new version that is MISSING is installed
        from the package index the way the join script installs the agent's libraries —
        never an upgrade of a library the board has (the way back does not cover libraries);
        the installed version must be the target;
     g. if the install fails, the saved version is put back and the update ends `failed` (no
        restart: the running process is still the old version). If an output came on
        meanwhile, the node waits again (bounded), then restarts through systemd.
  2. The NEW process owns its probation (main.py): begin_probation() at boot, before MQTT,
     counts the deadline from that boot (once per update); a Probation thread then checks
     every PROBATION_POLL_S, from PROBATION_WINDOW_S on, and confirms at the first healthy
     check (MQTT connected and the main loop alive) → phase=idle, firmware_update_succeeded.
     Meanwhile the main loop writes a heartbeat file (version, connected, what is ON).
  3. An INDEPENDENT systemd verify-timer runs run_verify() every ~60s. If a
     pending_verify update is past its deadline (the new version crash-looped,
     hung, or never connected), it puts the snapshot back — or, when the snapshot is
     damaged, installs the previous version again from its release archive — checks
     that the restored agent starts, emits firmware_update_rolled_back, and restarts.
     It holds off (bounded) while the new version's heartbeat says an output is ON, and
     a rollback that fails is tried again with a backoff instead of being given up. It
     runs the rollback code of the version that took the snapshot (verify.py puts the
     snapshot first on the import path when it is whole), so a new version that does
     not even import is still rolled back.

A command without `source` (a backend older than agent 0.3.4) installs from git
(--no-deps, as the archive) — that needs git on the board, and a board without it says so.

Firmware lifecycle events are reported over HTTP (http_reporter), NOT MQTT, so
they land even while the daemon is down / MQTT is mid-reconnect.
"""

import contextlib
import copy
import glob
import hashlib
import importlib
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from importlib import metadata as _metadata
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover — not on every platform the tests run on
    fcntl = None

logger = logging.getLogger(__name__)

DIST_NAME = "agal-one-agent"
PACKAGE = __name__.split(".")[0]  # agal_one_agent
GITHUB_REPO = "https://github.com/logapradeep/agal-one-agent.git"
VENV_PREFIX = sys.prefix
VENV_PIP = os.path.join(sys.prefix, "bin", "pip")
SERVICE_NAME = "agal-one-agent"
VERIFY_UNIT = "agal-one-agent-ota-verify"
SYSTEMD_DIR = "/etc/systemd/system"
CONFIG_PATH = os.environ.get("AGAL_ONE_AGENT_CONFIG", "/etc/agal-one-agent/config.yaml")
# The join script's requirement line for the agent it installed (backend enroll.ts installerScript):
# the release archive + sha256 of that version, for installing it again when its saved copy is damaged.
JOIN_REQUIREMENTS = os.environ.get("AGAL_ONE_AGENT_JOIN_REQUIREMENTS", "/opt/agal-one-agent/agent.requirements")

STATE_DIR = os.environ.get("AGAL_ONE_AGENT_STATE_DIR", "/var/lib/agal-one-agent")
STATE_FILE = os.path.join(STATE_DIR, "ota_state.json")
# Where the installed agent is looked up (None: the interpreter's own path, the
# snapshot excluded). Tests point it at a fake venv.
SITE_PACKAGES: Optional[str] = None

# The first agent that updates itself from the release archive (no git, no terminal). An update
# never installs an older one: its own updater would need git — and the terminal — for the next one.
MIN_OTA_VERSION = "v0.3.4"

# The new version must stay healthy this long (seconds) before self-confirming.
PROBATION_WINDOW_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_PROBATION_S", "60"))
# After PROBATION_WINDOW_S the new process checks this often and confirms at the first healthy check.
PROBATION_POLL_S = 5.0
# The verify-timer rolls back if still unconfirmed this long after the restart. It covers the old
# version's shutdown and the new one's start until begin_probation (which then sets the deadline below).
VERIFY_DEADLINE_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_DEADLINE_S", "150"))
# begin_probation: the deadline, counted from the new version's boot — MQTT connect (≤ 30 s), boot
# reconcile (≤ 30 s), the program pull, PROBATION_WINDOW_S and many PROBATION_POLL_S checks.
PROBATION_DEADLINE_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_PROBATION_DEADLINE_S", "300"))
# While pip runs (and while the node waits to restart) the deadline is kept this far
# ahead, so the verify-timer never puts the snapshot back under a running install.
PIP_TIMEOUT_S = 300
INSTALL_GRACE_S = 2 * PIP_TIMEOUT_S + VERIFY_DEADLINE_S
# How long an update waits for the node's outputs to switch off, and how often it looks.
BUSY_WAIT_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_BUSY_WAIT_S", "1800"))
BUSY_POLL_S = 10.0
# Agent 0.4.4 (contracts v1.26.0, nodes/README.md §13): what the wait's report names, and how often a change of it asks
# for a heartbeat at once (more often, the next 30-s heartbeat carries it).
WAIT_MAX_ON = 16
WAIT_MAX_NAME = 80
WAIT_MAX_RUNS = 64
WAIT_NUDGE_MIN_S = 5.0

# The new version's heartbeat during probation (written by its main loop), read by the verify-timer.
HEARTBEAT_EVERY_S = 10.0
HEARTBEAT_STALE_S = 45          # older than this: the new process is gone or hung
LOOP_STALE_S = 15.0             # the main loop has not ticked for this long: not healthy
CONFIRM_GRACE_S = 120           # past the deadline, an up-and-connected new version gets this long to confirm
# A rollback that fails is tried again after these waits (the last one repeats), never given up.
ROLLBACK_RETRY_S = (60, 120, 300, 600, 1800)
IMPORT_CHECK_TIMEOUT_S = 180    # a fresh interpreter importing the agent on a Pi 3B

# Only the GitHub release archive of this repository, for exactly the tag the
# command asks for. The hash is what makes it trusted; the allow-list keeps a
# command from pointing the node anywhere else.
ARCHIVE_SOURCE_RE = re.compile(
    r"^https://github\.com/logapradeep/agal-one-agent/archive/refs/tags/"
    r"(v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)\.tar\.gz$")
REDIRECT_HOSTS = frozenset({"github.com", "codeload.github.com"})  # GitHub answers the archive URL with a redirect
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024   # the release archive is ~3 MB
DOWNLOAD_TIMEOUT_S = 30                # per connect / read
DOWNLOAD_DEADLINE_S = 300              # the whole download
MIN_FREE_BYTES = 64 * 1024 * 1024      # archive + snapshot + pip's build, with room to spare

# Seams for the tests (no real sleeping, no real clock).
_sleep = time.sleep
_monotonic = time.monotonic
_wall_ms = lambda: time.time() * 1000.0  # noqa: E731 — the wait's dates when no trusted time is given

# The wait's own record (agent 0.4.4): the heartbeat's otaWait, read from memory by wait_report().
_WAIT_LOCK = threading.Lock()
_WAIT: Optional[dict] = None
_WAIT_NUDGED: Optional[float] = None

_UPDATE_LOCK = threading.Lock()


class OtaError(Exception):
    """An update step that could not be done; its message is the event's error."""


# ---- version helpers -------------------------------------------------------

def _norm_tag(version: str) -> str:
    """Normalize to a GitHub release tag form (vX.Y.Z)."""
    v = (version or "").strip()
    return v if v.startswith("v") else f"v{v}"


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.]+))?$")


def _version_key(version: Optional[str]):
    """A sortable key for a release version (a pre-release sorts before its release), or None."""
    m = _VERSION_RE.match((version or "").strip())
    if not m:
        return None
    pre = m.group(4)
    pre_key = (1,) if pre is None else (0,) + tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre_key)


def _ota_dir() -> str:
    return os.path.join(STATE_DIR, "ota")


def _snapshot_dir() -> str:
    return os.path.join(_ota_dir(), "previous")


def _heartbeat_file() -> str:
    return os.path.join(_ota_dir(), "heartbeat.json")


def _installed_package_file() -> str:
    return os.path.join(_ota_dir(), "installed.json")


def _find_dist():
    """The installed agal-one-agent distribution, or None. Never the snapshot's copy."""
    importlib.invalidate_caches()  # pip may have just changed site-packages
    if SITE_PACKAGES:
        path = [SITE_PACKAGES]
    else:
        snap = os.path.normpath(os.path.abspath(_ota_dir()))

        def in_snapshot(p: str) -> bool:
            p = os.path.normpath(os.path.abspath(p or "."))
            return p == snap or p.startswith(snap + os.sep)

        path = [p for p in sys.path if not in_snapshot(p)]
    for dist in _metadata.distributions(name=DIST_NAME, path=path):
        return dist
    return None


def installed_version() -> str:
    """Currently-installed package version as a tag (vX.Y.Z). Falls back to v0.0.0."""
    try:
        dist = _find_dist()
        return _norm_tag(dist.version) if dist is not None and dist.version else "v0.0.0"
    except Exception:  # noqa: BLE001
        return "v0.0.0"


# ---- writing to the SD card (a power cut on a farm is normal) ------------------

def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_durable(path: str, text: str, *, sync: bool = True) -> None:
    """Write ``path`` atomically (a temporary file, then a rename); with ``sync`` the file and its
    directory are flushed to disk first, so a power cut leaves the old file or the new one — never an
    empty one (ext4 writes a new file's data up to ~30 s after its rename)."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        if sync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX
    if sync:
        _fsync_dir(folder)


def _sync_all() -> None:
    """Everything written so far reaches the SD card: the saved copy before pip touches the venv, the
    new version's files before the restart, the restored files before the rollback's restart."""
    try:
        os.sync()
    except (AttributeError, OSError) as e:  # pragma: no cover — os.sync exists on Linux and macOS
        logger.debug("OTA: sync failed: %s", e)


# ---- state file (atomic, durable) --------------------------------------------

def read_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        return state if isinstance(state, dict) and state else {"phase": "idle"}
    except FileNotFoundError:
        return {"phase": "idle"}
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: could not read state (%s); treating as idle", e)
        return {"phase": "idle"}


def write_state(state: dict) -> None:
    state["updatedEpoch"] = int(time.time())
    try:
        _write_durable(STATE_FILE, json.dumps(state))
    except Exception as e:  # noqa: BLE001
        logger.error("OTA: failed to persist state: %s", e)


@contextlib.contextmanager
def _state_lock(wait_s: float = 0.0):
    """An exclusive lock between the verify-timer's rollback and the running agent's probation (a
    confirmation must not land in the middle of a rollback, or the other way round). Yields True when
    held; after ``wait_s`` without it, False (the caller tries again later)."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd = os.open(os.path.join(STATE_DIR, "ota.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.warning("OTA: the state lock cannot be opened (%s); going ahead without it", e)
        yield True
        return
    held = False
    try:
        give_up = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= give_up:
                    break
                time.sleep(0.2)
        yield held
    finally:
        if held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# ---- low-level ops (patched out in tests) ----------------------------------

def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _systemctl_cmd(*args: str) -> list:
    # The agent runs as root (the join script's unit has no User=); sudo only when it does not,
    # and never waiting for a password.
    return ["systemctl", *args] if _is_root() else ["sudo", "-n", "systemctl", *args]


def _systemctl(*args: str) -> "subprocess.CompletedProcess":
    try:
        return subprocess.run(_systemctl_cmd(*args), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, -1, "", str(e))


def _run_pip(args: list) -> "subprocess.CompletedProcess":
    """pip of the agent's venv. A pip that cannot start or runs past PIP_TIMEOUT_S is a failed one."""
    try:
        return subprocess.run([VENV_PIP, *args], capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, -1, "", f"pip did not finish: {e}")


def _pip_install(tag: str) -> "subprocess.CompletedProcess":
    """The git path (a command without `source`, from an older backend): install a tag,
    force-reinstalling so the exact ref lands. --no-deps, as the archive: a library the new version
    lacks is added afterwards (_ensure_requirements), and none the board has is upgraded — the way
    back covers the agent's own files only. Needs git on the board."""
    url = f"git+{GITHUB_REPO}@{tag}"
    logger.info("OTA: pip install %s", url)
    return _run_pip(["install", "--no-deps", "--force-reinstall", url])


def _pip_install_archive(archive: str, sha256: str, *, force: bool = False) -> "subprocess.CompletedProcess":
    """The downloaded, already-checked archive, installed the way the join script installs the
    agent: one requirement line with its hash, --no-deps --require-hashes (pip checks the hash a
    second time). ``force``: reinstall over the same version (the way back over a damaged install)."""
    req = os.path.join(os.path.dirname(archive), "agent.requirements")
    with open(req, "w") as f:
        f.write(f"{DIST_NAME} @ {pathlib.Path(archive).resolve().as_uri()} --hash=sha256:{sha256}\n")
    logger.info("OTA: pip install --no-deps --require-hashes %s", archive)
    return _run_pip(["install", "--no-deps", "--require-hashes", *(["--force-reinstall"] if force else []), "-r", req])


def _have_git() -> bool:
    return shutil.which("git") is not None


def _restart_service() -> None:
    """Queue a restart of the main unit and return once systemd has it (--no-block). Run to the end
    rather than fired off: from the verify-timer's oneshot unit a detached systemctl is killed with the
    unit's processes when the check exits, maybe before it asked for the restart."""
    logger.info("OTA: restarting %s", SERVICE_NAME)
    r = _systemctl("restart", "--no-block", SERVICE_NAME)
    if r.returncode != 0:
        logger.error("OTA: the restart of %s was not queued: %s", SERVICE_NAME,
                     (r.stderr or r.stdout or "").strip()[:300])


def _check_runs(version: Optional[str]) -> Optional[str]:
    """None when a fresh interpreter of the venv imports the agent and it is ``version``; else why not.
    A restored version that cannot start is not a way back."""
    python = os.path.join(_prefix(), "bin", "python")
    code = f"import {PACKAGE}.main, {PACKAGE} as a; print(a.__version__)"
    try:
        r = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True,
                           timeout=IMPORT_CHECK_TIMEOUT_S, cwd="/")
    except (OSError, subprocess.SubprocessError) as e:
        return f"it could not be started to check it: {e}"
    if r.returncode != 0:
        lines = [line for line in (r.stderr or r.stdout or "").strip().splitlines() if line.strip()]
        return f"it does not import: {(lines[-1] if lines else f'exit {r.returncode}')[:300]}"
    out = (r.stdout or "").strip().splitlines()
    got = _norm_tag(out[-1]) if out else "v0.0.0"
    if version and got != _norm_tag(version):
        return f"it starts as {got}, not {version}"
    return None


def _preload_package() -> None:
    """Import every module of the running agent before pip replaces its files. This process keeps
    running the old version until the restart — for up to BUSY_WAIT_S when an output comes on — and a
    module it imported for the first time meanwhile (a handler, a lazy import in a command) would be
    the NEW version's, next to old modules it may not fit."""
    import pkgutil  # noqa: PLC0415
    try:
        pkg = importlib.import_module(PACKAGE)
        for info in pkgutil.walk_packages(getattr(pkg, "__path__", []), PACKAGE + ".", onerror=lambda _n: None):
            if info.name.rsplit(".", 1)[-1] == "__main__" or info.name in sys.modules:
                continue
            try:
                importlib.import_module(info.name)
            except Exception as e:  # noqa: BLE001 — a module this board cannot import is not imported later either
                logger.debug("OTA: preload of %s skipped: %s", info.name, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: the running version's modules could not all be loaded first: %s", e)


def _preload_reporting() -> None:
    """Import what _emit needs before the snapshot is put back, so a process running one version's
    code never imports a module of the other version halfway through a rollback."""
    try:
        from . import http_reporter, config  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.debug("OTA: preload for reporting failed: %s", e)


def _emit(phase: str, state: dict, *, error: str = None) -> None:
    """Report a firmware lifecycle event over HTTP. Best-effort — a reporting
    failure must never crash an update/rollback."""
    node_uid = state.get("nodeUid")
    if not node_uid:
        logger.debug("OTA: no nodeUid in state; skipping %s report", phase)
        return
    try:
        from .http_reporter import HttpReporter
        # Best-effort auth token from the on-disk config (ADR-013 P0.5 —
        # telemetryIngress verifies it once enforcement is on). OTA events must
        # still go out even if the config is unreadable mid-update, so failures
        # fall back to an unauthenticated report.
        auth_token = ""
        base_url = ""
        try:
            from .config import AgentConfig
            cfg = AgentConfig.from_yaml(os.environ.get("AGAL_ONE_AGENT_CONFIG", CONFIG_PATH))
            auth_token = cfg.node.auth_token
            base_url = getattr(cfg.telemetry, "ingress_url", "") or ""
        except Exception:  # noqa: BLE001
            logger.debug("OTA: could not load auth token for %s report", phase)
        HttpReporter(node_uid, auth_token=auth_token, base_url=base_url).report_firmware_event(
            phase=phase,
            from_version=state.get("previous"),
            to_version=state.get("target"),
            command_id=state.get("commandId"),
            rollout_id=state.get("rolloutId"),
            error=error,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: firmware event report failed (%s): %s", phase, e)


# ---- the archive -------------------------------------------------------------

def source_error(source: str, sha256: Optional[str], target: str) -> Optional[str]:
    """Why this archive will not be installed, or None when it may be."""
    m = ARCHIVE_SOURCE_RE.match(source or "")
    if not m:
        return (f"source {str(source)[:200]!r} is not allowed: only "
                "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/vX.Y.Z.tar.gz is")
    if m.group(1) != target:
        return f"source is the archive of {m.group(1)}, but the update is to {target}"
    if not sha256 or not SHA256_RE.match(sha256):
        return "the update names an archive but no sha256 to check it against"
    return None


class _AllowListRedirect(urllib.request.HTTPRedirectHandler):
    """Follows GitHub's archive redirect (github.com → codeload.github.com) and nothing else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlsplit(newurl)
        if p.scheme != "https" or (p.hostname or "") not in REDIRECT_HOSTS:
            raise urllib.error.HTTPError(newurl, code, f"redirect to {p.scheme}://{p.hostname} refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_AllowListRedirect)


def _download(url: str, dest: str) -> None:
    """Stream ``url`` into ``dest`` over HTTPS: at most MAX_ARCHIVE_BYTES, DOWNLOAD_DEADLINE_S in all."""
    started = _monotonic()
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": f"{DIST_NAME}-ota"})
    try:
        with _opener().open(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise OtaError(f"the archive download answered HTTP {status}")
            final = urllib.parse.urlsplit(resp.geturl() or url)
            if final.scheme != "https" or (final.hostname or "") not in REDIRECT_HOSTS:
                raise OtaError(f"the archive came from {final.scheme}://{final.hostname}, which is not allowed")
            length = resp.headers.get("Content-Length") if resp.headers is not None else None
            if length and length.isdigit() and int(length) > MAX_ARCHIVE_BYTES:
                raise OtaError(f"the archive is {int(length)} bytes, more than the {MAX_ARCHIVE_BYTES} allowed")
            total = 0
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise OtaError(f"the archive is larger than the {MAX_ARCHIVE_BYTES} bytes allowed")
                    if _monotonic() - started > DOWNLOAD_DEADLINE_S:
                        raise OtaError(f"the archive download took longer than {DOWNLOAD_DEADLINE_S} s")
                    f.write(chunk)
        os.replace(part, dest)
    except OtaError:
        _remove(part)
        raise
    except Exception as e:  # noqa: BLE001 — not reached, refused, timed out
        _remove(part)
        raise OtaError(f"the archive could not be downloaded: {e}") from e


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug("OTA: could not remove %s: %s", path, e)


def _fetch_archive(source: str, sha256: str, target: str) -> str:
    """Download the archive into the state directory and check its sha256. Returns its path."""
    folder = os.path.join(_ota_dir(), "download")
    os.makedirs(folder, exist_ok=True)
    for old in glob.glob(os.path.join(folder, "*")):  # what an interrupted update left
        _remove(old)
    dest = os.path.join(folder, f"{DIST_NAME}-{target}.tar.gz")
    logger.info("OTA: downloading %s", source)
    _download(source, dest)
    got = _sha256_file(dest)
    if got != sha256:
        _remove(dest)
        raise OtaError(f"the archive's sha256 is {got}, not the {sha256} the update names: not installed")
    logger.info("OTA: archive %s checked (sha256 %s)", os.path.basename(dest), got)
    return dest


def _discard_downloads() -> None:
    folder = os.path.join(_ota_dir(), "download")
    for old in glob.glob(os.path.join(folder, "*")):
        _remove(old)


def _check_free_space() -> None:
    try:
        os.makedirs(_ota_dir(), exist_ok=True)
        free = shutil.disk_usage(_ota_dir()).free
    except OSError as e:
        raise OtaError(f"the state directory is not usable: {e}") from e
    if free < MIN_FREE_BYTES:
        raise OtaError(f"only {free // (1024 * 1024)} MB free on the board, {MIN_FREE_BYTES // (1024 * 1024)} MB needed")


# ---- the release archive of a version this board ran ---------------------------

_JOIN_LINE_RE = re.compile(r"^\s*agal-one-agent\s*@\s*(\S+)\s+--hash=sha256:([a-f0-9]{64})\s*$", re.M)


def _join_package() -> Optional[dict]:
    try:
        with open(JOIN_REQUIREMENTS) as f:
            m = _JOIN_LINE_RE.search(f.read())
    except OSError:
        return None
    return {"source": m.group(1), "sha256": m.group(2)} if m else None


def _known_package(version: str) -> Optional[dict]:
    """The release archive + sha256 of ``version`` when this board knows them: from the update that
    installed it (ota/installed.json) or from the join script that did (its requirement line)."""
    try:
        with open(_installed_package_file()) as f:
            recorded = json.load(f)
    except (OSError, ValueError):
        recorded = None
    for pkg in (recorded, _join_package()):
        if isinstance(pkg, dict) and source_error(pkg.get("source"), pkg.get("sha256"), version) is None:
            return {"source": pkg["source"], "sha256": pkg["sha256"]}
    return None


def _record_installed(version: Optional[str], pkg: Optional[dict]) -> None:
    """Remember the archive of the version now installed, for the way back of the next update."""
    if not version or not isinstance(pkg, dict) or source_error(pkg.get("source"), pkg.get("sha256"), version):
        return
    try:
        _write_durable(_installed_package_file(),
                       json.dumps({"version": version, "source": pkg["source"], "sha256": pkg["sha256"]}))
    except OSError as e:
        logger.warning("OTA: could not record the archive of %s: %s", version, e)


# ---- the snapshot (the way back, offline) ------------------------------------

def _prefix() -> str:
    return os.path.normpath(os.path.abspath(VENV_PREFIX))


def _safe_rel(rel: str) -> Optional[str]:
    """A path relative to the venv that stays inside it, or None."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    rel = os.path.normpath(rel)
    if rel == ".." or rel.startswith(".." + os.sep) or rel == ".":
        return None
    return rel


def _dist_paths(dist) -> list:
    """(absolute path, path relative to the venv) of every file the distribution's RECORD lists
    inside the venv; anything outside it is left out (and never touched)."""
    prefix = _prefix()
    out, outside = [], []
    for f in dist.files or []:
        path = os.path.normpath(os.path.abspath(str(dist.locate_file(f))))
        rel = _safe_rel(os.path.relpath(path, prefix))
        if rel is None:
            outside.append(path)
            continue
        out.append((path, rel))
    if outside:
        # e.g. compiled caches under a PYTHONPYCACHEPREFIX; they are rebuilt when needed
        logger.info("OTA: %d file(s) of %s %s are outside the venv and left alone (first: %s)",
                    len(outside), DIST_NAME, dist.version, outside[0])
    return out


def _pip_error(result) -> str:
    """What pip said went wrong, short enough for an event: its ERROR lines, else its last words."""
    text = (result.stderr or result.stdout or "").strip()
    errors = [line.strip() for line in text.splitlines() if line.strip().startswith("ERROR:")]
    return (" ".join(errors) or text)[-500:] or f"pip exited {result.returncode}"


def _take_snapshot(version: str) -> dict:
    """Copy every file of the running version (its RECORD: package, dist-info, console scripts)
    into the state directory, with each file's sha256, and flush it to the SD card before it is
    named the saved copy. Libraries are not part of it: an update installs the agent --no-deps and
    adds only a library that is missing (_ensure_requirements) — it never changes one the board has."""
    dist = _find_dist()
    if dist is None:
        raise OtaError("the running agent's install record was not found, so there would be no way back: not updated")
    if not dist.files:
        raise OtaError("the running agent has no RECORD of its files (not a pip install), so there would be "
                       "no way back: not updated")
    prefix = _prefix()
    site = os.path.normpath(os.path.abspath(str(dist.locate_file(""))))
    site_rel = _safe_rel(os.path.relpath(site, prefix))
    if site_rel is None:
        raise OtaError(f"the agent's site-packages {site} is outside its venv {prefix}: not updated")
    final = _snapshot_dir()
    tmp, old = final + ".tmp", final + ".old"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        entries = []
        for path, rel in _dist_paths(dist):
            if not os.path.isfile(path):
                continue  # RECORD lists a file that is not there (a .pyc never written)
            dst = os.path.join(tmp, "tree", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
            entries.append({"rel": rel, "sha256": _sha256_file(dst)})
        if not any(e["rel"].endswith(os.path.join(PACKAGE, "__init__.py")) for e in entries):
            raise OtaError("the running agent's files were not found on disk: not updated")
        manifest = {"version": version, "distVersion": dist.version, "sitePackages": site_rel,
                    "files": entries, "takenEpoch": int(time.time())}
        with open(os.path.join(tmp, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        # Every saved byte on the card before the copy is named the way back: a power cut during the
        # install must not leave a saved copy whose files came back empty.
        _sync_all()
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(final):
            os.rename(final, old)
        os.rename(tmp, final)
        _fsync_dir(os.path.dirname(final))
        shutil.rmtree(old, ignore_errors=True)
    except OtaError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        raise OtaError(f"the running version could not be saved for the way back: {e}") from e
    logger.info("OTA: %s saved for the way back (%d files)", version, len(entries))
    return {"dir": final, "version": version, "files": len(entries)}


def _restore_snapshot(expected_version: Optional[str] = None) -> tuple:
    """Put the saved version back, from the state directory alone: (ok, what happened).

    Every saved file is checked against its sha256 first, so nothing is taken out unless the way
    back is whole. Then the files of whatever agal-one-agent is installed now (its RECORD) are
    removed — with the package directory and every agal_one_agent-*.dist-info — and the saved
    files copied back. Libraries are not touched (an update never changes one the board has)."""
    snap = _snapshot_dir()
    try:
        with open(os.path.join(snap, "manifest.json")) as f:
            man = json.load(f)
    except FileNotFoundError:
        return False, "no saved version to go back to"
    except Exception as e:  # noqa: BLE001
        return False, f"the saved version is unreadable: {e}"
    if expected_version and man.get("version") != expected_version:
        return False, f"the saved version is {man.get('version')}, not {expected_version}"
    prefix = _prefix()
    site_rel = _safe_rel(man.get("sitePackages") or "")
    if site_rel is None:
        return False, "the saved version names no site-packages"
    site = os.path.join(prefix, site_rel)
    entries = []
    for e in man.get("files") or []:
        rel = _safe_rel(e.get("rel"))
        blob = os.path.join(snap, "tree", rel) if rel else None
        if rel is None:
            return False, f"the saved version holds a path outside the venv ({e.get('rel')!r})"
        try:
            if _sha256_file(blob) != e.get("sha256"):
                return False, f"the saved copy of {rel} is damaged"
        except OSError as err:
            return False, f"the saved copy of {rel} is missing: {err}"
        entries.append((rel, blob))
    if not entries:
        return False, "the saved version holds no files"
    try:
        cur = _find_dist()
        if cur is not None:
            for path, _rel in _dist_paths(cur):
                if os.path.islink(path) or os.path.isfile(path):
                    os.remove(path)
        doomed = [os.path.join(site, PACKAGE)] + glob.glob(os.path.join(site, f"{PACKAGE}-*.dist-info"))
        for d in doomed:
            if os.path.isdir(d) and os.path.dirname(os.path.normpath(d)) == os.path.normpath(site):
                shutil.rmtree(d)
        for rel, blob in entries:
            dst = os.path.join(prefix, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(blob, dst)
    except Exception as e:  # noqa: BLE001
        return False, f"putting {man.get('version')} back failed: {e}"
    importlib.invalidate_caches()
    logger.info("OTA: %s put back from the saved copy (%d files)", man.get("version"), len(entries))
    return True, f"put {man.get('version')} back from the saved copy ({len(entries)} files)"


def _restore_previous(state: dict) -> tuple:
    """The way back for this update: the snapshot when it took one; the git reinstall of
    `previous` for a state written by an agent older than 0.3.4 (it took none)."""
    if state.get("snapshot"):
        return _restore_snapshot(state.get("previous"))
    previous = state.get("previous")
    result = _pip_install(previous)
    if result.returncode != 0:
        return False, _pip_error(result)
    return True, f"reinstalled {previous} from git"


def _reinstall_from_archive(version: str, pkg: dict) -> tuple:
    """The way back over the network, when the saved copy cannot be used: the release archive of
    ``version`` with the sha256 this board recorded for it, installed --no-deps --require-hashes."""
    archive = None
    try:
        _check_free_space()
        archive = _fetch_archive(pkg["source"], pkg["sha256"], version)
        r = _pip_install_archive(archive, pkg["sha256"], force=True)
    except OtaError as e:
        return False, f"installing {version} again from its release archive failed: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"installing {version} again from its release archive broke off: {e}"
    finally:
        if archive is not None:
            _discard_downloads()
    if r.returncode != 0:
        return False, f"installing {version} again from its release archive failed: {_pip_error(r)[:300]}"
    got = installed_version()
    if got != version:
        return False, f"installing {version} again from its release archive left {got}"
    return True, f"installed {version} again from its release archive"


def _go_back(state: dict) -> tuple:
    """Put the previous version back and check that it starts: (ok, what happened). The saved copy
    first (offline); when that cannot be used, or what it put back does not start, the previous
    version's release archive when this board knows it."""
    previous = state.get("previous")
    ok, how = _restore_previous(state)
    if ok:
        why = _check_runs(previous)
        if why is None:
            _sync_all()
            return True, how
        how = f"{how}, but it does not start: {why}"
    pkg = state.get("previousPackage")
    if not isinstance(pkg, dict):
        return False, f"{how}; no release archive of {previous} is known to install it again"
    ok, how2 = _reinstall_from_archive(previous, pkg)
    if ok:
        why = _check_runs(previous)
        if why is None:
            _sync_all()
            return True, how2
        how2 = f"{how2}, but it does not start: {why}"
    return False, f"{how}; {how2}"


# ---- the verify-timer ---------------------------------------------------------

VERIFY_LAUNCHER = '''\
# Written by agal-one-agent (ota_updater._ensure_verify_timer): the managed-OTA
# rollback check, run every minute by {unit}.timer.
# Nothing is imported unless an update is in probation. Then it runs the rollback
# code of the version saved before that update (first on the import path) when
# every saved file still matches its sha256, so a new version that does not
# install or import cleanly is still rolled back; else the installed agent's.
import hashlib
import json
import os
import sys

STATE = {state!r}
SNAPSHOT = {snapshot!r}
PACKAGE = {package!r}

try:
    with open(STATE) as f:
        if json.load(f).get("phase") != "pending_verify":
            sys.exit(0)
except (OSError, ValueError, AttributeError):
    sys.exit(0)


def whole_snapshot_site():
    try:
        with open(os.path.join(SNAPSHOT, "manifest.json")) as f:
            man = json.load(f)
        for e in man["files"]:
            with open(os.path.join(SNAPSHOT, "tree", e["rel"]), "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != e["sha256"]:
                    return None
        site = os.path.join(SNAPSHOT, "tree", man["sitePackages"])
        return site if os.path.isfile(os.path.join(site, PACKAGE, "ota_updater.py")) else None
    except Exception:
        return None


main_verify = None
site = whole_snapshot_site()
if site:
    sys.path.insert(0, site)
    try:
        from {package}.ota_updater import main_verify
    except Exception:
        sys.path.remove(site)
        for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
            del sys.modules[name]
        main_verify = None
if main_verify is None:
    from {package}.ota_updater import main_verify

main_verify()
'''

VERIFY_SERVICE = """\
[Unit]
Description=Agal One Agent — managed-OTA rollback verifier
# Written by agal-one-agent before an update. Independent of {service}.service on
# purpose: this is the safety net that runs even when the main service is
# crash-looping on a bad OTA. It puts the saved previous version back + restarts
# once an unconfirmed update is past its deadline. No-op for any non-probation
# state (reads one JSON file and exits).
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# root: the rollback writes into the venv and restarts the main unit.
User=root
Environment=AGAL_ONE_AGENT_STATE_DIR={state_dir}
Environment=AGAL_ONE_AGENT_CONFIG={config}
ExecStart={python} -I {launcher}
"""

VERIFY_TIMER = """\
[Unit]
Description=Periodic managed-OTA rollback check for Agal One Agent

[Timer]
# Written by agal-one-agent before an update. 60 s after it is started and after
# boot, then every 60 s. Cheap (reads ota_state.json) and only acts when a
# pending_verify update is past its deadline. A healthy update self-confirms
# within ~PROBATION_WINDOW_S, well before the deadline.
OnActiveSec=60
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10s
Persistent=false

[Install]
WantedBy=timers.target
"""


def verify_units() -> dict:
    """{path: text} of the launcher and the two units, for this venv and state directory."""
    launcher = os.path.join(_ota_dir(), "verify.py")
    python = os.path.join(_prefix(), "bin", "python")
    return {
        launcher: VERIFY_LAUNCHER.format(unit=VERIFY_UNIT, state=STATE_FILE, snapshot=_snapshot_dir(), package=PACKAGE),
        os.path.join(SYSTEMD_DIR, f"{VERIFY_UNIT}.service"): VERIFY_SERVICE.format(
            service=SERVICE_NAME, state_dir=STATE_DIR, config=os.environ.get("AGAL_ONE_AGENT_CONFIG", CONFIG_PATH),
            python=python, launcher=launcher),
        os.path.join(SYSTEMD_DIR, f"{VERIFY_UNIT}.timer"): VERIFY_TIMER,
    }


def _ensure_verify_timer() -> Optional[str]:
    """Write the verify launcher and units where they are missing or differ, and enable the timer.
    None when the safety net is in place; else why not (the update then does not go ahead)."""
    changed_units = False
    try:
        for path, text in verify_units().items():
            try:
                with open(path) as f:
                    if f.read() == text:
                        continue
            except FileNotFoundError:
                pass
            _write_durable(path, text)
            changed_units = changed_units or path.startswith(SYSTEMD_DIR)
            logger.info("OTA: wrote %s", path)
    except OSError as e:
        return f"the rollback check could not be installed ({e}), so a bad version could not be undone: not updated"
    if changed_units:
        r = _systemctl("daemon-reload")
        if r.returncode != 0:
            return f"systemd did not reload the rollback check: {(r.stderr or r.stdout or '').strip()[:300]}"
    r = _systemctl("enable", "--now", f"{VERIFY_UNIT}.timer")
    if r.returncode != 0:
        return f"the rollback check could not be started: {(r.stderr or r.stdout or '').strip()[:300]}"
    return None


# ---- requirements of the new version -----------------------------------------

_EXTRA_RE = re.compile(r"\bextra\s*==")
_MISSING_RE = re.compile(r"^\S+ \S+ requires ([^\s,]+), which is not installed\.?$")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _canon(name: Optional[str]) -> str:
    return re.sub(r"[-_.]+", "-", name or "").lower()


def _requirement_problems() -> list:
    """What `pip check` says is wrong with agal-one-agent's own requirements."""
    r = _run_pip(["check"])
    if r.returncode == 0:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines()
            if line.strip().lower().replace("_", "-").startswith(DIST_NAME + " ")]


def _installed_libraries() -> dict:
    """{canonical name: version} of every distribution in the agent's site-packages."""
    dist = _find_dist()
    path = [str(dist.locate_file(""))] if dist is not None else ([SITE_PACKAGES] if SITE_PACKAGES else None)
    out = {}
    for d in (_metadata.distributions(path=path) if path else _metadata.distributions()):
        try:
            out[_canon(d.metadata["Name"])] = d.version
        except Exception:  # noqa: BLE001 — a broken dist-info is not a library pip would change
            continue
    return out


def _library_changes(reqs: list):
    """The libraries this board has that `pip install reqs` would change ("name old → new"), from
    pip's dry run; a str when pip could not say."""
    report = os.path.join(_ota_dir(), "pip-report.json")
    _remove(report)
    try:
        r = _run_pip(["install", "--dry-run", "--quiet", "--report", report, *reqs])
        if r.returncode != 0:
            return f"pip could not work out what the new version's libraries need: {_pip_error(r)[:300]}"
        try:
            with open(report) as f:
                items = (json.load(f) or {}).get("install") or []
        except (OSError, ValueError) as e:
            return f"pip did not say which libraries it would install ({e})"
        have = _installed_libraries()
        out = []
        for item in items:
            md = (item or {}).get("metadata") or {}
            name = md.get("name")
            if _canon(name) in have:
                out.append(f"{name} {have[_canon(name)]} → {md.get('version')}")
        return out
    finally:
        _remove(report)


def _ensure_requirements(on_step: Optional[Callable[[], None]] = None) -> Optional[str]:
    """None when every requirement of the installed agent is met. A library the new version added
    is installed from the package index, the way the join script installs the agent's libraries
    (the agent itself is installed --no-deps). A library the board HAS is never changed: the way
    back covers the agent's own files only, so an older version put back on top of an upgraded
    library might not start. A release that needs one upgraded is not installed (the libraries are
    still untouched when the update is undone)."""
    problems = _requirement_problems()
    if not problems:
        return None
    missing, other = set(), []
    for p in problems:
        m = _MISSING_RE.match(p)
        if m:
            missing.add(_canon(m.group(1)))
        else:
            other.append(p)
    if other:
        return ("the new version needs a library this board has at another version ("
                f"{'; '.join(other)[:300]}): not installed — the board's libraries are left as they are")
    dist = _find_dist()
    reqs = []
    for r in ((dist.requires or []) if dist is not None else []):
        m = _REQ_NAME_RE.match(r)
        if m and not _EXTRA_RE.search(r) and _canon(m.group(1)) in missing:
            reqs.append(r)
    if not reqs:
        return f"the new version needs {', '.join(sorted(missing))}, but names no requirement for it"
    if on_step is not None:
        on_step()
    changes = _library_changes(reqs)
    if isinstance(changes, str):
        return changes
    if changes:
        return (f"adding {', '.join(reqs)} would also change {', '.join(changes)} on this board, which the "
                "way back does not cover: not installed")
    logger.warning("OTA: the new version needs %s; installing %s", "; ".join(problems), ", ".join(reqs))
    if on_step is not None:
        on_step()
    r = _run_pip(["install", *reqs])
    if r.returncode != 0:
        return f"the new version's libraries could not be installed: {_pip_error(r)[:300]}"
    problems = _requirement_problems()
    return f"the new version's libraries are not met: {'; '.join(problems)[:300]}" if problems else None


# ---- the node's outputs --------------------------------------------------------

def _busy(outputs_on: Optional[Callable[[], list]]) -> list:
    if outputs_on is None:
        return []
    try:
        return [str(x) for x in (outputs_on() or [])]
    except Exception as e:  # noqa: BLE001 — not knowing is not "off"
        return [f"the outputs could not be read ({e})"]


def _wait_until_idle(outputs_on, max_wait_s: float, on_wait: Optional[Callable[[], None]] = None, *,
                     report: Optional[Callable[[], dict]] = None, on_change: Optional[Callable[[], None]] = None,
                     now_ms: Optional[Callable[[], float]] = None) -> list:
    """[] once nothing is ON; what is still ON when max_wait_s is over. Agent 0.4.4 (nodes/README.md §13): while it
    waits, its record says so — phase waiting_idle with what is on (``report``, the cards by name) — and a wait that
    runs out leaves phase busy with what stayed on (wait_report, the heartbeat's otaWait); ``on_change`` asks for a
    heartbeat at once when it begins, when what is on changes (at most every 5 s) and when it ends busy."""
    on = _busy(outputs_on)
    if not on:
        return []
    clock = now_ms or _wall_ms
    deadline = _monotonic() + max_wait_s
    started = float(clock())
    items, runs = _wait_items(report)
    base = {"since": _iso_ms(started)}
    _set_wait({"phase": "waiting_idle", "on": items, "runs": runs, **base,
               "until": _iso_ms(started + max_wait_s * 1000.0), "at": _iso_ms(started)}, on_change, force=True)
    logger.info("OTA: waiting up to %d s for %s to switch off before the update", int(max_wait_s), ", ".join(on))
    while on:
        left = deadline - _monotonic()
        if left <= 0:
            at = _iso_ms(float(clock()))
            _set_wait({"phase": "busy", "on": items, "runs": runs, **base, "endedAt": at, "at": at}, on_change, force=True)
            logger.warning("OTA: %s stayed on for the %d min the update waited — the update stops (the node says so)",
                           ", ".join(on), int(max_wait_s) // 60)
            return on
        if on_wait is not None:
            on_wait()
        _sleep(min(BUSY_POLL_S, left))
        on = _busy(outputs_on)
        if on:
            now_items, now_runs = _wait_items(report)
            changed = (now_items, now_runs) != (items, runs)
            items, runs = now_items, now_runs
            _set_wait({"phase": "waiting_idle", "on": items, "runs": runs, **base,
                       "until": _iso_ms(started + max_wait_s * 1000.0), "at": _iso_ms(float(clock()))},
                      on_change if changed else None)
    _set_wait(None, None)
    logger.info("OTA: the outputs are off; the update goes ahead")
    return []


def _iso_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def wait_report() -> Optional[dict]:
    """The heartbeat's `otaWait` block (contracts v1.26.0, functions.yaml 1.21.0 NodeStatusOtaWait), or None: while an
    update waits for the node's outputs to go off, {phase waiting_idle, on, runs, since, until, at}; after a wait that ran
    out, {phase busy, on — what stayed on, runs, since, endedAt, at} until the next update starts. From memory."""
    with _WAIT_LOCK:
        return copy.deepcopy(_WAIT) if _WAIT else None


def _wait_items(report: Optional[Callable[[], dict]]) -> tuple:
    """(on, runs) from ``report()`` — {"on": [{name, assetId?}], "runs": n} (main.outputs_report_for): each card once, a
    name trimmed and cut at 80, at most 16; runs 0 … 64. Not readable: nothing named, no run."""
    if report is None:
        return [], 0
    try:
        r = report() or {}
    except Exception as e:  # noqa: BLE001 — the report is words; the wait itself goes by outputs_on
        logger.debug("OTA: what is on could not be named: %s", e)
        return [], 0
    on: list = []
    seen: set = set()
    for x in r.get("on") or [] if isinstance(r, dict) else []:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()[:WAIT_MAX_NAME].strip()
        if not name:
            continue
        aid = x.get("assetId") if isinstance(x.get("assetId"), str) and x.get("assetId") else None
        key = ("asset", aid) if aid else ("name", name)
        if key in seen:
            continue
        seen.add(key)
        on.append({"name": name, **({"assetId": aid} if aid else {})})
        if len(on) >= WAIT_MAX_ON:
            break
    runs = r.get("runs") if isinstance(r, dict) else 0
    runs = runs if isinstance(runs, int) and not isinstance(runs, bool) else 0
    return on, max(0, min(WAIT_MAX_RUNS, runs))


def _set_wait(block: Optional[dict], on_change: Optional[Callable[[], None]], force: bool = False) -> None:
    """Replace the wait's record; ask for a heartbeat at once (``on_change``) when ``force`` or at most every 5 s."""
    global _WAIT, _WAIT_NUDGED
    with _WAIT_LOCK:
        _WAIT = block
        now = _monotonic()
        nudge = on_change is not None and (force or _WAIT_NUDGED is None or now - _WAIT_NUDGED >= WAIT_NUDGE_MIN_S)
        if nudge:
            _WAIT_NUDGED = now
    if nudge:
        try:
            on_change()
        except Exception as e:  # noqa: BLE001 — telling the cloud never stops an update
            logger.debug("OTA: the heartbeat could not be asked for: %s", e)


def _busy_error(on: list, waited_s: float) -> str:
    return (f"busy: {', '.join(on)[:300]} stayed on for the {int(waited_s) // 60} min the update waited — "
            "it restarts the node, which switches the outputs off; send it again when they are off")


# ---- the heartbeat (the new version, during probation) ----------------------------

def _write_heartbeat(beat: dict) -> None:
    try:
        _write_durable(_heartbeat_file(), json.dumps(beat), sync=False)  # stale after a power cut anyway
    except OSError as e:
        logger.debug("OTA: heartbeat not written: %s", e)


def read_heartbeat() -> Optional[dict]:
    try:
        with open(_heartbeat_file()) as f:
            beat = json.load(f)
        return beat if isinstance(beat, dict) else None
    except (OSError, ValueError):
        return None


def _clear_heartbeat() -> None:
    _remove(_heartbeat_file())


# ---- public state machine --------------------------------------------------

def perform_update(target_version: str, command_id: str = "", rollout_id: str = "",
                   node_uid: str = "", *, source: Optional[str] = None, sha256: Optional[str] = None,
                   outputs_on: Optional[Callable[[], list]] = None,
                   outputs_report: Optional[Callable[[], dict]] = None,
                   on_wait_change: Optional[Callable[[], None]] = None,
                   now_ms: Optional[Callable[[], float]] = None) -> str:
    """Install target_version and restart into probation. Returns a status string.

    ``source`` + ``sha256``: the release archive to install (no git needed). Without ``source``
    the tag is installed from git (an older backend). ``outputs_on`` names what the node has ON
    (main wires the program's outputs); the node is not restarted while it names anything.
    ``outputs_report`` names the same by card for the wait's report, ``on_wait_change`` asks for a
    heartbeat at once, ``now_ms`` is the node's trusted time for its dates (agent 0.4.4,
    nodes/README.md §13). On an install failure the saved previous version is put back, offline,
    and the update is marked failed (no restart)."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        logger.warning("OTA: an update is already in progress; %s (%s) ignored", target_version, command_id)
        return "an update is already in progress; this one is ignored"
    try:
        _set_wait(None, None)  # a new update begins: an earlier wait's busy report is over
        waits = {"report": outputs_report, "on_change": on_wait_change, "now_ms": now_ms}
        return _perform_update(target_version, command_id, rollout_id, node_uid,
                               (source or "").strip() or None, (sha256 or "").strip().lower() or None, outputs_on, waits)
    finally:
        _UPDATE_LOCK.release()


def _perform_update(target_version, command_id, rollout_id, node_uid, source, sha256, outputs_on, waits=None) -> str:
    waits = waits or {}
    target = _norm_tag(target_version)
    previous = installed_version()
    base = {"previous": previous, "target": target, "commandId": command_id,
            "rolloutId": rollout_id, "nodeUid": node_uid}

    def refuse(error: str) -> str:
        # Nothing was installed: the running version stays, the update is over.
        logger.error("OTA: update to %s not done: %s", target, error)
        _discard_downloads()
        write_state({**base, "phase": "failed", "error": error})
        _emit("failed", base, error=error)
        return f"Update to {target} not done: {error}"

    if not TAG_RE.match(target):
        return refuse(f"{str(target_version)[:40]!r} is not a release version")

    prior = read_state()
    if prior.get("phase") == "pending_verify":
        # The last update is still in probation or being undone: its state is what the verify-timer
        # acts on, so it is not overwritten (nothing is written or reported for this command; the
        # node's update status follows the update in progress).
        logger.warning("OTA: the update to %s is still being checked or undone; %s (%s) ignored",
                       prior.get("target"), target, command_id)
        return (f"Update to {target} not done: the update to {prior.get('target')} is still being checked "
                "or undone — send this one when it is over")

    if previous == target:
        logger.info("OTA: already on %s; no-op", target)
        write_state({"phase": "idle", "previous": target, "target": target,
                     "commandId": command_id, "rolloutId": rollout_id, "nodeUid": node_uid})
        _emit("succeeded", base)
        return f"Already on {target}"

    # Releases only move forward, and never to an agent whose own updater needs git (and so the
    # terminal) — whoever sends the command: the backend, a rollout, or anything holding the MQTT login.
    if _version_key(target) < _version_key(MIN_OTA_VERSION):
        return refuse(f"{target} is older than {MIN_OTA_VERSION}, the first agent that updates itself without "
                      "git: it is not installed (its next update would need the board's terminal)")
    running_key = _version_key(previous)
    if previous != "v0.0.0" and running_key is not None and _version_key(target) < running_key:
        return refuse(f"{target} is older than the running {previous}: an update only moves forward "
                      "(a newer release is how a bad one is undone)")

    if source:
        err = source_error(source, sha256, target)
        if err:
            return refuse(err)
    else:
        logger.warning("OTA: the update to %s carries no release archive (a backend older than agent 0.3.4): "
                       "installing from git, which needs git on the board", target)
        if not _have_git():
            return refuse("this board has no git, and the update carries no release archive (source + sha256) "
                          "to install instead — the backend has to send them")

    # A restart switches every output off: not while the node drives something.
    write_state({**base, "phase": "waiting"})
    on = _wait_until_idle(outputs_on, BUSY_WAIT_S, **waits)
    if on:
        return refuse(_busy_error(on, BUSY_WAIT_S))

    write_state({**base, "phase": "downloading" if source else "preparing"})
    _emit("started", base)
    archive = None
    try:
        _check_free_space()
        if source:
            archive = _fetch_archive(source, sha256, target)
        snapshot = _take_snapshot(previous)
        err = _ensure_verify_timer()
        if err:
            raise OtaError(err)
        _preload_package()  # this process runs the old version until the restart: all of it, from memory
    except OtaError as e:
        return refuse(str(e))
    except Exception as e:  # noqa: BLE001 — nothing is installed yet: say so, change nothing
        return refuse(f"the update could not be prepared: {e}")

    state = {**base, "phase": "pending_verify", "snapshot": snapshot,
             "via": "archive" if archive else "git",
             # the target's archive (recorded once it is confirmed) and the running version's, when
             # this board knows it: the way back over the network if the saved copy cannot be used
             "package": {"source": source, "sha256": sha256} if archive else None,
             "previousPackage": _known_package(previous),
             "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S}
    write_state(state)
    _sync_all()  # the saved copy and this state are on the SD card before pip changes the venv

    error = _install(state, archive, sha256, target)
    _discard_downloads()
    if error:
        return _undo(state, error)

    # Something switched on while pip ran: wait again, keeping the verify-timer's deadline ahead.
    def keep_deadline_ahead():
        write_state({**state, "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S})

    on = _wait_until_idle(outputs_on, BUSY_WAIT_S, on_wait=keep_deadline_ahead, **waits)
    if on:
        return _undo(state, _busy_error(on, BUSY_WAIT_S))

    _clear_heartbeat()  # whatever an older probation left is not the new version's
    write_state({**state, "deadlineEpoch": int(time.time()) + VERIFY_DEADLINE_S})
    _sync_all()  # the new version's files are on the card before the restart
    logger.info("OTA: %s installed; restarting into probation (deadline %ds)", target, VERIFY_DEADLINE_S)
    _restart_service()
    return f"Installing {target}; restarting into probation"


def _install(state: dict, archive: Optional[str], sha256: Optional[str], target: str) -> Optional[str]:
    """pip, then the new version's libraries, then its version: None when the target is in place."""
    def keep_deadline_ahead():
        # a library to install may take a while on a small board: the verify-timer stays out of it
        write_state({**state, "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S})

    try:
        result = _pip_install_archive(archive, sha256) if archive else _pip_install(target)
        if result.returncode != 0:
            return _pip_error(result)
        keep_deadline_ahead()
        error = _ensure_requirements(on_step=keep_deadline_ahead)
        if error:
            return error
        got = installed_version()
        return None if got == target else f"the release installed {got}, not {target}"
    except Exception as e:  # noqa: BLE001 — whatever it was, the saved version goes back
        return f"the install broke off: {e}"


def _undo(state: dict, error: str) -> str:
    """The install went wrong (or the node stayed busy): the saved previous version goes back,
    offline; no restart — this process still runs it. If it cannot be put back, the update stays
    pending_verify with its deadline passed, so the verify-timer keeps trying (with the network
    way back too) — no restart then either: this process runs the previous version from memory,
    and systemd starts it again from the restored files whenever it stops."""
    logger.error("OTA: update to %s failed: %s", state.get("target"), error)
    ok, how = _go_back(state)
    if not ok:
        logger.critical("OTA: putting %s back failed: %s", state.get("previous"), how)
        now = int(time.time())
        error = f"{error}; putting {state.get('previous')} back failed too: {how} — the rollback check tries again"
        write_state({**state, "deadlineEpoch": now, "rollbackAttempts": 1,
                     "rollbackRetryEpoch": now + ROLLBACK_RETRY_S[0], "rollbackError": how[:500],
                     "runningPrevious": True, "error": error[:1000]})
        _emit("failed", state, error=error[:1000])
        return f"Install of {state.get('target')} failed; {how}: {error}"
    failed = {k: v for k, v in state.items() if k != "deadlineEpoch"}
    write_state({**failed, "phase": "failed", "error": error[:1000]})
    _emit("failed", state, error=error[:1000])
    return f"Install of {state.get('target')} failed; {how}: {error}"


def settle_interrupted(node_uid: str = "") -> bool:
    """At boot, before MQTT connects: an update the agent was waiting on or downloading when it
    stopped did not install anything — it is reported failed, so the app does not show "updating"
    for ever. Never an update this process is running now."""
    if _UPDATE_LOCK.locked():
        return False
    state = read_state()
    if state.get("phase") not in ("waiting", "downloading", "preparing"):
        return False
    error = "interrupted: the agent restarted before the update was installed; nothing changed — send it again"
    _discard_downloads()
    st = {**state, "nodeUid": state.get("nodeUid") or node_uid}
    write_state({**st, "phase": "failed", "error": error})
    _emit("failed", st, error=error)
    logger.warning("OTA: %s", error)
    return True


def begin_probation(running_version: str) -> bool:
    """At boot, before MQTT connects: True when an update is in probation (main then starts a
    Probation once it is connected). When this process runs the update's target, the verify deadline
    is counted from now — PROBATION_DEADLINE_S — so the old version's shutdown and this boot do not
    use it up. Once per update: a version that crash-loops after this point is still rolled back."""
    state = read_state()
    if state.get("phase") != "pending_verify":
        return False
    if (_norm_tag(running_version) != state.get("target") or state.get("probationEpoch")
            or state.get("rollbackAttempts")):
        return True
    with _state_lock(wait_s=5.0) as held:
        if not held:  # the verify-timer is rolling back right now
            return True
        state = read_state()
        if state.get("phase") != "pending_verify":
            return False
        if state.get("probationEpoch") or state.get("rollbackAttempts"):
            return True
        now = int(time.time())
        write_state({**state, "probationEpoch": now,
                     "deadlineEpoch": max(int(state.get("deadlineEpoch") or 0), now + PROBATION_DEADLINE_S)})
    logger.info("OTA: %s booted in probation; the rollback check waits until %ds from now",
                state.get("target"), PROBATION_DEADLINE_S)
    return True


def confirm_update(node_uid: str = "", running: Optional[str] = None) -> bool:
    """Called by the new process (Probation) once it is healthy. Settles a pending_verify update to
    idle (new known-good) and reports success. ``running``: the version this process runs (default:
    the one installed). Returns True if a probation was confirmed, False if nothing to do."""
    with _state_lock(wait_s=5.0) as held:
        if not held:
            return False  # the verify-timer holds the state: asked again at the next check
        state = read_state()
        if state.get("phase") != "pending_verify":
            return False
        if state.get("rollbackAttempts"):
            return False  # a rollback is under way (it may have changed files): the verify-timer finishes it
        target = state.get("target")
        running = _norm_tag(running) if running else installed_version()
        if running not in ("v0.0.0", target):
            # The agent restarted while pip ran and the old version came back up: healthy, but not
            # the update — said so, instead of a "succeeded" for a version that is not running.
            error = f"the agent restarted during the install and runs {running}, not {target}"
            logger.warning("OTA: %s", error)
            write_state({"phase": "failed", "previous": running, "target": target, "error": error,
                         "commandId": state.get("commandId"), "rolloutId": state.get("rolloutId"),
                         "nodeUid": state.get("nodeUid") or node_uid})
            report = ("failed", {**state, "previous": running, "nodeUid": state.get("nodeUid") or node_uid}, error)
            confirmed = False
        else:
            logger.info("OTA: probation passed; confirming %s as known-good", target)
            write_state({
                "phase": "idle",
                "previous": target,            # the target is now the known-good baseline
                "target": target,
                "commandId": state.get("commandId"),
                "rolloutId": state.get("rolloutId"),
                "nodeUid": state.get("nodeUid") or node_uid,
            })
            _record_installed(target, state.get("package"))
            report = ("succeeded", state, None)
            confirmed = True
    _clear_heartbeat()
    _emit(report[0], report[1], error=report[2])
    return confirmed


class Probation:
    """The new version's probation, run by the new process itself (main.py) — not one check at one
    instant: from PROBATION_WINDOW_S after it starts, every PROBATION_POLL_S, it confirms at the first
    healthy check (MQTT connected and the main loop alive), for as long as the update is pending.
    ``tick()`` — called by the main loop — marks the loop alive and writes the heartbeat the
    verify-timer reads (version, connected, what is ON) every HEARTBEAT_EVERY_S."""

    def __init__(self, node_uid: str, running_version: str, is_connected: Callable[[], bool],
                 outputs_on: Optional[Callable[[], list]] = None):
        self.node_uid = node_uid
        self.version = _norm_tag(running_version)
        self._is_connected = is_connected
        self._outputs_on = outputs_on
        self._loop_at = _monotonic()
        self._beat_at: Optional[float] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _connected(self) -> bool:
        try:
            return bool(self._is_connected())
        except Exception:  # noqa: BLE001
            return False

    def tick(self) -> None:
        now = _monotonic()
        self._loop_at = now
        if self._done.is_set() or (self._beat_at is not None and now - self._beat_at < HEARTBEAT_EVERY_S):
            return
        self._beat_at = now
        _write_heartbeat({"epoch": int(time.time()), "version": self.version, "connected": self._connected(),
                          "outputsOn": _busy(self._outputs_on)[:10], "pid": os.getpid()})

    def healthy(self) -> bool:
        return self._connected() and _monotonic() - self._loop_at <= LOOP_STALE_S

    def check(self) -> bool:
        """One probation check: True once the probation is over (confirmed, failed or rolled back)."""
        if read_state().get("phase") != "pending_verify":
            return True
        if self.healthy():
            confirm_update(self.node_uid, running=self.version)
        return read_state().get("phase") != "pending_verify"

    def run(self) -> None:
        if self._done.wait(PROBATION_WINDOW_S):
            return
        while not self._done.is_set():
            try:
                if self.check():
                    break
            except Exception as e:  # noqa: BLE001 — a check that broke is tried again
                logger.warning("OTA: probation check failed: %s", e)
            self._done.wait(PROBATION_POLL_S)
        self._done.set()
        _clear_heartbeat()

    def start(self) -> "Probation":
        logger.info("OTA: in probation as %s; confirming at the first healthy check after %ds",
                    self.version, PROBATION_WINDOW_S)
        self._thread = threading.Thread(target=self.run, name="ota-probation", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()


def _rollback_hold(state: dict, now: int, deadline: int) -> Optional[str]:
    """Why the rollback waits although the deadline is past, or None. Only for a new version that is
    alive (its heartbeat is fresh and names the target): while it has an output ON — a rollback
    restarts the node, which would cut a running pump — up to BUSY_WAIT_S; while it is connected and
    about to confirm itself, up to CONFIRM_GRACE_S."""
    beat = read_heartbeat()
    target = state.get("target")
    if not beat or beat.get("version") != target:
        return None
    try:
        age = now - float(beat.get("epoch"))
    except (TypeError, ValueError):
        return None
    if age > HEARTBEAT_STALE_S or age < -HEARTBEAT_STALE_S:
        return None
    on = [str(x) for x in (beat.get("outputsOn") or [])]
    if on and now < deadline + BUSY_WAIT_S:
        return (f"holding the rollback: {target} has {', '.join(on)[:200]} on, and the restart would switch "
                f"it off (waits up to {(deadline + BUSY_WAIT_S - now) // 60} min more)")
    if beat.get("connected") and now < deadline + CONFIRM_GRACE_S:
        return f"holding the rollback: {target} is up and connected and confirms itself"
    return None


def run_verify() -> str:
    """Independent systemd-timer entry. Rolls back an unconfirmed update once it
    is past its deadline. No-op for any non-probation state."""
    with _state_lock() as held:
        if not held:
            return "busy (the agent is confirming its update); checked again next time"
        return _run_verify()


def _run_verify() -> str:
    state = read_state()
    if state.get("phase") != "pending_verify":
        return f"noop (phase={state.get('phase')})"

    now = int(time.time())
    deadline = int(state.get("deadlineEpoch", 0))
    if now < deadline:
        return f"in probation ({deadline - now}s left)"
    retry_at = int(state.get("rollbackRetryEpoch") or 0)
    if now < retry_at:
        return f"rollback tried again in {retry_at - now}s (last: {str(state.get('rollbackError'))[:200]})"
    hold = _rollback_hold(state, now, deadline)
    if hold:
        logger.info("OTA verify: %s", hold)
        return hold

    previous = state.get("previous")
    target = state.get("target")
    logger.warning("OTA verify: %s did not confirm by deadline; rolling back to %s",
                   target, previous)

    _preload_reporting()
    ok, how = _go_back(state)
    if not ok:
        # Never given up: a rollback that failed leaves the board for the terminal. The state stays
        # pending_verify and the next attempt comes after a backoff (the saved copy may be readable
        # again, the network back for the archive, space freed).
        attempts = int(state.get("rollbackAttempts") or 0) + 1
        delay = ROLLBACK_RETRY_S[min(attempts, len(ROLLBACK_RETRY_S)) - 1]
        logger.error("OTA verify: putting %s back failed (attempt %d, next in %ds): %s", previous, attempts, delay, how)
        write_state({**state, "rollbackAttempts": attempts, "rollbackRetryEpoch": now + delay,
                     "rollbackError": how[:500]})
        if attempts == 1 and not state.get("runningPrevious"):  # _undo already reported its failure
            _emit("failed", state, error=f"putting {previous} back failed: {how[:700]} — the board keeps trying")
        return f"rollback FAILED (attempt {attempts}, next in {delay}s): {how}"

    _record_installed(previous, state.get("previousPackage"))
    write_state({
        "phase": "rolled_back",
        "previous": previous,
        "target": target,
        "commandId": state.get("commandId"),
        "rolloutId": state.get("rolloutId"),
        "nodeUid": state.get("nodeUid"),
    })
    _clear_heartbeat()
    _emit("rolled_back", state)
    if state.get("runningPrevious"):
        # The update's own process failed to put the files back and still runs the previous version
        # from memory: the files now match it, so it is left running (a restart would cut its outputs).
        return f"rolled back {target} -> {previous} ({how}); the running agent is already {previous}"
    _restart_service()
    return f"rolled back {target} -> {previous} ({how})"


def main_verify() -> None:
    """Console entry for the systemd verify-timer (agal-one-agent-ota-verify)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [ota-verify] %(levelname)s: %(message)s",
    )
    try:
        outcome = run_verify()
        logger.info("OTA verify: %s", outcome)
    except Exception as e:  # noqa: BLE001 — the timer must never hard-fail
        logger.error("OTA verify crashed (non-fatal): %s", e)


if __name__ == "__main__":
    main_verify()
