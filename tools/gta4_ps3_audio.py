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


def repack_cbr(mp3_bytes, bitrate, exe, tmp_base=None):
    """Losslessly pack to CBR <bitrate> (mp3packer, no re-encode). Temp files go under
    tmp_base (the output-folder scratch dir) so one AV exclusion covers everything."""
    with tempfile.TemporaryDirectory(dir=tmp_base) as td:
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


def _process_bank(pc_data, ps3_data, mp3packer, tmp_base, grow=True):
    """Non-streamed speech/pain bank: per-sound payload swap in GROW mode.

    Grow mode rebuilds the audio-data region so each swapped sound gets a slot sized
    to its whole MP3 (lead-in + audio + trailing), rewrites that sound's size /
    sample-count / data-offset / rate fields, and shifts the later data offsets; the
    header, sub-sound table and per-sound info structs (everything before `base`) keep
    their positions, so the loader's binary search over the table (image 0x887DA0 /
    RVA 0x487DCC) still works. The output bank is larger than the input - that is
    expected and fine.

    The long-hunted crash was NOT caused by relayout: it was the MP3 payload being
    LARGER than the unchanged `size` field, so the engine's load buffer (sized from
    `size`) overran the neighbouring bank object. Grow mode sets `size` to the real
    payload length, so the buffer fits exactly and cannot overflow. (Slot mode avoided
    the overflow by clamping the MP3 to the padded slot, but the encoder lead-in then
    displaced the real tail off the `size`-byte decode window -> voice lines clipped at
    the end. Grow keeps the whole payload, so the ASI decodes all of it and only the
    silent lead-in is trimmed at playback.) The CBR is matched to the PS3 source rate
    (sample_rate*4 kbps == the ADPCM byte rate) and the leading Info/Xing frame is
    stripped; the per-sound rate field is patched to the source rate for correct pitch."""
    try:
        pcb = bank_swap.parse_bank(pc_data)
    except Exception as e:
        return None, "skip: not a parseable bank (%s)" % e
    if (not pcb['le']) or pcb['total'] == 0 or pcb['total'] > 100000 \
       or pcb['base'] >= len(pc_data):
        return None, "skip: implausible bank header"
    try:
        # Per-bank scratch dir lives INSIDE tmp_base (under the output folder), not in the
        # system temp, so a single Defender exclusion on the output folder covers it. Still
        # auto-deleted per bank.
        with tempfile.TemporaryDirectory(dir=tmp_base) as td:
            out, st = bank_swap.swap_bank(pc_data, ps3_data, mp3packer, grow=grow,
                                          tmpdir=td, log=lambda *a, **k: None)
    except Exception as e:
        return None, "skip: bank-swap error (%s)" % e
    if st['swapped'] == 0:
        return None, "skip: bank, nothing swappable (%d PCM, %d no-match, %d rate)" \
                     % (st['skipped_codec'], st['skipped_nomatch'], st['skipped_rate'])
    note = "BANK ok  %d sounds (%s +%dB)" % (st['swapped'],
                                             "grow" if grow else "slot", st.get('grew_by', 0))
    if st['skipped_codec']:
        note += ", %d PCM kept" % st['skipped_codec']
    if st['skipped_nomatch']:
        note += ", %d no-match" % st['skipped_nomatch']
    if st.get('skipped_rate'):
        note += ", %d rate-skip" % st['skipped_rate']
    if st.get('failed'):
        note += ", %d FAILED (timeout/err -> kept ADPCM)" % st['failed']
    if st.get('info_stripped'):
        note += ", %d info-frame stripped" % st['info_stripped']
    return bytes(out), note


