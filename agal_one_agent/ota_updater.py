"""Managed OTA (Over-The-Air) self-update for the Agal One agent, with
health-gated rollback — and nothing to do on the board's terminal.

The hard problem: the process that triggers an update *dies* when it restarts
the service, so rollback cannot live in that process. This module solves it with
an on-disk state marker plus an INDEPENDENT systemd verify-timer.

Flow (see Agal/contracts/enums.yaml#OtaStatus + firmware-rollout.schema.json):

  1. perform_update(target, source=, sha256=, outputs_on=) — runs on its own thread
     (main.start_firmware_update), never in the MQTT callback:
     a. the command is checked: a release version, and — when it names an archive —
        an archive URL on the allow-list (ARCHIVE_SOURCE_RE: the GitHub release archive
        of logapradeep/agal-one-agent for exactly that tag) with a sha256;
     b. the node waits while anything it drives is ON (outputs_on): a restart cuts every
        output, so it is not restarted under a running pump. Up to BUSY_WAIT_S, then the
        update ends `failed` with an error that starts "busy:";
     c. firmware_update_started; the archive is downloaded over HTTPS into the state
        directory (size cap, timeouts, redirects only to GitHub over HTTPS) and its
        sha256 checked BEFORE anything is installed;
     d. a SNAPSHOT of the running version is taken (every file its pip RECORD lists:
        the agal_one_agent package, its dist-info and its console scripts), so the way
        back needs no network, no git and no archive of the old version;
     e. the verify-timer units are written and enabled if the board lacks them (the join
        script installs only the main service);
     f. ota_state.json → pending_verify; `pip install --no-deps --require-hashes -r` of
        the local archive — the flags the join script uses (backend enroll.ts
        installerScript); a requirement of the new version that is missing is installed
        from the package index the way the join script installs the agent's libraries;
        the installed version must be the target;
     g. if the install fails, the snapshot is put back and the update ends `failed` (no
        restart: the running process is still the old version). If an output came on
        meanwhile, the node waits again (bounded), then restarts through systemd.
  2. The NEW process boots in probation. main.py, on seeing phase=pending_verify,
     waits PROBATION_WINDOW_S of healthy uptime (config loaded + MQTT connected)
     then calls confirm_update() → phase=idle, emit firmware_update_succeeded.
  3. An INDEPENDENT systemd verify-timer runs run_verify() every ~60s. If a
     pending_verify update is past its deadline (the new version crash-looped,
     hung, or never connected), it puts the snapshot back, emits
     firmware_update_rolled_back, and restarts. It runs the rollback code of the
     version that took the snapshot (verify.py puts the snapshot first on the import
     path), so a new version that does not even import is still rolled back.

A command without `source` (a backend older than agent 0.3.4) installs from git
as before — that needs git on the board, and a board without it says so.

Firmware lifecycle events are reported over HTTP (http_reporter), NOT MQTT, so
they land even while the daemon is down / MQTT is mid-reconnect.
"""

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
from importlib import metadata as _metadata
from typing import Callable, Optional

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

STATE_DIR = os.environ.get("AGAL_ONE_AGENT_STATE_DIR", "/var/lib/agal-one-agent")
STATE_FILE = os.path.join(STATE_DIR, "ota_state.json")
# Where the installed agent is looked up (None: the interpreter's own path, the
# snapshot excluded). Tests point it at a fake venv.
SITE_PACKAGES: Optional[str] = None

# The new version must stay healthy this long (seconds) before self-confirming.
PROBATION_WINDOW_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_PROBATION_S", "60"))
# The verify-timer rolls back if still unconfirmed this long after the restart.
# MUST be > PROBATION_WINDOW_S so a healthy version confirms first.
VERIFY_DEADLINE_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_DEADLINE_S", "150"))
# While pip runs (and while the node waits to restart) the deadline is kept this far
# ahead, so the verify-timer never puts the snapshot back under a running install.
PIP_TIMEOUT_S = 300
INSTALL_GRACE_S = 2 * PIP_TIMEOUT_S + VERIFY_DEADLINE_S
# How long an update waits for the node's outputs to switch off, and how often it looks.
BUSY_WAIT_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_BUSY_WAIT_S", "1800"))
BUSY_POLL_S = 10.0

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

