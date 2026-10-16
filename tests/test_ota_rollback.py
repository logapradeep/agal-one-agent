"""No-hardware, no-network tests for the managed OTA (ota_updater): the release archive path,
the offline way back, the busy wait, the git fallback, and the rollback state machine.

pip, the download, systemctl, the clock and the HTTP event report are fakes. The venv is a
real directory tree in tmp_path — what pip leaves for agal-one-agent (package, dist-info with a
RECORD, console script) — so the snapshot and the restore run for real against it.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse

import pytest

from agal_one_agent import ota_updater
from agal_one_agent.ota_updater import OtaError

PY = "python3.13"
ARCHIVE = "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/{tag}.tar.gz"

OTA_STUB = '''\
# the ota_updater of {version} (a stand-in)
def main_verify():
    import os
    with open(os.environ["OTA_MARKER"], "w") as f:
        f.write("{version}")
'''


class FakeVenv:
    """A venv as pip leaves it: site-packages with the agent and a dependency, bin/ with scripts."""

    def __init__(self, root):
        self.prefix = root / "venv"
        self.site = self.prefix / "lib" / PY / "site-packages"
        self.bin = self.prefix / "bin"
        self.site.mkdir(parents=True)
        self.bin.mkdir()
        (self.site / "paho").mkdir()
        (self.site / "paho" / "__init__.py").write_text("# a dependency: never touched\n")
        (self.site / "paho_mqtt-2.1.0.dist-info").mkdir()
        (self.site / "paho_mqtt-2.1.0.dist-info" / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: paho-mqtt\nVersion: 2.1.0\n")
        (self.bin / "pip").write_text("#!pip\n")

    def uninstall(self):
        for d in self.site.glob("agal_one_agent-*.dist-info"):
            for line in (d / "RECORD").read_text().splitlines():
                p = os.path.normpath(os.path.join(self.site, line.split(",")[0]))
                if os.path.isfile(p):
                    os.remove(p)
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self.site / "agal_one_agent", ignore_errors=True)

    def install(self, version, *, requires=(), half=False):
        """pip install of agal-one-agent <version>; half=True: pip died after removing the old one."""
        self.uninstall()
        dist = f"agal_one_agent-{version}.dist-info"
        files = {
            "agal_one_agent/__init__.py": f"VERSION = {version!r}\n",
            "agal_one_agent/main.py": f"# main of {version}\n",
            "agal_one_agent/ota_updater.py": OTA_STUB.format(version=version),
            "agal_one_agent/__pycache__/main.cpython-313.pyc": f"pyc of {version}",
            f"{dist}/METADATA": ("Metadata-Version: 2.1\nName: agal-one-agent\n"
                                 f"Version: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)),
            f"{dist}/INSTALLER": "pip\n",
            "../../../bin/agal-one-agent": f"#!python\n# console script of {version}\n",
        }
        if half:
            files = {"agal_one_agent/__init__.py": "# half written"}
        for rel, text in files.items():
            p = os.path.normpath(os.path.join(self.site, rel))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        if not half:
            record = "".join(f"{rel},,\n" for rel in files) + f"{dist}/RECORD,,\n"
            (self.site / dist / "RECORD").write_text(record)

    def tree(self) -> dict:
        out = {}
        for dirpath, _, names in os.walk(self.prefix):
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p) as f:
                    out[os.path.relpath(p, self.prefix)] = f.read()
        return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release(tag: str) -> bytes:
    return f"agal-one-agent release {tag}".encode()


@pytest.fixture
def ota(tmp_path, monkeypatch):
    """ota_updater against a fake venv on agent 0.3.3, with every outside effect faked.

    h.archives: url → bytes the fake download returns (anything else is "not reached").
    h.pip_fail: versions whose install fails (half-way: the old version is gone).
    h.git: whether the board has git. h.now: the fake monotonic clock (sleep advances it).
    h.dry_report: what pip's dry run says it would install (default: each requirement, new).
    h.runs: what the "does the restored agent start" check answers, one per call (default: it does).
    h.order: pip installs and syncs to the SD card, in the order they happened."""
    venv = FakeVenv(tmp_path)
    venv.install("0.3.3")
    state = tmp_path / "state"
    monkeypatch.setattr(ota_updater, "STATE_DIR", str(state))
    monkeypatch.setattr(ota_updater, "STATE_FILE", str(state / "ota_state.json"))
    monkeypatch.setattr(ota_updater, "SITE_PACKAGES", str(venv.site))
    monkeypatch.setattr(ota_updater, "VENV_PREFIX", str(venv.prefix))
    monkeypatch.setattr(ota_updater, "SYSTEMD_DIR", str(tmp_path / "systemd"))
    monkeypatch.setattr(ota_updater, "BUSY_WAIT_S", 600)
    monkeypatch.setattr(ota_updater, "JOIN_REQUIREMENTS", str(tmp_path / "opt" / "agent.requirements"))

    h = types.SimpleNamespace(
        venv=venv, archives={}, archive_versions={}, pip_fail=set(), pip=[], downloads=[], git=True,
        git_asked=0, systemctl=[], systemctl_rc=0, restarts=0, emits=[], now=0.0, sleeps=[],
        check_out=[], deps_installed=[], dry_runs=[], dry_report=None, runs=[], runs_asked=[], order=[],
    )

    def add_release(tag, data=None):
        data = data or _release(tag)
        h.archives[ARCHIVE.format(tag=tag)] = data
        h.archive_versions[data] = tag[1:]
        return ARCHIVE.format(tag=tag), _sha(data)

    h.add_release = add_release

    def fake_download(url, dest):
        h.downloads.append(url)
        if url not in h.archives:
            raise OtaError("the archive could not be downloaded: not reached")
        with open(dest, "wb") as f:
            f.write(h.archives[url])

    def pip_result(version):
        if version in h.pip_fail:
            venv.install(version, half=True)
            return subprocess.CompletedProcess([], 1, "", "ERROR: boom")
        venv.install(version, requires=getattr(h, "requires", ()))
        return subprocess.CompletedProcess([], 0, "Successfully installed", "")

    def fake_run_pip(args):
        h.pip.append(list(args))
        if "--dry-run" in args:
            reqs = [a for a in args[args.index("--report") + 2:]]
            h.dry_runs.append(reqs)
            report = h.dry_report if h.dry_report is not None else {
                "install": [{"metadata": {"name": r.split(">")[0].split("=")[0], "version": "1.0"}} for r in reqs]}
            with open(args[args.index("--report") + 1], "w") as f:
                json.dump(report, f)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "install":
            h.order.append("pip install")
        if args[0] == "check":
            out = h.check_out.pop(0) if h.check_out else ""
            return subprocess.CompletedProcess(args, 1 if out else 0, out, "")
        if "-r" in args:
            line = open(args[args.index("-r") + 1]).read().strip()
            uri = line.split(" @ ", 1)[1].split(" --hash=", 1)[0]
            data = open(urllib.parse.unquote(urllib.parse.urlsplit(uri).path), "rb").read()
            assert line.endswith(f"--hash=sha256:{_sha(data)}")
            return pip_result(h.archive_versions[data])
        if args[-1].startswith("git+"):
            return pip_result(args[-1].rsplit("@", 1)[1][1:])
        h.deps_installed.append(list(args[1:]))
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_have_git():
        h.git_asked += 1
        return h.git

    def fake_systemctl(*args):
        h.systemctl.append(args)
        return subprocess.CompletedProcess(args, h.systemctl_rc, "", "" if h.systemctl_rc == 0 else "Failed")

    def fake_restart():
        h.restarts += 1

    def fake_emit(phase, st, *, error=None):
        h.emits.append((phase, error, dict(st)))

    def fake_sleep(s):
        h.sleeps.append(s)
        h.now += s

    monkeypatch.setattr(ota_updater, "_download", fake_download)
    monkeypatch.setattr(ota_updater, "_run_pip", fake_run_pip)
    monkeypatch.setattr(ota_updater, "_have_git", fake_have_git)
    monkeypatch.setattr(ota_updater, "_systemctl", fake_systemctl)
    monkeypatch.setattr(ota_updater, "_restart_service", fake_restart)
    monkeypatch.setattr(ota_updater, "_emit", fake_emit)
    def fake_check_runs(version):
        h.runs_asked.append(version)
        return h.runs.pop(0) if h.runs else None

    monkeypatch.setattr(ota_updater, "_sleep", fake_sleep)
    monkeypatch.setattr(ota_updater, "_monotonic", lambda: h.now)
    monkeypatch.setattr(ota_updater, "_check_runs", fake_check_runs)
    monkeypatch.setattr(ota_updater, "_sync_all", lambda: h.order.append("sync"))
    monkeypatch.setattr(ota_updater, "_preload_package", lambda: h.order.append("preload"))
    return h


def _phases(h):
    return [p for p, _, _ in h.emits]


def _installs(h):
    return [a for a in h.pip if a[0] == "install"]


def _update(h, tag="v0.3.4", **kw):
    url, sha = h.add_release(tag)
    kw.setdefault("source", url)
    kw.setdefault("sha256", sha)
    return ota_updater.perform_update(tag, command_id="cmd1", rollout_id="r1", node_uid="node-x", **kw)


# ---- the release archive -------------------------------------------------------

def test_good_archive_is_downloaded_checked_installed_and_restarted(ota):
    before = time.time()
    out = _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify"
    assert (st["previous"], st["target"], st["via"]) == ("v0.3.3", "v0.3.4", "archive")
    assert (st["commandId"], st["rolloutId"], st["nodeUid"]) == ("cmd1", "r1", "node-x")
    assert before + ota_updater.VERIFY_DEADLINE_S - 2 <= st["deadlineEpoch"] <= time.time() + ota_updater.VERIFY_DEADLINE_S + 2
    assert ota.downloads == [ARCHIVE.format(tag="v0.3.4")]
    # the join script's flags: one hashed requirement line, --no-deps --require-hashes
    assert _installs(ota)[0][:4] == ["install", "--no-deps", "--require-hashes", "-r"]
    assert ota_updater.installed_version() == "v0.3.4"
    assert ota.restarts == 1
    assert _phases(ota) == ["started"]
    assert "restarting into probation" in out
    # the archive does not stay on the SD card; the saved 0.3.3 does
    assert os.listdir(os.path.join(ota_updater.STATE_DIR, "ota", "download")) == []
    assert st["snapshot"]["version"] == "v0.3.3"


def test_hash_mismatch_is_refused_before_anything_is_installed(ota):
    before = ota.venv.tree()
    url, _ = ota.add_release("v0.3.4")
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256="0" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "sha256" in st["error"] and "not installed" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before
    assert os.listdir(os.path.join(ota_updater.STATE_DIR, "ota", "download")) == []


@pytest.mark.parametrize("source", [
    "http://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com/someone-else/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://evil.example/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com.evil.example/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.zip",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz?x=1",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/heads/main.tar.gz",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.2.tar.gz",  # not the target
    "file:///tmp/agal-one-agent-v0.3.4.tar.gz",
], ids=["http", "other-owner", "other-host", "lookalike-host", "zip", "query", "branch", "other-tag", "file"])
def test_source_not_on_the_allow_list_is_refused(ota, source):
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=source, sha256="a" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert ota.downloads == [] and ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]
    assert ota_updater.installed_version() == "v0.3.3"


@pytest.mark.parametrize("sha", [None, "", "abc", "g" * 64])
def test_archive_without_a_usable_sha256_is_refused(ota, sha):
    url, _ = ota.add_release("v0.3.4")
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256=sha)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "sha256" in st["error"]
    assert ota.downloads == [] and ota.pip == []


def test_a_version_that_is_not_a_release_is_refused(ota):
    ota_updater.perform_update("main; rm -rf /", node_uid="node-x")
    assert ota_updater.read_state()["phase"] == "failed"
    assert ota.pip == [] and ota.git_asked == 0


def test_download_failure_is_failed_and_changes_nothing(ota):
    before = ota.venv.tree()
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=ARCHIVE.format(tag="v0.3.4"), sha256="a" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "not reached" in st["error"]
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before and ota.restarts == 0


# ---- the way back, offline -----------------------------------------------------

def test_pip_failure_puts_the_previous_version_back_offline(ota):
    before = ota.venv.tree()
    ota.pip_fail = {"0.3.4"}
    out = _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "boom" in st["error"]
    assert ota.venv.tree() == before, "0.3.3 is back, file for file — the half install is gone"
    assert ota_updater.installed_version() == "v0.3.3"
    assert len(_installs(ota)) == 1 and len(ota.downloads) == 1, "the way back needed no pip and no download"
    assert ota.restarts == 0          # never restart into a half-broken state mid-run
    assert _phases(ota) == ["started", "failed"]
    assert "put v0.3.3 back" in out


def test_verify_timer_rollback_puts_the_previous_version_back_offline(ota):
    before = ota.venv.tree()
    _update(ota)
    assert ota_updater.installed_version() == "v0.3.4"
    # the new version never confirmed; the board is offline now and has no git
    ota.archives.clear()
    ota.git = False
    st = ota_updater.read_state()
    st["deadlineEpoch"] = int(time.time()) - 1
    ota_updater.write_state(st)
    pip_before, downloads_before = len(ota.pip), len(ota.downloads)

    out = ota_updater.run_verify()

    assert ota_updater.read_state()["phase"] == "rolled_back"
    assert ota.venv.tree() == before
    assert len(ota.pip) == pip_before and len(ota.downloads) == downloads_before
    assert ota.restarts == 2           # into probation, then back
    assert _phases(ota)[-1] == "rolled_back"
    assert "rolled back v0.3.4 -> v0.3.3" in out


def test_a_damaged_saved_copy_is_not_put_back(ota):
    _update(ota)
    now = ota.venv.tree()
    snap = os.path.join(ota_updater.STATE_DIR, "ota", "previous", "tree", "lib", PY, "site-packages",
                        "agal_one_agent", "main.py")
    with open(snap, "w") as f:
        f.write("# damaged on the SD card")
    st = ota_updater.read_state()
    st["deadlineEpoch"] = 0
    ota_updater.write_state(st)
    out = ota_updater.run_verify()
    assert "FAILED" in out and "damaged" in out
    assert ota.venv.tree() == now, "nothing was taken out when the way back was not whole"
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify", "a failed rollback is tried again, never given up"
    assert st["rollbackAttempts"] == 1 and st["rollbackRetryEpoch"] > time.time()
    assert ota.restarts == 1, "no restart: nothing was put back"
    assert _phases(ota)[-1] == "failed" and "keeps trying" in ota.emits[-1][1]
    # the next check, before the backoff is over, does nothing
    assert "tried again in" in ota_updater.run_verify()
    assert ota_updater.read_state()["rollbackAttempts"] == 1


def test_an_install_without_a_record_is_not_updated(ota):
    for rec in ota.venv.site.glob("agal_one_agent-*.dist-info/RECORD"):
        rec.unlink()
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "no way back" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0


def test_a_release_that_installs_another_version_is_undone(ota):
    url, sha = ota.add_release("v0.3.4", data=b"mislabelled")
    ota.archive_versions[b"mislabelled"] = "0.3.5"
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256=sha)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "installed v0.3.5, not v0.3.4" in st["error"]
    assert ota_updater.installed_version() == "v0.3.3" and ota.restarts == 0


def test_a_library_the_new_version_added_is_installed(ota):
    ota.requires = ("paho-mqtt>=2.0.0", "newlib>=1.2", 'RPi.GPIO>=0.7.1; extra == "rpi"')
    ota.check_out = ["agal-one-agent 0.3.4 requires newlib, which is not installed.", ""]
    _update(ota)
    assert ota.dry_runs == [["newlib>=1.2"]], "pip is asked first what installing it would change"
    assert ota.deps_installed == [["newlib>=1.2"]], "only the missing library: not the met ones, not the extras"
    assert ota_updater.read_state()["phase"] == "pending_verify" and ota.restarts == 1


def test_a_library_that_cannot_be_met_undoes_the_update(ota):
    ota.requires = ("newlib>=1.2",)
    ota.check_out = ["agal-one-agent 0.3.4 requires newlib, which is not installed."] * 2
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "newlib" in st["error"]
    assert ota_updater.installed_version() == "v0.3.3" and ota.restarts == 0


def test_the_verify_launcher_runs_the_saved_versions_code(ota, tmp_path):
    _update(ota)
    launcher = os.path.join(ota_updater.STATE_DIR, "ota", "verify.py")
    service = open(os.path.join(ota_updater.SYSTEMD_DIR, "agal-one-agent-ota-verify.service")).read()
    assert f"ExecStart={ota.venv.prefix}/bin/python -I {launcher}" in service
    assert f"Environment=AGAL_ONE_AGENT_STATE_DIR={ota_updater.STATE_DIR}" in service
    marker = tmp_path / "marker"
    r = subprocess.run([sys.executable, "-I", launcher], env={**os.environ, "OTA_MARKER": str(marker)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "0.3.3", "the rollback check is the saved 0.3.3's, not the new install's"


# ---- the verify-timer is in place before an update ------------------------------

def test_the_verify_timer_is_installed_when_the_board_lacks_it(ota):
    _update(ota)
    units = sorted(os.listdir(ota_updater.SYSTEMD_DIR))
    assert units == ["agal-one-agent-ota-verify.service", "agal-one-agent-ota-verify.timer"]
    assert ("daemon-reload",) in ota.systemctl
    assert ("enable", "--now", "agal-one-agent-ota-verify.timer") in ota.systemctl
    # the next update finds them in place: no reload, the timer only (re)enabled
    ota.systemctl.clear()
    ota_updater.write_state({"phase": "idle"})
    _update(ota, tag="v0.3.5")
    assert ota.systemctl == [("enable", "--now", "agal-one-agent-ota-verify.timer")]


def test_no_update_when_the_verify_timer_cannot_be_started(ota):
    ota.systemctl_rc = 1
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "rollback check" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0
    assert ota_updater.installed_version() == "v0.3.3"


# ---- not while a motor runs ------------------------------------------------------

def test_a_busy_node_waits_for_its_outputs_then_updates(ota):
    calls = {"n": 0}

    def outputs_on():
        calls["n"] += 1
        return ["Pump (device.power)"] if calls["n"] <= 3 else []

    def download_after_wait(url, dest, _real=ota_updater._download):
        assert calls["n"] >= 4, "nothing is downloaded while the pump runs"
        return _real(url, dest)

    ota_updater._download = download_after_wait  # restored by monkeypatch (set in the fixture)
    _update(ota, outputs_on=outputs_on)
    assert ota.sleeps == [ota_updater.BUSY_POLL_S] * 3
    assert ota.restarts == 1 and _phases(ota) == ["started"]


def test_a_node_that_stays_busy_is_failed_busy_and_left_alone(ota):
    before = ota.venv.tree()
    _update(ota, outputs_on=lambda: ["Pump (device.power)", "the run of plot plot-1"])
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert st["error"].startswith("busy: Pump (device.power), the run of plot plot-1")
    assert sum(ota.sleeps) == pytest.approx(ota_updater.BUSY_WAIT_S)
    assert ota.downloads == [] and ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]
    assert ota.venv.tree() == before


def test_outputs_that_cannot_be_read_count_as_busy(ota):
    def broken():
        raise RuntimeError("runtime lock")
    _update(ota, outputs_on=broken)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "could not be read" in st["error"]
    assert ota.downloads == []


def test_an_output_that_came_on_during_the_install_delays_the_restart(ota):
    seq = iter([[], ["Valve 1 (device.power)"], ["Valve 1 (device.power)"], []])
    _update(ota, outputs_on=lambda: next(seq))
    assert ota.sleeps == [ota_updater.BUSY_POLL_S] * 2
    assert ota.restarts == 1
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify"
    assert st["deadlineEpoch"] <= time.time() + ota_updater.VERIFY_DEADLINE_S + 2


def test_an_output_that_stays_on_after_the_install_undoes_it_without_a_restart(ota):
    before = ota.venv.tree()
    seq = iter([[]])
    _update(ota, outputs_on=lambda: next(seq, ["Pump (device.power)"]))
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["error"].startswith("busy: Pump")
    assert ota.venv.tree() == before and ota.restarts == 0
    assert _phases(ota) == ["started", "failed"]


# ---- git, only for a command without an archive -----------------------------------

def test_with_an_archive_git_is_never_used(ota):
    ota.git = False
    _update(ota)
    assert ota.git_asked == 0
    assert not any(a[-1].startswith("git+") for a in ota.pip)
    assert ota.restarts == 1


def test_without_an_archive_git_is_used_when_the_board_has_it(ota, caplog):
    ota_updater.perform_update("0.3.4", node_uid="node-x")
    assert _installs(ota)[0][-1] == "git+https://github.com/logapradeep/agal-one-agent.git@v0.3.4"
    assert "needs git" in caplog.text
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify" and st["via"] == "git"
    assert st["snapshot"]["version"] == "v0.3.3", "the git path gets the offline way back too"
    assert ota.restarts == 1


def test_without_an_archive_and_without_git_it_says_so(ota):
    ota.git = False
    ota_updater.perform_update("0.3.4", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "no git" in st["error"]
    assert ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]


# ---- the state machine (as before) ---------------------------------------------------

def test_perform_update_noop_when_same_version(ota):
    ota_updater.perform_update("0.3.3", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert ota.restarts == 0
    assert ota.pip == []
    assert "succeeded" in _phases(ota)


def test_a_second_update_while_one_runs_is_ignored(ota):
    gate, entered = threading.Event(), threading.Event()

    def slow():
        entered.set()
        gate.wait(5)
        return []

    t = threading.Thread(target=lambda: _update(ota, outputs_on=slow))
    t.start()
    assert entered.wait(5)
    assert "already in progress" in ota_updater.perform_update("v0.3.5", node_uid="node-x")
    gate.set()
    t.join(5)
    assert ota.restarts == 1 and ota.downloads == [ARCHIVE.format(tag="v0.3.4")]


def test_an_update_interrupted_before_the_install_is_settled_failed_at_boot(ota):
    ota_updater.write_state({"phase": "waiting", "previous": "v0.3.3", "target": "v0.3.4", "nodeUid": "node-x"})
    assert ota_updater.settle_interrupted() is True
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["error"].startswith("interrupted")
    assert _phases(ota) == ["failed"]
    assert ota_updater.settle_interrupted() is False


def test_confirm_update_settles_probation(ota):
    ota.venv.install("0.3.4")
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    assert ota_updater.confirm_update(node_uid="node-x") is True
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert st["previous"] == "v0.3.4"   # the target is now the known-good baseline
    assert "succeeded" in _phases(ota)


def test_confirm_update_with_the_old_version_running_reports_failed(ota):
    # the agent restarted while pip ran and 0.3.3 came back up: healthy, but not the update
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    assert ota_updater.confirm_update(node_uid="node-x") is False
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["previous"] == "v0.3.3"
    assert _phases(ota) == ["failed"]


def test_confirm_update_noop_when_not_probation(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    assert ota_updater.confirm_update() is False
    assert ota.emits == []


def test_run_verify_rolls_back_a_state_without_a_snapshot_through_git(ota):
    # ota_state.json written by an agent older than 0.3.4 (no snapshot): git, as before
    ota.venv.install("0.1.6")
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) - 1, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "rolled_back"
    assert _installs(ota) == [["install", "--no-deps", "--force-reinstall",
                               "git+https://github.com/logapradeep/agal-one-agent.git@v0.1.5"]]
    assert ota.restarts == 1
    assert "rolled_back" in _phases(ota)
    assert "rolled back" in out


def test_run_verify_noop_in_probation(ota):
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "pending_verify"  # unchanged
    assert ota.pip == [] and ota.restarts == 0
    assert "probation" in out


def test_run_verify_noop_when_idle(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    out = ota_updater.run_verify()
    assert ota.pip == [] and ota.restarts == 0
    assert "noop" in out


def test_run_verify_rollback_failure_marks_failed(ota):
    ota.pip_fail = {"0.1.5"}
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) - 1, "nodeUid": "node-x",
    })
    ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify" and st["rollbackAttempts"] == 1, "tried again, never given up"
    assert "boom" in st.get("rollbackError", "")
    assert ota.restarts == 0
    assert "failed" in _phases(ota)


# ---- the real download (no network: a fake opener) --------------------------------

class _Resp(io.BytesIO):
    def __init__(self, data, status=200, url="https://codeload.github.com/x.tar.gz", length=None):
        super().__init__(data)
        self.status = status
        self._url = url
        self.headers = {"Content-Length": str(length if length is not None else len(data))}

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener_for(resp):
    return lambda: types.SimpleNamespace(open=lambda req, timeout: resp)


def test_download_streams_into_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ota_updater, "_opener", _opener_for(_Resp(b"x" * 1000)))
    dest = tmp_path / "a.tar.gz"
    ota_updater._download(ARCHIVE.format(tag="v0.3.4"), str(dest))
    assert dest.read_bytes() == b"x" * 1000


@pytest.mark.parametrize("resp, match", [
    (_Resp(b"x", length=10 ** 9), "more than"),
    (_Resp(b"x" * 300, length=0), "larger than"),
    (_Resp(b"", status=404), "HTTP 404"),
    (_Resp(b"x", url="http://codeload.github.com/x.tar.gz"), "not allowed"),
    (_Resp(b"x", url="https://evil.example/x.tar.gz"), "not allowed"),
], ids=["declared-too-big", "streamed-too-big", "404", "ended-on-http", "ended-elsewhere"])
def test_download_refusals(tmp_path, monkeypatch, resp, match):
    monkeypatch.setattr(ota_updater, "MAX_ARCHIVE_BYTES", 200)
    monkeypatch.setattr(ota_updater, "_opener", _opener_for(resp))
    dest = tmp_path / "a.tar.gz"
    with pytest.raises(OtaError, match=match):
        ota_updater._download(ARCHIVE.format(tag="v0.3.4"), str(dest))
    assert not dest.exists() and not (tmp_path / "a.tar.gz.part").exists()


@pytest.mark.parametrize("newurl, ok", [
    ("https://codeload.github.com/logapradeep/agal-one-agent/tar.gz/refs/tags/v0.3.4", True),
    ("http://codeload.github.com/logapradeep/agal-one-agent/tar.gz/refs/tags/v0.3.4", False),
    ("https://evil.example/agal-one-agent.tar.gz", False),
])
def test_redirects_only_to_github_over_https(newurl, ok):
    import urllib.request
    handler = ota_updater._AllowListRedirect()
    req = urllib.request.Request(ARCHIVE.format(tag="v0.3.4"))
    if ok:
        assert handler.redirect_request(req, None, 302, "Found", {}, newurl).full_url == newurl
    else:
        with pytest.raises(urllib.error.HTTPError, match="refused"):
            handler.redirect_request(req, None, 302, "Found", {}, newurl)


# ---- main: off the MQTT loop, with the archive and what the node has ON ------------

def test_start_firmware_update_runs_off_the_mqtt_loop_with_the_archive(monkeypatch):
    from unittest.mock import MagicMock
    from agal_one_agent import main as agent_main

    seen, gate = {}, threading.Event()

    def perform_update(version, command_id="", rollout_id="", node_uid="", **kw):
        seen.update(version=version, command_id=command_id, node_uid=node_uid, **kw)
        gate.wait(5)
        return "ok"

    monkeypatch.setattr(ota_updater, "perform_update", perform_update)
    mqtt = MagicMock()
    outputs = lambda: []  # noqa: E731
    started = time.monotonic()
    t = agent_main.start_firmware_update(
        {"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4",
         "source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64}, mqtt, "node-1", outputs_on=outputs)
    assert time.monotonic() - started < 0.5, "the MQTT loop does not wait for the update"
    gate.set()
    t.join(5)
    assert seen == {"version": "v0.3.4", "command_id": "c1", "node_uid": "node-1",
                    "source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64, "outputs_on": outputs}
    mqtt.publish_command_ack.assert_called_once_with("c1", "executing", ingress=False)


def test_a_command_from_an_older_backend_has_no_archive(monkeypatch):
    from unittest.mock import MagicMock
    from agal_one_agent import main as agent_main

    seen = {}
    monkeypatch.setattr(ota_updater, "perform_update", lambda version, **kw: seen.update(kw) or "ok")
    agent_main.handle_firmware_update({"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4"},
                                      MagicMock(), "node-1")
    assert seen["source"] is None and seen["sha256"] is None


def test_main_routes_firmware_update_off_the_loop_with_the_outputs(tmp_path, monkeypatch):
    import yaml
    from agal_one_agent import main as agent_main
    from agal_one_agent.http_reporter import HttpReporter
    from agal_one_agent.mqtt_client import AgalOneMqttClient

    class Captured(Exception):
        pass

    handler = {}

    def capture(self, fn):
        handler["fn"] = fn
        raise Captured

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({
        "node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"}, "mqtt": {"broker": "broker.example"},
        "telemetry": {"ingress_url": "https://ingress.example/x", "buffer_db_path": str(tmp_path / "buf.db")},
        "blocks": {"state_dir": str(tmp_path / "blocks")}}))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(AgalOneMqttClient, "set_command_handler", capture)
    monkeypatch.setattr(HttpReporter, "report_hardware", lambda self, report: None)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", str(cfg), "--offline"])
    with pytest.raises(Captured):
        agent_main.main()

    calls = []
    monkeypatch.setattr(agent_main, "start_firmware_update",
                        lambda command, mqtt, node_uid, outputs_on=None: calls.append((command, node_uid, outputs_on)))
    handler["fn"]({"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4"})
    assert len(calls) == 1 and calls[0][1] == "node-1"
    assert calls[0][2]() == [], "no program, nothing on"


def test_outputs_on_names_what_the_program_has_on():
    import _contracts
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agal_one_agent import main as agent_main
    from agal_one_agent.blocks.clock import SimClock
    from agal_one_agent.blocks.io import SimulatedIO
    from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
    from agal_one_agent.blocks.simulate import build_bundle_from_defaults

    if not os.path.isdir(_contracts.DEFAULTS):
        pytest.skip(f"defaults not found at {_contracts.DEFAULTS}")
    clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=clock)
    rt.compile(build_bundle_from_defaults(_contracts.DEFAULTS, "farm"))

    def run(seconds):
        for _ in range(seconds):
            rt.step()
            clock.advance(1)

    run(1)
    assert agent_main.outputs_on_for(rt) == []
    rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    run(6)
    on = agent_main.outputs_on_for(rt)
    assert "Pump (device.power)" in on and "Valve 1 (device.power)" in on
    assert "the run of plot plot-1" in on
    rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    run(2)
    assert agent_main.outputs_on_for(rt) == []


def test_outputs_on_names_the_legacy_outputs_that_are_on(monkeypatch):
    from agal_one_agent import main as agent_main
    from agal_one_agent.handlers import gpio_handler, pwm_handler

    monkeypatch.setattr(gpio_handler, "_levels", {})
    monkeypatch.setattr(pwm_handler, "_duties", {})
    pin = types.SimpleNamespace(gpio_number=17, physical_pin=11, label="Pump relay", assigned_to=None,
                                pwm_frequency_hz=None, pwm_polarity=None)
    gpio_handler.GpioHandler().write(pin, 1)
    pwm_handler.PwmHandler().write(types.SimpleNamespace(**{**vars(pin), "gpio_number": 18, "label": "Fan"}), 40)
    assert agent_main.outputs_on_for(None) == ["Pump relay (GPIO17)", "Fan (PWM GPIO18)"]
    gpio_handler.GpioHandler().write(pin, 0)
    pwm_handler.PwmHandler().write(types.SimpleNamespace(**{**vars(pin), "gpio_number": 18, "label": "Fan"}), 0)
    assert agent_main.outputs_on_for(None) == []


def test_an_unexpected_error_before_the_install_is_reported_and_changes_nothing(ota, monkeypatch):
    before = ota.venv.tree()

    def boom():
        raise PermissionError("read-only file system")

    monkeypatch.setattr(ota_updater, "_check_free_space", boom)
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "read-only" in st["error"]
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before and ota.restarts == 0


def test_an_unexpected_error_during_the_install_puts_the_previous_version_back(ota, monkeypatch):
    before = ota.venv.tree()

    def broken(archive, sha256):
        ota.venv.install("0.3.4", half=True)
        raise OSError("No space left on device")

    monkeypatch.setattr(ota_updater, "_pip_install_archive", broken)
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "broke off" in st["error"]
    assert ota.venv.tree() == before and ota.restarts == 0


# ---- review fixes (2026-10-15) ------------------------------------------------------

def _past_deadline(**extra):
    st = ota_updater.read_state()
    st.update({"deadlineEpoch": int(time.time()) - 1, **extra})
    ota_updater.write_state(st)
    return st


def _damage_snapshot():
    snap = os.path.join(ota_updater.STATE_DIR, "ota", "previous", "tree", "lib", PY, "site-packages",
                        "agal_one_agent", "main.py")
    with open(snap, "w") as f:
        f.write("# came back empty after a power cut")


def _join(ota, tag="v0.3.3"):
    """The join script's requirement line for the agent it installed (enroll.ts installerScript)."""
    url, sha = ota.add_release(tag)
    req = ota_updater.JOIN_REQUIREMENTS
    os.makedirs(os.path.dirname(req), exist_ok=True)
    with open(req, "w") as f:
        f.write(f"agal-one-agent @ {url} --hash=sha256:{sha}\n")
    return url, sha


