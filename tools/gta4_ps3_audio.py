#!/usr/bin/env python3
"""
gta4_ps3_audio.py  --  PS3 MP3 audio mod for GTA IV (PC)
========================================================
Replaces the audio payload of streamed ADPCM ivauds (radio/cutscene) with the PS3
MP3 (payload swap; container skeleton kept intact). Non-streamed speech/pain BANKS
in the same RPF are detected automatically and swapped per sub-sound into their PC
slot (size/nsamp untouched; see bank_swap.py, slot mode).

FIX for L/R offset + silence (instead of the old map/hash approach "path D"):
each channel is LOSSLESSLY packed to CBR 128 kbps before the swap (mp3packer,
no re-encode -- only a bit-reservoir rebuild). 128 kbps = 16000 bytes/s = exactly
the IMA-ADPCM rate at 32 kHz that the game is wired to:
  - constant frame size + shared readPos -> both channels at the same frame
    index -> NO offset (by construction)
  - MP3 byte rate == ADPCM byte rate -> no rate mismatch -> NO silence
  - CBR-128 mono == ADPCM slot size -> fits the container exactly
The ASI decoder needs NO change for this (Fix #5 is enough), NO map file.

Required in the same folder: rpf3.py, rage_aud_deinterleave.py, ivaud_payloadswap.py,
bank_swap.py, hashes.txt, pycryptodome.
Also: mp3packer (on PATH or via --mp3packer <path>).

SINGLE: py gta4_ps3_audio.py <GTAIV.exe> <pc.rpf> <ps3.rpf> -o <out.rpf>
BATCH:  py gta4_ps3_audio.py <GTAIV.exe> --batch <pc_dir> <ps3_dir> -o <out_dir>
Options: --dry-run  --quiet  --mp3packer <path>  --cbr <kbps, default 128>
"""
import sys, os, io, argparse, contextlib, struct, traceback, subprocess, tempfile

import rpf3
import rage_aud_deinterleave as deint
import ivaud_payloadswap as swap
import bank_swap      # non-streamed speech/pain banks; also provides frame_len

MP3PACKER_DEFAULT = "mp3packer"   # on PATH; otherwise --mp3packer <full path>


def _pc_rate(pc):
    table_off = struct.unpack_from('<Q', pc, 0x00)[0]
    return struct.unpack_from('<I', pc, table_off + 0x04)[0]


def repack_cbr(mp3_bytes, bitrate, exe):
    """Losslessly pack to CBR <bitrate> (mp3packer, no re-encode)."""
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.mp3")
        outp = os.path.join(td, "out.mp3")
        with open(inp, 'wb') as f:
            f.write(mp3_bytes)
        # -b <bitrate> = minimum bitrate; if the content fits, the result is CBR.
        # -r = push data as far forward as possible (bit reservoir minimized)
        #      -> SELF-CONTAINED frames with no look-back dependency + no stuffing
        #      between frames -> minimp3 reads the stream cleanly, no gaps.
        #      WITHOUT -r mp3packer writes a reservoir layout + signature between
        #      frames -> minimp3 loses sync -> dropouts. (-f = overwrite)
        r = subprocess.run([exe, "-b", str(bitrate), "-r", "-f", inp, outp],
                           capture_output=True, text=True)
        if not os.path.exists(outp) or os.path.getsize(outp) == 0:
            raise RuntimeError("mp3packer produced no output: %s"
                               % ((r.stderr or r.stdout or "").strip()[:200]))
        with open(outp, 'rb') as f:
            return f.read()


def _frame_sizes(mp3_bytes):
    """List of MPEG Layer III frame byte-sizes (for the strict-CBR check)."""
    sizes = []
    i, n = 0, len(mp3_bytes)
    if n > 10 and mp3_bytes[:3] == b'ID3':
        i = 10 + (((mp3_bytes[6] & 0x7f) << 21) | ((mp3_bytes[7] & 0x7f) << 14) |
                  ((mp3_bytes[8] & 0x7f) << 7) | (mp3_bytes[9] & 0x7f))
    while i + 4 <= n:
        fl = bank_swap.frame_len(mp3_bytes, i)
        if fl == 0:
            i += 1; continue
        if i + fl > n:
            break
        sizes.append(fl); i += fl
    return sizes


