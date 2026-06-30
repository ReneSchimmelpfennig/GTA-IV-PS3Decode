#!/usr/bin/env python3
# slotcheck.py - find bank sounds that play as NOISE in SLOT mode, with runtime-faithful
# MP3 detection (forward sync scan, exactly like minimp3) and an optional diff against the
# ORIGINAL (pre-swap) RPF so we can tell a broken swap apart from an untouched ADPCM sound.
#
# The ASI at runtime reads the slot bytes and lets minimp3 SCAN for the first frame sync.
# So a frame a few bytes into the slot still decodes fine. A sound only plays as NOISE when:
#   - it was SWAPPED to MP3 (bytes differ from the original ADPCM), AND
#   - no full MPEG frame can be found in its slot -> the ASI falls back to the ADPCM
#     decoder and runs MP3 bytes through it -> noise.
# An untouched ADPCM sound also has "no MPEG frame", but it decodes correctly as ADPCM.
# The original-RPF diff is what separates these two.
#
# Usage:
#   py slotcheck.py <GTAIV.exe> <converted.rpf> [original.rpf] [name-filter]
#   (3rd arg is treated as the original RPF if it ends in .rpf, otherwise as a name filter)
#
# Needs rpf3.py + bank_swap.py + hashes.txt + pycryptodome in the same folder.

import sys, struct
from rpf3 import extract_aes_key, rpf3_read
from bank_swap import parse_bank, frame_len

SCAN_CAP   = 0x4000   # how far into a slot we hunt for the first frame sync
DIFF_BYTES = 64       # leading bytes compared against the original to decide "swapped?"

# --- self-contained MPEG-1/2/2.5 Layer III header parser (for chain integrity) ---
_BR = {  # bitrate tables (kbps) by (version_is_mpeg1, layer3) -> index list
    1: [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0],   # MPEG1 L3
    0: [0, 8,16,24,32,40,48,56, 64, 80, 96,112,128,144,160,0],   # MPEG2/2.5 L3
}
_SR = {3:[44100,48000,32000,0], 2:[22050,24000,16000,0], 0:[11025,12000,8000,0]}  # by version bits

