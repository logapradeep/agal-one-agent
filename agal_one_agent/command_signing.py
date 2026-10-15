"""Every command is checked before it is carried out — signed for THIS node (contracts v1.14.2).

Why. Every node connects to the HiveMQ broker with one shared device login, written into every
node's ``config.yaml``, so whoever holds any node's config could publish to ANY node's
``agal/{nodeUid}/commands``: the topic name was the only separation, and a nodeUid is no secret.
Founder, 2026-10-15: yes to signing every command with the node's own secret and having the node
refuse an unsigned or forged one.

The secret is the node's ``authToken`` (``config.yaml`` ``node.auth_token``), already its Bearer
token to telemetryIngress. The backend signs every message it publishes to the command topic
(``publishCommand``); this module is the node's half of ``Agal/contracts/nodes/command-signing.md``
and a port of ``nodes/reference/signing.mjs`` — the shared vectors
(``nodes/vectors/command-signing.json``) must give the same answer here (tests/test_command_signing.py).

- ``canonicalize`` (§3): RFC 8785 in effect. Keys sorted by UTF-16 code units at every level, no
  whitespace, strings escaped as ``JSON.stringify`` escapes them, and every number written as
  ECMAScript writes the IEEE 754 double it parses to — which ``json.dumps`` does not (``1.0``,
  ``1e-07``, ``-0.0``).
- ``verify_command`` (§5.2): the six checks in the reference's order — no token: refused
  ``bad_signature`` (§5.5: it can check nothing, so it carries out nothing); no ``sig``:
  ``unsigned``; shape or MAC (``hmac.compare_digest``): ``bad_signature``; an id accepted before:
  ``replayed``; below the replay floor, no trusted now, or more than 10 minutes old or 2 minutes
  ahead of it: ``stale``. Only an accepted command is remembered; a refused copy never burns an id.
- ``ReplayStore`` (§5.3): the accepted ids with their ``sigTs``, aged 7 days on the BACKEND's
  clock (``highWater``), at most 10,000 (``evictedFloor``), persisted in the state directory
  BEFORE the command is carried out — so a restart, or a crash halfway, followed by a
  redelivery does not carry it out twice.
- ``CommandGate``: the MQTT boundary (``mqtt_client._on_message``). It hands the handler the very
  object it verified, logs every refusal, answers a refused tracked id with a failed command ack
  carrying ``reason`` — ``bad_signature`` (at most 5 in 10 minutes) and ``stale`` (at most 20), never
  ``unsigned`` or ``replayed`` — and reports a ``firmwareUpdate`` refused by the time check (not the
  floor) as a failed ``firmware_update`` too. Its "now" is ``server_time.trusted_now``: the
  backend's time from its HTTPS answers, else an NTP / RTC wall clock, else none (§5.4).

A node whose ``auth_token`` is empty cannot check anything, so it carries out NOTHING from its
command topic (§5.5, v1.14.2 review — agents up to 0.3.3, and 0.3.4 as first written, carried such
messages out): every message is refused and logged, at start and at most once a minute — "no token:
join this node again". Nothing legitimate is lost: the backend never publishes to a node it holds no
token for.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)

SIG_ALG = "hs256-v1"
#: CommandRefusalReason (enums.yaml): why a node refused a message on its command topic.
REFUSAL_REASONS = ("unsigned", "bad_signature", "replayed", "stale")
#: An accepted command's id is remembered this long, measured on the backend's clock (sigTs).
REPLAY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
#: With a trusted clock: refused when sigTs is older than this ...
MAX_AGE_MS = 10 * 60 * 1000
#: ... or further ahead than this.
MAX_AHEAD_MS = 2 * 60 * 1000
#: At most this many ids are remembered; the oldest leaves first and raises the floor.
REPLAY_SET_MAX = 10000
#: Number.MAX_SAFE_INTEGER — sigTs must be a safe integer, as the reference checks.
MAX_SAFE_INTEGER = 2 ** 53 - 1

_SIG_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")  # a str holds a pair as one code point: any left is lone
_STRING_ESCAPE = re.compile(r'[\x00-\x1f"\\]')
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
#: The ids the backend keeps a document for (commandAck's TRACKED_COMMAND_ID): an /assetCommands
#: auto-id. Only these are answered when refused (command-signing.md §6).
TRACKED_COMMAND_ID = re.compile(r"[A-Za-z0-9]{20}")


class CanonicalError(ValueError):
    """The value has no canonical form (it is not a JSON value)."""


# --------------------------------------------------------------------------- §3 canonical JSON

def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_key(s: str) -> bytes:
    # Big-endian UTF-16 bytes compare as the code units do: JavaScript's default sort().
    return s.encode("utf-16-be", "surrogatepass")


def _es_number(x: float) -> str:
    """ECMAScript Number::toString(x) for a finite double (RFC 8785 §3.2.2.3)."""
    if x == 0:
        return "0"  # 0 and -0
    if x < 0:
        return "-" + _es_number(-x)
    # repr() gives the shortest digits that round-trip — the s and k of the ECMAScript algorithm.
    mantissa, _, exp = repr(x).partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    e = (int(exp) if exp else 0) - len(frac)  # x = int(digits) * 10**e
    stripped = digits.rstrip("0")
    e += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = e + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    sign = "+" if n - 1 >= 0 else "-"
    head = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{head}e{sign}{abs(n - 1)}"


def _es_string(s: str) -> str:
    if _LONE_SURROGATE.search(s):
        raise CanonicalError("a string with a lone surrogate is not well-formed Unicode")
    return '"' + _STRING_ESCAPE.sub(
        lambda m: _SHORT_ESCAPES.get(m.group(0)) or "\\u%04x" % ord(m.group(0)), s) + '"'


def canonicalize(value: Any) -> str:
    """§3 — the canonical JSON text of a JSON value (as ``json.loads`` gives it). Raises
    ``CanonicalError`` for anything that is not one (NaN, Infinity, a lone surrogate, a tuple, an
    object)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):  # JSON.parse gives a double: 2**53 + 1 is written 9007199254740992
        try:
            value = float(value)
        except OverflowError:
            raise CanonicalError("not a JSON number: an integer beyond the double range") from None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError(f"not a JSON number: {value}")
        return _es_number(value)
    if isinstance(value, str):
        return _es_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise CanonicalError("an object key is not a string")
            if _LONE_SURROGATE.search(k):
                raise CanonicalError("a key with a lone surrogate is not well-formed Unicode")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(f"{_es_string(k)}:{canonicalize(value[k])}" for k in keys) + "}"
    raise CanonicalError(f"not a JSON value: {type(value).__name__}")


