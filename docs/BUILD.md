# Building the ASI

The plugin is a 32-bit Windows DLL renamed to `.asi`, loaded by an ASI loader
(e.g. Ultimate ASI Loader) from `...\Grand Theft Auto IV\GTAIV\plugins\`.

## Requirements

- **Visual Studio 2022** (or any toolchain producing 32-bit Windows DLLs)
- **MinHook** — https://github.com/TsudaKageyu/minhook (BSD-2-Clause)
- **minimp3.h** — https://github.com/lieff/minimp3 (CC0)

## Project setup

1. Create a **Dynamic-Link Library (.dll)**, **Win32 / x86**, **Release** project.
2. Add `dllmain.cpp` to the project.
3. Add the MinHook sources (or link the MinHook static lib) and put `minimp3.h`
   next to `dllmain.cpp`.
4. Build. Rename the resulting `.dll` to `GTA4MP3.asi`.
5. Copy `GTA4MP3.asi` to `...\Grand Theft Auto IV\GTAIV\plugins\`.

> `dllmain.cpp` does `#define MINIMP3_IMPLEMENTATION` itself, so `minimp3.h`
> needs no separate compilation unit.

## Target / addresses

Built and tested against **GTAIV.exe v1.2.0.59** (Steam Complete Edition),
ImageBase `0x400000`. The hook RVAs live at the top of `dllmain.cpp`:

```cpp
static const uintptr_t RVA_Decode = 0x00497440;  // audIMA_ADPCM_Decode
static const uintptr_t RVA_Init   = 0x0048F0D0;  // audVoicePcAdpcm::Init
static const uintptr_t STATE_OFF  = 0x14E;        // ADPCM state @ voice+0x14E (hook key)
```

Runtime base is resolved via `GetModuleHandleA(nullptr)` (ASLR-safe). For other
game versions, re-derive these from the addresses/notes in
[`FORMAT.md`](FORMAT.md).

## Tuning

`MARGIN` (currently `640`) is how much look-ahead the decoder keeps before
decoding the last frame in its buffer — this is what eliminates minimp3's
mid-stream "zero-sample frame" loss. `640` is the minimum that keeps a 128 kbps
frame (576 B) plus the next header in view; raise it only if you feed content
above 128 kbps.
