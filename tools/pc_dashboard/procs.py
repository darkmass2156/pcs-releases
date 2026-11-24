"""Per-process CPU use, stdlib only (ctypes, NtQuerySystemInformation / SystemProcessInformation).

One call returns every process with its user + kernel time; two snapshots a second apart give the CPU each used.
100 % = one logical core flat out (a single-thread hog shows as ~100 %, all 24 cores = 2400 %).
Read-only, no handles opened, ~2 ms per call. `py procs.py` prints the top 10 for 5 s.
"""
import ctypes
import time

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_SPI = 5                     # SystemProcessInformation
_MISMATCH = 0xC0000004       # STATUS_INFO_LENGTH_MISMATCH


def _snapshot(size=[1 << 20]):
    """{pid: (name, cpu_100ns)} for every process."""
    while True:
        buf = ctypes.create_string_buffer(size[0])
        ret = ctypes.c_ulong(0)
        st = _ntdll.NtQuerySystemInformation(_SPI, buf, size[0], ctypes.byref(ret))
        if (st & 0xFFFFFFFF) == _MISMATCH:
            size[0] = max(size[0] * 2, ret.value + 65536)
            continue
        if st != 0:
            return {}
        break
    raw = buf.raw
    base = ctypes.addressof(buf)
    out, off = {}, 0
    u32 = lambda o: int.from_bytes(raw[o:o + 4], "little")
    u64 = lambda o: int.from_bytes(raw[o:o + 8], "little")
    while True:
        nxt = u32(off)
        user, kern = u64(off + 40), u64(off + 48)
        nlen, nbuf = int.from_bytes(raw[off + 56:off + 58], "little"), u64(off + 64)
        pid = u64(off + 80)
        name = "Idle"
        if nbuf and nlen:
            p = nbuf - base
            if 0 <= p < len(raw) - nlen:
                name = raw[p:p + nlen].decode("utf-16le", "replace")
        out[pid] = (name, user + kern)
        if not nxt:
            break
        off += nxt
    return out


class Sampler:
    """Call top() about once a second; returns [(name, pct_of_one_core), ...] busiest first since the last call."""

    def __init__(self):
        self.prev, self.t = None, None

    def top(self, n=5, min_pct=2.0):
        try:
            now, t = _snapshot(), time.perf_counter()
        except Exception:
            return []
        prev, pt = self.prev, self.t
        self.prev, self.t = now, t
        if not prev or t <= pt:
            return []
        span = (t - pt) * 1e7                                 # 100 ns units
        use = {}
        for pid, (name, cpu) in now.items():
            if pid == 0 or pid not in prev or prev[pid][0] != name:
                continue                                      # Idle, new or reused pid
            d = cpu - prev[pid][1]
            if d > 0:
                use[(name, pid)] = 100.0 * d / span
        top = sorted(use.items(), key=lambda kv: -kv[1])[:n]
        return [(name, round(p)) for (name, _), p in top if p >= min_pct]


def fmt(top):
    """'MsMpEng.exe:98|chrome.exe:12' (the flight-log column)."""
    return "|".join(f"{n.replace('|', '/').replace(',', ' ')}:{p}" for n, p in top)


def parse(s):
    out = []
    for part in (s or "").split("|"):
        name, _, p = part.rpartition(":")
        if name:
            try:
                out.append((name, int(p)))
            except ValueError:
                pass
    return out


if __name__ == "__main__":
    s = Sampler()
    s.top()
    for _ in range(5):
        time.sleep(1)
        print(fmt(s.top(10)) or "(nothing above 2 %)")
