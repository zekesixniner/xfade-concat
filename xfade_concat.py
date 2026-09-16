#!/usr/bin/env python3
"""xfade_concat.py - join clips with soft transitions, re-encoding only the transitions.

Built for native Windows ffmpeg (run from PowerShell) so NVDEC/NVENC work,
which they do not under WSL1.

Smart mode (default) - the idea from gopro-max-gpx-pipeline's xfade_concat.py,
made frame-exact:
  * the middle of every clip is stream-copied, cut on IDR keyframes only
  * only the short stretches around each transition are re-encoded:
      [last keyframe .. transition] + xfade + [transition .. next keyframe]
  * every piece carries its own VPS/SPS/PPS in-band, so NVENC pieces and the
    camera's/OVRLEY's own bitstream can live in one file (MP4 tag hev1)
  * DTS of every piece is normalised to one common reorder delay, so the
    joined file has monotonic timestamps (no frozen frames at the joins)
Full mode (--mode full) re-encodes everything (any source codec).

Audio is always rebuilt in one cheap pass (acrossfade at the joins, AAC).
All cut points are whole frames; a timeline JSON maps each source clip onto
the output.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

__version__ = "0.2.0"

GOPRO_RE = re.compile(r"^G([A-Z])(\d{2})(\d{4})", re.IGNORECASE)
RENUMBER = "setpts=N/FRAME_RATE/TB"  # hw-frame safe (touches timestamps only)

# HEVC NAL unit types
NAL_LEADING = {6, 7, 8, 9}  # RADL/RASL: decoded after an IRAP but displayed before it
NAL_IRAP = {16, 17, 18, 19, 20, 21}  # BLA, IDR, CRA

# H.273 code points for the hevc_metadata bitstream filter
PRIMARIES = {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
             "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12}
TRANSFER = {"bt709": 1, "gamma22": 4, "gamma28": 5, "smpte170m": 6, "smpte240m": 7,
            "linear": 8, "iec61966-2-4": 11, "bt1361e": 12, "iec61966-2-1": 13,
            "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16, "arib-std-b67": 18}
MATRIX = {"rgb": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
          "ycgco": 8, "bt2020nc": 9, "bt2020c": 10}

# --------------------------------------------------------------------------- #
# messages (EN/SV, --lang or GOPRO_LANG like gopro-max-gpx-pipeline)
# --------------------------------------------------------------------------- #
MESSAGES = {
    "en": {
        "probing": "Probing {n} file(s)...",
        "probing_kf": "Scanning keyframes near the cut points...",
        "encoder_line": "{w}x{h} @ {fps} fps ({fpsf:.3f}), {depth}-bit {codec}, mode={mode}, "
                        "encoder={enc}, decode={dec}",
        "table_head": "{i:>3}  {file:<28} {length:>9} {head:>7} {tail:>7} {fade:>7} {out:>13}",
        "output_line": "Output: {dur} ({frames} frames), {pieces} piece(s): "
                       "copied {copy}, re-encoded {enc}{audio}",
        "with_audio": ", audio",
        "without_audio": ", no audio",
        "cached": "  (cached)",
        "audio": "[audio] building crossfaded audio track",
        "join": "[join] concat -> {name}",
        "done": "Done: {out}\n      {timeline}\n      {frames} frames, {dur}",
        "note_360": "Note: 2:1 frame - run inject360-inplace on the output as the last step.",
        "warn_vfr": "warning: {name} looks VFR (r={r}, avg={a}); cuts may drift",
        "warn_no_audio": "warning: no audio stream {s} in {names} -> output without audio",
        "warn_shorten": "warning: transition {a} -> {b} shortened to {d:.3f}s (clips too short for {want:.3f}s)",
        "warn_no_nvenc": "warning: ffmpeg has no hevc_nvenc -> using libx265 (CPU, slow)",
        "warn_fallback_full": "warning: smart mode needs HEVC 4:2:0 8/10-bit sources ({name}: {codec} {pix}) -> --mode full",
        "warn_no_idr": "warning: {name}: no usable IDR keyframes within {w}s of the cuts -> "
                       "the whole clip is re-encoded (open GOP or very long GOP?)",
        "warn_bframes": "warning: --bframes is ignored in smart mode (re-encoded pieces use 0)",
        "warn_dts": "warning: the join reported timestamp problems:\n{log}",
        "warn_ps": "warning: pieces have different VPS/SPS/PPS; the joined file may not play everywhere",
        "err_ffmpeg": "ffmpeg failed (exit {code}):\n  {cmd}",
        "err_ffprobe": "ffprobe failed on {path}:\n{err}",
        "err_not_found": "'{exe}' not found (add C:\\ffmpeg\\bin to PATH or pass --ffmpeg/--ffprobe)",
        "err_exists": "{out} exists (use -y to overwrite)",
        "err_no_match": "no files match '{pat}'",
        "err_not_file": "not a file: {path}",
        "err_no_video": "no video stream in {path}",
        "err_no_inputs": "need at least one input clip",
        "err_list_and_inputs": "use either --list or input files, not both",
        "err_mismatch": "{a}: {wa}x{ha}@{fa} differs from {b}: {wb}x{hb}@{fb}",
        "err_channels": "audio channel counts differ between clips: {ch}",
        "err_fades_count": "--fades needs {n} comma-separated values (one per junction), got {got}",
        "err_time": "invalid {what} value '{val}' (use seconds like 1.5 or frames like 45f)",
        "err_trim": "{name}: head+tail ({ht:.3f}s) >= clip length ({length:.3f}s)",
        "err_fade_io": "{name}: --fade-in/--fade-out do not fit in the clip",
        "err_frames": "{name}: expected {want} frames, got {got}",
        "err_delay": "{name}: timestamp delay is {got} frame(s), expected {want} - "
                     "source GOP structure changes mid-file? Try --mode full",
        "err_list_kv": "{file}:{line}: expected key=value, got '{tok}'",
        "err_list_key": "{file}:{line}: unknown option '{key}' (head/tail/fade)",
    },
    "sv": {
        "probing": "Läser in {n} fil(er)...",
        "probing_kf": "Letar keyframes nära klippunkterna...",
        "encoder_line": "{w}x{h} @ {fps} fps ({fpsf:.3f}), {depth}-bit {codec}, läge={mode}, "
                        "kodare={enc}, avkodning={dec}",
        "table_head": "{i:>3}  {file:<28} {length:>9} {head:>7} {tail:>7} {fade:>7} {out:>13}",
        "output_line": "Utdata: {dur} ({frames} rutor), {pieces} bit(ar): "
                       "kopierat {copy}, omkodat {enc}{audio}",
        "with_audio": ", ljud",
        "without_audio": ", utan ljud",
        "cached": "  (cachad)",
        "audio": "[ljud] bygger ljudspår med crossfades",
        "join": "[skarv] concat -> {name}",
        "done": "Klart: {out}\n       {timeline}\n       {frames} rutor, {dur}",
        "note_360": "Obs: 2:1-bild - kör inject360-inplace på resultatet som sista steg.",
        "warn_vfr": "varning: {name} verkar ha variabel bildfrekvens (r={r}, avg={a}); klipp kan glida",
        "warn_no_audio": "varning: ljudström {s} saknas i {names} -> utdata utan ljud",
        "warn_shorten": "varning: övergång {a} -> {b} kortad till {d:.3f}s (klippen för korta för {want:.3f}s)",
        "warn_no_nvenc": "varning: ffmpeg saknar hevc_nvenc -> använder libx265 (CPU, långsamt)",
        "warn_fallback_full": "varning: smart-läget kräver HEVC 4:2:0 8/10-bit ({name}: {codec} {pix}) -> --mode full",
        "warn_no_idr": "varning: {name}: inga användbara IDR-keyframes inom {w}s från klippunkterna -> "
                       "hela klippet kodas om (öppen GOP eller mycket lång GOP?)",
        "warn_bframes": "varning: --bframes ignoreras i smart-läget (omkodade bitar använder 0)",
        "warn_dts": "varning: skarvningen rapporterade tidsstämpelproblem:\n{log}",
        "warn_ps": "varning: bitarna har olika VPS/SPS/PPS; filen kanske inte spelas överallt",
        "err_ffmpeg": "ffmpeg misslyckades (felkod {code}):\n  {cmd}",
        "err_ffprobe": "ffprobe misslyckades för {path}:\n{err}",
        "err_not_found": "hittar inte '{exe}' (lägg C:\\ffmpeg\\bin i PATH eller ange --ffmpeg/--ffprobe)",
        "err_exists": "{out} finns redan (använd -y för att skriva över)",
        "err_no_match": "inga filer matchar '{pat}'",
        "err_not_file": "ingen fil: {path}",
        "err_no_video": "ingen videoström i {path}",
        "err_no_inputs": "behöver minst ett klipp",
        "err_list_and_inputs": "använd antingen --list eller filnamn, inte båda",
        "err_mismatch": "{a}: {wa}x{ha}@{fa} skiljer sig från {b}: {wb}x{hb}@{fb}",
        "err_channels": "antal ljudkanaler skiljer mellan klippen: {ch}",
        "err_fades_count": "--fades behöver {n} kommaseparerade värden (ett per skarv), fick {got}",
        "err_time": "ogiltigt värde för {what}: '{val}' (sekunder som 1.5 eller rutor som 45f)",
        "err_trim": "{name}: head+tail ({ht:.3f}s) >= klippets längd ({length:.3f}s)",
        "err_fade_io": "{name}: --fade-in/--fade-out får inte plats i klippet",
        "err_frames": "{name}: väntade {want} rutor, fick {got}",
        "err_delay": "{name}: tidsstämpelfördröjning {got} ruta/rutor, väntade {want} - "
                     "ändras källans GOP-struktur i filen? Prova --mode full",
        "err_list_kv": "{file}:{line}: väntade nyckel=värde, fick '{tok}'",
        "err_list_key": "{file}:{line}: okänt alternativ '{key}' (head/tail/fade)",
    },
}
LANG = "en"


def t(key: str, **kw) -> str:
    return MESSAGES.get(LANG, MESSAGES["en"]).get(key, MESSAGES["en"][key]).format(**kw)


def warn(key: str, **kw) -> None:
    print(t(key, **kw), file=sys.stderr)


def die(key: str, **kw) -> None:
    prefix = "fel" if LANG == "sv" else "error"
    print(f"{prefix}: {t(key, **kw)}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fmt_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def run(cmd: list[str], verbose: bool) -> None:
    if verbose:
        print("  $ " + fmt_cmd(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        die("err_ffmpeg", code=res.returncode, cmd=fmt_cmd(cmd))


def secs(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def fsec(frames: int, fps: Fraction) -> str:
    return f"{secs(frames, fps):.9f}"


def hms(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def parse_time(value: str, fps: Fraction, what: str) -> int:
    """'1.5' = seconds, '45f' = frames. Returns frames."""
    v = str(value).strip().lower()
    try:
        n = int(v[:-1]) if v.endswith("f") else round(Fraction(v) * fps)
    except (ValueError, ZeroDivisionError):
        die("err_time", what=what, val=value)
    if n < 0:
        die("err_time", what=what, val=value)
    return n


def expand_inputs(items: list[str]) -> list[Path]:
    """PowerShell does not expand wildcards for native programs - do it here."""
    out: list[Path] = []
    for item in items:
        if any(ch in item for ch in "*?["):
            matches = sorted(glob.glob(item))
            if not matches:
                die("err_no_match", pat=item)
            out.extend(Path(m) for m in matches)
        else:
            out.append(Path(item))
    return out


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #
@dataclass
class Keyframe:
    idx: int  # frame index (presentation order)
    pts: int  # ticks, source stream time base
    delay: int  # pts - dts in frames
    safe: bool = True  # no leading pictures -> frame-exact cut point


@dataclass
class Clip:
    path: Path
    frames: int
    fps: Fraction
    tb: Fraction
    width: int
    height: int
    pix_fmt: str
    codec: str
    v_start: float
    f_start: float
    audio: list[dict]
    color: dict
    gopro: tuple | None = None  # (letter, chapter, number)
    head: int = 0
    tail: int = 0
    fade: int = 0  # transition into the NEXT clip
    head_raw: str | None = None
    tail_raw: str | None = None
    fade_raw: str | None = None
    out_start: int = 0
    keyframes: dict = field(default_factory=dict)  # idx -> Keyframe

    @property
    def fdur_ticks(self) -> Fraction:
        return (1 / self.fps) / self.tb


def ffprobe_json(ffprobe: str, args: list[str], path: Path) -> dict:
    cmd = [ffprobe, "-v", "error", *args, "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        die("err_ffprobe", path=path, err=res.stderr.strip())
    return json.loads(res.stdout)


def probe_clip(ffprobe: str, path: Path) -> Clip:
    if not path.is_file():
        die("err_not_file", path=path)
    info = ffprobe_json(
        ffprobe,
        ["-show_entries",
         "format=start_time:stream=index,codec_type,codec_name,width,height,pix_fmt,"
         "r_frame_rate,avg_frame_rate,time_base,nb_frames,start_time,color_range,"
         "color_space,color_transfer,color_primaries,channels,channel_layout"],
        path)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        die("err_no_video", path=path)
    v = video[0]
    fps = Fraction(v["r_frame_rate"])
    avg = v.get("avg_frame_rate", "0/0")
    if avg not in ("0/0", v["r_frame_rate"]) and abs(float(Fraction(avg) - fps)) > 0.01:
        warn("warn_vfr", name=path.name, r=fps, a=Fraction(avg))
    frames = int(v.get("nb_frames") or 0)
    if frames <= 0:
        cnt = ffprobe_json(ffprobe, ["-select_streams", "v:0", "-count_packets",
                                     "-show_entries", "stream=nb_read_packets"], path)
        frames = int(cnt["streams"][0]["nb_read_packets"])
    color = {k: v[k] for k in ("color_range", "color_space", "color_transfer",
                               "color_primaries") if v.get(k) and v[k] != "unknown"}
    m = GOPRO_RE.match(path.name)
    return Clip(
        path=path.resolve(), frames=frames, fps=fps, tb=Fraction(v["time_base"]),
        width=int(v["width"]), height=int(v["height"]), pix_fmt=v.get("pix_fmt", ""),
        codec=v.get("codec_name", ""),
        v_start=float(v.get("start_time") or 0.0),
        f_start=float(info.get("format", {}).get("start_time") or 0.0),
        audio=[s for s in streams if s.get("codec_type") == "audio"],
        color=color,
        gopro=(m.group(1).upper(), int(m.group(2)), int(m.group(3))) if m else None)


def scan_keyframes(ffmpeg: str, clip: Clip, start_s: float, dur_s: float) -> None:
    """Stream-copy a window through trace_headers (no decoding) and record keyframes.

    A keyframe (IRAP) is a safe, frame-exact cut point only if no leading pictures
    (RADL/RASL) follow it: those are decoded after it but belong to the previous
    stretch in display order (open GOP, IDR_W_RADL with leading pictures)."""
    seek = max(0.0, start_s - clip.f_start)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "debug", "-ss", f"{seek:.6f}", "-copyts",
           "-i", str(clip.path), "-t", f"{dur_s:.3f}", "-map", "0:v:0", "-c", "copy",
           "-bsf:v", "trace_headers", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    pkt_re = re.compile(r"Packet: \d+ bytes, (key frame, )?pts (-?\d+|NOPTS), dts (-?\d+|NOPTS)")
    nal_re = re.compile(r"nal_unit_type: (\d+)\(")
    cur = None  # [key, pts, dts, first_vcl_type]
    tracking: Keyframe | None = None
    for line in res.stderr.splitlines():
        if "trace_headers" not in line:
            continue
        m = pkt_re.search(line)
        if m:
            key = bool(m.group(1))
            if m.group(2) == "NOPTS" or m.group(3) == "NOPTS":
                cur = None
                continue
            cur = [key, int(m.group(2)), int(m.group(3)), None]
            continue
        m = nal_re.search(line)
        if not m or cur is None or cur[3] is not None:
            continue
        nal = int(m.group(1))
        if nal >= 32:  # parameter sets / SEI
            continue
        cur[3] = nal
        key, pts, dts = cur[0], cur[1], cur[2]
        if nal in NAL_LEADING:
            if tracking is not None:
                tracking.safe = False
        elif nal not in NAL_IRAP:  # first trailing picture ends the leading run
            tracking = None
        if key and nal in NAL_IRAP:
            idx = round((float(pts * clip.tb) - clip.v_start) * clip.fps)
            kf = Keyframe(idx=idx, pts=pts, delay=round((pts - dts) / clip.fdur_ticks))
            clip.keyframes[idx] = kf
            tracking = kf


# --------------------------------------------------------------------------- #
# list file
# --------------------------------------------------------------------------- #
def read_list_file(list_path: Path) -> list[tuple[Path, dict]]:
    """Lines: <path> [head=..] [tail=..] [fade=..]   ('#' starts a comment)."""
    rows: list[tuple[Path, dict]] = []
    base = list_path.resolve().parent
    for lineno, raw in enumerate(list_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip('"').strip("'") for p in shlex.split(line, posix=False)]
        p = Path(parts[0])
        if not p.is_absolute():
            p = base / p
        opts: dict = {}
        for tok in parts[1:]:
            if "=" not in tok:
                die("err_list_kv", file=list_path, line=lineno, tok=tok)
            k, val = tok.split("=", 1)
            if k.lower() not in ("head", "tail", "fade"):
                die("err_list_key", file=list_path, line=lineno, key=k)
            opts[k.lower()] = val
        rows.append((p, opts))
    return rows


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    kind: str  # "enc" | "xfade" | "copy"
    clip: int
    src: int  # first source frame in clip
    frames: int
    clip_b: int | None = None
    src_b: int = 0
    fade_in: bool = False  # whole piece fades from black
    fade_out: bool = False  # whole piece fades to black
    file: Path | None = field(default=None, repr=False)


def is_chapter_pair(a: Clip, b: Clip) -> bool:
    return (a.gopro is not None and b.gopro is not None
            and a.gopro[0] == b.gopro[0] and a.gopro[2] == b.gopro[2]
            and b.gopro[1] == a.gopro[1] + 1)


def assign_trims(clips: list[Clip], args, fps: Fraction) -> None:
    head = parse_time(args.head, fps, "--head")
    tail = parse_time(args.tail, fps, "--tail")
    fade = parse_time(args.fade, fps, "--fade")
    for c in clips:
        c.head, c.tail, c.fade = head, tail, fade

    # continuous GoPro chapters (GS01xxxx -> GS02xxxx): seamless cut, no trims inside
    if not args.list and not args.no_group:
        for a, b in zip(clips, clips[1:]):
            if is_chapter_pair(a, b):
                a.fade = a.tail = b.head = 0

    for c in clips:
        if c.head_raw is not None:
            c.head = parse_time(c.head_raw, fps, f"head ({c.path.name})")
        if c.tail_raw is not None:
            c.tail = parse_time(c.tail_raw, fps, f"tail ({c.path.name})")
        if c.fade_raw is not None:
            c.fade = parse_time(c.fade_raw, fps, f"fade ({c.path.name})")

    if args.fades:
        vals = args.fades.split(",")
        if len(vals) != len(clips) - 1:
            die("err_fades_count", n=len(clips) - 1, got=len(vals))
        for c, v in zip(clips, vals):
            c.fade = parse_time(v, fps, "--fades")
    clips[-1].fade = 0

    for c in clips:
        if c.frames - c.head - c.tail <= 0:
            die("err_trim", name=c.path.name, ht=secs(c.head + c.tail, fps),
                length=secs(c.frames, fps))
    # like the original script: a transition may use at most 40 % of either clip
    for a, b in zip(clips, clips[1:]):
        limit = math.floor(0.4 * min(a.frames - a.head - a.tail, b.frames - b.head - b.tail))
        if a.fade > limit:
            warn("warn_shorten", a=a.path.name, b=b.path.name, d=secs(limit, fps),
                 want=secs(a.fade, fps))
            a.fade = limit


def build_plan(clips: list[Clip], fps: Fraction, fade_in: int, fade_out: int,
               smart: bool, min_copy: int) -> tuple[list[Piece], int]:
    n = len(clips)
    pieces: list[Piece] = []
    cursor = 0
    for i, c in enumerate(clips):
        s, e = c.head, c.frames - c.tail
        in_ov = clips[i - 1].fade if i else 0
        c.out_start = cursor - in_ov
        c0 = s + in_ov
        c1 = e - c.fade
        fi = fade_in if i == 0 else 0
        fo = fade_out if i == n - 1 else 0
        if c0 + fi > c1 - fo:
            die("err_fade_io", name=c.path.name)

        def add(kind, src, frames, **kw):
            nonlocal cursor
            if frames > 0:
                pieces.append(Piece(kind, i, src, frames, **kw))
                cursor += frames

        add("enc", c0, fi, fade_in=True)
        b0, b1 = c0 + fi, c1 - fo  # body
        ks = ke = None
        if smart and b1 > b0:
            safe = sorted(k for k, kf in c.keyframes.items() if kf.safe)
            ks = next((k for k in safe if k >= b0), None)
            ends = [k for k in safe if k <= b1] + ([c.frames] if b1 == c.frames else [])
            ke = max(ends) if ends else None
            if ks is None or ke is None or ke - ks < min_copy:
                ks = ke = None
        if ks is None:
            add("enc", b0, b1 - b0)
        else:
            add("enc", b0, ks - b0)
            add("copy", ks, ke - ks)
            add("enc", ke, b1 - ke)
        add("enc", b1, fo, fade_out=True)
        if i < n - 1 and c.fade > 0:
            add("xfade", e - c.fade, c.fade, clip_b=i + 1, src_b=clips[i + 1].head)
    return pieces, cursor


# --------------------------------------------------------------------------- #
# ffmpeg command builders
# --------------------------------------------------------------------------- #
class Enc:
    def __init__(self, args, clips: list[Clip], smart: bool, delay: int):
        c0 = clips[0]
        self.args = args
        self.fps = c0.fps
        self.smart = smart
        self.delay = delay  # common reorder delay (frames) for every piece
        depth = args.bit_depth
        if depth == "auto":
            depth = "10" if ("10" in c0.pix_fmt or "p010" in c0.pix_fmt) else "8"
        self.depth = int(depth)
        self.hw = not args.cpu_decode
        self.planar = "yuv420p10le" if self.depth == 10 else "yuv420p"
        self.hwfmt = "p010le" if self.depth == 10 else "nv12"
        self.encfmt = self.hwfmt if args.encoder == "nvenc" else self.planar

        # MP4 timescale: source time base in smart mode (exact copy), frame-exact always
        num, den = self.fps.numerator, self.fps.denominator
        ts = 1
        for c in clips:
            ts = math.lcm(ts, c.tb.denominator)
        if (ts * den) % num:
            ts = math.lcm(ts, num)
        if ts < 10000:
            ts *= math.ceil(10000 / ts)
        self.timescale = ts
        bframes = 0 if smart else args.bframes
        gop = args.gop if args.gop else round(2 * self.fps)

        if args.encoder == "nvenc":
            a = ["-c:v", "hevc_nvenc", "-preset", args.preset, "-tune", "hq",
                 "-rc", "vbr", "-cq", str(args.cq), "-b:v", "0", "-bf", str(bframes),
                 "-profile:v", "main10" if self.depth == 10 else "main"]
            if not self.hw:
                a += ["-pix_fmt", self.encfmt]
        else:
            a = ["-c:v", "libx265", "-preset", args.x265_preset, "-crf", str(args.cq),
                 "-x265-params", f"bframes={bframes}:log-level=error",
                 "-pix_fmt", self.encfmt]
        a += ["-g", str(gop)]
        for k, flag in {"color_range": "-color_range", "color_space": "-colorspace",
                        "color_transfer": "-color_trc",
                        "color_primaries": "-color_primaries"}.items():
            if k in c0.color:
                a += [flag, c0.color[k]]
        if args.encode_extra:
            a += shlex.split(args.encode_extra, posix=(os.name != "nt"))
        self.video_args = a

        # pin colour VUI so frame props cannot make pieces' SPS differ (2 = unspecified)
        col = c0.color
        vui = ":".join(f"{k}={v}" for k, v in {
            "colour_primaries": PRIMARIES.get(col.get("color_primaries"), 2),
            "transfer_characteristics": TRANSFER.get(col.get("color_transfer"), 2),
            "matrix_coefficients": MATRIX.get(col.get("color_space"), 2),
            "video_full_range_flag": 1 if col.get("color_range") == "pc" else 0}.items())
        bsf = [f"hevc_metadata={vui}"]
        if smart:
            bsf.append("dump_extra=freq=keyframe")  # parameter sets in-band
            if delay:
                bsf.append(f"setts=dts=DTS-round({fsec(delay, self.fps)}/TB)")
        self.enc_bsf = ",".join(bsf)
        self.tag = "hev1" if smart else "hvc1"

    def mux_args(self) -> list[str]:
        return ["-tag:v", self.tag, "-video_track_timescale", str(self.timescale)]

    def input_args(self, clip: Clip, frame: int) -> list[str]:
        a: list[str] = []
        if self.hw:
            a += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        if frame > 0:
            # half a frame early: the accurate-seek trim then keeps exactly `frame`
            t0 = (clip.v_start - clip.f_start) + (frame - 0.5) / float(clip.fps)
            a += ["-ss", f"{t0:.6f}"]
        return a + ["-i", str(clip.path)]

    def to_cpu(self) -> str:
        if self.hw:
            return f"hwdownload,format={self.hwfmt},format={self.planar}"
        return f"format={self.planar}"

    def fades(self, p: Piece) -> list[str]:
        if p.fade_in:
            return [f"fade=t=in:start_frame=0:nb_frames={p.frames}"]
        if p.fade_out:
            return [f"fade=t=out:start_frame=0:nb_frames={p.frames}"]
        return []

    def piece_cmd(self, p: Piece, clips: list[Clip], out: Path) -> list[str]:
        base = [self.args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-stats", "-y"]
        a = clips[p.clip]
        if p.kind == "copy":
            kf = a.keyframes.get(p.src)
            seek = float(kf.pts * a.tb) - a.f_start + 0.5 / float(a.fps) if p.src else 0.0
            p0 = kf.pts if kf else 0
            extra = self.delay - (kf.delay if kf else 0)
            setts = f"setts=pts=PTS-{p0}:dts=DTS-{p0}"
            if extra:
                setts += f"-round({fsec(extra, self.fps)}/TB)"
            cmd = base + (["-ss", f"{seek:.6f}"] if p.src else []) + [
                "-copyts", "-i", str(a.path), "-map", "0:v:0", "-frames:v", str(p.frames),
                "-c", "copy", "-bsf:v", f"hevc_mp4toannexb,{setts}"]
            return cmd + self.mux_args() + ["-an", "-sn", "-dn", "-f", "mp4", str(out)]

        if p.kind == "enc":
            cmd = base + self.input_args(a, p.src)
            fades = self.fades(p)
            if fades or (self.hw and self.args.encoder != "nvenc"):
                chain = ",".join([RENUMBER, self.to_cpu(), *fades, f"format={self.encfmt}"])
            else:
                # renumber timestamps to exact frame slots: a seek landing half a frame
                # in could otherwise make the CFR sync duplicate the first frame
                chain = RENUMBER
            cmd += ["-filter_complex", f"[0:v:0]{chain}[v]", "-map", "[v]"]
        else:
            b = clips[p.clip_b]
            cmd = base + self.input_args(a, p.src) + self.input_args(b, p.src_b)
            prep = f"trim=end_frame={p.frames},{RENUMBER},{self.to_cpu()},settb=AVTB"
            graph = (f"[0:v:0]{prep}[a];[1:v:0]{prep}[b];"
                     f"[a][b]xfade=transition={self.args.transition}:"
                     f"duration={fsec(p.frames, self.fps)}:offset=0,"
                     + ",".join([*self.fades(p), f"format={self.encfmt}"]) + "[v]")
            cmd += ["-filter_complex", graph, "-map", "[v]"]
        return cmd + ["-frames:v", str(p.frames), "-an", "-sn", "-dn", *self.video_args,
                      "-bsf:v", self.enc_bsf, *self.mux_args(), "-f", "mp4", str(out)]


def audio_cmd(args, clips: list[Clip], fps: Fraction, total: int, fade_in: int,
              fade_out: int, out: Path) -> list[str]:
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-y"]
    for c in clips:
        cmd += ["-i", str(c.path)]
    layout = clips[0].audio[args.audio_stream].get("channel_layout")
    afmt = "aformat=sample_fmts=fltp" + (f":channel_layouts={layout}" if layout else "")
    parts = []
    for i, c in enumerate(clips):
        s = c.v_start + secs(c.head, fps)
        e = c.v_start + secs(c.frames - c.tail, fps)
        parts.append(f"[{i}:a:{args.audio_stream}]aresample=48000:async=1:first_pts=0,{afmt},"
                     f"apad,atrim=start={s:.9f}:end={e:.9f},asetpts=PTS-STARTPTS[a{i}]")
    cur = "a0"
    for i in range(1, len(clips)):
        d = clips[i - 1].fade
        nxt = f"x{i}"
        if d > 0:
            parts.append(f"[{cur}][a{i}]acrossfade=d={fsec(d, fps)}:"
                         f"c1={args.audio_curve}:c2={args.audio_curve}[{nxt}]")
        else:
            parts.append(f"[{cur}][a{i}]concat=n=2:v=0:a=1[{nxt}]")
        cur = nxt
    tail = []
    if fade_in:
        tail.append(f"afade=t=in:st=0:d={fsec(fade_in, fps)}")
    if fade_out:
        tail.append(f"afade=t=out:st={fsec(total - fade_out, fps)}:d={fsec(fade_out, fps)}")
    tail.append(f"atrim=end={fsec(total, fps)}")
    parts.append(f"[{cur}]{','.join(tail)}[aout]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[aout]",
                  "-c:a", "aac", "-b:a", args.audio_bitrate, "-f", "mp4", str(out)]


def first_packet(ffprobe: str, path: Path) -> tuple[int, int, int]:
    """(nb_frames, pts, dts) of the video stream's first packet, in stream ticks."""
    info = ffprobe_json(ffprobe, ["-select_streams", "v:0", "-read_intervals", "%+#1",
                                  "-show_entries", "stream=nb_frames:packet=pts,dts"], path)
    frames = int(info["streams"][0].get("nb_frames") or 0)
    pk = (info.get("packets") or [{}])[0]
    return frames, int(pk.get("pts", 0)), int(pk.get("dts", 0))


