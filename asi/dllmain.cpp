// ============================================================================
//  GTA IV - MP3 decoder  -  Fix #15 "SHORT-CLIP FRAME WALK" (PRODUCTION)
//  ---------------------------------------------------------------------------
//  Short one-shot bank lines (e.g. Niko's jump grunt) played as noise: minimp3's
//  mp3dec_decode_frame refuses to CONFIRM a frame unless enough following frames
//  fit in the buffer. Our probe read window is numSamples bytes = ~2x the real
//  MP3 size, so the tail is neighbouring-sound bytes; minimp3's multi-frame
//  confirmation hits that garbage, returns ps=0, and we fall back to ADPCM ->
//  noise. Fix: when the normal probe fails on a one-shot bank (vseg==1), locate
//  the MP3 start (sync chain, ADPCM-safe) and decode FRAME BY FRAME, handing
//  minimp3 exactly one frame per call (its single-frame path needs no following
//  frame), stopping when the header chain breaks = end of the real payload.
//  Verified against minimp3: a 480-byte exact-frame call decodes; the whole-
//  buffer call on the same short clip returns ps=0 (the bug). Streamed/radio
//  (vseg==2) and all clips minimp3 already accepts are untouched.
// ============================================================================
//  GTA IV - MP3 decoder  -  Fix #14 "UNCONDITIONAL PRE-oInit RESET"
//  ---------------------------------------------------------------------------
//  hkInit resets our per-voice decoder context for EVERY voice, BEFORE oInit.
//  Rationale, established by diagnosis:
//   - A one-shot BANK voice decodes its whole sound DURING oInit, so the only
//     point at which we can hand it a clean context is before oInit. Earlier
//     builds that gated the reset (bit-0x10, or segCount read before/after
//     oInit) left a bank inheriting a recycled slot's stale context (isMp3 + old
//     inbuf from a previous radio/bank sound) -> intermittent bank noise.
//   - Resetting unconditionally before oInit also keeps radio tune-in clean
//     (streamed voices decode later via DecodeDriver, off the fresh context).
//  The "It's Your Call" softlock that earlier accompanied an unconditional reset
//  was NOT the reset itself: it was TrimLeadingSilence eating the onset of quiet
//  one-shot lines on the fresh-probe path. That trim has been removed (Fix #14),
//  so resetting every voice is now safe. Decode hook otherwise unchanged.
//  Verified in-game: radio clean, no softlock, ambient/scripted speech correct;
//  slotcheck.py confirmed the converted speech RPFs carry no broken swaps.
// ============================================================================
//  GTA IV - MP3 decoder  -  Fix #11 "BANK TAIL RECOVERY"
//  ---------------------------------------------------------------------------
//  Fix #11 (over #10): one-shot bank voice lines were clipped at the END (the
//  payload/crash work was done; this is the remaining audible defect). Two
//  rate-gated causes, both on the bank path (the latch's non-preroll branch):
//   (a) MARGIN look-ahead is never flushed. DecodeAll() holds back the last
//       ~MARGIN bytes so it won't decode a partial frame; for a STREAMED voice
//       those bytes ride into the next chunk, but a bank is ONE call -> the last
//       ~1-1.5 frames were never decoded and the line's end was simply missing.
//       Fix: DecodeAll(c, flush=true) on the bank branch decodes the held-back
//       tail frame(s).
//   (b) Lead-in handling. Earlier builds used TrimLeadingSilence on the bank
//       branch to drop the PS3 encoder/decoder lead-in (~2116 samples @24kHz).
//       REMOVED in Fix #14: on the fresh-probe path it could eat a quiet line's
//       onset and stall scripted progress (softlock). We now keep the small
//       lead-in as harmless leading silence instead.
//  Streamed radio/cutscene (>=32kHz) is unchanged: it still gets the FRONT
//  pre-roll. Everything else below is byte-for-byte Fix #10.
//  ---------------------------------------------------------------------------
//  Fix #10 "CONTEXT-LIFETIME SAFETY":
//  Fix #10 (over #9): the per-voice context was delete()d by hkInit while the
//  audio thread could still be decoding into it (the decode body held no lock).
//  A scripted voice re-init mid-playback (reproducible in TBoGT "Chinese
//  Takeout") then freed the context under an in-flight decode -> access
//  violation, while unrelated voices (e.g. the restored ADPCM radio) kept
//  playing right up to the crash. Now: the context is RESET IN PLACE on Init
//  (never freed during play) and the whole decode body holds g_cs, so Init and
//  Decode for any voice are serialized. Output was already hard-clamped by
//  Emit() (writes exactly 2x byteCount samples), so this is a lifetime fix, not
//  an overflow fix.
//  ---------------------------------------------------------------------------
//  Fix #9 "MARGIN + FIFO + PRE-ROLL":
//  Dropout cause (proven offline on ch0_r.mp3): when fed in chunks, minimp3
//  decodes the LAST frame in the buffer speculatively, before the next frame
//  header has arrived -> it can't validate -> 0 samples, frame lost
//  (~1 per chunk boundary, 3.2% silence). Fix:
//   (1) MARGIN: don't touch the last frame until >= MARGIN bytes of look-ahead
//       are buffered (minimp3 sees the next header -> full samples).
//   (2) FIFO: buffer all completed frames, emit 'want' from it -> smooths the
//       integer-frame quantization.
//   (3) PRE-ROLL: the first 32768-byte chunk is not a whole number of frames,
//       so the first call is ~1024 samples (32ms) short. Fix #8 padded that at
//       the END of the first buffer -> the silence surfaced ~2s into playback as
//       an audible hiccup (confirmed by the tune-in decode log). Inject the
//       lead-in silence at the FRONT instead: the stream starts ~68ms later
//       (inaudible, hidden in the station switch) and then plays gap-free. The
//       cushion also absorbs the small mid-playback frame-quantization dips.
//       Rate-gated: applied only to streamed radio/cutscene audio (>=32kHz);
//       low-rate one-shot speech/pain banks are skipped so their tail isn't cut.
//  Result: dropout-free steady state (Fix #8) with the tune-in gap moved to an
//  inaudible lead-in. Best paired with the tag-cleaned pipeline (clean_stream),
//  which makes the latch deficit a uniform 1024 on every stream.
// ============================================================================
#include <windows.h>
#include <cstdint>
#include <vector>
#include <map>
#define MINIMP3_IMPLEMENTATION
#define MINIMP3_NO_STDIO
#include "minimp3.h"
#include "MinHook.h"

