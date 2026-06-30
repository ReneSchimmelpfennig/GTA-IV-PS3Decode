#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bank_swap.py - payload swap for GTA IV non-streamed BANKS (speech.rpf, radio_x.rpf
announcements, pain/voice banks). Replaces the PC ADPCM sub-sounds with the PS3 MP3
counterparts (packed to a rate-matched CBR); container/header/codec stay untouched
-> routing through the software decoder (our ASI hook) is preserved.

DEFAULT = grow mode (the version that sounds best in practice):
  Rebuild the audio data region so each swapped sound gets a slot the exact size of
  its MP3 (the rate-matched CBR is typically ~1-2 frames larger than the original PC
  slot). The leading Info/Xing header frame is stripped (silent, lossless) and any
  trailing non-frame bytes trimmed, so the full audio plays and the tail is never cut.
  size/nsamp/data_off are patched; the header, sound table and info-struct region
  (everything before `base`) keep their positions. The whole bank is relaid out, so
  the output file is a bit larger than the input.

  --match-length (off by default): also pad each sound with silence up to the PS3
  length. Only useful if the PS3 lines are longer than the MP3; on these assets the
  PS3 lines are actually shorter, so it is a no-op there and best left off.

--slot (v1): write the MP3 straight into the fixed PC slot, size/nsamp UNTOUCHED,
  oversize (silent) tail truncated, short padded. Simpler, keeps PC pacing, but cuts
  real audio if a PS3 line is longer than its PC slot (a WARNING is logged then).

Bank format (verified):
  Header : table=u64@0x00, total=u32@0x10, base=u32@0x18  (PC little-, PS3 big-endian)
  Table  : per sound u64 @ table+i*0x10 -> info @ sio+off, sio=table+total*0x10
  Info   : data_off=u64@+0x00 (rel. base), name_hash=u32@+0x08, size=u32@+0x0c,
           nsamp=u32@+0x10, rate=u16@+0x18, codec=u32@+0x1c
  Audio  : base+data_off, length size.  nsamp=2*size (4-bit IMA, 2 samples/byte).