def param_sets_hash(ffprobe: str, path: Path) -> str:
    """Hash of VPS/SPS/PPS in the hvcC box (SEI arrays such as x265's info string skipped)."""
    res = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams",
                          "-show_data", str(path)], capture_output=True, text=True)
    raw = bytearray()
    in_dump = False
    for line in res.stdout.splitlines():
        if line.startswith("extradata="):
            in_dump = True
            continue
        if in_dump:
            m = re.match(r"^[0-9a-f]{8}: (.{39})", line)
            if not m:
                break
            raw += bytes.fromhex(m.group(1).replace(" ", ""))
    if len(raw) < 23:
        return hashlib.sha1(bytes(raw)).hexdigest()
    keep = bytearray()
    pos = 23
    for _ in range(raw[22]):
        nal_type = raw[pos] & 0x3F
        count = int.from_bytes(raw[pos + 1:pos + 3], "big")
        pos += 3
        for _ in range(count):
            size = int.from_bytes(raw[pos:pos + 2], "big")
            if nal_type not in (39, 40):
                keep += raw[pos:pos + 2 + size]
            pos += 2 + size
    return hashlib.sha1(bytes(keep)).hexdigest()


def piece_key(p: Piece, clips: list[Clip], enc: Enc) -> str:
    def src(i):
        c = clips[i]
        st = c.path.stat()
        return [str(c.path), st.st_size, st.st_mtime_ns]
    spec = {"p": [p.kind, p.src, p.frames, p.src_b, p.fade_in, p.fade_out],
            "a": src(p.clip), "b": src(p.clip_b) if p.clip_b is not None else None,
            "tr": enc.args.transition, "enc": enc.video_args, "bsf": enc.enc_bsf,
            "hw": enc.hw, "delay": enc.delay, "ts": enc.timescale, "v": __version__}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    global LANG
    env_lang = os.environ.get("GOPRO_LANG", "en").lower()

    ap = argparse.ArgumentParser(
        description="Join clips with soft transitions (xfade/acrossfade). Smart mode "
                    "stream-copies the clip bodies and re-encodes only the transitions "
                    "with NVENC. Times: seconds (1.5) or frames (45f).")
    ap.add_argument("inputs", nargs="*", help="input clips (wildcards are expanded)")
    ap.add_argument("-o", "--output", required=True, help="output .mp4")
    ap.add_argument("-y", "--overwrite", action="store_true", help="overwrite output")
    ap.add_argument("--list", type=Path,
                    help="text file: '<path> [head=..] [tail=..] [fade=..]' per line")
    ap.add_argument("--lang", choices=["en", "sv"], default=env_lang if env_lang in MESSAGES else "en",
                    help="message language (default: $GOPRO_LANG or en)")

    g = ap.add_argument_group("transitions / trimming")
    g.add_argument("--fade", "--duration", default="1.5",
                   help="overlap per junction: this much of clip A's end AND clip B's "
                        "start is blended; 0 = hard cut (default 1.5)")
    g.add_argument("--fades", help="per-junction overlap list, e.g. '1,0,2.5' (overrides)")
    g.add_argument("--head", default="0", help="cut from the start of each clip (default 0)")
    g.add_argument("--tail", default="0", help="cut from the end of each clip (default 0)")
    g.add_argument("--fade-in", default="0", help="fade from black at output start")
    g.add_argument("--fade-out", default="0", help="fade to black at output end")
    g.add_argument("--transition", default="fade",
                   help="xfade transition: fade (default), dissolve, fadeblack, fadewhite, ...")
    g.add_argument("--no-sort", action="store_true",
                   help="keep given order (GoPro names are otherwise sorted by file, chapter)")
    g.add_argument("--no-group", action="store_true",
                   help="treat GoPro chapters as separate clips (fade/trim between them)")

    g = ap.add_argument_group("mode / video encoding")
    g.add_argument("--mode", choices=["smart", "full"], default="smart",
                   help="smart = copy clip bodies, re-encode transitions only (default); "
                        "full = re-encode everything")
    g.add_argument("--gop-window", type=float, default=30.0,
                   help="seconds to search for an IDR keyframe near each cut (default 30)")
    g.add_argument("--min-copy", default="2",
                   help="shortest stretch worth stream-copying (default 2 s)")
    g.add_argument("--encoder", choices=["auto", "nvenc", "x265"], default="auto",
                   help="auto = hevc_nvenc if available, else libx265 (CPU)")
    g.add_argument("--cq", type=int, default=15, help="NVENC -cq / x265 -crf (default 15)")
    g.add_argument("--preset", default="p7", help="NVENC preset p1..p7 (default p7)")
    g.add_argument("--x265-preset", default="medium")
    g.add_argument("--bframes", type=int, default=0, help="B-frames, full mode only (default 0)")
    g.add_argument("--gop", type=int, default=0, help="GOP length (default 2 x fps)")
    g.add_argument("--bit-depth", choices=["auto", "8", "10"], default="auto")
    g.add_argument("--cpu-decode", action="store_true", help="decode on CPU instead of NVDEC")
    g.add_argument("--encode-extra", default="", help="extra encoder args, e.g. \"-temporal-aq 1\"")

    g = ap.add_argument_group("audio")
    g.add_argument("--no-audio", action="store_true")
    g.add_argument("--audio-stream", type=int, default=0, help="audio stream index per clip")
    g.add_argument("--audio-bitrate", default="192k")
    g.add_argument("--audio-curve", default="tri", help="acrossfade curve (tri, qsin, ...)")

    g = ap.add_argument_group("run")
    g.add_argument("--work-dir", type=Path, help="default: <output>_work next to the output")
    g.add_argument("--keep-work", "--keep-temp", action="store_true",
                   help="keep pieces after success")
    g.add_argument("--dry-run", action="store_true", help="print plan and commands only")
    g.add_argument("-v", "--verbose", action="store_true", help="print ffmpeg commands")
    g.add_argument("--ffmpeg", default="ffmpeg")
    g.add_argument("--ffprobe", default="ffprobe")
    g.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    LANG = args.lang

    for exe in (args.ffmpeg, args.ffprobe):
        if not shutil.which(exe) and not Path(exe).is_file():
            die("err_not_found", exe=exe)
    if args.encoder == "auto":
        encs = subprocess.run([args.ffmpeg, "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
        args.encoder = "nvenc" if "hevc_nvenc" in encs else "x265"
        if args.encoder == "x265":
            warn("warn_no_nvenc")
    if args.encoder == "x265":
        args.cpu_decode = True

    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite and not args.dry_run:
        die("err_exists", out=output)

    # ---- inputs
    if args.list:
        if args.inputs:
            die("err_list_and_inputs")
        rows = read_list_file(args.list)
        paths, overrides = [p for p, _ in rows], [o for _, o in rows]
    else:
        paths = expand_inputs(args.inputs)
        overrides = [{} for _ in paths]
    if not paths:
        die("err_no_inputs")

    print(t("probing", n=len(paths)))
    clips = [probe_clip(args.ffprobe, p) for p in paths]
    for c, o in zip(clips, overrides):
        c.head_raw, c.tail_raw, c.fade_raw = o.get("head"), o.get("tail"), o.get("fade")
    if not args.list and not args.no_sort and all(c.gopro for c in clips):
        clips.sort(key=lambda c: (c.gopro[0], c.gopro[2], c.gopro[1]))

    c0 = clips[0]
    for c in clips[1:]:
        if (c.width, c.height) != (c0.width, c0.height) or c.fps != c0.fps:
            die("err_mismatch", a=c.path.name, wa=c.width, ha=c.height, fa=c.fps,
                b=c0.path.name, wb=c0.width, hb=c0.height, fb=c0.fps)
    fps = c0.fps

    use_audio = not args.no_audio
    if use_audio:
        missing = [c.path.name for c in clips if len(c.audio) <= args.audio_stream]
        if missing:
            warn("warn_no_audio", s=args.audio_stream, names=", ".join(missing))
            use_audio = False
        else:
            chans = {c.audio[args.audio_stream].get("channels") for c in clips}
            if len(chans) > 1:
                die("err_channels", ch=sorted(chans))

    smart = args.mode == "smart"
    if smart:
        for c in clips:
            if c.codec != "hevc" or c.pix_fmt not in ("yuv420p", "yuv420p10le", "yuvj420p"):
                warn("warn_fallback_full", name=c.path.name, codec=c.codec, pix=c.pix_fmt)
                smart = False
                break
    if smart and args.bframes:
        warn("warn_bframes")

    assign_trims(clips, args, fps)
    fade_in = parse_time(args.fade_in, fps, "--fade-in")
    fade_out = parse_time(args.fade_out, fps, "--fade-out")
    min_copy = parse_time(args.min_copy, fps, "--min-copy")

    # ---- keyframe scan around the cut points (smart mode)
    if smart:
        print(t("probing_kf"))
        w = args.gop_window
        for i, c in enumerate(clips):
            in_ov = clips[i - 1].fade if i else 0
            b0 = secs(c.head + in_ov + (fade_in if i == 0 else 0), fps) + c.v_start
            b1 = secs(c.frames - c.tail - c.fade - (fade_out if i == len(clips) - 1 else 0),
                      fps) + c.v_start
            windows = [(max(0.0, b0 - 1.0), w + 1.0)]
            if b1 < secs(c.frames, fps) + c.v_start - 1e-6:
                windows.append((max(0.0, b1 - w), w + 1.0))
            if len(windows) == 2 and windows[1][0] <= windows[0][0] + windows[0][1]:
                windows = [(windows[0][0], windows[1][0] + windows[1][1] - windows[0][0])]
            for start, dur in windows:
                scan_keyframes(args.ffmpeg, c, start, dur)

    pieces, total = build_plan(clips, fps, fade_in, fade_out, smart, min_copy)
    # one common reorder delay for all pieces = largest delay among copied keyframes
    delay = max((clips[p.clip].keyframes[p.src].delay for p in pieces if p.kind == "copy"),
                default=0)
    if smart:
        for i, c in enumerate(clips):
            body = [p for p in pieces if p.clip == i and p.kind != "xfade"]
            if not any(p.kind == "copy" for p in body) and sum(p.frames for p in body) > 2 * min_copy:
                warn("warn_no_idr", name=c.path.name, w=args.gop_window)
    enc = Enc(args, clips, smart, delay)

    # ---- plan printout
    copy_f = sum(p.frames for p in pieces if p.kind == "copy")
    print("\n" + t("encoder_line", w=c0.width, h=c0.height, fps=fps, fpsf=float(fps),
                   depth=enc.depth, codec=c0.codec, mode="smart" if smart else "full",
                   enc=args.encoder, dec="NVDEC" if enc.hw else "CPU"))
    print(t("table_head", i="#", file="file", length="length", head="head", tail="tail",
            fade="fade->", out="out start"))
    for i, c in enumerate(clips):
        fade_txt = f"{secs(c.fade, fps):7.3f}" if i < len(clips) - 1 else "      -"
        print(f"{i:>3}  {c.path.name:<28} {secs(c.frames, fps):9.3f} {secs(c.head, fps):7.3f} "
              f"{secs(c.tail, fps):7.3f} {fade_txt} {hms(secs(c.out_start, fps)):>13}")
    print(t("output_line", dur=hms(secs(total, fps)), frames=total, pieces=len(pieces),
            copy=hms(secs(copy_f, fps)), enc=hms(secs(total - copy_f, fps)),
            audio=t("with_audio") if use_audio else t("without_audio")) + "\n")

    work = (args.work_dir or output.with_name(output.stem + "_work")).resolve()
    for idx, p in enumerate(pieces):
        p.file = work / f"{idx:03d}_{p.kind}_{piece_key(p, clips, enc)}.mp4"

    timeline = {
        "tool": f"xfade_concat {__version__}",
        "output": str(output),
        "mode": "smart" if smart else "full",
        "fps": str(fps),
        "output_frames": total,
        "output_duration_s": secs(total, fps),
        "copied_s": secs(copy_f, fps),
        "reencoded_s": secs(total - copy_f, fps),
        "mapping": "out_t = out_start_s + (src_t - src_in_s)   for src_in_s <= src_t < src_out_s",
        "clips": [{
            "file": str(c.path),
            "src_in_s": secs(c.head, fps),
            "src_out_s": secs(c.frames - c.tail, fps),
            "out_start_s": secs(c.out_start, fps),
            "out_end_s": secs(c.out_start + c.frames - c.head - c.tail, fps),
            "fade_from_prev_s": secs(clips[i - 1].fade, fps) if i else 0.0,
            "fade_to_next_s": secs(c.fade, fps),
        } for i, c in enumerate(clips)],
    }

    def label(p: Piece) -> str:
        src = clips[p.clip].path.name
        if p.clip_b is not None:
            src += f" -> {clips[p.clip_b].path.name}"
        return f"{p.kind:<5} {src}  [{secs(p.src, fps):.3f}s +{secs(p.frames, fps):.3f}s]"

    if args.dry_run:
        for p in pieces:
            print(label(p))
            print("  $ " + fmt_cmd(enc.piece_cmd(p, clips, p.file)))
        if use_audio:
            print("[audio]\n  $ " + fmt_cmd(audio_cmd(args, clips, fps, total, fade_in, fade_out,
                                                      work / "audio.m4a")))
        print("\n" + json.dumps(timeline, indent=2))
        return

    work.mkdir(parents=True, exist_ok=True)
    fdur = Fraction(1) / fps

    def piece_ok(path: Path, p: Piece) -> tuple[bool, str]:
        frames, pts, dts = first_packet(args.ffprobe, path)
        if frames != p.frames:
            return False, t("err_frames", name=path.name, want=p.frames, got=frames)
        if smart:
            tb = Fraction(1, enc.timescale)
            got = round((pts - dts) * tb / fdur)
            if got != delay:
                return False, t("err_delay", name=path.name, got=got, want=delay)
        return True, ""

    # ---- video pieces, serially (one NVDEC/NVENC chip; parallel jobs corrupt frames)
    for idx, p in enumerate(pieces):
        head = f"[{idx + 1}/{len(pieces)}] {label(p)}"
        if p.file.exists() and piece_ok(p.file, p)[0]:
            print(head + t("cached"))
            continue
        print(head)
        tmp = p.file.with_name(p.file.stem + ".partial.mp4")
        run(enc.piece_cmd(p, clips, tmp), args.verbose)
        ok, msg = piece_ok(tmp, p)
        if not ok:
            print(msg, file=sys.stderr)
            sys.exit(1)
        os.replace(tmp, p.file)

    if not smart and len({param_sets_hash(args.ffprobe, p.file) for p in pieces}) > 1:
        warn("warn_ps")

    # ---- audio
    audio_file = None
    if use_audio:
        audio_file = work / "audio.m4a"
        print(t("audio"))
        tmp = work / "audio.partial.m4a"
        run(audio_cmd(args, clips, fps, total, fade_in, fade_out, tmp), args.verbose)
        os.replace(tmp, audio_file)

    # ---- join
    list_txt = work / "pieces.txt"
    list_txt.write_text("".join("file '" + p.file.as_posix().replace("'", r"'\''") + "'\n"
                                for p in pieces), encoding="utf-8")
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_txt)]
    if audio_file:
        cmd += ["-i", str(audio_file), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-c", "copy", "-tag:v", enc.tag, str(output)]
    print(t("join", name=output.name))
    if args.verbose:
        print("  $ " + fmt_cmd(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        die("err_ffmpeg", code=res.returncode, cmd=fmt_cmd(cmd))
    ts_issues = [ln for ln in res.stderr.splitlines() if "monoton" in ln.lower()]
    if ts_issues:
        warn("warn_dts", log="\n".join(ts_issues[:10]))

    got = first_packet(args.ffprobe, output)[0]
    if got != total:
        die("err_frames", name=output.name, want=total, got=got)

    tl_path = output.with_suffix(".timeline.json")
    tl_path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + t("done", out=output, timeline=tl_path, frames=total, dur=hms(secs(total, fps))))
    if c0.width == 2 * c0.height:
        print(t("note_360"))


if __name__ == "__main__":
    main()