def _is_strict_cbr(mp3_bytes):
    sz = _frame_sizes(mp3_bytes)
    return len(sz) < 2 or len(set(sz)) == 1


def _process_bank(pc_data, ps3_data, mp3packer):
    """Non-streamed speech/pain bank: per-sound payload swap, grow mode (rebuild the
    data region to fit each whole MP3; the leading Info/Xing frame is stripped)."""
    try:
        pcb = bank_swap.parse_bank(pc_data)
    except Exception as e:
        return None, "skip: not a parseable bank (%s)" % e
    if (not pcb['le']) or pcb['total'] == 0 or pcb['total'] > 100000 \
       or pcb['base'] >= len(pc_data):
        return None, "skip: implausible bank header"
    try:
        with tempfile.TemporaryDirectory() as td:
            out, st = bank_swap.swap_bank(pc_data, ps3_data, mp3packer, grow=True,
                                          tmpdir=td, log=lambda *a, **k: None)
    except Exception as e:
        return None, "skip: bank-swap error (%s)" % e
    if st['swapped'] == 0:
        return None, "skip: bank, nothing swappable (%d PCM, %d no-match, %d rate)" \
                     % (st['skipped_codec'], st['skipped_nomatch'], st['skipped_rate'])
    note = "BANK ok  %d sounds" % st['swapped']
    if st['skipped_codec']:
        note += ", %d PCM kept" % st['skipped_codec']
    if st['skipped_nomatch']:
        note += ", %d no-match" % st['skipped_nomatch']
    return bytes(out), note


def process_ivaud(pc_data, ps3_data, cbr, mp3packer):
    # Non-streamed bank? (streamCount field @0x10 != 0) -> per-sound bank swap.
    if len(pc_data) >= 0x30 and struct.unpack_from('<I', pc_data, 0x10)[0] != 0:
        return _process_bank(pc_data, ps3_data, mp3packer)

    if not swap.is_swappable(pc_data):
        return None, "skip: PC is not a streamed ADPCM ivaud"

    try:
        h = deint.parse_header(ps3_data)
    except Exception as e:
        return None, "skip: PS3 is not an MP3 ivaud (%s)" % e

    pc_ch = struct.unpack_from('<I', pc_data, 0x24)[0]
    if h['channels'] != pc_ch:
        return None, "skip: channel count PC=%d != PS3=%d" % (pc_ch, h['channels'])

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mp3 = deint.deinterleave(ps3_data)
    except Exception as e:
        return None, "skip: deinterleave error (%s)" % e

    if not all(swap.is_mp3_stream(m) for m in mp3):
        return None, "skip: deinterleaved streams are not all MP3"

    # --- LOSSLESS pack to CBR <cbr> (byte = time) ---
    try:
        mp3 = [repack_cbr(m, cbr, mp3packer) for m in mp3]
    except Exception as e:
        return None, "ERROR: mp3packer (%s)" % e

    warn = ""
    nonstrict = [c for c, m in enumerate(mp3) if not _is_strict_cbr(m)]
    if nonstrict:
        warn = "  WARNING: channel %s is not strict CBR %d (min-CBR > %d) -> possible residual offset" \
               % (nonstrict, cbr, cbr)

    pc_rate = _pc_rate(pc_data)
    target = h['sample_rate'] if h['sample_rate'] != pc_rate else None

    try:
        out, info = swap.payload_swap(pc_data, mp3, target_rate=target)
    except Exception as e:
        return None, "skip: swap error (%s)" % e

    note = "OK  %d channels @ %d Hz  CBR %d" % (info['channels'], h['sample_rate'], cbr)
    if target:
        note += "  [rate %d->%d]" % (pc_rate, h['sample_rate'])
    if not info['complete']:
        det = ", ".join("ch%d mp3=%d slot=%d (%d%%)" %
                        (c, len(mp3[c]), info['capacity'][c],
                         int(100 * len(mp3[c]) / max(1, info['capacity'][c])))
                        for c in range(info['channels']))
        note += "  TRUNCATED [%s]" % det
    return out, note + warn


