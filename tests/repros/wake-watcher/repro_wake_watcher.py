#!/usr/bin/env python3
"""The wake watcher loop (#121): the hands-free entry point runs unattended
for hours and had no repro -- #41 (pw-record spawn storm on recorder EOF) and
the wake half of #48 were fixed blind.

ONE PROCESS PER CASE, like offline-handoff: a red must name its scenario, and
the watcher is a daemon thread started by the SERVICE CONSTRUCTOR, so each
process gets exactly one watcher and no case can leak a running thread into
the next.  The thread under test IS the production one (found by its
`wake-watcher` name) -- nothing here calls `_wake_watcher()` by hand, so the
wiring in `__init__` is covered too.

    python3 repro_wake_watcher.py A|B|C|D|E|F|G|H

  A  not-idle            armed, state=listening       -> zero Popen, zero detect
  B  recorder EOF        detect_stream -> None        -> one spawn per retry: ramp 0.5,1,2,4,8
                                                        then 10 s forever; ONE warning   #41 #137
  C  WyomingError        server unreachable           -> same ramp, then 60 s forever; ONE warning
  D  detection, idle     detect_stream -> name        -> start_listening(quick=True, wake=True) ONCE
  E  detection, raced    state left idle before verdict -> start_listening NOT called
  F  generic exception   detect_stream raises         -> proc.kill() + wait() in finally, ramp then 10
  G  first recorder dies, second healthy              -> armed < 2 s of fake time, NO warning   #137
  H  shutdown mid-stream shutdown() then recorder EOF -> thread exits: no sleep, no warning    #137
  I  extension absent   armed, idle, no Desktop name -> zero Popen, zero detect (master switch)

#137 in one line: every "recorder produced no audio (rc=1) (retrying every
10s)" in three days of journal was logged by the OLD pid 0.5 s into
`Stopping` -- systemd's control-group SIGTERM ends pw-record, detect_stream
waits its 0.5 s for a verdict -- and was misread as the NEW pid failing at
start. H is that observation; G is the start-up window the journal DOES show
(login: the LAN name not resolving yet, then a 60 s sleep). The ramp is
bounded and resets on a healthy stream, so the steady cadence #41 set is
unchanged -- B and C assert it is still reached and then held.

Every case that spawns also asserts the recorder was killed: pw-record ignores
SIGTERM and a watcher that leaks one per cycle is a slower #41.

Seams, all in-process (no audio, no network, no daemon):
  * time     -> a shim whose sleep() RECORDS the call, waits ~0 s, and after the
               case's budget raises _StopWatcher (a BaseException, so the
               watcher's `except Exception` cannot swallow it and `finally`
               still runs).  Every other attribute delegates to the real module.
               SCOPED TO THE WATCHER THREAD by identity: the service module has
               ONE `time`, and the constructor also starts `tts-queue-dispatcher`,
               which calls time.sleep(0.2) whenever current_state is not idle --
               exactly what A, D and E set.  An unscoped shim recorded those
               (29-30 spurious 0.2 s entries per 250 ms window, measured) and
               killed the dispatcher with _StopWatcher; the shipped windows
               (6-12 ms) merely fit inside the dispatcher's first real
               _tts_queue.get(timeout=0.2).  Any other thread gets the real
               time.sleep and is neither recorded nor stopped.
  * subprocess -> a namespace whose Popen returns a kill-tracking fake built on
               dead-recorder/fakes.py; PIPE/DEVNULL are the real constants.
  * wyoming_mod.detect_stream -> per-case fake; WyomingError is the REAL class.
  * GLib.idle_add -> runs the callback inline (there is no main loop here).
  * svc.start_listening -> records kwargs, flips state to listening, like the
               real one would from the main loop.

Baselines. Pre-#41: 11c8f60 (= 22cc2f3^) -- case B is a spawn storm there
(detect_stream returns None and the loop respawns pw-record with no sleep
between), so B must FAIL against it.  Pre-#137: a20afea -- G (second spawn
after 10 s, not 0.5) and H (a warning and a 10 s sleep during shutdown) must
FAIL against it; B, C and F also go red there because they assert the ramp.
A, D, E are regression guards and are expected green on every side.

exit 0 = clean, 1 = the defect is present, 2 = setup failure (the watcher
thread never appeared or never stopped -- neither a pass nor a bug).
"""
import logging
import os
import subprocess
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "dead-recorder"))