_UPDATE_LOCK = threading.Lock()


class OtaError(Exception):
    """An update step that could not be done; its message is the event's error."""


# ---- version helpers -------------------------------------------------------

def _norm_tag(version: str) -> str:
    """Normalize to a GitHub release tag form (vX.Y.Z)."""
    v = (version or "").strip()
    return v if v.startswith("v") else f"v{v}"


def _ota_dir() -> str:
    return os.path.join(STATE_DIR, "ota")


def _snapshot_dir() -> str:
    return os.path.join(_ota_dir(), "previous")


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


# ---- state file (atomic) ---------------------------------------------------

def read_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f) or {"phase": "idle"}
    except FileNotFoundError:
        return {"phase": "idle"}
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: could not read state (%s); treating as idle", e)
        return {"phase": "idle"}


def write_state(state: dict) -> None:
    state["updatedEpoch"] = int(time.time())
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # atomic on POSIX
    except Exception as e:  # noqa: BLE001
        logger.error("OTA: failed to persist state: %s", e)


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
    force-reinstalling so the exact ref lands. Deps come along (not --no-deps), as before.
    Needs git on the board."""
    url = f"git+{GITHUB_REPO}@{tag}"
    logger.info("OTA: pip install %s", url)
    return _run_pip(["install", "--upgrade", "--force-reinstall", url])


def _pip_install_archive(archive: str, sha256: str) -> "subprocess.CompletedProcess":
    """The downloaded, already-checked archive, installed the way the join script installs the
    agent: one requirement line with its hash, --no-deps --require-hashes (pip checks the hash a
    second time)."""
    req = os.path.join(os.path.dirname(archive), "agent.requirements")
    with open(req, "w") as f:
        f.write(f"{DIST_NAME} @ {pathlib.Path(archive).resolve().as_uri()} --hash=sha256:{sha256}\n")
    logger.info("OTA: pip install --no-deps --require-hashes %s", archive)
    return _run_pip(["install", "--no-deps", "--require-hashes", "-r", req])


def _have_git() -> bool:
    return shutil.which("git") is not None


def _restart_service() -> None:
    logger.info("OTA: restarting %s", SERVICE_NAME)
    subprocess.Popen(
        _systemctl_cmd("restart", SERVICE_NAME),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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
    into the state directory, with each file's sha256. Dependencies are not part of it: the
    archive path installs with --no-deps, as the join script does."""
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
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(final):
            os.rename(final, old)
        os.rename(tmp, final)
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
    files copied back. Dependencies stay as they are."""
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


# ---- the verify-timer ---------------------------------------------------------

VERIFY_LAUNCHER = '''\
# Written by agal-one-agent (ota_updater._ensure_verify_timer): the managed-OTA
# rollback check, run every minute by {unit}.timer.
# It runs the rollback code of the version saved before the last update (it is
# first on the import path), so a new version that does not install or import
# cleanly is still rolled back. With nothing saved, the installed agent's runs.
import json
import os
import sys

SNAPSHOT = {snapshot!r}
try:
    with open(os.path.join(SNAPSHOT, "manifest.json")) as f:
        site = os.path.join(SNAPSHOT, "tree", json.load(f)["sitePackages"])
    if os.path.isfile(os.path.join(site, {package!r}, "ota_updater.py")):
        sys.path.insert(0, site)
except Exception:
    pass

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
        launcher: VERIFY_LAUNCHER.format(unit=VERIFY_UNIT, snapshot=_snapshot_dir(), package=PACKAGE),
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
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
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


def _requirement_problems() -> list:
    """What `pip check` says is wrong with agal-one-agent's own requirements."""
    r = _run_pip(["check"])
    if r.returncode == 0:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines()
            if line.strip().lower().replace("_", "-").startswith(DIST_NAME + " ")]