# power cuts: the saved copy and the state are on the SD card before pip runs

def test_the_saved_copy_and_the_state_reach_the_card_before_pip_runs(ota):
    _update(ota)
    first_install = ota.order.index("pip install")
    assert ota.order[:first_install].count("sync") >= 2, "the snapshot and the pending_verify state are synced"
    assert "preload" in ota.order[:first_install], "every old module is loaded before pip replaces them"
    assert ota.order[-1] == "sync", "the new files are synced before the restart"


def test_write_state_flushes_the_file_and_its_directory(ota, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(ota_updater.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    ota_updater.write_state({"phase": "idle"})
    assert len(synced) == 2, "the temporary file before the rename, the directory after it"
    assert ota_updater.read_state()["phase"] == "idle"


# a rollback that fails is tried again, and the network way back is used

def test_a_damaged_saved_copy_is_replaced_by_the_previous_release_archive(ota):
    _join(ota, "v0.3.3")
    before = ota.venv.tree()
    _update(ota)
    assert ota_updater.read_state()["previousPackage"]["source"].endswith("/v0.3.3.tar.gz")
    _damage_snapshot()
    _past_deadline()
    out = ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "rolled_back", out
    assert "again from its release archive" in out
    assert ota.downloads[-1].endswith("/v0.3.3.tar.gz")
    assert ota_updater.installed_version() == "v0.3.3" and ota.venv.tree() == before
    assert ota.runs_asked == ["v0.3.3"], "the reinstalled version is checked to start"
    assert ota.restarts == 2 and _phases(ota)[-1] == "rolled_back"


def test_a_restored_version_that_does_not_start_is_not_a_way_back(ota):
    _update(ota)
    _past_deadline()
    ota.runs = ["it does not import: ModuleNotFoundError: No module named 'newlib'"]
    out = ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify" and st["rollbackAttempts"] == 1
    assert "does not start" in st["rollbackError"] and "no release archive of v0.3.3" in st["rollbackError"]
    assert "FAILED" in out and ota.restarts == 1


def test_a_failed_rollback_is_retried_after_its_backoff(ota):
    _update(ota)
    _damage_snapshot()
    _past_deadline()
    assert "FAILED" in ota_updater.run_verify()
    _join(ota, "v0.3.3")  # the archive is known by the next attempt (not to this update's state, though)
    st = ota_updater.read_state()
    st["previousPackage"] = ota_updater._known_package("v0.3.3")
    st["rollbackRetryEpoch"] = int(time.time()) - 1
    ota_updater.write_state(st)
    out = ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "rolled_back", out
    assert _phases(ota).count("failed") == 1, "the failure was reported once, then the way back"
    assert _phases(ota)[-1] == "rolled_back"


def test_backoff_grows_and_is_never_given_up(ota):
    _update(ota)
    _damage_snapshot()
    _past_deadline()
    delays = []
    for _ in range(7):
        st = ota_updater.read_state()
        st["rollbackRetryEpoch"] = 0
        ota_updater.write_state(st)
        before = int(time.time())
        ota_updater.run_verify()
        st = ota_updater.read_state()
        delays.append(st["rollbackRetryEpoch"] - before)
    assert st["phase"] == "pending_verify" and st["rollbackAttempts"] == 7
    assert [round(d, -1) for d in delays[:5]] == [60, 120, 300, 600, 1800]
    assert all(d >= 1800 for d in delays[5:])
    assert _phases(ota).count("failed") == 1


def test_confirm_waits_while_a_rollback_is_under_way(ota):
    _update(ota)
    st = _past_deadline(rollbackAttempts=1)
    assert ota_updater.confirm_update("node-x", running="v0.3.4") is False
    assert ota_updater.read_state()["phase"] == "pending_verify"


def test_an_undo_that_cannot_put_the_files_back_leaves_it_to_the_verify_timer(ota, monkeypatch):
    ota.pip_fail = {"0.3.4"}
    restore = {"result": (False, "disk full")}
    monkeypatch.setattr(ota_updater, "_restore_snapshot", lambda expected=None: restore["result"])
    out = _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify" and st["runningPrevious"] is True
    assert st["deadlineEpoch"] <= time.time() and st["rollbackAttempts"] == 1
    assert "tries again" in st["error"] and "disk full" in out
    assert ota.restarts == 0 and _phases(ota) == ["started", "failed"]
    # the next attempt works: the files match the running agent again, which is left running
    restore["result"] = (True, "put v0.3.3 back")
    st["rollbackRetryEpoch"] = 0
    ota_updater.write_state(st)
    assert "already v0.3.3" in ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "rolled_back"
    assert ota.restarts == 0, "no restart: it would switch off what the running agent drives"


def test_a_new_update_does_not_overwrite_one_still_in_probation(ota):
    _update(ota)
    before = ota_updater.read_state()
    emits = len(ota.emits)
    out = _update(ota, tag="v0.3.5")
    assert "still being checked or undone" in out
    assert ota_updater.read_state() == before and len(ota.emits) == emits


# no downgrade, never below the first agent that updates without git

def test_an_older_version_than_the_running_one_is_refused(ota):
    ota.venv.install("0.3.6")
    out = _update(ota, tag="v0.3.5")
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "older than the running v0.3.6" in st["error"], out
    assert ota.downloads == [] and ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]


@pytest.mark.parametrize("tag", ["v0.3.1", "v0.3.3", "v0.2.0", "v0.3.4-rc.1"])
def test_a_version_below_the_first_archive_updater_is_refused(ota, tag):
    ota.venv.install("0.3.2")  # even when it would be an upgrade
    _update(ota, tag=tag)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "older than v0.3.4" in st["error"]
    assert ota.downloads == [] and ota.pip == []


def test_version_order():
    k = ota_updater._version_key
    assert k("v0.3.4-rc.1") < k("v0.3.4") < k("0.3.5") < k("v0.10.0") < k("v1.0.0")
    assert k("v0.3.4-rc.2") > k("v0.3.4-rc.1") and k("main") is None


# libraries: added when missing, never changed

def test_a_release_that_needs_a_library_upgraded_is_not_installed(ota):
    before = ota.venv.tree()
    ota.requires = ("paho-mqtt>=3.0",)
    ota.check_out = ["agal-one-agent 0.3.4 has requirement paho-mqtt>=3.0, but you have paho-mqtt 2.1.0."]
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "another version" in st["error"] and "paho-mqtt>=3.0" in st["error"]
    assert ota.deps_installed == [] and ota.dry_runs == [], "the libraries were never touched"
    assert ota.venv.tree() == before and ota.restarts == 0


def test_a_new_library_that_would_upgrade_one_the_board_has_is_not_installed(ota):
    before = ota.venv.tree()
    ota.requires = ("newlib>=1.2",)
    ota.check_out = ["agal-one-agent 0.3.4 requires newlib, which is not installed."]
    ota.dry_report = {"install": [{"metadata": {"name": "newlib", "version": "1.2"}},
                                  {"metadata": {"name": "paho_mqtt", "version": "3.0.0"}}]}
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "paho_mqtt 2.1.0 → 3.0.0" in st["error"]
    assert ota.deps_installed == [], "only the dry run"
    assert ota.venv.tree() == before


# probation, owned by the new process

def _probation_state(**extra):
    st = {"phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4", "nodeUid": "node-x",
          "snapshot": {"version": "v0.3.3"}, "deadlineEpoch": int(time.time()) + 10, **extra}
    ota_updater.write_state(st)
    return st


def test_begin_probation_counts_the_deadline_from_the_boot_once(ota):
    ota.venv.install("0.3.4")
    _probation_state(deadlineEpoch=int(time.time()) - 5)  # a slow shutdown used up the old deadline
    assert ota_updater.begin_probation("0.3.4") is True
    st = ota_updater.read_state()
    assert st["deadlineEpoch"] >= time.time() + ota_updater.PROBATION_DEADLINE_S - 2
    first = st["deadlineEpoch"]
    # a version that crash-loops after this point does not get its deadline moved again
    st["deadlineEpoch"] = int(time.time()) + 3
    ota_updater.write_state(st)
    assert ota_updater.begin_probation("0.3.4") is True
    assert ota_updater.read_state()["deadlineEpoch"] < first


def test_begin_probation_leaves_the_old_versions_deadline(ota):
    st = _probation_state()
    assert ota_updater.begin_probation("0.3.3") is True, "confirm reports that the old version came up"
    assert ota_updater.read_state()["deadlineEpoch"] == st["deadlineEpoch"]
    ota_updater.write_state({"phase": "idle"})
    assert ota_updater.begin_probation("0.3.4") is False


def test_probation_confirms_at_the_first_healthy_check_not_one_instant(ota):
    ota.venv.install("0.3.4")
    _probation_state(package={"source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64})
    connected = {"now": False}
    p = ota_updater.Probation("node-x", "0.3.4", lambda: connected["now"])
    p.tick()
    assert p.check() is False, "a reconnecting MQTT at one check is not a failed probation"
    assert ota_updater.read_state()["phase"] == "pending_verify"
    connected["now"] = True
    p.tick()
    assert p.check() is True
    st = ota_updater.read_state()
    assert st["phase"] == "idle" and st["previous"] == "v0.3.4"
    assert _phases(ota) == ["succeeded"]
    assert ota_updater._known_package("v0.3.4") == {"source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64}, \
        "the confirmed version's archive is recorded for the way back of the next update"


def test_probation_is_not_healthy_when_the_main_loop_stopped(ota):
    ota.venv.install("0.3.4")
    _probation_state()
    p = ota_updater.Probation("node-x", "0.3.4", lambda: True)
    p.tick()
    ota.now += ota_updater.LOOP_STALE_S + 1
    assert p.check() is False and ota_updater.read_state()["phase"] == "pending_verify"


def test_the_heartbeat_names_the_version_the_connection_and_what_is_on(ota):
    _probation_state()
    p = ota_updater.Probation("node-x", "0.3.4", lambda: False, outputs_on=lambda: ["Pump (device.power)"])
    p.tick()
    beat = ota_updater.read_heartbeat()
    assert (beat["version"], beat["connected"], beat["outputsOn"]) == ("v0.3.4", False, ["Pump (device.power)"])
    epoch = beat["epoch"]
    p.tick()  # not rewritten before HEARTBEAT_EVERY_S
    ota.now += ota_updater.HEARTBEAT_EVERY_S
    p.tick()
    assert ota_updater.read_heartbeat()["epoch"] >= epoch


def _beat(version="v0.3.4", age=0, connected=False, on=()):
    os.makedirs(os.path.join(ota_updater.STATE_DIR, "ota"), exist_ok=True)
    with open(os.path.join(ota_updater.STATE_DIR, "ota", "heartbeat.json"), "w") as f:
        json.dump({"epoch": int(time.time()) - age, "version": version, "connected": connected,
                   "outputsOn": list(on)}, f)


def test_the_rollback_waits_while_the_new_version_runs_a_pump(ota):
    _update(ota)
    _past_deadline()
    _beat(on=["Pump (device.power)"])
    out = ota_updater.run_verify()
    assert "holding the rollback" in out and "Pump" in out
    assert ota_updater.read_state()["phase"] == "pending_verify" and ota.restarts == 1
    # bounded: past the deadline by more than the busy wait, it goes back anyway
    _past_deadline(deadlineEpoch=int(time.time()) - ota_updater.BUSY_WAIT_S - 1)
    _beat(on=["Pump (device.power)"])
    assert "rolled back" in ota_updater.run_verify()


def test_the_rollback_waits_briefly_for_an_up_and_connected_new_version(ota):
    _update(ota)
    _past_deadline()
    _beat(connected=True)
    assert "confirms itself" in ota_updater.run_verify()
    _past_deadline(deadlineEpoch=int(time.time()) - ota_updater.CONFIRM_GRACE_S - 1)
    assert "rolled back" in ota_updater.run_verify()


@pytest.mark.parametrize("beat", [dict(age=120, on=["Pump"]), dict(version="v0.3.3", on=["Pump"])],
                         ids=["stale", "other-version"])
def test_a_stale_or_foreign_heartbeat_does_not_hold_the_rollback(ota, beat):
    _update(ota)
    _past_deadline()
    _beat(**beat)
    assert "rolled back" in ota_updater.run_verify()


def test_run_verify_does_not_act_while_the_state_is_locked(ota):
    _update(ota)
    _past_deadline()
    with ota_updater._state_lock() as held:
        assert held
        assert "busy" in ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "pending_verify"


# the restart reaches systemd; the boot clean-up never touches a running update

def test_the_restart_is_queued_and_waited_for(monkeypatch):
    calls = []
    monkeypatch.setattr(ota_updater, "_systemctl",
                        lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    ota_updater._restart_service()
    assert calls == [("restart", "--no-block", "agal-one-agent")]


def test_settle_interrupted_leaves_a_running_update_alone(ota):
    ota_updater.write_state({"phase": "downloading", "previous": "v0.3.3", "target": "v0.3.4", "nodeUid": "node-x"})
    with ota_updater._UPDATE_LOCK:
        assert ota_updater.settle_interrupted() is False
    assert ota_updater.read_state()["phase"] == "downloading" and ota.emits == []


# the verify launcher

def test_the_verify_launcher_imports_nothing_when_no_update_is_in_probation(ota, tmp_path):
    _update(ota)
    ota_updater.write_state({"phase": "idle"})
    marker = tmp_path / "marker"
    launcher = os.path.join(ota_updater.STATE_DIR, "ota", "verify.py")
    r = subprocess.run([sys.executable, "-I", launcher], env={**os.environ, "OTA_MARKER": str(marker)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()


def test_the_verify_launcher_uses_the_installed_code_when_the_saved_copy_is_damaged(ota, tmp_path):
    _update(ota)
    _damage_snapshot()
    marker = tmp_path / "marker"
    launcher = os.path.join(ota_updater.STATE_DIR, "ota", "verify.py")
    r = subprocess.run([sys.executable, launcher],
                       env={**os.environ, "OTA_MARKER": str(marker), "PYTHONPATH": str(ota.venv.site)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "0.3.4", "a damaged saved copy is not imported"


def test_main_settles_and_begins_probation_before_mqtt_connects(tmp_path, monkeypatch):
    import yaml
    from agal_one_agent import main as agent_main
    from agal_one_agent.http_reporter import HttpReporter
    from agal_one_agent.mqtt_client import AgalOneMqttClient

    class Captured(Exception):
        pass

    order = []

    def connect(self):
        order.append("connect")
        raise Captured

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({
        "node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"}, "mqtt": {"broker": "broker.example"},
        "telemetry": {"ingress_url": "https://ingress.example/x", "buffer_db_path": str(tmp_path / "buf.db")},
        "blocks": {"state_dir": str(tmp_path / "blocks")}}))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(AgalOneMqttClient, "connect", connect)
    monkeypatch.setattr(HttpReporter, "report_hardware", lambda self, report: None)
    monkeypatch.setattr(ota_updater, "settle_interrupted", lambda node_uid="": order.append("settle"))
    monkeypatch.setattr(ota_updater, "begin_probation", lambda v: order.append(("probation", v)) or False)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", str(cfg)])
    with pytest.raises(Captured):
        agent_main.main()
    assert order == ["settle", ("probation", agent_main.__version__), "connect"]


# what an update waits on

def test_an_active_low_gpio_port_that_is_off_is_not_reported_on(monkeypatch):
    from agal_one_agent import main as agent_main
    from agal_one_agent.blocks.io import HardwareIO, PortRef
    from agal_one_agent.handlers import gpio_handler, pwm_handler

    monkeypatch.setattr(gpio_handler, "_levels", {})
    monkeypatch.setattr(pwm_handler, "_duties", {})
    port = PortRef("pump-1", "device.power", {"kind": "gpio", "gpioNumber": 17, "direction": "out", "activeLow": True})
    io_ = HardwareIO()
    io_.write_output(port, False)
    assert agent_main.outputs_on_for(None) == [], "OFF is held HIGH on an active-low relay: still OFF"
    io_.write_output(port, True)
    assert agent_main.outputs_on_for(None) == ["device.power (GPIO17)"]
    io_.write_output(port, False)
    assert agent_main.outputs_on_for(None) == []


def test_preload_imports_every_module_of_the_running_agent():
    for name in ["agal_one_agent.handlers.uart_handler", "agal_one_agent.handlers.onewire_handler"]:
        sys.modules.pop(name, None)
    ota_updater._preload_package()
    assert "agal_one_agent.handlers.uart_handler" in sys.modules
    assert "agal_one_agent.handlers.onewire_handler" in sys.modules
    assert "agal_one_agent.__main__" not in sys.modules