def parse_hdr(d, pos):
    """Return (frame_len_bytes, samplerate, channels, samples) for a Layer III frame at pos, else None."""
    if pos + 4 > len(d):
        return None
    b0, b1, b2, b3 = d[pos], d[pos+1], d[pos+2], d[pos+3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:        # 11-bit frame sync
        return None
    ver = (b1 >> 3) & 3                           # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reserved
    layer = (b1 >> 1) & 3                         # 1 = Layer III
    if ver == 1 or layer != 1:
        return None
    br_i = (b2 >> 4) & 0xF
    sr_i = (b2 >> 2) & 3
    pad  = (b2 >> 1) & 1
    if br_i == 0 or br_i == 15 or sr_i == 3:
        return None
    is_mpeg1 = (ver == 3)
    bitrate = _BR[1 if is_mpeg1 else 0][br_i] * 1000
    srate   = _SR[ver][sr_i]
    if bitrate == 0 or srate == 0:
        return None
    chan = 1 if ((b3 >> 6) & 3) == 3 else 2
    if is_mpeg1:
        samples = 1152; flen = (144 * bitrate) // srate + pad
    else:
        samples = 576;  flen = (72 * bitrate) // srate + pad
    if flen < 4:
        return None
    return (flen, srate, chan, samples)

def walk_chain(d, start, cap):
    """Walk consecutive Layer III frames from start (bounded by cap bytes).
    Returns (n_frames, total_samples, rates_set, chans_set, bytes_consumed)."""
    pos, end = start, min(start + cap, len(d))
    nfr = 0; total = 0; rates = set(); chans = set()
    while pos + 4 <= end:
        h = parse_hdr(d, pos)
        if not h:
            break
        flen, srate, chan, samples = h
        if pos + flen > end:                      # frame body runs past the window
            break
        rates.add(srate); chans.add(chan)
        total += samples; nfr += 1; pos += flen
        if nfr > 200000:
            break
    return nfr, total, rates, chans, pos - start

def scan_first_frame(d, dabs, cap):
    """First offset k in [0,cap) where a valid Layer III frame starts, else -1. Mirrors minimp3's sync scan."""
    end = min(dabs + cap, len(d) - 4)
    k = dabs
    while k <= end:
        if frame_len(d, k) > 0:
            return k - dabs
        k += 1
    return -1

def vbr_len_in_slot(d, start, cap_bytes):
    pos, end = start, start + cap_bytes
    n = 0
    while pos < end and pos + 4 <= len(d):
        fl = frame_len(d, pos)
        if fl <= 0:
            break
        pos += fl; n += 1
        if n > 100000:
            break
    return pos - start, n

def looks_like_bank(d):
    if len(d) < 0x20:
        return False
    try:
        total = struct.unpack_from('<I', d, 0x10)[0]
    except Exception:
        return False
    return 0 < total < 100000

def build_orig_heads(info):
    """(bank-name, sound-idx) -> leading DIFF_BYTES of each 0x400 slot in the original RPF."""
    heads = {}
    for e in info['entries']:
        if e['type'] != 'file':
            continue
        d = e['data']
        if not d or not looks_like_bank(d):
            continue
        try:
            bank = parse_bank(d)
        except Exception:
            continue
        for s in bank['sounds']:
            if s.codec != 0x400 or s.dabs + 4 > len(d):
                continue
            heads[(e['name'], s.idx)] = bytes(d[s.dabs:s.dabs + DIFF_BYTES])
    return heads

def main():
    if len(sys.argv) < 3:
        print("usage: py slotcheck.py <GTAIV.exe> <converted.rpf> [original.rpf] [name-filter]")
        return 2
    exe, rpf = sys.argv[1], sys.argv[2]
    orig_path = None
    flt = None
    for a in sys.argv[3:]:
        if a.lower().endswith('.rpf') and orig_path is None:
            orig_path = a
        else:
            flt = a.lower()

    key = extract_aes_key(exe)
    info = rpf3_read(rpf, key)

    orig_heads = None
    if orig_path:
        try:
            orig_heads = build_orig_heads(rpf3_read(orig_path, key))
            print("(loaded original for diff: %d slots)" % len(orig_heads))
        except Exception as ex:
            print("(could not load original '%s': %s -> running without diff)" % (orig_path, ex))
            orig_heads = None

    tot_mp3 = tot_zeroed = tot_other = 0
    noise_swapped = []   # SWAPPED (changed vs original) but no frame found in slot -> real NOISE
    nofr_unknown  = []   # no frame, no original to diff -> ambiguous (could be unswapped ADPCM)
    nofr_orig_ok  = 0    # no frame, but bytes == original -> never swapped -> fine (counted only)
    late_frame    = []   # frame found at offset>0 (lead-in/junk) - decodes fine, listed as info
    underread     = []   # vbr-in-slot > size -> engine never reads the tail
    suspect       = []   # frame found, but chain breaks early / rate changes mid-clip -> likely corrupt
    per_bank_noise = {}

    for e in info['entries']:
        if e['type'] != 'file':
            continue
        d = e['data']
        if not d or not looks_like_bank(d):
            continue
        name = e['name']
        if flt and flt not in name.lower():
            continue
        try:
            bank = parse_bank(d)
        except Exception:
            continue
        for s in bank['sounds']:
            if s.codec != 0x400:
                tot_other += 1
                continue
            if s.dabs + 4 > len(d):
                continue
            head = bytes(d[s.dabs:s.dabs + 12])
            if not any(head):
                tot_zeroed += 1
                continue

            ff = frame_len(d, s.dabs)
            foff = 0 if ff > 0 else scan_first_frame(d, s.dabs, SCAN_CAP)
            row = (name, s.idx, s.hash, s.size, max(ff, 0), foff, s.rate, s.nsamp, head.hex())

            if foff >= 0:
                tot_mp3 += 1
                if foff > 0:
                    late_frame.append(row)
                # tail-read check only meaningful when we actually have a frame
                vbr, _ = vbr_len_in_slot(d, s.dabs + foff, max(s.size, 0x4000))
                if vbr > s.size:
                    underread.append(row)
                # frame-chain integrity: walk the WHOLE clip. A clip with a valid first
                # frame but a chain that breaks early (decoded samples << expected nsamp)
                # or that changes samplerate/channels mid-stream is corrupt and plays as
                # noise even though the head looks fine - the one-bad-clip signature.
                nfr, total, rates, chans, _c = walk_chain(d, s.dabs + foff, max(s.size, 0x4000))
                cover = (total / s.nsamp) if s.nsamp else 1.0
                if len(rates) > 1 or len(chans) > 1 or cover < 0.5:
                    srep = "/".join(str(r) for r in sorted(rates)) or "-"
                    suspect.append((name, s.idx, s.hash, s.size, nfr, total, s.nsamp,
                                    round(cover, 2), srep, head.hex()))
                continue

            # No frame anywhere in the slot -> ADPCM fallback at runtime.
            if orig_heads is not None:
                ob = orig_heads.get((name, s.idx))
                changed = (ob is None) or (bytes(d[s.dabs:s.dabs + DIFF_BYTES]) != ob)
                if changed:
                    noise_swapped.append(row)
                    per_bank_noise[name] = per_bank_noise.get(name, 0) + 1
                else:
                    nofr_orig_ok += 1
            else:
                nofr_unknown.append(row)
                per_bank_noise[name] = per_bank_noise.get(name, 0) + 1

    print("=" * 86)
    print("SLOTCHECK  %s" % rpf)
    print("MP3 sounds (frame found): %d   zeroed/silent: %d   non-0x400: %d"
          % (tot_mp3, tot_zeroed, tot_other))
    print("-" * 86)
    print("NOISE  (swapped, but NO decodable frame in slot -> ADPCM noise):  %d" % len(noise_swapped))
    if orig_heads is not None:
        print("       (no-frame slots identical to original = never swapped, fine): %d" % nofr_orig_ok)
    else:
        print("NOFRAME (no frame; supply original.rpf to split swapped-vs-untouched): %d" % len(nofr_unknown))
    print("LATE-FRAME (frame present but at offset>0 - decodes fine, FYI):    %d" % len(late_frame))
    print("UNDER-READ (vbr-in-slot > size -> tail not read):                 %d" % len(underread))
    print("SUSPECT (frame chain breaks early / rate changes -> corrupt clip): %d" % len(suspect))
    print("=" * 86)

    def show(rows, title, limit=60):
        if not rows:
            return
        rows = sorted(rows, key=lambda r: r[3])
        print("\n%s  (worst %d of %d, smallest size first):" % (title, min(limit, len(rows)), len(rows)))
        print("  %-26s %4s %-10s %7s %6s %5s %6s %8s  %s"
              % ("bank", "idx", "hash", "size", "frame", "foff", "rate", "nsamp", "head[0:12]"))
        for (name, idx, h, size, ff, foff, rate, ns, hx) in rows[:limit]:
            print("  %-26s %4d 0x%08X %7d %6d %5d %6d %8d  %s"
                  % (name[:26], idx, h, size, ff, foff, rate, ns, hx))

    show(noise_swapped, "NOISE (swapped, no frame in slot)")
    show(nofr_unknown,  "NOFRAME (ambiguous - no original supplied)")
    show(late_frame,    "LATE-FRAME (frame at offset>0, plays fine)")
    show(underread,     "UNDER-READ (tail clipped)")

    if suspect:
        rows = sorted(suspect, key=lambda r: r[7])   # lowest coverage first
        print("\nSUSPECT (corrupt frame chain)  (worst %d of %d, lowest coverage first):"
              % (min(60, len(rows)), len(rows)))
        print("  %-26s %4s %-10s %7s %5s %8s %8s %5s %-12s  %s"
              % ("bank", "idx", "hash", "size", "frms", "decSmp", "nsamp", "cover", "rate(s)", "head[0:12]"))
        for (name, idx, h, size, nfr, total, ns, cov, srep, hx) in rows[:60]:
            print("  %-26s %4d 0x%08X %7d %5d %8d %8d %5.2f %-12s  %s"
                  % (name[:26], idx, h, size, nfr, total, ns, cov, srep, hx))

    if per_bank_noise:
        print("\nNoise per bank:")
        for name in sorted(per_bank_noise, key=lambda n: -per_bank_noise[n]):
            print("  %-30s %d" % (name, per_bank_noise[name]))

    print("\nLegend: size=engine read window (orig ADPCM bytes) | frame=first-frame bytes at dabs")
    print("        foff=byte offset where minimp3 finds the first frame (0=at start, -1=none)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