import fakes    # noqa: E402
import harness  # noqa: E402

WAKE = dict(wake_word=True, wyoming_host="wake.test", wyoming_wake_port=10400,
            wake_word_model="test_model")
SPAWN_CAP = 25          # a pre-#41 storm hits this in well under a second
JOIN_TIMEOUT = 5.0


class _StopWatcher(BaseException):
    """Ends the infinite loop from inside a fake sleep. BaseException on
    purpose: the watcher has a bare `except Exception` that would otherwise
    turn this into a logged error plus another sleep."""


def _quiet_stop(args, _prev=threading.excepthook):
    """_StopWatcher is how a case ENDS the thread, not an error: keep it out of
    stderr so a real traceback from the watcher is not buried under six
    expected ones."""
    if args.exc_type is not _StopWatcher:
        _prev(args)


threading.excepthook = _quiet_stop


class FakeTime:
    """time module shim: sleep() is recorded and near-instant -- on the
    watcher thread ONLY. Every other caller gets the real time.sleep."""

    def __init__(self):
        self.sleeps = []            # every sleep() since arm()
        self.armed = False
        self.stop_after = None      # number of recorded sleeps before stopping
        self.thread = None          # the one thread whose sleeps are ours

    def sleep(self, seconds):
        if threading.current_thread() is not self.thread:
            time.sleep(seconds)     # not under test: behave like the real module
            return
        if self.armed:
            self.sleeps.append(seconds)
            if self.stop_after is not None and len(self.sleeps) >= self.stop_after:
                raise _StopWatcher()
        time.sleep(0.002)           # yield, never spin

    def __getattr__(self, name):
        return getattr(time, name)