def process_ivaud(pc_data, ps3_data, cbr, mp3packer, tmp_base, grow=True):
    # Non-streamed bank? (streamCount field @0x10 != 0) -> per-sound bank swap.
    if len(pc_data) >= 0x30 and struct.unpack_from('<I', pc_data, 0x10)[0] != 0:
        return _process_bank(pc_data, ps3_data, mp3packer, tmp_base, grow=grow)

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
        mp3 = [repack_cbr(m, cbr, mp3packer, tmp_base) for m in mp3]
    except Exception as e:
        return None, "ERROR: mp3packer (%s)" % e

    # --- strip Xing/Info/encoder tag frames from every channel (symmetrically) ---
    # mp3packer leaves tag frames (and a leading header) in the stream; the in-game
    # decoder skips them (0 samples) and briefly starves -> tune-in / mid-play pads.
    # Cleaning is identical per channel, so L/R frame alignment is preserved.
    cleaned = [bank_swap.clean_stream(m) for m in mp3]
    mp3 = [c[0] for c in cleaned]
    n_tags = sum(c[1] for c in cleaned)
    n_junk = sum(c[2] for c in cleaned)

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
    if n_tags or n_junk:
        note += "  [cleaned %d tag frames, %d junk bytes]" % (n_tags, n_junk)
    if not info['complete']:
        det = ", ".join("ch%d mp3=%d slot=%d (%d%%)" %
                        (c, len(mp3[c]), info['capacity'][c],
                         int(100 * len(mp3[c]) / max(1, info['capacity'][c])))
                        for c in range(info['channels']))
        note += "  TRUNCATED [%s]" % det
    return out, note + warn


def process_rpf(key, pc_rpf, ps3_rpf, out_rpf, cbr, mp3packer, tmp_base,
                dry_run=False, quiet=False, grow=True):
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
        new, status = process_ivaud(e['data'], ps3e['data'], cbr, mp3packer, tmp_base, grow=grow)
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
        # Grow mode grows every swapped bank -> compact (dense repack) so the archive
        # doesn't ~double and cross the 2 GB offset limit. Slot mode keeps every bank at
        # its original size, so the layout-preserving (in-place) write is correct and the
        # file stays byte-for-byte the original size where unchanged.
        rpf3.rpf3_write(out_rpf, pc_info, replacements, compact=grow)

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
    ap.add_argument("--lead-in", type=int, default=bank_swap.LEAD_IN_SAMPLES,
                    help="leading encoder/decoder lead-in samples stripped from each MP3 "
                         "(whole frames, lossless; default %d, 0 disables). Removing it keeps "
                         "banks inside their wave-slot (no crash) and removes the slot-mode "
                         "end-clip." % bank_swap.LEAD_IN_SAMPLES)
    ap.add_argument("--slot", action="store_true",
                    help="SLOT mode: keep every bank at its original size (clamp MP3 to the "
                         "padded slot) and write the RPF layout-preserving (no compaction). "
                         "Diagnostic / fallback - voice lines may clip at the end. Default is "
                         "GROW mode (full payload, larger banks, compacted RPF).")
    ap.add_argument("--tmpdir", default=None,
                    help="scratch folder for mp3packer temp files (default: <out>/_gta4mp3_tmp). "
                         "Put this (or the output folder) in your AV exclusions to avoid "
                         "real-time-scan locks slowing the run.")
    args = ap.parse_args()
    bank_swap.LEAD_IN_SAMPLES = args.lead_in   # file-level lead-in trim (see bank_swap.drop_lead_in)

    # check mp3packer availability
    try:
        subprocess.run([args.mp3packer], capture_output=True, text=True)
    except FileNotFoundError:
        print("ERROR: mp3packer not found (%s). Specify it with --mp3packer <path>." % args.mp3packer)
        return

    print("AES key from", args.exe, "...")
    key = rpf3.extract_aes_key(args.exe)

    # Fixed scratch folder under the output location (not the system temp), so ONE Defender
    # exclusion on the output folder covers both the temp files and the finished RPFs.
    if args.tmpdir:
        tmp_base = args.tmpdir
    elif args.batch:
        tmp_base = os.path.join(args.out, "_gta4mp3_tmp")
    else:
        tmp_base = os.path.join(os.path.dirname(os.path.abspath(args.out)) or ".", "_gta4mp3_tmp")
    os.makedirs(tmp_base, exist_ok=True)
    print("Temp folder:", tmp_base, "(add this or the output folder to your AV exclusions)")

    grow = not args.slot
    print("Mode:", "GROW (full payload, compacted RPF)" if grow
          else "SLOT (original sizes, layout-preserving) [diagnostic]")

    if not args.batch:
        print("Processing: %s + %s  (CBR %d)" % (os.path.basename(args.pc), os.path.basename(args.ps3), args.cbr))
        st = process_rpf(key, args.pc, args.ps3, args.out, args.cbr, args.mp3packer,
                         tmp_base, dry_run=args.dry_run, quiet=args.quiet, grow=grow)
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
                             tmp_base, dry_run=args.dry_run, quiet=args.quiet, grow=grow)
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