def process_rpf(key, pc_rpf, ps3_rpf, out_rpf, cbr, mp3packer,
                dry_run=False, quiet=False):
    pc_info  = rpf3.rpf3_read(pc_rpf, key)
    ps3_info = rpf3.rpf3_read(ps3_rpf, key)
    ps3_by_hash = {e['name_hash']: e for e in ps3_info['entries'] if e['type'] == 'file'}

    replacements = {}
    n_swapped = n_skipped = n_nops3 = 0
    for e in pc_info['entries']:
        if e['type'] != 'file':
            continue
        ps3e = ps3_by_hash.get(e['name_hash'])
        if ps3e is None:
            n_nops3 += 1; continue
        new, status = process_ivaud(e['data'], ps3e['data'], cbr, mp3packer)
        if new is not None:
            replacements[e['name_hash']] = new
            n_swapped += 1
            if not quiet:
                print("  [SWAP] %-28s %s" % (e['name'], status))
        else:
            n_skipped += 1
            if not quiet and "not a streamed ADPCM" not in status:
                print("  [skip] %-28s %s" % (e['name'], status))

    if not dry_run and replacements:
        rpf3.rpf3_write(out_rpf, pc_info, replacements)

    return dict(swapped=n_swapped, skipped=n_skipped, no_ps3=n_nops3,
                total=len([e for e in pc_info['entries'] if e['type'] == 'file']),
                wrote=(not dry_run and bool(replacements)))


def main():
    ap = argparse.ArgumentParser(description="PS3 MP3 audio mod for GTA IV (PC), CBR repack")
    ap.add_argument("exe"); ap.add_argument("pc"); ap.add_argument("ps3")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--mp3packer", default=MP3PACKER_DEFAULT, help="path to mp3packer")
    ap.add_argument("--cbr", type=int, default=128, help="CBR bitrate kbps (default 128 = ADPCM rate)")
    args = ap.parse_args()

    # check mp3packer availability
    try:
        subprocess.run([args.mp3packer], capture_output=True, text=True)
    except FileNotFoundError:
        print("ERROR: mp3packer not found (%s). Specify it with --mp3packer <path>." % args.mp3packer)
        return

    print("AES key from", args.exe, "...")
    key = rpf3.extract_aes_key(args.exe)

    if not args.batch:
        print("Processing: %s + %s  (CBR %d)" % (os.path.basename(args.pc), os.path.basename(args.ps3), args.cbr))
        st = process_rpf(key, args.pc, args.ps3, args.out, args.cbr, args.mp3packer,
                         dry_run=args.dry_run, quiet=args.quiet)
        print("\nDone: %d swapped, %d skipped, %d without PS3 (of %d)"
              % (st['swapped'], st['skipped'], st['no_ps3'], st['total']))
        if st['wrote']: print("Written:", args.out)
        elif args.dry_run: print("(dry run)")
        else: print("(no candidates)")
        return

    os.makedirs(args.out, exist_ok=True)
    ps3_files = {f.lower(): f for f in os.listdir(args.ps3) if f.lower().endswith('.rpf')}
    pc_rpfs = sorted(f for f in os.listdir(args.pc) if f.lower().endswith('.rpf'))
    if not pc_rpfs:
        print("No *.rpf in", args.pc); return

    grand = dict(swapped=0, files=0, written=0)
    for fn in pc_rpfs:
        m = ps3_files.get(fn.lower())
        if not m:
            print("\n== %s ==  (no PS3 counterpart)" % fn); continue
        print("\n== %s ==" % fn)
        try:
            st = process_rpf(key, os.path.join(args.pc, fn), os.path.join(args.ps3, m),
                             os.path.join(args.out, fn), args.cbr, args.mp3packer,
                             dry_run=args.dry_run, quiet=args.quiet)
        except Exception as e:
            print("  ERROR:", e)
            if not args.quiet: traceback.print_exc()
            continue
        print("  -> %d swapped, %d skipped (of %d)" % (st['swapped'], st['skipped'], st['total']))
        grand['swapped'] += st['swapped']; grand['files'] += 1
        if st['wrote']: grand['written'] += 1

    print("\n===== BATCH DONE =====")
    print("%d RPF pairs, %d written, %d ivauds swapped" % (grand['files'], grand['written'], grand['swapped']))
    if args.dry_run: print("(dry run)")


if __name__ == "__main__":
    main()
