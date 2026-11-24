"""Decode WHEA CPER records (UEFI spec appendix N) from the RawData of WHEA-Logger events (2026-11-22, owner: the
"informational" ID 3 events). Returns a short description: notification type (boot record = an error from before the
last reboot, kept by the firmware), section types, and for processor / machine-check sections the APIC ID, bank and
MCi_STATUS where present.  py cper.py <hexfile with lines time|hex>  prints them."""
import struct
import uuid

NOTIFY = {
    "2dce8bb1-bdd7-450e-b9ad-9cf4ebd4f890": "CMC (corrected machine check)",
    "4e292f96-d843-4a55-a8c2-d481f27ebeee": "CPE",
    "e8f56ffe-919c-4cc5-ba88-65abe14913bb": "MCE (machine check)",
    "cf93c01f-1a16-4dfc-b8bc-9c4daf67c104": "PCIe",
    "cc5263e8-9308-454a-89d0-340bd39bc98e": "INIT",
    "5bad89ff-b7e6-42c9-814a-cf2485d6e98a": "NMI",
    "3d61a466-ab40-409a-a698-f362d464b38f": "BOOT (error kept from before the last reboot)",
    "667dd791-c6b3-4c27-8a6b-0f8e722deb41": "DMAr",
    "09a9d5ac-5204-4214-96e5-94992e752bcd": "SEA",
    "5c284c81-b0ae-4e87-a322-b04c85624323": "SEI",
}
SECTION = {
    "9876ccad-47b4-4bdb-b65e-16f193c4f3db": "processor generic",
    "dc3ea0b0-a144-4797-b95b-53fa242b6e1d": "IA32/x64 processor",
    "a5bc1114-6f64-4ede-b863-3e83ed7c83b1": "memory",
    "d995e954-bbc1-430f-ad91-b44dcb3c6f35": "PCIe",
    "c5753963-3b84-4095-bf78-eddad3f9c9dd": "PCI/PCI-X bus",
    "81212a96-09ed-4996-9471-8d729c8e69ed": "firmware error record reference",
    "8a1e1d01-42f9-4557-9c33-565e5cce6d8d": "Microsoft: generic (WHEA)",
    "390f56d5-ca86-4649-95c4-73a408ae5834": "Microsoft: MCA / machine check",   # WHEA_XPF_MCA_SECTION
    "e71254e8-c1b9-4940-ab76-909703a4320f": "Microsoft: recovery",
    "e5ae0bb8-89ad-4ba9-9a85-55df27c5958b": "Microsoft: error record header?",
}
SEV = {0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational"}


def _g(b, o):
    return str(uuid.UUID(bytes_le=bytes(b[o:o + 16])))


def decode(raw):
    b = bytes.fromhex(raw) if isinstance(raw, str) else raw
    if len(b) < 128 or b[:4] != b"CPER":
        return {"ok": False, "why": "not a CPER record"}
    nsec = struct.unpack_from("<H", b, 10)[0]
    sev = struct.unpack_from("<I", b, 12)[0]
    ts = b[24:32]
    # header: 32 platform id, 48 partition id, 64 creator id, 80 notification type, 96 record id
    out = {"ok": True, "severity": SEV.get(sev, sev), "notify": NOTIFY.get(_g(b, 80), _g(b, 80)),
           "boot": _g(b, 80) == "3d61a466-ab40-409a-a698-f362d464b38f", "time": None, "sections": []}
    if ts[5]:                                            # sec min hour flags day month year century
        out["time"] = "20%02d-%02d-%02d %02d:%02d:%02d" % (ts[6], ts[5], ts[4], ts[2], ts[1], ts[0])
    for k in range(nsec):
        d = 128 + 72 * k
        if d + 72 > len(b):
            break
        off, ln = struct.unpack_from("<II", b, d)
        st = _g(b, d + 16)
        ssev = struct.unpack_from("<I", b, d + 64)[0]
        sec = {"type": SECTION.get(st, st), "severity": SEV.get(ssev, ssev), "len": ln}
        body = b[off:off + ln]
        txt = body.decode("utf-16-le", "ignore")
        if "VenHw(" in txt:                              # a UEFI device path names the reporting firmware item
            sec["path"] = txt[txt.index("VenHw("):].split(")")[0] + ")"
        if st == "dc3ea0b0-a144-4797-b95b-53fa242b6e1d" and len(body) >= 64:   # IA32/x64 processor
            valid, apic = struct.unpack_from("<QQ", body, 0)
            sec["apic"] = apic if valid & 1 else None
        if st == "9876ccad-47b4-4bdb-b65e-16f193c4f3db" and len(body) >= 0x48:  # processor generic
            valid = struct.unpack_from("<Q", body, 0)[0]
            sec["ptype"] = body[8]
            sec["err_type"] = {0: "unknown", 1: "cache", 2: "TLB", 4: "bus", 8: "micro-arch"}.get(body[10], body[10])
            sec["op"] = body[11]
            if valid & (1 << 12):
                sec["apic"] = struct.unpack_from("<Q", body, 0x40)[0] if len(body) >= 0x48 else None
        if st == "390f56d5-ca86-4649-95c4-73a408ae5834" and len(body) >= 32:    # WHEA_XPF_MCA_SECTION
            ver, cpu, bank = struct.unpack_from("<III", body, 0)
            status = struct.unpack_from("<Q", body, 16)[0] if len(body) >= 24 else 0
            sec.update({"cpu": cpu, "bank": bank, "status": "0x%016X" % status,
                        "corrected": not bool(status >> 61 & 1), "valid": bool(status >> 63)})
        out["sections"].append(sec)
    return out


def describe(raw):
    r = decode(raw)
    if not r["ok"]:
        return r["why"]
    parts = [f"{r['severity']}, {r['notify']}"]
    for s in r["sections"]:
        x = s["type"]
        if s.get("err_type"):
            x += " " + str(s["err_type"])
        if s.get("apic") is not None:
            x += f" APIC {s['apic']}"
        if s.get("path"):
            x += " " + s["path"]
        if "bank" in s:
            x += f" cpu {s['cpu']} bank {s['bank']} {s['status']}"
        parts.append(x)
    return "; ".join(parts)


if __name__ == "__main__":
    import sys
    for line in open(sys.argv[1], encoding="ascii", errors="ignore"):
        if "|" not in line:
            continue
        t, h = line.strip().split("|", 1)
        print(t, describe(h))