static const uintptr_t RVA_Decode = 0x00497440;
static const uintptr_t RVA_Init   = 0x0048F0D0;
static const uintptr_t STATE_OFF  = 0x14E;
static const size_t    MARGIN     = 640;    // look-ahead before the last frame (>=580 needed:
                                            // frame 576 + next header 4). 640 = minimal warm-up
                                            // (~32ms, one blip on tune-in) for strict CBR-128.
                                            // Raise it only if content were >128 kbps
                                            // (the pipeline never produces that).
static const int       PREROLL_SAMPLES = 2176;  // lead-in silence injected at latch
                                            // (see header). 2176 @32kHz = ~68ms: covers
                                            // the first-chunk deficit + 1 frame cushion.
static const int       PREROLL_MIN_HZ  = 32000; // only pre-roll streamed audio >= this
                                            // rate (radio/cutscene); NOT low-rate banks.

typedef int(__fastcall* Decode_t)(void*, void*, int16_t*, void*, unsigned int, int);
static Decode_t oDecode = nullptr;
typedef void(__fastcall* Init_t)(void*, void*, void*);
static Init_t oInit = nullptr;

struct VC {
    mp3dec_t mp3;
    std::vector<uint8_t> inbuf;
    size_t inpos;
    std::vector<int16_t> fifo;
    int isMp3;
};
static std::map<void*, VC*> g_ctx;
static CRITICAL_SECTION g_cs;

// RAII lock held across the WHOLE decode body, so an Init on another thread can
// neither reset nor free a voice context while we are decoding into it.
struct Lock { Lock() { EnterCriticalSection(&g_cs); } ~Lock() { LeaveCriticalSection(&g_cs); } };

// Caller MUST already hold g_cs. A context is created once per voice slot and then
// reused; hkInit resets it IN PLACE (never frees it during play), so a pointer
// handed out here can never be invalidated under a concurrent/ongoing decode.
static VC* GetCtxLocked(void* s) {
    auto it = g_ctx.find(s);
    if (it != g_ctx.end()) return it->second;
    VC* c = new VC(); mp3dec_init(&c->mp3); c->inpos = 0; c->isMp3 = -1; g_ctx[s] = c;
    return c;
}
static bool SC(void* d, const void* s, size_t n) {
    __try { memcpy(d, s, n); return true; } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}