def _ensure_requirements() -> Optional[str]:
    """None when every requirement of the installed agent is met. A release that added a library
    gets it from the package index, the way the join script installs the agent's libraries
    (the agent itself is installed --no-deps)."""
    problems = _requirement_problems()
    if not problems:
        return None
    dist = _find_dist()
    reqs = [r for r in ((dist.requires or []) if dist is not None else []) if not _EXTRA_RE.search(r)]
    logger.warning("OTA: the new version needs %s; installing %s", "; ".join(problems), ", ".join(reqs))
    r = _run_pip(["install", *reqs]) if reqs else None
    if r is not None and r.returncode != 0:
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


def _wait_until_idle(outputs_on, max_wait_s: float, on_wait: Optional[Callable[[], None]] = None) -> list:
    """[] once nothing is ON; what is still ON when max_wait_s is over."""
    on = _busy(outputs_on)
    if not on:
        return []
    deadline = _monotonic() + max_wait_s
    logger.info("OTA: waiting up to %d s for %s to switch off before the update", int(max_wait_s), ", ".join(on))
    while on:
        left = deadline - _monotonic()
        if left <= 0:
            return on
        if on_wait is not None:
            on_wait()
        _sleep(min(BUSY_POLL_S, left))
        on = _busy(outputs_on)
    logger.info("OTA: the outputs are off; the update goes ahead")
    return []


def _busy_error(on: list, waited_s: float) -> str:
    return (f"busy: {', '.join(on)[:300]} stayed on for the {int(waited_s) // 60} min the update waited — "
            "it restarts the node, which switches the outputs off; send it again when they are off")


# ---- public state machine --------------------------------------------------

def perform_update(target_version: str, command_id: str = "", rollout_id: str = "",
                   node_uid: str = "", *, source: Optional[str] = None, sha256: Optional[str] = None,
                   outputs_on: Optional[Callable[[], list]] = None) -> str:
    """Install target_version and restart into probation. Returns a status string.

    ``source`` + ``sha256``: the release archive to install (no git needed). Without ``source``
    the tag is installed from git (an older backend). ``outputs_on`` names what the node has ON
    (main wires the program's outputs); the node is not restarted while it names anything.
    On an install failure the saved previous version is put back, offline, and the update is
    marked failed (no restart)."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        logger.warning("OTA: an update is already in progress; %s (%s) ignored", target_version, command_id)
        return "an update is already in progress; this one is ignored"
    try:
        return _perform_update(target_version, command_id, rollout_id, node_uid,
                               (source or "").strip() or None, (sha256 or "").strip().lower() or None, outputs_on)
    finally:
        _UPDATE_LOCK.release()


def _perform_update(target_version, command_id, rollout_id, node_uid, source, sha256, outputs_on) -> str:
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

    if previous == target:
        logger.info("OTA: already on %s; no-op", target)
        write_state({"phase": "idle", "previous": target, "target": target,
                     "commandId": command_id, "rolloutId": rollout_id, "nodeUid": node_uid})
        _emit("succeeded", base)
        return f"Already on {target}"

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
    on = _wait_until_idle(outputs_on, BUSY_WAIT_S)
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
    except OtaError as e:
        return refuse(str(e))
    except Exception as e:  # noqa: BLE001 — nothing is installed yet: say so, change nothing
        return refuse(f"the update could not be prepared: {e}")

    state = {**base, "phase": "pending_verify", "snapshot": snapshot,
             "via": "archive" if archive else "git",
             "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S}
    write_state(state)

    error = _install(state, archive, sha256, target)
    _discard_downloads()
    if error:
        return _undo(state, error)

    # Something switched on while pip ran: wait again, keeping the verify-timer's deadline ahead.
    def keep_deadline_ahead():
        write_state({**state, "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S})

    on = _wait_until_idle(outputs_on, BUSY_WAIT_S, on_wait=keep_deadline_ahead)
    if on:
        return _undo(state, _busy_error(on, BUSY_WAIT_S))

    write_state({**state, "deadlineEpoch": int(time.time()) + VERIFY_DEADLINE_S})
    logger.info("OTA: %s installed; restarting into probation (deadline %ds)", target, VERIFY_DEADLINE_S)
    _restart_service()
    return f"Installing {target}; restarting into probation"


def _install(state: dict, archive: Optional[str], sha256: Optional[str], target: str) -> Optional[str]:
    """pip, then the new version's libraries, then its version: None when the target is in place."""
    try:
        result = _pip_install_archive(archive, sha256) if archive else _pip_install(target)
        if result.returncode != 0:
            return _pip_error(result)
        # a library to install may take a while on a small board: the verify-timer stays out of it
        write_state({**state, "deadlineEpoch": int(time.time()) + INSTALL_GRACE_S})
        error = _ensure_requirements()
        if error:
            return error
        got = installed_version()
        return None if got == target else f"the release installed {got}, not {target}"
    except Exception as e:  # noqa: BLE001 — whatever it was, the saved version goes back
        return f"the install broke off: {e}"


