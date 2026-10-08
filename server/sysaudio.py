"""Native computer-audio capture via the ScreenCaptureKit helper (native/syscap)."""
import logging
import subprocess
import threading
from pathlib import Path

import numpy as np

HELPER = Path(__file__).resolve().parent.parent / "native" / "syscap"
SR = 16000
log = logging.getLogger("murmur")


def available() -> bool:
    return HELPER.exists()


class NativeSystemAudio:
    """Runs the helper and hands out computer audio aligned to the mic stream.

    The mic (from the browser) is the master clock: for every n mic samples we take n computer
    samples. If the helper is momentarily behind we pad with silence; if it gets ahead (clock
    drift, bursty delivery) we drop the oldest excess so the channels stay in sync.
    """
    MAX_LEAD = SR // 2

    def __init__(self):
        self.buf = np.zeros(0, np.float32)
        self.lock = threading.Lock()
        self.error = ""
        self.proc = subprocess.Popen([str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        while True:
            data = self.proc.stdout.read(6400)
            if not data:
                break
            a = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.float32)
            with self.lock:
                self.buf = np.concatenate([self.buf, a])

    def _read_err(self):
        for line in self.proc.stderr:
            msg = line.decode(errors="replace").strip()
            log.info(msg)
            if "failed" in msg or "grant" in msg or "stopped" in msg:
                self.error = (self.error + " " + msg).strip()

    def take(self, n: int) -> np.ndarray:
        with self.lock:
            if len(self.buf) > n + self.MAX_LEAD:
                self.buf = self.buf[len(self.buf) - n - self.MAX_LEAD:]
            out, self.buf = self.buf[:n], self.buf[n:]
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out), np.float32)])
        return out

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