PC<->PS3 matching is by name_hash (not by index!). Aliases (several sounds sharing
the same data_off) are swapped only once.
"""
import struct, sys, os, subprocess, tempfile, argparse, math, time
from concurrent.futures import ThreadPoolExecutor

# Parallel mp3packer workers (one external process per swappable sound is the bottleneck;
# mp3packer is CPU-bound and subprocess.run releases the GIL, so threads scale across cores).
MP3PACKER_WORKERS = max(2, min(16, (os.cpu_count() or 4)))
# Retry a sound a few times before giving up: under heavy parallelism the AV scanner can
# briefly lock a freshly-written temp file, which is transient and clears on retry.
MP3PACKER_RETRIES = 4
# Hard cap per mp3packer call. A small mono speech sound packs in <1s; this only trips on
# a hang (malformed input / loop), so one bad sound can't freeze the whole conversion.
MP3PACKER_TIMEOUT = 30

# valid MPEG bitrate (kbps) for byte=time: ADPCM byte rate = rate/2 -> kbps = rate*4/1000
MP3_BITRATES = {32,40,48,56,64,80,96,112,128,160,192,224,256,320}
def rate_to_kbps(rate):
    if rate <= 0 or (rate*4) % 1000 != 0:
        return None
    k = rate*4//1000
    return k if k in MP3_BITRATES else None

# --- MPEG-1/2 Layer III frame length + Info/Xing header detection ---
_BR_V1 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
_BR_V2 = [0, 8,16,24,32,40,48,56, 64, 80, 96,112,128,144,160,0]
_SR    = {3:[44100,48000,32000,0], 2:[22050,24000,16000,0], 0:[11025,12000,8000,0]}

def frame_len(b, i=0):
    """Byte length of the Layer III frame at b[i], or 0 if it is not a valid frame."""
    if i+4 > len(b):
        return 0
    if b[i] != 0xFF or (b[i+1] & 0xE0) != 0xE0:
        return 0
    ver = (b[i+1] >> 3) & 3
    layer = (b[i+1] >> 1) & 3
    bri = (b[i+2] >> 4) & 0xF
    sri = (b[i+2] >> 2) & 3
    pad = (b[i+2] >> 1) & 1
    if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
        return 0
    br = (_BR_V1 if ver == 3 else _BR_V2)[bri] * 1000
    sr = _SR[ver][sri]
    if br == 0 or sr == 0:
        return 0
    return (144*br//sr + pad) if ver == 3 else (72*br//sr + pad)

def strip_info_frame(b):
    """If the first frame is an Info/Xing header frame (silent, no audio), drop it."""
    flen = frame_len(b, 0)
    if flen == 0 or flen > len(b):
        return b, False
    ver = (b[1] >> 3) & 3
    mono = ((b[3] >> 6) & 3) == 3
    side = (17 if mono else 32) if ver == 3 else (9 if mono else 17)
    if b[4+side:4+side+4] in (b'Xing', b'Info'):
        return b[flen:], True
    return b, False

def frames_end(b):
    """Offset just past the last complete Layer III frame (trims trailing junk)."""
    i = 0; n = len(b)
    while i+4 <= n and not (b[i] == 0xFF and (b[i+1] & 0xE0) == 0xE0):
        i += 1
    last = i
    while i+4 <= n:
        fl = frame_len(b, i)
        if fl == 0:
            i += 1; continue
        if i+fl > n:
            break
        i += fl; last = i
    return last

# Encoder/decoder lead-in measured against the PC ADPCM: the PS3 MP3 decodes to ~2116
# leading samples (@24kHz) of warm-up/delay BEFORE the real audio. We strip the whole
# leading frames that lie inside that lead-in directly from the file (lossless - just
# dropping frame bytes, never re-encoding). Two payoffs:
#   1. the file shrinks, so a swapped bank stays inside its fixed wave-slot budget and
#      no longer overflows into the neighbouring bank object (the Chinese Takeout crash);
#   2. the real audio moves to the front, so it fits inside the engine's `size`-byte
#      decode window -> the slot-mode end-clip disappears too.
# MP3 is frame-granular (576 samples/frame for the MPEG-2 24kHz speech, 1152 for MPEG-1),
# so a sub-frame remainder (< one frame, near-silent) is left; the ASI trims that residual
# at output. We only ever drop frames that are ENTIRELY within the lead-in, so real audio
# is never clipped.
LEAD_IN_SAMPLES = 2116

def _main_data_begin(b, i):
    """main_data_begin (the 9-bit MPEG-1 / 8-bit MPEG-2 bit-reservoir back-pointer) of
    the Layer III frame at b[i]. 0 means the frame's main data is fully self-contained,
    i.e. it references NO previous frame -> a safe stream cut point. Returns -1 if the
    side info would run past the buffer."""
    ver  = (b[i+1] >> 3) & 3
    prot = b[i+1] & 1                       # protection bit: 0 => 16-bit CRC follows header
    si   = i + 4 + (0 if prot else 2)       # start of side information
    if ver == 3:                            # MPEG-1: main_data_begin = 9 bits
        if si+2 > len(b): return -1
        return (b[si] << 1) | (b[si+1] >> 7)
    if si+1 > len(b): return -1             # MPEG-2/2.5: main_data_begin = 8 bits
    return b[si]

def drop_lead_in(b, lead_in_samples=None):
    """Drop whole leading frames covering the encoder/decoder lead-in - but ONLY cut in
    front of a frame whose main_data_begin == 0. Cutting before a frame that back-references
    the bit reservoir of a dropped frame would leave the decoder without that frame's main
    data -> garbage/noise on the first frames. So we drop the largest run of in-lead-in
    frames that ends on a self-contained frame; if none exists in the window we drop nothing
    (lossless, never noisy). Returns (trimmed_bytes, samples_dropped)."""
    if lead_in_samples is None:
        lead_in_samples = LEAD_IN_SAMPLES
    if lead_in_samples <= 0:
        return b, 0
    i = 0; n = len(b); acc = 0; cut = 0; cut_samples = 0
    while i+4 <= n:
        fl = frame_len(b, i)
        if fl == 0 or i+fl > n:
            break
        spf = 1152 if ((b[i+1] >> 3) & 3) == 3 else 576   # MPEG-1 vs MPEG-2/2.5 Layer III
        if acc + spf > lead_in_samples:
            break                           # next frame already carries real audio -> stop
        acc += spf; i += fl
        if i+4 <= n and _main_data_begin(b, i) == 0:
            cut = i; cut_samples = acc       # reservoir-safe boundary -> remember it
    return b[cut:], cut_samples

def clean_stream(b):
    """Rebuild a pure CBR stream: keep only valid Layer III AUDIO frames, dropping
    Xing/Info/encoder tag frames (silent, 0 samples) wherever they occur, plus any
    leading/trailing/embedded non-frame junk bytes.

    Used on radio channel MP3s so the in-game decoder never has to skip tag frames
    mid-stream. Skipped tag frames decode to 0 samples, which starves the decoder
    FIFO for that call -> small warm-up / mid-playback silence pads. A leading tag
    also shifts the first-frame offset -> a larger tune-in pad. Removing all tag
    frames yields uniform CBR (every input byte = audio), so the FIFO stays full.

    Both stereo channels carry their tags at the same positions, so cleaning each
    channel identically preserves L/R frame alignment (the streamed swap shares one
    read position across channels). Returns (clean_bytes, dropped_tags, junk_bytes).
    """
    out = bytearray()
    i, n = 0, len(b)
    dropped = junk = 0
    while i < n:
        fl = frame_len(b, i)
        if fl == 0 or i + fl > n:        # not a valid/complete frame -> junk byte
            i += 1; junk += 1
            continue
        ver  = (b[i+1] >> 3) & 3
        mono = ((b[i+3] >> 6) & 3) == 3
        side = (17 if mono else 32) if ver == 3 else (9 if mono else 17)
        if b[i+4+side:i+4+side+4] in (b'Xing', b'Info'):
            dropped += 1                 # tag frame (silent) -> drop
        else:
            out += b[i:i+fl]             # real audio frame -> keep
        i += fl
    return bytes(out), dropped, junk

def detect_le(d):
    le = struct.unpack('<Q', d[0:8])[0]
    return le < len(d)

class Sound:
    __slots__=('idx','hash','data_off','size','nsamp','rate','codec','dabs','info_off')
    def __init__(self,**k): [setattr(self,n,k[n]) for n in k]

def parse_bank(d):
    le = detect_le(d); E='<' if le else '>'
    table=struct.unpack(E+'Q',d[0:8])[0]
    total=struct.unpack(E+'I',d[0x10:0x14])[0]
    base =struct.unpack(E+'I',d[0x18:0x1c])[0]
    sio=table+total*0x10
    snds=[]
    for i in range(total):
        off=struct.unpack(E+'Q',d[table+i*0x10:table+i*0x10+8])[0]
        info=sio+off
        data_off=struct.unpack(E+'Q',d[info:info+8])[0]
        h   =struct.unpack(E+'I',d[info+0x08:info+0x0c])[0]
        size=struct.unpack(E+'I',d[info+0x0c:info+0x10])[0]
        ns  =struct.unpack(E+'I',d[info+0x10:info+0x14])[0]
        rate=struct.unpack(E+'H',d[info+0x18:info+0x1a])[0]
        cod =struct.unpack(E+'I',d[info+0x1c:info+0x20])[0]
        snds.append(Sound(idx=i,hash=h,data_off=data_off,size=size,nsamp=ns,
                          rate=rate,codec=cod,dabs=base+data_off,info_off=info))
    return dict(le=le,table=table,total=total,base=base,sio=sio,sounds=snds)

def run_mp3packer(exe,inp,outp,kbps):
    # stdin=DEVNULL: if mp3packer ever prompts (e.g. on an odd input) it gets EOF instead of
    # blocking forever on the terminal. timeout: a small mono sound packs in well under a
    # second, so this only ever fires on a genuine hang/loop -> we kill it and the caller
    # treats that one sound as failed (kept ADPCM) instead of stalling the whole run.
    try:
        r=subprocess.run([exe,"-b",str(kbps),"-r","-f",inp,outp],
                         capture_output=True,text=True,
                         stdin=subprocess.DEVNULL,timeout=MP3PACKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("mp3packer timed out after %ds (%s) - input likely malformed"
                           % (MP3PACKER_TIMEOUT, os.path.basename(inp)))
    if r.returncode!=0 or not os.path.exists(outp):
        raise RuntimeError("mp3packer failed (%s):\n%s\n%s"%(inp,r.stdout,r.stderr))
    return open(outp,'rb').read()

def _new_stats(**extra):
    s = dict(swapped=0, skipped_codec=0, skipped_nomatch=0, skipped_rate=0,
             alias=0, failed=0, padded=0, truncated=0, skipped_toobig=0,
             audio_cut=0, max_cut_ms=0.0)
    s.update(extra); return s

# ---------------------------------------------------------------------------
#  DEFAULT: slot mode (v1) - write into the fixed PC slot, size/nsamp untouched
# ---------------------------------------------------------------------------
def _padded(n):
    """Engine loads each bank sound in 2048-byte blocks -> the real slot is the
    byte size rounded UP to 2048 (confirmed: 'size is with padding', and SparkIV's
    Mono parser reads ceil(size/2048)*2048 bytes). That padding is slack we can use."""
    return (n + 2047) & ~2047


def _strip_id3(b):
    """Drop ID3v2 (leading) and ID3v1 (trailing 128 B 'TAG') so the encoder tag we
    saw in memory ('mp3packer...') doesn't eat slot space. minimp3 syncs on the
    frame header and ignores tags, so this never affects decoding - it only frees
    bytes so the MP3 fits its slot. Frame data is left untouched."""
    if len(b) >= 10 and b[:3] == b'ID3':
        sz = (b[6] & 0x7f) << 21 | (b[7] & 0x7f) << 14 | (b[8] & 0x7f) << 7 | (b[9] & 0x7f)
        if 10 + sz <= len(b):
            b = b[10 + sz:]
    if len(b) >= 128 and b[-128:-125] == b'TAG':
        b = b[:-128]
    return b


def _swap_bank_slot(pc_bytes, ps3_bytes, exe, tmp, dry, log):
    pc = parse_bank(pc_bytes)
    ps3_by_hash = {}
    for s in parse_bank(ps3_bytes)['sounds']:
        ps3_by_hash.setdefault(s.hash, s)
    out = bytearray(pc_bytes)
    done = set()
    stats = _new_stats()
    # Slot end for each sound = start of the next sound's data (sounds are packed at
    # 2048-padded offsets). Used so we never write past a sound's own padded slot.
    dabs_sorted = sorted(set(s.dabs for s in pc['sounds']))
    def slot_end(dabs):
        for d in dabs_sorted:
            if d > dabs:
                return d
        return len(pc_bytes)
    for s in pc['sounds']:
        if s.codec != 0x400:
            stats['skipped_codec'] += 1; continue
        if s.dabs in done:
            stats['alias'] += 1; continue
        src = ps3_by_hash.get(s.hash)
        if src is None:
            stats['skipped_nomatch'] += 1
            log("  [no-match] i=%d hash=%#010x"%(s.idx, s.hash)); continue
        # VBR (native PS3 MP3), not CBR. Banks are one-shot (no mid-stream tune-in), so the
        # uniform-CBR requirement of the streamed path does not apply. The native VBR stream
        # is SMALLER than the 4-bit PC ADPCM it replaces, so it drops into the original slot
        # with the `size` field UNCHANGED -> the bank stays byte-for-byte its original size
        # -> it always fits its fixed wave-slot (no growth, no overflow, no Chinese-Takeout
        # crash). We keep the whole stream (no frame dropping), so there is no bit-reservoir
        # cut -> no decode noise. The ~2116-sample encoder/decoder lead-in stays in the
        # frames and is trimmed at OUTPUT by the ASI's TrimLeadingSilence; because the full
        # VBR stream fits the slot (no input truncation), the real audio is complete -> no
        # end-clip. clean_stream() strips ID3/Xing/tag frames and inter-frame junk, leaving
        # pure Layer III audio frames. No CBR rate gate, so 44.1kHz sounds (Liberty Rock)
        # that had no valid CBR bitrate now convert too.
        packed, _tags, _junk = clean_stream(ps3_bytes[src.dabs:src.dabs + src.size])
        if not packed:
            stats['skipped_nomatch'] += 1
            log("  [skip] i=%d hash=%#010x  no MP3 frames in PS3 source"%(s.idx, s.hash)); continue
        cap = min(_padded(s.size), slot_end(s.dabs) - s.dabs)
        warn = ""
        if src.nsamp > 2 * s.size:        # PS3 real audio genuinely longer than the slot's playback length
            cut_ms = (src.nsamp - 2 * s.size) * 1000.0 / src.rate if src.rate else 0.0
            stats['audio_cut'] += 1; stats['max_cut_ms'] = max(stats['max_cut_ms'], cut_ms)
            warn = "  note: PS3 audio %d > slot %d samples -> ~%.0fms tail not played" \
                   % (src.nsamp, 2 * s.size, cut_ms)
        # Truncate the MP3 to the padded slot if it is larger. This is SAFE (the engine
        # loads exactly the padded slot, so we never cross into the next sound) AND
        # effectively lossless for playback: the slot only plays 2*size samples, and a
        # padded slot's worth of MP3 always decodes to at least that many - so the trimmed
        # bytes are trailing frames the slot never reaches. The ONLY real tail loss is when
        # the source audio is genuinely longer than the slot's duration (nsamp > 2*size),
        # which is the inherent slot-mode limit, flagged above. So we keep the sound as MP3
        # rather than falling back to ADPCM; nothing audible is lost on a normal sound.
        if cap <= 0:                       # degenerate (no slot space) -> keep original ADPCM
            stats['skipped_toobig'] += 1
            log("  [keep-adpcm] i=%d hash=%#010x  no slot space (kept original)"
                % (s.idx, s.hash)); continue
        w = packed[:cap]
        trim = len(packed) - len(w)
        if trim > 0:
            stats['truncated'] += 1        # benign overhead trim (frames beyond 2*size)
        elif len(w) < s.size:
            stats['padded'] += 1
        assert len(w) <= cap, "guard: blob exceeds slot"   # never overflow the slot
        if not dry:
            # Zero the whole padded slot, then drop the MP3 in. Zeroing the tail stops the
            # decoder from seeing stale ADPCM bytes (a chance 0xFF could look like a frame).
            out[s.dabs:s.dabs + cap] = b'\x00' * cap
            out[s.dabs:s.dabs + len(w)] = w
            # Patch ONLY the sound's rate field (info+0x18) to the PS3 source rate so the
            # pitch is correct when PC and PS3 rates differ (e.g. Liberty Rock: PC 44.1kHz,
            # PS3 32kHz). It is a no-op when the rates already match. This is safe: the rate
            # field is orthogonal to the payload-overflow that caused the crash, 32kHz is an
            # ordinary rate the engine resamples routinely, and size/nsamp stay the ORIGINAL
            # ADPCM values (we only replace the payload bytes inside the existing slot), so
            # the engine's size<<2 decode buffer and 2*size sample budget are unchanged and
            # the bank's byte size - hence its wave-slot fit - is identical to the original.
            struct.pack_into('<H', out, s.info_off + 0x18, src.rate)
        done.add(s.dabs); stats['swapped'] += 1
        log("  [swap] i=%d hash=%#010x ps3_rate=%d VBR  PS3 %dB -> MP3 %dB / slot %dB (cap %dB)  %s%s"
            % (s.idx, s.hash, src.rate, src.size, len(packed), s.size, cap,
               "trim %dB (overhead, no playback loss)" % trim if trim > 0
               else "slack %dB" % (cap - len(w)), warn))
    return bytes(out), stats

# ---------------------------------------------------------------------------
#  --grow (experimental): rebuild data region to fit the whole MP3
# ---------------------------------------------------------------------------
def _pow2_align(values, cap=0x800, floor=0x10):
    g = 0
    for v in values:
        g = math.gcd(g, v)
    if g == 0:
        return floor
    a = g & (-g)
    return min(max(a, floor), cap)

def _swap_bank_grow(pc_bytes, ps3_bytes, exe, tmp, dry, keep_info, match_length, log):
    pc = parse_bank(pc_bytes)
    if not pc['le']:
        raise RuntimeError("PC bank is not little-endian - unexpected")
    base = pc['base']
    ps3_by_hash = {}
    for s in parse_bank(ps3_bytes)['sounds']:
        ps3_by_hash.setdefault(s.hash, s)
    align = _pow2_align([base] + [s.data_off for s in pc['sounds']])
    regions, order = {}, []
    for s in pc['sounds']:
        if s.data_off not in regions:
            regions[s.data_off] = []; order.append(s.data_off)
        regions[s.data_off].append(s)
    stats = _new_stats(grew_by=0, max_overflow=0, info_stripped=0, align=align)
    plan = {}
    # Phase 1: classify each region (fast, sequential). Collect the ones that need
    # mp3packer into 'jobs'; everything kept (PCM / no-match / no-CBR) goes straight
    # into plan.
    jobs = []
    for off in order:
        rep = regions[off][0]
        if len(regions[off]) > 1:
            stats['alias'] += len(regions[off]) - 1
        if rep.codec != 0x400:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_codec']+=1; continue
        src = ps3_by_hash.get(rep.hash)
        if src is None:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_nomatch']+=1; continue
        # CBR + target rate come from the PS3 SOURCE rate (the actual audio), not the PC
        # slot rate. If they differ (e.g. LRR: PC 44.1k, PS3 32k -> 44.1k has no valid CBR),
        # we pack at the source rate and patch the bank sound's rate field below.
        kbps = rate_to_kbps(src.rate)
        if kbps is None:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_rate']+=1; continue
        jobs.append((off, rep, src, kbps))

    # Phase 2: run mp3packer for all jobs IN PARALLEL (the slow part). The worker only
    # touches its own temp files + returns bytes; no shared state is mutated here.
    def _pack(job):
        off, rep, src, kbps = job
        # Temp names are keyed by the region's data_off (unique per job), NOT just the hash:
        # a bank can hold the same hash at two different offsets, and naming by hash alone
        # made two parallel workers write/read the SAME file at once -> a half-written MP3 ->
        # mp3packer chokes and hangs. data_off is unique per region, so every job is isolated.
        tag="%08x_%x"%(rep.hash,off)
        ip=os.path.join(tmp,"s_%s_in.mp3"%tag); op=os.path.join(tmp,"s_%s_cbr.mp3"%tag)
        # RETRY: under heavy parallelism the real-time AV scanner intermittently locks a
        # freshly-written temp .mp3, so mp3packer occasionally fails to open it for a single
        # sound. That lock clears in milliseconds, so a short retry recovers it instead of
        # dropping the sound to ADPCM. A truly broken source still fails after all attempts.
        last_err=None
        for attempt in range(MP3PACKER_RETRIES):
            try:
                open(ip,'wb').write(ps3_bytes[src.dabs:src.dabs+src.size])
                packed=run_mp3packer(exe,ip,op,kbps)
                stripped=False
                if not keep_info:
                    packed,stripped=strip_info_frame(packed)
                end=frames_end(packed)
                if end>0: packed=packed[:end]
                packed,_drop=drop_lead_in(packed)   # strip leading lead-in frames (lossless)
                return (off, packed, stripped, None)
            except Exception as e:
                last_err=e
                if attempt < MP3PACKER_RETRIES-1:
                    time.sleep(0.25*(attempt+1))
        return (off, None, False, last_err)

    results = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=MP3PACKER_WORKERS) as ex:
            for off, packed, stripped, err in ex.map(_pack, jobs):
                results[off] = (packed, stripped, err)

    # Phase 3: fold the results into plan (sequential, fast); stats updated here only.
    for off, rep, src, kbps in jobs:
        packed, stripped, err = results[off]
        if err is not None:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['failed']+=1
            # Always surface a persistent failure (with the reason) so a deterministic
            # bad source can be seen, not silently dropped to ADPCM.
            sys.stderr.write("  [FAIL after %d tries] hash=%#010x i=%d: %s\n"
                             % (MP3PACKER_RETRIES, rep.hash, rep.idx, err))
            continue
        if stripped: stats['info_stripped']+=1
        mp3_bytes=len(packed)
        if match_length and src.nsamp > 2*mp3_bytes:
            packed=packed+b'\x00'*(((src.nsamp+1)//2)-mp3_bytes); stats['padded']+=1
        over=len(packed)-rep.size
        if over>0: stats['max_overflow']=max(stats['max_overflow'],over); stats['grew_by']+=over
        plan[off]=dict(data=packed,size=len(packed),nsamp=2*len(packed),swapped=True,
                       rate=src.rate)
        stats['swapped']+=1
    out=bytearray(pc_bytes[:base]); blob=bytearray()
    for off in order:
        P=plan[off]; blob+=b'\x00'*((-len(blob))%align); new_off=len(blob); blob+=P['data']
        for s in regions[off]:
            struct.pack_into('<Q',out,s.info_off+0x00,new_off)
            if P['swapped']:
                struct.pack_into('<I',out,s.info_off+0x0c,P['size'])
                struct.pack_into('<I',out,s.info_off+0x10,P['nsamp'])
                struct.pack_into('<I',out,s.info_off+0x28,P['nsamp'])  # second sample-count copy
                struct.pack_into('<H',out,s.info_off+0x18,P['rate'])   # match PS3 rate
                # numStates (@+0x34) is NOT a buffer-sizing field. In the engine's sound
                # lookup (FUN_008880f0 @ 0x8880f0) it is the COPY LENGTH of the WaveInfo
                # header + ADPCM states array into a 0x800-bounded buffer:
                #     len = numStates*3 + 0x38   (for codec 0x400)
                # Growing numStates to ceil(grown_size/2048) makes that copy overrun the
                # buffer and corrupt the adjacent bank object -> crash in the by-hash search
                # (the Chinese Takeout crash). For our MP3 sounds the ADPCM states are dead
                # data (the hook decodes MP3 and never reads them), so numStates only has to
                # stay small enough not to overflow. The ORIGINAL value is proven safe (slot
                # mode never touched it), so we leave the verbatim header value untouched -
                # we deliberately do NOT patch +0x34.
    blob+=b'\x00'*((-len(blob))%align); out+=blob
    return bytes(out), stats

def swap_bank(pc_bytes, ps3_bytes, mp3packer_exe, tmpdir=None, dry=False,
              keep_info=False, grow=True, match_length=False, log=print):
    """Default = grow mode (rebuild data region, fit the whole MP3). grow=False
    selects slot mode (v1: write into the fixed PC slot, truncate the tail)."""
    tmp = tmpdir or tempfile.mkdtemp(prefix="bankswap_")
    os.makedirs(tmp, exist_ok=True)
    if grow:
        return _swap_bank_grow(pc_bytes, ps3_bytes, mp3packer_exe, tmp, dry,
                               keep_info, match_length, log)
    return _swap_bank_slot(pc_bytes, ps3_bytes, mp3packer_exe, tmp, dry, log)

def verify_bank(bank_bytes, log=print):
    b = parse_bank(bank_bytes); ok = True
    for s in b['sounds']:
        end = s.dabs + s.size
        if s.dabs < b['base'] or end > len(bank_bytes):
            log("  [VERIFY-FAIL] i=%d data %#x..%#x out of bounds"%(s.idx,s.dabs,end)); ok=False
    log("  [verify] %s (%d sounds, %d B)"%("OK" if ok else "FAILED",len(b['sounds']),len(bank_bytes)))
    return ok

def main():
    global LEAD_IN_SAMPLES
    ap=argparse.ArgumentParser(description="GTA IV bank payload swap (PC ADPCM <- PS3 MP3)")
    ap.add_argument("pc_bank"); ap.add_argument("ps3_bank")
    ap.add_argument("-o","--out",required=True)
    ap.add_argument("--mp3packer",default="mp3packer")
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--slot",action="store_true",
                    help="slot mode (v1): write into the fixed PC slot, truncate the tail")
    ap.add_argument("--match-length",action="store_true",help="(grow only) pad to PS3 length")
    ap.add_argument("--keep-info",action="store_true",help="(grow only) keep leading Info/Xing frame")
    ap.add_argument("--lead-in",type=int,default=LEAD_IN_SAMPLES,
                    help="leading samples of encoder/decoder lead-in to strip from each MP3 "
                         "(whole frames only, lossless; default %d, 0 disables)"%LEAD_IN_SAMPLES)
    a=ap.parse_args()
    LEAD_IN_SAMPLES=a.lead_in
    pc=open(a.pc_bank,'rb').read(); ps3=open(a.ps3_bank,'rb').read()
    out,st=swap_bank(pc,ps3,a.mp3packer,dry=a.dry_run,grow=not a.slot,
                     match_length=a.match_length,keep_info=a.keep_info)
    print("stats:",st)
    if st.get('truncated'):
        print("NOTE: %d sound(s) had their MP3 trimmed to the padded slot. This is benign "
              "overhead trimming (frames beyond the slot's playback length) - no audible "
              "loss; the slot still plays its full 2*size samples." % st['truncated'])
    if st.get('skipped_toobig'):
        print("NOTE: %d sound(s) had no usable slot space -> kept as original ADPCM "
              "(lossless fallback)." % st['skipped_toobig'])
    if st['audio_cut']:
        print("NOTE: %d sound(s) had PS3 source audio genuinely LONGER than the PC slot's "
              "duration -> ~%.0fms real tail not played. This is the inherent slot-mode "
              "limit (would need grow mode); everything else is unaffected."
              % (st['audio_cut'], st['max_cut_ms']))
    if not a.dry_run:
        verify_bank(out)
        open(a.out,'wb').write(out); print("written:",a.out)

if __name__=="__main__":
    main()
