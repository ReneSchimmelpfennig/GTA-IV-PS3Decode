// ============================================================================
//  GTA IV - MP3 decoder  -  Fix #9 "MARGIN + FIFO + PRE-ROLL"  (PRODUCTION)
//  ---------------------------------------------------------------------------
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

static VC* GetCtx(void* s) {
    EnterCriticalSection(&g_cs);
    VC* c; auto it = g_ctx.find(s);
    if (it != g_ctx.end()) c = it->second;
    else { c = new VC(); mp3dec_init(&c->mp3); c->inpos = 0; c->isMp3 = -1; g_ctx[s] = c; }
    LeaveCriticalSection(&g_cs);
    return c;
}
static bool SC(void* d, const void* s, size_t n) {
    __try { memcpy(d, s, n); return true; } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}

// Decodes the available whole frames -> mono samples into fifo.
// MARGIN: until the stream end is reached, do NOT touch the last frame in the
// buffer (otherwise a 0-sample frame). The held-back bytes carry into the next
// call.
static void DecodeAll(VC* c) {
    mp3dec_frame_info_t info;
    int16_t fp[MINIMP3_MAX_SAMPLES_PER_FRAME];
    const size_t MAXF = 1600;
    for (;;) {
        size_t avail = (c->inpos <= c->inbuf.size()) ? (c->inbuf.size() - c->inpos) : 0;
        if (avail < 4) break;
        if (avail < MARGIN) break;                 // keep look-ahead -> wait for the next chunk
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

int __fastcall hkDecode(void* st, void* edx, int16_t* outPcm, void* inData, unsigned int nib, int numSamples) {
    if (numSamples <= 0 || !outPcm || !inData)
        return oDecode(st, edx, outPcm, inData, nib, numSamples);
    VC* c = GetCtx(st);
    int want = numSamples * 2;

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
            // PRE-ROLL: inject lead-in silence at the FRONT, once, at latch. The
            // first chunk is not a whole number of frames, so the first call is
            // ~1024 samples short; padding at the END drops that gap ~2s into
            // playback (audible hiccup). Front-loading it shifts the whole stream
            // ~68ms later and plays gap-free, and leaves a FIFO cushion that
            // absorbs mid-stream frame-quantization dips. Gated by rate: only
            // streamed radio/cutscene (>=32kHz) has this deficit; low-rate banks
            // are one-shot and would only lose their tail end -> skip them.
            if (PREROLL_SAMPLES > 0 && pi.hz >= PREROLL_MIN_HZ)
                c->fifo.insert(c->fifo.begin(), (size_t)PREROLL_SAMPLES, 0);
            Emit(c, outPcm, want);
            return numSamples * 4;
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
    void* st = (void*)((char*)voice + STATE_OFF);
    EnterCriticalSection(&g_cs);
    auto it = g_ctx.find(st);
    if (it != g_ctx.end()) { delete it->second; g_ctx.erase(it); }
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
