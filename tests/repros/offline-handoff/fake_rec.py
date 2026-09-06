"""Synthetic recorder: 30 ms / 16 kHz / s16le frames emitted at REAL TIME.

Models pw-record honestly: content is a function of WALL-CLOCK capture time,
so when the pipe fills and this writer blocks, the audio of that interval is
genuinely gone -- exactly what a real mic recorder does.

sample[0] carries the capture frame index (small magnitude, ~45 rms, well
under the 300 floor) so a consumer can reconstruct the capture timeline and
detect dropouts.
"""
import math
import os
import struct
import sys
import time

FRAME_BYTES = 960
FRAME_SAMPLES = 480
FRAME_S = 0.030
SPEECH_MS = int(os.environ.get("FAKE_REC_SPEECH_MS", "4000"))
# Simulates the USB mic being yanked: pw-record exits, stdout hits EOF.
EXIT_MS = int(os.environ.get("FAKE_REC_EXIT_MS", "0"))

_speech = []
for i in range(FRAME_SAMPLES):
    t = i / 16000.0
    v = (6000 * math.sin(2 * math.pi * 130 * t)
         + 3000 * math.sin(2 * math.pi * 260 * t)
         + 1500 * math.sin(2 * math.pi * 520 * t)
         + 900 * math.sin(2 * math.pi * 1300 * t))
    _speech.append(int(max(-32000, min(32000, v))))
_silence = [0] * FRAME_SAMPLES

out = sys.stdout.buffer
t0 = time.monotonic()
n = 0
while True:
    target = t0 + n * FRAME_S
    now = time.monotonic()
    if now < target:
        time.sleep(target - now)
    elapsed = time.monotonic() - t0
    if EXIT_MS and elapsed * 1000 >= EXIT_MS:
        sys.exit(0)
    idx = int(elapsed / FRAME_S)            # capture index, NOT write index
    body = _speech if elapsed * 1000 < SPEECH_MS else _silence
    frame = [min(idx, 32000)] + body[1:]
    out.write(struct.pack('<480h', *frame))
    out.flush()
    n += 1
