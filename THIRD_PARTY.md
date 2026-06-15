# Third-party components

This project depends on the following, which are **not** included and keep their
own licenses. Download them yourself and place as noted in the README.

| Component | License | Use | Where |
|-----------|---------|-----|-------|
| [minimp3](https://github.com/lieff/minimp3) | CC0 / public domain | MP3 decoding inside the ASI | `asi/minimp3.h` |
| [MinHook](https://github.com/TsudaKageyu/minhook) | BSD-2-Clause | function hooking | build into `asi/` |
| [mp3packer](https://github.com/Reuben-Thorpe/mp3packer) | GPL | lossless VBR→CBR repacking (external tool, called by the converters) | on PATH or via `--mp3packer` |

If you redistribute builds that statically include MinHook, include its BSD-2-Clause
notice. mp3packer is invoked as a separate executable, not linked.
