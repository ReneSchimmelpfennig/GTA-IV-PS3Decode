# Diagnostic builds

Optional logging variants used while reverse-engineering. Not needed for normal
use; kept because they're handy if you extend the mod or port to another version.
Each writes to `gta4mp3.log` next to the game exe.

- `dllmain_feedcheck.cpp` — logs per call: cumulative input offset + first bytes of
  `inData`. Used to prove the game feeds contiguous MP3.
- `dllmain_diag_dropout.cpp` — logs underruns / resyncs / skipped ("junk") bytes
  with frame sizes. Used to localise the dropout cause.