// Decodes the available whole frames -> mono samples into fifo.
// Strict MPEG-1/2/2.5 Layer III frame length (bytes) for a header at p, else 0.
// Used to walk short one-shot bank clips frame-by-frame (see DecodeBankFrames).
static int mp3_frame_len(const uint8_t* p, int avail) {
    if (avail < 4) return 0;
    if (p[0] != 0xFF || (p[1] & 0xE0) != 0xE0) return 0;        // frame sync
    int ver = (p[1] >> 3) & 3, layer = (p[1] >> 1) & 3;
    if (ver == 1 || layer != 1) return 0;                       // ver 01 reserved; layer 01 = III
    int br = (p[2] >> 4) & 0xF, sr = (p[2] >> 2) & 3, pad = (p[2] >> 1) & 1;
    if (br == 0 || br == 15 || sr == 3) return 0;
    static const int V1[16] = { 0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0 };
    static const int V2[16] = { 0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0 };
    static const int SR[4][3] = { {11025,12000,8000},{0,0,0},{22050,24000,16000},{44100,48000,32000} };
    int mpeg1 = (ver == 3);
    int kbps = (mpeg1 ? V1[br] : V2[br]) * 1000, rate = SR[ver][sr];
    if (!kbps || !rate) return 0;
    return mpeg1 ? (144 * kbps / rate + pad) : (72 * kbps / rate + pad);
}

// MARGIN: until the stream end is reached, do NOT touch the last frame in the
// buffer (otherwise a 0-sample frame). The held-back bytes carry into the next
// call. flush=true (one-shot banks, which never get a "next call") decodes the
// held-back tail frame(s) too, so the end of the voice line is not lost.
static void DecodeAll(VC* c, bool flush = false) {
    mp3dec_frame_info_t info;
    int16_t fp[MINIMP3_MAX_SAMPLES_PER_FRAME];
    const size_t MAXF = 1600;
    for (;;) {
        size_t avail = (c->inpos <= c->inbuf.size()) ? (c->inbuf.size() - c->inpos) : 0;
        if (avail < 4) break;
        if (!flush && avail < MARGIN) break;       // keep look-ahead -> wait for the next chunk
        int s = mp3dec_decode_frame(&c->mp3, c->inbuf.data() + c->inpos, (int)avail, fp, &info);
        if (info.frame_bytes == 0) {
            if (avail > MAXF) { c->inpos += 1; continue; }   // real junk -> skip
            break;
        }
        c->inpos += info.frame_bytes;
        if (s <= 0) continue;
        size_t base = c->fifo.size();
        c->fifo.resize(base + s);
        if (info.channels == 1)
            for (int i = 0; i < s; i++) c->fifo[base + i] = fp[i];
        else
            for (int i = 0; i < s; i++) c->fifo[base + i] = fp[i * info.channels];
    }
    if (c->inpos > (1u << 20)) {                   // tame memory, keep a reservoir cushion
        size_t keep = 4096;
        size_t cut = (c->inpos > keep) ? (c->inpos - keep) : 0;
        if (cut) { c->inbuf.erase(c->inbuf.begin(), c->inbuf.begin() + cut); c->inpos -= cut; }
    }
}

static void Emit(VC* c, int16_t* out, int want) {
    size_t have = c->fifo.size();
    size_t n = ((size_t)want < have) ? (size_t)want : have;
    if (n) memcpy(out, c->fifo.data(), n * 2);
    if ((size_t)want > n) memset(out + n, 0, ((size_t)want - n) * 2);
    if (n) c->fifo.erase(c->fifo.begin(), c->fifo.begin() + n);
}

// Find the first MP3 frame start within buf confirmed by a SECOND valid header at the
// the predicted offset (sync chaining). The chain rejects stray FF-syncs in ADPCM data;
// a single frame that fills the rest of the buffer is accepted as the last/only frame.
// Scans only a small lead-in window. Returns the offset, or -1.
static int FindMp3Start(const uint8_t* buf, int len) {
    int scan = (len < 1024) ? len : 1024;
    for (int o = 0; o + 4 <= scan; o++) {
        int f1 = mp3_frame_len(buf + o, len - o);
        if (f1 <= 0) continue;
        int nxt = o + f1;
        if (nxt + 4 <= len) {
            if (mp3_frame_len(buf + nxt, len - nxt) > 0) return o;   // 2-frame chain -> MP3
        } else {
            return o;                                                // single/last frame -> accept
        }
    }
    return -1;
}