class WakeLogs(logging.Handler):
    """Collects the watcher's WARNING+ lines: a transient failure that self-heals
    must not promise a retry, and shutdown must not either."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        msg = record.getMessage()
        if "Wake watcher" in msg:
            self.records.append(msg)


RAMP = [0.5, 1.0, 2.0, 4.0, 8.0]     # the bounded start-up ramp (#137)


class KilledDeadProc(fakes.DeadProc):
    """A recorder whose stdout is at EOF -- and that remembers being killed."""

    def __init__(self):
        super().__init__()
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 1


class KilledLiveProc(fakes.LiveProc):
    """A healthy recorder that remembers being killed."""

    def __init__(self):
        super().__init__()
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


class Rig:
    def __init__(self, proc_factory):
        self.mod, _events, _inj = harness.load()
        self.ftime = FakeTime()
        self.procs = []
        self.spawn_at = []          # fake seconds slept before each spawn
        self.detects = 0
        self.starts = []
        self.detect = lambda host, port, model, chunks: None
        self.logs = WakeLogs()
        self.mod.log.addHandler(self.logs)

        def popen(*a, **k):
            if len(self.procs) >= SPAWN_CAP:
                raise _StopWatcher()
            p = proc_factory()
            self.spawn_at.append(sum(self.ftime.sleeps))
            self.procs.append(p)
            return p

        def detect_stream(host, port, model, chunks):
            self.detects += 1
            return self.detect(host, port, model, chunks)

        m = self.mod
        m.time = self.ftime
        m.subprocess = types.SimpleNamespace(
            Popen=popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
        m._build_rec_cmd = lambda *a, **k: ["pw-record", "--fake"]
        m.wyoming_mod.detect_stream = detect_stream
        m.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

        self.svc = harness.make_service(m)       # starts the REAL watcher thread
        self.thread = next((t for t in threading.enumerate()
                            if t.name == "wake-watcher"), None)
        self.ftime.thread = self.thread      # scope the fake sleep to it
        self.dispatcher = next((t for t in threading.enumerate()
                                if t.name == "tts-queue-dispatcher"), None)

        def start_listening(quick=False, wake=False):
            self.starts.append(dict(quick=quick, wake=wake))
            self.set_state("listening")
            return "ok"
        self.svc.start_listening = start_listening

    def set_state(self, st):
        # current_state is a read-only property; the watcher polls it. Written
        # under the service's own lock, bypassing _set_state so no signal is
        # emitted into a main loop that does not exist here.
        with self.svc._state_lock:
            self.svc._state = st

    def arm(self, stop_after):
        """Enable the wake word and let the watcher run until `stop_after`
        sleeps have been recorded (or SPAWN_CAP spawns). Returns False if the
        thread never stopped -- a setup failure, not a verdict."""
        if self.thread is None:
            return False
        self.ftime.stop_after = stop_after
        self.ftime.armed = True
        self.mod.CONFIG.update(WAKE)
        self.thread.join(JOIN_TIMEOUT)
        return not self.thread.is_alive()


def report(label, checks):
    failures = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"    {'ok  ' if ok else 'FAIL'}: {msg}")
    if failures:
        print(f"[FAIL] {label}: {len(failures)} check(s) failed")
        return 1
    print(f"[PASS] {label}")
    return 0


def case_a():
    """Not idle: the watcher must never touch the recorder or the server.

    The window is deliberately LONG (~240 ms, past the dispatcher's first
    real 0.2 s queue timeout): while state is `listening` the dispatcher
    thread sleeps 0.2 s per hold-poll through the same module `time`, and
    this case doubles as the harness's own guard that only the watcher's
    sleeps are recorded and only the watcher is stopped."""
    stop_after = 120
    rig = Rig(KilledLiveProc)
    rig.set_state("listening")
    if not rig.arm(stop_after=stop_after):
        return 2
    spurious = [s for s in rig.ftime.sleeps if s != 0.5]
    disp_alive = rig.dispatcher is not None and rig.dispatcher.is_alive()
    print(f"  sleeps={len(rig.ftime.sleeps)} spurious={spurious} spawns={len(rig.procs)} "
          f"detects={rig.detects} dispatcher_alive={disp_alive}")
    return report("A not-idle -> zero spawns", [
        (len(rig.procs) == 0, "no pw-record spawned while not idle"),
        (rig.detects == 0, "detect_stream never called while not idle"),
        (all(s == 0.5 for s in rig.ftime.sleeps),
         f"idle poll is the 0.5 s tick (spurious: {spurious})"),
        (len(rig.ftime.sleeps) == stop_after,
         f"exactly {stop_after} sleeps recorded -- only the watcher's (got {len(rig.ftime.sleeps)})"),
        (disp_alive, "tts-queue-dispatcher survived the armed window (not stopped by the shim)"),
    ])


def _drain(host, port, model, chunks):
    for _ in chunks:      # the real client consumes until EOF
        pass
    return None


def case_b():
    """Recorder EOF (#41, #137): one spawn per retry -- the bounded ramp, then
    the 10 s steady cadence held for good, one WARNING and only at steady
    state. Never a storm."""
    rig = Rig(KilledDeadProc)
    rig.detect = _drain
    stop_after = len(RAMP) + 2
    if not rig.arm(stop_after=stop_after):
        return 2
    want = RAMP + [10, 10]
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} detects={rig.detects} "
          f"warnings={rig.logs.records}")
    return report("B recorder EOF -> ramp, then 10 s backoff (#41)", [
        (len(rig.procs) == stop_after,
         f"one recorder per retry, {stop_after} in all (got {len(rig.procs)}"
         f"{' -- SPAWN STORM' if len(rig.procs) >= SPAWN_CAP else ''})"),
        (rig.ftime.sleeps == want, f"sleeps are {want} (got {rig.ftime.sleeps})"),
        (all(p.killed for p in rig.procs), "every spawned recorder was killed"),
        (not rig.starts, "no session opened on EOF"),
        (len(rig.logs.records) == 1 and "retrying every 10s" in rig.logs.records[0],
         f"exactly one WARNING, at steady state, naming the 10 s cadence (got {rig.logs.records})"),
    ])


def case_c():
    """Wake server unreachable: the same bounded ramp, then 60 s held for good
    -- a permanently missing server still costs one connect per minute."""
    rig = Rig(KilledLiveProc)
    err = rig.mod.wyoming_mod.WyomingError

    def unreachable(host, port, model, chunks):
        raise err("connection refused")
    rig.detect = unreachable
    stop_after = len(RAMP) + 2
    if not rig.arm(stop_after=stop_after):
        return 2
    want = RAMP + [60, 60]
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} detects={rig.detects} "
          f"warnings={rig.logs.records}")
    return report("C WyomingError -> ramp, then 60 s backoff", [
        (len(rig.procs) == stop_after, f"one recorder per attempt (got {len(rig.procs)})"),
        (rig.ftime.sleeps == want, f"sleeps are {want} (got {rig.ftime.sleeps})"),
        (all(p.killed for p in rig.procs), "recorder killed after every error"),
        (not rig.starts, "no session opened on a server error"),
        (len(rig.logs.records) == 1 and "retrying every 60s" in rig.logs.records[0],
         f"exactly one WARNING, at steady state, naming the 60 s cadence (got {rig.logs.records})"),
    ])


def case_d():
    """Detection while idle: exactly one start_listening(quick=True, wake=True)."""
    rig = Rig(KilledLiveProc)

    def hear(host, port, model, chunks):
        next(iter(chunks))          # pull one frame, then the server answers
        return "test_model"
    rig.detect = hear
    if not rig.arm(stop_after=4):
        return 2
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} starts={rig.starts}")
    return report("D detection while idle -> start_listening once", [
        (rig.starts == [dict(quick=True, wake=True)],
         f"start_listening(quick=True, wake=True) called exactly once (got {rig.starts})"),
        (rig.ftime.sleeps[:1] == [2.0], f"cooldown sleep follows (got {rig.ftime.sleeps[:1]})"),
        (len(rig.procs) == 1, f"one recorder for the detection (got {len(rig.procs)})"),
        (all(p.killed for p in rig.procs), "recorder killed after handing off"),
    ])


def case_e():
    """Detection that lands after the state changed (a hotkey won the race):
    the verdict is stale and must not open a second session."""
    rig = Rig(KilledLiveProc)

    def hear_late(host, port, model, chunks):
        next(iter(chunks))
        rig.set_state("listening")  # the user pressed the hotkey meanwhile
        return "test_model"
    rig.detect = hear_late
    if not rig.arm(stop_after=3):
        return 2
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} starts={rig.starts}")
    return report("E detection after state changed -> not called", [
        (rig.starts == [], f"start_listening not called on a stale detection (got {rig.starts})"),
        (rig.detects == 1, f"one detection round (got {rig.detects})"),
        (all(p.killed for p in rig.procs), "recorder killed"),
    ])


def case_f():
    """Any other failure: finally kills (SIGKILL, pw-record ignores SIGTERM)
    and waits on the recorder, then backs off 10 s."""
    rig = Rig(KilledLiveProc)

    def boom(host, port, model, chunks):
        raise RuntimeError("unexpected")
    rig.detect = boom
    stop_after = len(RAMP) + 1
    if not rig.arm(stop_after=stop_after):
        return 2
    want = RAMP + [10]
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} "
          f"killed={[p.killed for p in rig.procs]} waited={[p.waited for p in rig.procs]}")
    return report("F generic exception -> finally kills proc", [
        (len(rig.procs) == stop_after, f"a recorder per attempt (got {len(rig.procs)})"),
        (rig.procs and all(p.killed for p in rig.procs), "proc.kill() ran in finally, every time"),
        (rig.procs and all(p.waited for p in rig.procs), "proc.wait() ran in finally, every time"),
        (rig.ftime.sleeps == want, f"generic backoff is the ramp then 10 s (got {rig.ftime.sleeps})"),
    ])


def case_g():
    """#137: the first recorder dies the moment it starts (PipeWire not ready,
    device not enumerated yet), the second is healthy and hears the wake word.
    The watcher must be armed again within 2 s of fake time -- on a20afea it
    slept 10 s first -- and a failure that healed itself is no WARNING."""
    holder = {}

    def factory():
        return KilledDeadProc() if not holder["rig"].procs else KilledLiveProc()
    rig = holder["rig"] = Rig(factory)

    def second_time_lucky(host, port, model, chunks):
        if rig.detects == 1:
            return _drain(host, port, model, chunks)
        next(iter(chunks))          # healthy recorder: one frame, then a verdict
        return "test_model"
    rig.detect = second_time_lucky
    if not rig.arm(stop_after=2):   # the retry sleep, then the 2 s cooldown
        return 2
    armed_at = rig.spawn_at[1] if len(rig.spawn_at) > 1 else None
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} spawn_at={rig.spawn_at} "
          f"starts={rig.starts} warnings={rig.logs.records}")
    print(f"  second recorder started after {armed_at} s of fake time")
    return report("G first recorder dies, second healthy -> armed < 2 s (#137)", [
        (len(rig.procs) == 2, f"two recorders: the dead one and its replacement (got {len(rig.procs)})"),
        (armed_at is not None and armed_at < 2.0,
         f"re-armed within 2 s of fake time (got {armed_at} s)"),
        (rig.starts == [dict(quick=True, wake=True)],
         f"the replacement heard the wake word and opened the mic once (got {rig.starts})"),
        (all(p.killed for p in rig.procs), "both recorders killed"),
        (rig.logs.records == [],
         f"a self-healed first failure logs no WARNING (got {rig.logs.records})"),
    ])


def case_h():
    """#137's actual observation, reproduced: shutdown() runs while the watcher
    is streaming, then the recorder hits EOF (systemd's control-group SIGTERM
    ends pw-record at the same instant). The thread must exit -- no 10 s sleep,
    no "retrying every 10s" promised by a process that is leaving."""
    rig = Rig(KilledDeadProc)

    def sigterm_mid_stream(host, port, model, chunks):
        try:
            rig.svc.shutdown()      # the real one: sets the flag first
        except Exception as e:      # harness stubs may not like the rest of it
            print(f"  note: shutdown() raised inside the harness: {e!r}")
        return _drain(host, port, model, chunks)
    rig.detect = sigterm_mid_stream
    if not rig.arm(stop_after=1):
        return 2
    flag = getattr(rig.svc, "_shutting_down", None)
    print(f"  sleeps={rig.ftime.sleeps} spawns={len(rig.procs)} flag={flag} "
          f"warnings={rig.logs.records} thread_alive={rig.thread.is_alive()}")
    return report("H shutdown mid-stream -> watcher exits quietly (#137)", [
        (flag is True, f"shutdown() raised _shutting_down before tearing down (got {flag})"),
        (rig.ftime.sleeps == [], f"no retry sleep during shutdown (got {rig.ftime.sleeps})"),
        (rig.logs.records == [], f"no WARNING promising a retry (got {rig.logs.records})"),
        (len(rig.procs) == 1 and all(p.killed for p in rig.procs), "the one recorder was killed"),
        (not rig.thread.is_alive(), "the watcher thread returned on its own"),
    ])


def case_i():
    """The extension is the master switch (JP, 2026-09-10): armed and idle but
    with no `org.gnome.Speaks.Desktop` owner, the watcher must not even spawn a
    recorder -- on 2026-09-10 the wake model false-fired twice in 13 minutes
    with the extension disabled and typed a phone call through ydotool. A tree
    without the gate spawns and streams here exactly as in case D."""
    stop_after = 40
    rig = Rig(KilledLiveProc)
    rig.set_state("idle")
    rig.mod.REQUIRE_EXTENSION = True
    rig.mod._extension_present = False
    rig.detect = lambda host, port, model, chunks: "test_model"
    if not rig.arm(stop_after=stop_after):
        return 2
    print(f"  sleeps={len(rig.ftime.sleeps)} spawns={len(rig.procs)} "
          f"detects={rig.detects} starts={rig.starts}")
    return report("I extension absent -> parked, zero spawns", [
        (len(rig.procs) == 0, f"no pw-record spawned while the extension is absent (got {len(rig.procs)})"),
        (rig.detects == 0, f"detect_stream never called (got {rig.detects})"),
        (rig.starts == [], f"start_listening never called (got {rig.starts})"),
        (all(s == 0.5 for s in rig.ftime.sleeps),
         "parked on the 0.5 s idle tick"),
    ])


CASES = {"A": case_a, "B": case_b, "C": case_c, "D": case_d, "E": case_e, "F": case_f,
         "G": case_g, "H": case_h, "I": case_i}


def main():
    which = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    if which not in CASES:
        print(f"usage: {sys.argv[0]} {'|'.join(CASES)}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")
    rc = CASES[which]()
    if rc == 2:
        print("!! SETUP FAILURE: wake-watcher thread not found or never stopped")
    return rc


if __name__ == "__main__":
    sys.exit(main())