def _undo(state: dict, error: str) -> str:
    """The install went wrong (or the node stayed busy): the saved previous version goes back,
    offline; no restart — this process still runs it."""
    logger.error("OTA: update to %s failed: %s", state.get("target"), error)
    ok, how = _restore_previous(state)
    if not ok:
        logger.critical("OTA: putting %s back failed: %s", state.get("previous"), how)
        error = f"{error}; putting {state.get('previous')} back failed too: {how}"
    failed = {k: v for k, v in state.items() if k != "deadlineEpoch"}
    write_state({**failed, "phase": "failed", "error": error[:1000]})
    _emit("failed", state, error=error[:1000])
    return f"Install of {state.get('target')} failed; {how}: {error}"


def settle_interrupted(node_uid: str = "") -> bool:
    """At boot: an update the agent was waiting on or downloading when it stopped did not install
    anything — it is reported failed, so the app does not show "updating" for ever."""
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


def confirm_update(node_uid: str = "") -> bool:
    """Called by the daemon after PROBATION_WINDOW_S of healthy uptime. Settles a
    pending_verify update to idle (new known-good) and reports success.
    Returns True if a probation was confirmed, False if nothing to do."""
    state = read_state()
    if state.get("phase") != "pending_verify":
        return False
    target = state.get("target")
    running = installed_version()
    if running not in ("v0.0.0", target):
        # The agent restarted while pip ran and the old version came back up: healthy, but not
        # the update — said so, instead of a "succeeded" for a version that is not running.
        error = f"the agent restarted during the install and runs {running}, not {target}"
        logger.warning("OTA: %s", error)
        write_state({"phase": "failed", "previous": running, "target": target, "error": error,
                     "commandId": state.get("commandId"), "rolloutId": state.get("rolloutId"),
                     "nodeUid": state.get("nodeUid") or node_uid})
        _emit("failed", {**state, "previous": running, "nodeUid": state.get("nodeUid") or node_uid}, error=error)
        return False
    logger.info("OTA: probation passed; confirming %s as known-good", target)
    write_state({
        "phase": "idle",
        "previous": target,            # the target is now the known-good baseline
        "target": target,
        "commandId": state.get("commandId"),
        "rolloutId": state.get("rolloutId"),
        "nodeUid": state.get("nodeUid") or node_uid,
    })
    _emit("succeeded", state)
    return True


def run_verify() -> str:
    """Independent systemd-timer entry. Rolls back an unconfirmed update once it
    is past its deadline. No-op for any non-probation state."""
    state = read_state()
    if state.get("phase") != "pending_verify":
        return f"noop (phase={state.get('phase')})"

    now = int(time.time())
    deadline = int(state.get("deadlineEpoch", 0))
    if now < deadline:
        return f"in probation ({deadline - now}s left)"

    previous = state.get("previous")
    target = state.get("target")
    logger.warning("OTA verify: %s did not confirm by deadline; rolling back to %s",
                   target, previous)

    _preload_reporting()
    ok, how = _restore_previous(state)
    rolled = {
        "phase": "rolled_back",
        "previous": previous,
        "target": target,
        "commandId": state.get("commandId"),
        "rolloutId": state.get("rolloutId"),
        "nodeUid": state.get("nodeUid"),
    }
    if not ok:
        logger.error("OTA verify: putting %s back failed: %s", previous, how)
        rolled["phase"] = "failed"
        rolled["error"] = how
        write_state(rolled)
        _emit("failed", state, error=how)
        _restart_service()
        return f"rollback FAILED: {how}"

    write_state(rolled)
    _emit("rolled_back", state)
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
