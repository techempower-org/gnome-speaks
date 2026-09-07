#!/usr/bin/env python3
"""repro: the stuck-key reset restarts the unit that OWNS the live ydotoold (#102).

Two units can exist (packaged `ydotool.service`, fix-ydotool.sh's
`ydotoold.service`); restarting the wrong one spawns a second daemon that dies
with "Another ydotoold is running with the same socket" and the stuck key is
never cleared. This builds a fake /proc with one ydotoold owned by
`ydotool.service` and asserts the reset targets THAT unit.

  U1  fake /proc: ydotoold owned by user unit ydotool.service -> detected
  U2  reset runs `systemctl --user restart ydotool.service`, not ydotoold
  U3  daemon owned by a SYSTEM unit -> no systemctl call, one warning
  U4  no daemon -> `start` (not restart) of the first existing candidate

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c5_ydotoold_unit.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
_STATE = os.path.join(SCRATCH_ROOT, f"service-audit-ydotool-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_ydo", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_ydo"] = mod
    spec.loader.exec_module(mod)
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    return mod


def fake_proc(unit, scope="user"):
    root = tempfile.mkdtemp(prefix="proc-", dir=os.path.dirname(_STATE))
    # a non-ydotoold process, to prove the scan filters by executable
    os.makedirs(os.path.join(root, "100"))
    os.symlink("/usr/bin/bash", os.path.join(root, "100", "exe"))
    open(os.path.join(root, "100", "cgroup"), "w").write("0::/user.slice/user-1000.slice/user@1000.service/app.slice/foo.service\n")
    if unit:
        os.makedirs(os.path.join(root, "200"))
        os.symlink("/usr/bin/ydotoold", os.path.join(root, "200", "exe"))
        path = (f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"
                if scope == "user" else f"0::/system.slice/{unit}\n")
        open(os.path.join(root, "200", "cgroup"), "w").write(path)
    return root


def main():
    if not os.path.isfile(SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {SVC_PATH}")
        return 2
    mod = load()
    if not hasattr(mod, "_reset_ydotoold"):
        print("!! SETUP FAILURE: service has no _reset_ydotoold")
        return 2
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R: returncode = 0
        return R()
    mod.subprocess.run = fake_run
    mod.time.sleep = lambda s: None
    mod._YDOTOOL_V1 = True

    detect = getattr(mod, "_ydotoold_unit", None)
    proc = fake_proc("ydotool.service")
    got = detect(proc) if detect else None
    check("U1", got == ("user", "ydotool.service"), f"detected {got!r}")

    if detect:
        mod._ydotoold_unit = lambda proc_root="/proc", _p=proc: detect(_p)
    calls.clear()
    mod._reset_ydotoold()
    restarted = [c for c in calls if "restart" in c or "start" in c]
    check("U2", restarted == [["systemctl", "--user", "restart", "ydotool.service"]],
          f"systemctl calls: {restarted}")

    if detect:
        proc_sys = fake_proc("ydotool.service", scope="system")
        mod._ydotoold_unit = lambda proc_root="/proc", _p=proc_sys: detect(_p)
        calls.clear()
        mod._reset_ydotoold()
        check("U3", calls == [], f"system-owned daemon must not be restarted from user scope: {calls}")

        proc_none = fake_proc(None)
        mod._ydotoold_unit = lambda proc_root="/proc", _p=proc_none: detect(_p)
        calls.clear()
        mod._reset_ydotoold()
        starts = [c for c in calls if c[:3] == ["systemctl", "--user", "start"]]
        cats = [c for c in calls if c[:3] == ["systemctl", "--user", "cat"]]
        check("U4", len(starts) == 1 and starts[0][3] == "ydotool.service" and cats,
              f"no daemon -> probe {len(cats)} unit(s), start {starts}")
    else:
        check("U3", False, "no _ydotoold_unit seam"); check("U4", False, "no _ydotoold_unit seam")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- the reset does not target the unit that owns ydotoold")
        return 1
    print("PASS: the stuck-key reset restarts the unit that owns the live ydotoold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
