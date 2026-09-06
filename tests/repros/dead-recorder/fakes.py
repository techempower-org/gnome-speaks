"""Shared fakes for the dead-recorder repros (issues #57 / #48).

A recorder that dies is modelled with the SAME shape the real one has:
pw-record keeps stdout open until it exits, so EOF and a non-None poll()
arrive together.  poll() reports alive until the first read, which is the
mic yanked while the session is live (a stale prewarm is a different case,
covered by repro_h).
"""
import time


class DeadStdout:
    def __init__(self, proc):
        self._proc = proc

    def read(self, n):
        self._proc.exited = True     # pw-record exits right after its EOF
        return b""


class DeadProc:
    """pw-record whose device vanished mid-session: stdout EOF, then exit."""

    def __init__(self, exited=False):
        self.exited = exited
        self.returncode = 1 if exited else None
        self.stdout = DeadStdout(self)

    def poll(self):
        self.returncode = 1 if self.exited else None
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 1


class LiveStdout:
    """Silence at 16 kHz s16: enough for the sender to never hit EOF."""

    def read(self, n):
        time.sleep(0.01)
        return b"\x00" * n


class LiveProc:
    """A recorder that is perfectly healthy — the negative control."""

    def __init__(self):
        self.returncode = None
        self.stdout = LiveStdout()

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class ScriptedWS:
    """WebSocket that replays a script of messages, then times out forever.

    delay models Azure answering AFTER the audio stream ends — which is the
    whole ordering problem: the recorder is already gone by the time the
    verdict arrives.
    """

    def __init__(self, timeout_exc, script=(), delay=0.15):
        self._exc = timeout_exc
        self._script = list(script)
        self._delay = delay

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        pass

    def recv(self):
        if self._script:
            time.sleep(self._delay)
            return self._script.pop(0)
        time.sleep(0.05)
        raise self._exc()


class LoopingWS(ScriptedWS):
    """Answers the same message forever — one utterance per cycle, quickly."""

    def recv(self):
        time.sleep(self._delay)
        return self._script[0]