def mac_of(canonical: str, token: str) -> str:
    """HMAC-SHA256 of the canonical text, keyed by the token's UTF-8 bytes, as unpadded base64url."""
    digest = hmac.new(token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def signed_text(message: dict) -> str:
    """The canonical text a signature covers: the message with every member but ``sig``."""
    return canonicalize({k: v for k, v in message.items() if k != "sig"})


def sign_command(message: dict, token: str, sig_ts: int) -> tuple[dict, str]:
    """§4 — what the backend does (``publishCommand``); the node never signs. Here for the tests and
    a bench: the signed message and the canonical text it covers."""
    if not isinstance(token, str) or not token:
        raise ValueError("no token: the backend does not publish to a node it holds no credential for")
    plain = json.loads(json.dumps(message))
    if not _command_id_ok(plain.get("commandId")):
        raise ValueError("a signed command needs a commandId (1–128 characters, no /)")
    if isinstance(sig_ts, bool) or not isinstance(sig_ts, int) or not 0 <= sig_ts <= MAX_SAFE_INTEGER:
        raise ValueError("sigTs is milliseconds since the epoch, an integer")
    plain.pop("sig", None)
    unsigned = {**plain, "sigAlg": SIG_ALG, "sigTs": sig_ts}
    canonical = canonicalize(unsigned)
    return {**unsigned, "sig": mac_of(canonical, token)}, canonical


# --------------------------------------------------------------------------- §5 verifying

def _command_id_ok(v: Any) -> bool:
    # /^[^/]{1,128}$/ counts UTF-16 code units, as JavaScript does.
    return isinstance(v, str) and "/" not in v and 1 <= _utf16_len(v) <= 128


def _safe_int(v: Any) -> Optional[int]:
    """The integer a JSON number is when JavaScript would call it a safe integer (Number.isSafeInteger
    of the double it parses to), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if abs(v) <= MAX_SAFE_INTEGER else None
    if isinstance(v, float) and math.isfinite(v) and v.is_integer() and abs(v) <= MAX_SAFE_INTEGER:
        return int(v)
    return None


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")  # json.loads takes NaN / Infinity; JSON.parse does not


def parse_json(text: Union[str, bytes]) -> Any:
    """JSON as ``JSON.parse`` reads it: UTF-8, no NaN / Infinity."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    return json.loads(text, parse_constant=_no_constants)


class ReplayState:
    """§5.3 — the ids a node accepted, in memory (the reference's ``newReplayState``)."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}  # commandId → sigTs, in the order they were accepted
        self.high_water = 0
        self.evicted_floor = -1

    def has(self, command_id: str) -> bool:
        return command_id in self.seen

    def floor(self) -> int:
        """The oldest sigTs a command may carry and still be accepted (any clock)."""
        return max(self.high_water - REPLAY_WINDOW_MS, self.evicted_floor + 1)

    def remember(self, command_id: str, sig_ts: int) -> None:
        self._remember(command_id, sig_ts)

    def _remember(self, command_id: str, sig_ts: int) -> None:
        self.seen[command_id] = sig_ts
        if sig_ts > self.high_water:
            self.high_water = sig_ts
        window_start = self.high_water - REPLAY_WINDOW_MS
        for cid in [cid for cid, ts in self.seen.items() if ts < window_start]:
            del self.seen[cid]
        while len(self.seen) > REPLAY_SET_MAX:
            oldest_id, oldest_ts = None, math.inf
            for cid, ts in self.seen.items():
                if ts < oldest_ts:  # the first accepted of the oldest, as the reference picks it
                    oldest_id, oldest_ts = cid, ts
            del self.seen[oldest_id]
            self.evicted_floor = max(self.evicted_floor, int(oldest_ts))


class Verdict:
    """What the node does with one message: ``accepted`` (with ``command``, and ``verified`` False
    only on a node without a token) or refused with ``reason``, ``detail`` and, when the message
    had a usable one, ``command_id``. ``message`` is the parsed message when its MAC verified (a
    ``replayed`` or ``stale`` one: its fields are the backend's)."""

    __slots__ = ("accepted", "command", "verified", "reason", "command_id", "detail", "message", "window")

    def __init__(self, accepted: bool, *, command: Optional[dict] = None, verified: bool = False,
                 reason: Optional[str] = None, command_id: Optional[str] = None, detail: str = "",
                 message: Optional[dict] = None, window: bool = False):
        self.accepted = accepted
        self.command = command
        self.verified = verified
        self.reason = reason
        self.command_id = command_id
        self.detail = detail
        self.message = message
        #: A ``stale`` refused by check 6 — the time window, or no trusted now — not by the floor
        #: (check 5, a capture at least 7 days older than a command already carried out).
        self.window = window

    def __repr__(self) -> str:
        if self.accepted:
            return f"Verdict(accepted, verified={self.verified})"
        return f"Verdict({self.reason}, commandId={self.command_id!r}, {self.detail!r})"


def verify_command(wire: Any, token: Optional[str], state: ReplayState, *,
                   clock_trusted: Union[bool, Callable[[], bool]] = False,
                   now_ms: Optional[int] = None) -> Verdict:
    """§5 — the reference's ``verifyCommand``. ``wire`` is the message as received (text or bytes)
    or an already-parsed object; ``token`` the node's authToken (empty or None: a node without one,
    which refuses everything — §5.5); ``state`` kept across calls. ``clock_trusted`` says whether
    ``now_ms`` is a trusted now (§5.4; False: none, and a message that reaches check 6 is stale); it
    may be a callable, asked only when a message reaches that check. Only an accepted command is
    remembered."""
    msg = wire
    if isinstance(wire, (str, bytes, bytearray)):
        try:
            msg = parse_json(wire)
        except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: nested past Python's limit
            return Verdict(False, reason="bad_signature", detail="not JSON")
    if not isinstance(msg, dict):
        return Verdict(False, reason="bad_signature", detail="not a JSON object")
    command_id = msg.get("commandId") if _command_id_ok(msg.get("commandId")) else None

    def refuse(reason: str, detail: str, verified_message: Optional[dict] = None, window: bool = False) -> Verdict:
        return Verdict(False, reason=reason, command_id=command_id, detail=detail, message=verified_message,
                       window=window)

    if not isinstance(token, str) or not token:
        return refuse("bad_signature", "this node holds no token: join it again")
    if "sig" not in msg:
        return refuse("unsigned", "no sig")
    sig = msg["sig"]
    if not isinstance(sig, str) or not _SIG_RE.fullmatch(sig):
        return refuse("bad_signature", "sig is not 43 base64url characters")
    if msg.get("sigAlg") != SIG_ALG:
        return refuse("bad_signature", f"sigAlg is not {SIG_ALG}")
    sig_ts = _safe_int(msg.get("sigTs"))
    if sig_ts is None or sig_ts < 0:
        return refuse("bad_signature", "sigTs is not an integer of milliseconds")
    if not command_id:
        return refuse("bad_signature", "no usable commandId")
    try:
        canonical = signed_text(msg)
    except CanonicalError as e:
        return refuse("bad_signature", f"no canonical form: {e}")
    except RecursionError:
        return refuse("bad_signature", "no canonical form: nested too deep")
    expected = base64.urlsafe_b64decode(mac_of(canonical, token) + "=")
    given = base64.urlsafe_b64decode(sig + "=")  # 43 characters: 32 bytes, as Buffer.from(…, "base64url")
    if not hmac.compare_digest(given, expected):
        return refuse("bad_signature", "the signature does not match")

    if state.has(command_id):
        return refuse("replayed", "this commandId was accepted before", msg)
    if sig_ts < state.floor():
        return refuse("stale", "older than the replay window", msg)
    trusted = clock_trusted() if callable(clock_trusted) else bool(clock_trusted)
    if not trusted:
        return refuse("stale", "no trusted time yet", msg, window=True)
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if now - sig_ts > MAX_AGE_MS:
        return refuse("stale", "signed more than 10 minutes ago", msg, window=True)
    if sig_ts - now > MAX_AHEAD_MS:
        return refuse("stale", "signed more than 2 minutes in the future", msg, window=True)
    state.remember(command_id, sig_ts)
    return Verdict(True, command=msg, verified=True)


# --------------------------------------------------------------------------- §5.3 persisted

REPLAY_SNAPSHOT_FILE = "command_replay.json"
REPLAY_JOURNAL_FILE = "command_replay.journal"


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class ReplayStore(ReplayState):
    """The replay set kept in the state directory (``blocks.state_dir``): a snapshot
    (``command_replay.json`` — highWater, evictedFloor and the ids in the order accepted) and a
    journal of the ids accepted since (``command_replay.journal``, one line each). ``remember``
    appends and fsyncs one line BEFORE it returns, so the command is carried out only once it is
    on the card; the journal is folded into the snapshot (written to a temporary file, synced,
    renamed) every ``compact_every`` ids and at start. A torn last line — a power cut during the
    append, before the command ran — is ignored.

    If the write fails (a full or read-only card), the id is still remembered in memory and the
    command is still carried out, logged as an error: refusing would leave the node unable to
    take any command, the one that switches the pump off included. A snapshot that cannot be read
    is kept aside as ``command_replay.json.corrupt`` and the set starts empty, logged as an error
    (with a trusted clock nothing older than 10 minutes is accepted anyway)."""

    COMPACT_EVERY = 2000

    def __init__(self, state_dir: str, *, compact_every: Optional[int] = None):
        super().__init__()
        self.state_dir = state_dir
        self.snapshot_path = os.path.join(state_dir, REPLAY_SNAPSHOT_FILE)
        self.journal_path = os.path.join(state_dir, REPLAY_JOURNAL_FILE)
        self.compact_every = self.COMPACT_EVERY if compact_every is None else max(1, compact_every)
        self._journal_lines = 0
        self._lock = threading.Lock()
        self.write_failures = 0
        self._load()

    # ---- loading

    def _load(self) -> None:
        try:
            with open(self.snapshot_path, encoding="utf-8") as f:
                snap = json.load(f)
            self._take_snapshot(snap)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 — a damaged file must not stop the agent
            logger.error("command replay set %s unreadable (%s): kept aside, starting empty", self.snapshot_path, e)
            self.seen, self.high_water, self.evicted_floor = {}, 0, -1
            try:
                os.replace(self.snapshot_path, self.snapshot_path + ".corrupt")
            except OSError:
                pass
        lines = 0
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                for line in f:
                    lines += 1
                    try:
                        entry = json.loads(line)
                        cid, ts = entry["id"], _safe_int(entry["ts"])
                    except Exception:  # noqa: BLE001
                        logger.warning("command replay journal: line %d unreadable (a torn write?): ignored", lines)
                        continue
                    if _command_id_ok(cid) and ts is not None and ts >= 0:
                        self._remember(cid, ts)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error("command replay journal %s unreadable: %s", self.journal_path, e)
        self._journal_lines = lines
        if lines:
            self._compact()

    def _take_snapshot(self, snap: dict) -> None:
        if not isinstance(snap, dict) or not isinstance(snap.get("seen"), list):
            raise ValueError("not a replay snapshot")
        high = _safe_int(snap.get("highWater"))
        floor = _safe_int(snap.get("evictedFloor"))
        if high is None or floor is None:
            raise ValueError("highWater / evictedFloor missing")
        seen: dict[str, int] = {}
        for item in snap["seen"]:
            cid, ts = item[0], _safe_int(item[1])
            if not _command_id_ok(cid) or ts is None:
                raise ValueError("a malformed id")
            seen[cid] = ts
        self.seen, self.high_water, self.evicted_floor = seen, high, floor

    # ---- writing

    def remember(self, command_id: str, sig_ts: int) -> None:
        with self._lock:
            self._remember(command_id, sig_ts)
            try:
                os.makedirs(self.state_dir, exist_ok=True)
                line = json.dumps({"id": command_id, "ts": sig_ts}) + "\n"
                with open(self.journal_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                if self._journal_lines == 0:
                    _fsync_dir(self.state_dir)  # the journal's own name, the first time
                self._journal_lines += 1
            except OSError as e:
                self.write_failures += 1
                logger.error("command %s: the replay set could not be written (%s); carried out, remembered "
                             "until the agent restarts", command_id, e)
                return
            if self._journal_lines >= self.compact_every:
                self._compact()

    def _compact(self) -> None:
        snap = {"version": 1, "highWater": self.high_water, "evictedFloor": self.evicted_floor,
                "seen": [[cid, ts] for cid, ts in self.seen.items()]}
        tmp = self.snapshot_path + ".tmp"
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.snapshot_path)
            _fsync_dir(self.state_dir)
            # Only once the snapshot holds every id is the journal emptied (a crash between the two
            # replays the journal over the snapshot: remembering an id twice changes nothing).
            with open(self.journal_path, "w", encoding="utf-8") as f:
                f.flush()
                os.fsync(f.fileno())
            self._journal_lines = 0
        except OSError as e:
            self.write_failures += 1
            logger.error("command replay set: snapshot %s not written: %s", self.snapshot_path, e)


# --------------------------------------------------------------------------- the MQTT boundary

class CommandGate:
    """Where every message from the command topic is checked (``AgalOneMqttClient._on_message``,
    before ``on_command``). ``admit(raw)`` gives the command to carry out — the very object that
    was verified — or None.

    ``ack(command_id, status, error=…, reason=…)`` is the command-ack call
    (``AgalOneMqttClient.publish_command_ack``: the ordered HTTPS queue of command_acks.py, plus
    the status topic). ``firmware_report(command_id=…, to_version=…, rollout_id=…, error=…)`` posts a
    failed ``firmware_update`` (``HttpReporter.report_firmware_event``); it is called off this
    thread. ``trusted_now()`` is the node's trusted now in ms, or None (``server_time.trusted_now``:
    the backend's time from its HTTPS answers, else an NTP / RTC wall clock — §5.4); without one,
    ``clock_ok`` and ``now_ms`` make it (the wall clock when ``clock_ok()``)."""

    #: Answers per refusal reason in any REFUSAL_ACKS_WINDOW_S (v1.14.2 review: counted apart — a
    #: verified MAC is needed for stale, so forged traffic can spend only the bad_signature budget).
    REFUSAL_ACKS_MAX = {"stale": 20, "bad_signature": 5}
    REFUSAL_ACKS_WINDOW_S = 600.0
    #: Refusals that are only logged: the backend moves no command for them (functions.yaml
    #: commandAck), and answering let anyone with the shared login spend the answers with junk ids.
    LOGGED_ONLY = frozenset({"unsigned", "replayed"})
    FIRMWARE_REPORTED_MAX = 64
    NO_TOKEN_LOG_EVERY_S = 60.0

    def __init__(self, token: str, state: ReplayState, *,
                 ack: Optional[Callable[..., None]] = None,
                 firmware_report: Optional[Callable[..., None]] = None,
                 clock_ok: Optional[Callable[[], bool]] = None,
                 now_ms: Optional[Callable[[], int]] = None,
                 trusted_now: Optional[Callable[[], Optional[int]]] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 firmware_report_async: bool = True):
        self.token = token or ""
        self.state = state
        self.ack = ack
        self.firmware_report = firmware_report
        if clock_ok is None:
            from .blocks.clock import SystemClock
            clock_ok = SystemClock().clock_ok
        self.clock_ok = clock_ok
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._trusted_now = trusted_now
        self.monotonic = monotonic
        self.firmware_report_async = firmware_report_async
        self._lock = threading.Lock()
        self._refusal_acks: dict[str, deque[float]] = {r: deque() for r in self.REFUSAL_ACKS_MAX}
        self._firmware_reported: deque[str] = deque()
        self._no_token_logged_at = -math.inf
        # Counters (tests and a debugging session read them).
        self.accepted = 0
        self.refused: dict[str, int] = {r: 0 for r in REFUSAL_REASONS}
        self.refusal_acks_sent = 0
        self.refusal_acks_withheld = 0
        if not self.token:
            self._no_token_logged_at = self.monotonic()
            logger.error("node.auth_token is empty: this node checks nothing, so it carries out NOTHING from its "
                         "command topic — no token: join this node again (the join command from the app).")

    @property
    def protected(self) -> bool:
        return bool(self.token)

    def trusted_now(self) -> Optional[int]:
        """The node's trusted now in ms since the epoch (§5.4), or None."""
        if self._trusted_now is not None:
            return self._trusted_now()
        try:
            ok = bool(self.clock_ok())
        except Exception:  # noqa: BLE001 — a probe failure: not trusted
            ok = False
        return int(self.now_ms()) if ok else None

    def admit(self, raw: Union[bytes, str, dict]) -> Optional[dict]:
        with self._lock:
            # The node's trusted now (cheap: the backend's time, or a clock probe cached for a minute).
            now = self.trusted_now()
            verdict = verify_command(raw, self.token, self.state, clock_trusted=now is not None, now_ms=now)
            if verdict.accepted:
                self.accepted += 1
                return verdict.command
            self._refused(verdict)
            return None

    # ---- a refused message

    def _refused(self, v: Verdict) -> None:
        self.refused[v.reason] = self.refused.get(v.reason, 0) + 1
        if not self.token:
            # A node with no token can post nothing either (the ingress takes an ack only with the
            # node's token): it logs, loudly, at most once a minute.
            t = self.monotonic()
            if t - self._no_token_logged_at >= self.NO_TOKEN_LOG_EVERY_S:
                self._no_token_logged_at = t
                logger.error("command refused: no token: join this node again — commandId %s not carried out "
                             "(every message on the command topic is refused until the node has a token)",
                             v.command_id or "(none)")
            return
        # Only a message whose MAC verified (replayed, stale) has a type worth naming: the type of an
        # unsigned or forged one could be anyone's.
        verified_type = v.message.get("type") if v.message is not None else None
        what = f" {verified_type!s:.40}" if verified_type is not None else ""
        logger.warning("command refused: %s%s — commandId %s — %s (not carried out)",
                       v.reason, what, v.command_id or "(none)", v.detail)
        if v.reason in self.LOGGED_ONLY:
            return  # never posted (§6): the backend moves no command for it
        tracked = v.command_id is not None and TRACKED_COMMAND_ID.fullmatch(v.command_id) is not None
        firmware_msg = verified_type == "firmwareUpdate"
        # A firmwareUpdate refused by the TIME check is a real update arriving late (or before the node
        # had the time): the node's otaStatus must not stay "updating". One below the floor is a
        # capture replayed (v1.14.2 review): not reported.
        firmware = (v.reason == "stale" and v.window and firmware_msg and self.firmware_report is not None
                    and v.command_id not in self._firmware_reported)
        if not tracked and not firmware:
            return  # a prefixed id (sync_…, scan_bus_…, test_port_…) has no document; nothing to answer
        if not self._spend(v.reason):
            self.refusal_acks_withheld += 1
            logger.warning("command refused: %s — no answer posted (%d %s refusals answered in the last %d minutes)",
                           v.command_id, self.REFUSAL_ACKS_MAX.get(v.reason, 0), v.reason,
                           int(self.REFUSAL_ACKS_WINDOW_S // 60))
            return
        error = f"refused: {v.reason} — {v.detail}"
        if tracked and self.ack is not None:
            self.refusal_acks_sent += 1
            try:
                # A verified firmwareUpdate's id names a /nodeCommands doc, which commandAck does not
                # track: its ack stays on MQTT, as every firmwareUpdate ack does (main.py), and its
                # outcome goes as the firmware_update event below.
                self.ack(v.command_id, "failed", error=error, reason=v.reason, ingress=not firmware_msg)
            except Exception as e:  # noqa: BLE001 — answering never takes the MQTT loop down
                logger.error("command refused: %s — the failed ack could not be queued: %s", v.command_id, e)
        if firmware:
            self._firmware_reported.append(v.command_id)
            while len(self._firmware_reported) > self.FIRMWARE_REPORTED_MAX:
                self._firmware_reported.popleft()
            kwargs = {"command_id": v.command_id, "to_version": str(v.message.get("version") or "") or None,
                      "rollout_id": v.message.get("rolloutId") if isinstance(v.message.get("rolloutId"), str) else None,
                      "error": error}
            if self.firmware_report_async:
                threading.Thread(target=self._report_firmware, kwargs=kwargs, name="firmware-refused",
                                 daemon=True).start()
            else:
                self._report_firmware(**kwargs)

    def _report_firmware(self, **kwargs) -> None:
        try:
            self.firmware_report(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("command refused: the firmware_update failed report did not go out: %s", e)

    def _spend(self, reason: str) -> bool:
        """At most REFUSAL_ACKS_MAX[reason] answers in any REFUSAL_ACKS_WINDOW_S: a flood must not
        spend the node's ingress budget (1000 an hour, shared with everything it reports), and forged
        traffic (bad_signature) must not crowd out the stale answers a real command needs."""
        limit = self.REFUSAL_ACKS_MAX.get(reason, 0)
        window = self._refusal_acks.setdefault(reason, deque())
        now = self.monotonic()
        while window and now - window[0] >= self.REFUSAL_ACKS_WINDOW_S:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


def wire_command_gate(mqtt_client, http_reporter, *, token: str, state_dir: str, offline: bool,
                      clock_ok: Optional[Callable[[], bool]] = None,
                      server_time=None) -> Optional[CommandGate]:
    """Give the MQTT client its gate: every message on the command topic is checked before
    ``on_command`` (main.py calls this; tests/test_command_signing.py checks that it does). Its now
    is the backend's time from ``server_time`` (the reporter's, when none is given), else the wall
    clock when ``clock_ok()`` (§5.4). Offline (no MQTT) nothing is wired and None is returned."""
    if offline:
        return None

    def firmware_report(*, command_id, to_version, rollout_id, error):
        http_reporter.report_firmware_event(phase="failed", to_version=to_version, command_id=command_id,
                                            rollout_id=rollout_id, error=error)

    if clock_ok is None:
        from .blocks.clock import SystemClock
        clock_ok = SystemClock().clock_ok
    if server_time is None:
        server_time = getattr(http_reporter, "server_time", None)
    from .server_time import ServerTime, trusted_now
    gate = CommandGate(token, ReplayStore(state_dir) if token else ReplayState(),
                       ack=mqtt_client.publish_command_ack, firmware_report=firmware_report, clock_ok=clock_ok,
                       trusted_now=trusted_now(server_time if isinstance(server_time, ServerTime) else None, clock_ok))
    mqtt_client.set_command_gate(gate)
    return gate
