#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rpf3.py  -  RPF3 archive reader/writer with AES (GTA IV)

Standalone, extracted from the proven gtaiv_ps3_allinone.py.
Contains NO audio logic - only reading/writing RPF3 containers.

Hard-won, do-not-touch details:
  - AES-ECB 16x encrypt/decrypt of the TOC
  - AES key extraction from GTAIV.exe (version-dependent offsets)
  - 2 GB boundary: file offsets with bit 31 set are misinterpreted as a directory
    -> every offset must stay < 0x80000000

Dependency: pycryptodome (pip install pycryptodome)
Optional:   pyrpfiv (for hash->name resolution; without it -> hash_XXXXXXXX names)
"""

import os
import struct
import hashlib

try:
    from Crypto.Cipher import AES
except ImportError:
    raise SystemExit("[!] pycryptodome missing: pip install pycryptodome")

# -- RPF3 constants -----------------------------------------------------------
RPF3_MAGIC  = b'RPF3'
HEADER_SIZE = 20
TOC_START   = 2048
ENTRY_SIZE  = 16

KEY_OFFSETS = {
    '1.0.4.0': 12037876, '1.0.4r2': 12456816, '1.0.6.0': 12477760,
    '1.0.7.0': 12481856, '1.0.8.0': 13197272,
    '1.2.0.32': 12956476, '1.2.0.43': 12956476, '1.2.0.59': 12957500,
}
KEY_SHA1 = 'DEA375EF1E6EF2223A1221C2C575C47BF17EFA5E'

LIMIT_2GB = 0x80000000


# -- AES ----------------------------------------------------------------------
def extract_aes_key(exe_path):
    """Finds the RPF3 AES key in GTAIV.exe (via known offsets + SHA1 check)."""
    with open(exe_path, 'rb') as f:
        for version, offset in KEY_OFFSETS.items():
            f.seek(offset)
            key = f.read(32)
            if len(key) == 32 and hashlib.sha1(key).hexdigest().upper() == KEY_SHA1:
                return key
    raise RuntimeError("AES key not found - check the GTAIV.exe path and version")


def aes_crypt(data, key, encrypt=False):
    """AES-ECB 16x (GTA IV RPF3 TOC encryption)."""
    result = bytes(data)
    for _ in range(16):
        cipher = AES.new(key, AES.MODE_ECB)
        result = cipher.encrypt(result) if encrypt else cipher.decrypt(result)
    return result


# -- helpers ------------------------------------------------------------------
def align2048(n):
    return (n + 2047) & ~2047


def _load_hashes():
    """hash->name map from pyrpfiv (optional)."""
    try:
        import pyrpfiv
        path = os.path.join(os.path.dirname(pyrpfiv.__file__), 'hashes.ini')
        name_map = {}
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('['):
                    k, v = line.split('=', 1)
                    try:
                        name_map[int(k)] = v
                    except ValueError:
                        pass
        return name_map
    except Exception:
        return {}


# -- reader -------------------------------------------------------------------
def rpf3_read(rpf_path, aes_key):
    """
    Reads an RPF3 file. Returns a dict with 'entries' (list).
    entries: type='directory' (name_hash, content_index, content_count)
             type='file'      (name_hash, size, offset, data)
    """
    with open(rpf_path, 'rb') as f:
        raw = f.read()

    magic = raw[:4]
    if magic != RPF3_MAGIC:
        raise ValueError("not an RPF3 archive: magic=%r, expected %r" % (magic, RPF3_MAGIC))
    toc_size, entry_count, unknown, encrypted = struct.unpack_from('<IiiI', raw, 4)

    # Sanity-check the little-endian header. Garbage here (huge/negative counts, an
    # out-of-range TOC) means we are reading the header the wrong way round - almost
    # always a big-endian PS3/360 console RPF fed to this little-endian PC reader.
    # Without this guard the bad values cause a MemoryError (giant entry_count) or an
    # AES "not block aligned" error further down; catch it here with a clear message.
    plausible = (0 < toc_size <= len(raw)
                 and 0 <= entry_count <= 1_000_000
                 and entry_count * ENTRY_SIZE <= toc_size + ENTRY_SIZE)
    if not plausible:
        be_toc, be_cnt = struct.unpack_from('>Ii', raw, 4)
        hint = ""
        if 0 < be_toc <= len(raw) and 0 <= be_cnt <= 1_000_000:
            hint = (" Read big-endian the header is plausible (toc=%d, entries=%d) -> this "
                    "is a big-endian PS3/360 console RPF. This tool reads little-endian PC "
                    "RPF3 only; use a PC (little-endian) RPF as the PS3 source." % (be_toc, be_cnt))
        raise ValueError("RPF3 header invalid as little-endian (toc_size=%d, entry_count=%d)."
                         "%s" % (toc_size, entry_count, hint))

    toc_raw = raw[TOC_START: TOC_START + toc_size]
    if encrypted:
        if len(toc_raw) % 16 != 0:
            raise ValueError("encrypted TOC is not 16-byte aligned (len=%d) - file is "
                             "corrupt or not a PC RPF3." % len(toc_raw))
        toc_raw = aes_crypt(toc_raw, aes_key, encrypt=False)

    entries = []
    for i in range(entry_count):
        off = i * ENTRY_SIZE
        name_hash, d1, d2, d3 = struct.unpack_from('<IIII', toc_raw, off)
        is_dir = (d2 & 0x80000000) != 0
        d2 &= 0x7FFFFFFF
        if is_dir:
            entries.append({
                'type': 'directory', 'name_hash': name_hash,
                'content_count': d1, 'content_index': d2,
                'unknown': d3, 'index': i,
            })
        else:
            abs_offset = d2
            size = d1
            data = raw[abs_offset: abs_offset + size] if size > 0 else b''
            entries.append({
                'type': 'file', 'name_hash': name_hash,
                'size': size, 'offset': abs_offset,
                'unknown': d3, 'index': i, 'data': data,
            })

    name_map = _load_hashes()
    for e in entries:
        e['name'] = name_map.get(e['name_hash'], f'hash_{e["name_hash"]:08X}')

    return {
        'magic': magic, 'toc_size': toc_size, 'entry_count': entry_count,
        'unknown': unknown, 'encrypted': encrypted, 'entries': entries,
        'aes_key': aes_key,
    }


# -- writer -------------------------------------------------------------------
def rpf3_write(rpf_out_path, rpf_info, replacements, compact=False):
    """
    Writes a new RPF3 file.

    Two layout strategies:

    * compact=False (LAYOUT-PRESERVING, for slot mode): each entry is written back
      at its ORIGINAL byte offset. A replacement that fits its original slot is
      written in place, so unchanged / same-size entries keep their exact offsets
      and the file keeps its exact size. Only a genuinely larger replacement is
      appended at the end. Good when almost nothing grows.

    * compact=True (COMPACT REBUILD, for grow mode): every file is repacked densely
      from data_start (2048-aligned), discarding the dead space the in-place+append
      scheme would leave behind. In grow mode EVERY swapped bank is larger than its
      slot, so the layout-preserving scheme would append all of them and leave the
      originals as dead weight -> the archive roughly DOUBLES and large ones cross
      the 2 GB offset limit (offsets with bit 31 set read as directories). Compacting
      writes each entry exactly once, so the file ends up ~original size + the MP3-vs-
      ADPCM overhead (~10-20%), staying well under 2 GB. This is safe: the crash was
      payload > size, never the RPF layout (that was ruled out), so re-laying-out the
      archive cannot reintroduce it.

    replacements: {name_hash: new_bytes}. 2 GB safeguard: any blob that would still
    push the file past 2 GB is kept original (offsets >= 2 GB read as directories).
    """
    entries   = rpf_info['entries']
    aes_key   = rpf_info['aes_key']
    encrypted = rpf_info['encrypted']
    toc_size  = rpf_info['toc_size']

    file_entries = [e for e in entries if e['type'] == 'file']
    data_start   = align2048(TOC_START + toc_size)

    # Original end-of-data = highest (offset + padded size). This is the size we
    # preserve; appended (oversized) blobs extend past it.
    orig_end = data_start
    for e in file_entries:
        end = e['offset'] + align2048(e['size'])
        if end > orig_end:
            orig_end = end

    # Decide placement.
    placement  = {}     # name_hash -> (absolute_offset, blob)
    over_limit = []
    if compact:
        # COMPACT REBUILD: repack every file densely from data_start, in original-offset
        # order (keeps relative locality), discarding all dead space.
        cur = data_start
        for e in sorted(file_entries, key=lambda e: e['offset']):
            h = e['name_hash']
            blob = replacements.get(h)
            if blob is None:
                blob = e['data']
            elif cur + align2048(len(blob)) >= LIMIT_2GB:
                # Extremely unlikely once compacted, but if even this layout would cross
                # 2 GB, fall back to the smaller original (ADPCM) for this one entry.
                blob = e['data']; over_limit.append(e.get('name', f"hash_{h:08X}"))
            placement[h] = (cur, blob)
            cur += align2048(len(blob))
        total = cur
    else:
        # LAYOUT-PRESERVING: in-place when the blob fits its original slot; otherwise the
        # replacement grew and is appended at the end (slot left as dead space).
        append_list = []
        for e in file_entries:
            h    = e['name_hash']
            repl = replacements.get(h)
            if repl is not None and len(repl) > e['size']:
                append_list.append((e, repl))
            else:
                placement[h] = (e['offset'], repl if repl is not None else e['data'])
        total = orig_end
        for e, blob in append_list:
            h = e['name_hash']
            if total + len(blob) >= LIMIT_2GB:          # would cross 2 GB -> keep original
                placement[h] = (e['offset'], e['data'])
                over_limit.append(e.get('name', f"hash_{h:08X}"))
                continue
            placement[h] = (total, blob)
            total += align2048(len(blob))

    if over_limit:
        print(f"    [!] {len(over_limit)} files would cross the 2 GB boundary - kept original.")

    # Build the whole file image at the preserved size and overlay each entry at
    # its absolute offset. Entries never overlap header/TOC (offsets >= data_start).
    buf = bytearray(total)
    for h, (offset, blob) in placement.items():
        buf[offset:offset + len(blob)] = blob       # surrounding padding stays zero

    # TOC: original offset/size for every entry, updated only where we changed it.
    toc_data = bytearray(toc_size)
    for e in entries:
        off = e['index'] * ENTRY_SIZE
        if e['type'] == 'directory':
            d2 = e['content_index'] | 0x80000000
            struct.pack_into('<IIII', toc_data, off,
                             e['name_hash'], e['content_count'], d2, e['unknown'])
        else:
            h = e['name_hash']
            offset, blob = placement[h]
            struct.pack_into('<IIII', toc_data, off,
                             h, len(blob), offset, e['unknown'])

    toc_enc = aes_crypt(bytes(toc_data), aes_key, encrypt=True) if encrypted else bytes(toc_data)

    header = struct.pack('<4sIiiI', rpf_info['magic'], toc_size,
                         rpf_info['entry_count'], rpf_info['unknown'],
                         rpf_info['encrypted'])

    # Overlay header + TOC into the image, then write it out in one piece.
    buf[0:len(header)] = header
    buf[TOC_START:TOC_START + len(toc_enc)] = toc_enc

    with open(rpf_out_path, 'wb') as f:
        f.write(buf)


# -- self test ----------------------------------------------------------------
if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        exe, rpf = sys.argv[1], sys.argv[2]
        key = extract_aes_key(exe)
        print("AES key found:", key.hex())
        info = rpf3_read(rpf, key)
        print("RPF3:", info['magic'], "entries:", info['entry_count'],
              "encrypted:", info['encrypted'])
        files = [e for e in info['entries'] if e['type'] == 'file']
        dirs  = [e for e in info['entries'] if e['type'] == 'directory']
        print(f"  {len(files)} files, {len(dirs)} directories")
        for e in files[:8]:
            print(f"    {e['name']}  size={e['size']}  off=0x{e['offset']:X}")
    else:
        print("usage: python rpf3.py <GTAIV.exe> <archive.rpf>")