// One-shot bank decode. minimp3's normal call refuses to CONFIRM a frame unless enough
// following frames fit in the buffer - which fails for short clips, because our read
// window (numSamples bytes) is ~2x the real MP3 size and the tail is neighbouring-sound
// bytes. So we hand minimp3 EXACTLY one frame per call (its single-frame path), walking
// the header chain until it breaks = end of the real MP3 payload. Appends like DecodeAll.
static void DecodeBankFrames(VC* c, const uint8_t* buf, int start, int len) {
    int16_t fp[MINIMP3_MAX_SAMPLES_PER_FRAME];
    mp3dec_frame_info_t info;
    int pos = start;
    while (pos + 4 <= len) {
        int fl = mp3_frame_len(buf + pos, len - pos);
        if (fl <= 0) break;                  // garbage/zeros -> real MP3 ended
        if (pos + fl > len) break;           // last frame only partial -> stop (drop ~1 frame)
        int s = mp3dec_decode_frame(&c->mp3, buf + pos, fl, fp, &info);
        pos += fl;
        if (s <= 0) continue;
        size_t base = c->fifo.size();
        c->fifo.resize(base + s);
        if (info.channels == 1)
            for (int i = 0; i < s; i++) c->fifo[base + i] = fp[i];
        else
            for (int i = 0; i < s; i++) c->fifo[base + i] = fp[i * info.channels];
    }
}

// NOTE (Fix #14): TrimLeadingSilence was removed. On the fresh-probe path it scanned
// up to 2400 leading samples below ~-48 dBFS and erased them - which for a soft-spoken
// one-shot line could eat the entire onset, starving the engine's conversation-progress
// trigger -> the "It's Your Call" softlock. We now keep the small encoder lead-in (a few
// ms of harmless leading silence) instead of risking the line's audible start.


int __fastcall hkDecode(void* st, void* edx, int16_t* outPcm, void* inData, unsigned int nib, int numSamples) {
    if (numSamples <= 0 || !outPcm || !inData)
        return oDecode(st, edx, outPcm, inData, nib, numSamples);  // no context touched
    Lock lk;                       // held until return: serializes with hkInit so a
                                   // voice re-init cannot free/reset this context
                                   // while we decode into it (use-after-free guard).
    VC* c = GetCtxLocked(st);
    int want = numSamples * 2;     // OUTPUT CLAMP: Emit() writes exactly 'want'
                                   // (= 2x byteCount) samples, never more.

    if (c->isMp3 == -1) {
        std::vector<uint8_t> tmp((size_t)numSamples);
        if (!SC(tmp.data(), inData, (size_t)numSamples))
            return oDecode(st, edx, outPcm, inData, nib, numSamples);
        bool z = true; for (size_t i = 0; i < tmp.size(); ++i) if (tmp[i]) { z = false; break; }
        if (z) return oDecode(st, edx, outPcm, inData, nib, numSamples);
        mp3dec_frame_info_t pi; int16_t pp[MINIMP3_MAX_SAMPLES_PER_FRAME];
        int ps = mp3dec_decode_frame(&c->mp3, tmp.data(), (int)tmp.size(), pp, &pi);
        if (ps > 0 && pi.frame_bytes > 0 && pi.hz >= 8000) {
            c->isMp3 = 1; mp3dec_init(&c->mp3);
            int off = pi.frame_offset; if (off < 0 || off >(int)tmp.size()) off = 0;
            c->inbuf.assign(tmp.begin() + off, tmp.end());
            c->inpos = 0; c->fifo.clear();
            DecodeAll(c);
            // PRE-ROLL vs ONE-SHOT, decided by rate (the streamed/bank proxy):
            // - Streamed radio/cutscene (>=32kHz): inject lead-in silence at the FRONT.
            // - One-shot banks (the only call they get): FLUSH the frame(s) MARGIN held
            //   back so the last ~1-1.5 frames are decoded. We no longer trim the lead-in
            //   here (Fix #14): the small encoder/decoder lead-in is kept as harmless
            //   leading silence rather than risk eating a quiet line's onset.
            if (PREROLL_SAMPLES > 0 && pi.hz >= PREROLL_MIN_HZ) {
                c->fifo.insert(c->fifo.begin(), (size_t)PREROLL_SAMPLES, 0);
            } else {
                // One-shot bank: flush the MARGIN-held tail so the whole line decodes.
                // Do NOT trim leading silence (see Fix #14 note above) - it could eat the
                // onset of a quiet line and stall the scripted conversation.
                DecodeAll(c, /*flush=*/true);
            }
            Emit(c, outPcm, want);
            return numSamples * 4;
        }
        // minimp3 could not confirm a frame. For one-shot BANKS this is the short-clip
        // case (read window padded with neighbouring-sound bytes). If a real MP3 frame
        // chain starts here, decode it frame-by-frame; otherwise it is genuine ADPCM.
        {
            int vseg = -1; SC(&vseg, (const char*)st - STATE_OFF + 0x120, sizeof(vseg));
            if (vseg == 1) {
                int o = FindMp3Start(tmp.data(), (int)tmp.size());
                if (o >= 0) {
                    c->isMp3 = 1; mp3dec_init(&c->mp3); c->fifo.clear();
                    DecodeBankFrames(c, tmp.data(), o, (int)tmp.size());
                    Emit(c, outPcm, want);
                    return numSamples * 4;
                }
            }
        }
        c->isMp3 = 0;
    }
    if (c->isMp3 != 1)
        return oDecode(st, edx, outPcm, inData, nib, numSamples);

    size_t old = c->inbuf.size();
    c->inbuf.resize(old + (size_t)numSamples);
    if (!SC(c->inbuf.data() + old, inData, (size_t)numSamples)) c->inbuf.resize(old);
    DecodeAll(c);
    Emit(c, outPcm, want);
    return numSamples * 4;
}

void __fastcall hkInit(void* voice, void* edx, void* p2) {
    // A new sound is starting on this voice slot. Reset our per-voice context BEFORE
    // oInit so whatever decodes next starts fresh:
    //  - one-shot BANK voices decode their WHOLE sound DURING oInit, so the only chance
    //    to hand them a clean context (no inherited isMp3 / stale inbuf from a previous
    //    sound that recycled this slot) is before oInit;
    //  - streamed voices decode later via DecodeDriver, so this covers them too.
    // Resetting unconditionally (not gated on segCount) keeps radio tune-in clean. The
    // bank softlock that earlier accompanied an unconditional reset was caused by
    // TrimLeadingSilence eating quiet one-shot lines on the fresh-probe path - that trim
    // has been removed, so resetting every voice here is safe.
    void* st = (void*)((char*)voice + STATE_OFF);
    EnterCriticalSection(&g_cs);
    {
        auto it = g_ctx.find(st);
        if (it != g_ctx.end()) {
            VC* c = it->second;
            c->inbuf.clear(); c->inpos = 0;
            c->fifo.clear();
            mp3dec_init(&c->mp3);
            c->isMp3 = -1;
        }
    }
    LeaveCriticalSection(&g_cs);

    oInit(voice, edx, p2);
}

static DWORD WINAPI Inst(LPVOID) {
    Sleep(2000);
    InitializeCriticalSection(&g_cs);
    uintptr_t b = (uintptr_t)GetModuleHandleA(nullptr);
    if (MH_Initialize() != MH_OK) return 1;
    void* t = (void*)(b + RVA_Decode);
    if (MH_CreateHook(t, &hkDecode, (void**)&oDecode) != MH_OK) return 1;
    if (MH_EnableHook(t) != MH_OK) return 1;
    void* ti = (void*)(b + RVA_Init);
    if (MH_CreateHook(ti, &hkInit, (void**)&oInit) == MH_OK) MH_EnableHook(ti);
    return 0;
}
BOOL APIENTRY DllMain(HMODULE h, DWORD r, LPVOID) {
    if (r == DLL_PROCESS_ATTACH) { DisableThreadLibraryCalls(h); CreateThread(0, 0, Inst, 0, 0, 0); }
    else if (r == DLL_PROCESS_DETACH) { MH_Uninitialize(); }
    return TRUE;
}
