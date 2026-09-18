# xfade-concat

Join video clips with soft transitions (video `xfade` + audio `acrossfade`)
**without re-encoding the whole video**. Only the short stretches around each
transition are re-encoded with NVIDIA **NVENC (HEVC)**; the rest of every clip
is stream-copied, as with `ffmpeg -f concat -c copy`.

Runs on native Windows (PowerShell), since NVDEC/NVENC do not work under WSL1.
Made for 8K equirectangular GoPro MAX/MAX2 footage (8- and 10-bit), e.g. the
per-clip renders from OVRLEY. Grew out of `xfade_concat.py` in
[gopro-max-gpx-pipeline](https://github.com/zekesixniner/gopro-max-gpx-pipeline),
with frame-exact cuts and hardware encoding.

## How much of the ends is used

A transition of length **D** blends the **last D of clip A** with the **first D of
clip B**. Both ends are consumed, so each junction shortens the total by D.
`--head` / `--tail` cut material away *before* the transition is placed.

```
clip A  |--head--|==============body==============|##D##|--tail--|
clip B                                   |--head--|##D##|===========body====...
output  |==============A body==============|#A→B#|===========B body====...
```

Times are seconds (`1.5`) or frames (`45f`).

| option | meaning |
|---|---|
| `--fade 1.5` (alias `--duration`) | overlap at every junction; `0` = hard cut |
| `--fades 1,0,2.5` | one value per junction, overrides `--fade` |
| `--head 0` / `--tail 0` | cut from the start / end of every clip first |
| `--fade-in` / `--fade-out` | fade from / to black at the very start / end |
| `--transition fade` | any xfade transition; `fade`, `dissolve`, `fadeblack` suit 360° best |
| `--list clips.txt` | per-clip control, ranges, speed and an `out=` typo guard (see below) |

A transition never uses more than 40 % of either clip; longer values are
shortened with a warning.

### Generating the list file

Rather than typing paths by hand, let the tool write a starter list you then edit:

```powershell
# one row per file, each with its length as a comment
python xfade_concat.py --make-list clips.txt GS01*-png_ovr.mp4

# 12 range rows spread through one master, ready to have the times replaced
python xfade_concat.py --make-list clips.txt ..\GS00080-85_png_ovr.mp4 --rows 12
```

The file it writes carries the column reference and the exact command to run in
its own header, so there is nothing to look up while editing. Add `-y` to
overwrite an existing list.

### Per-clip control

```text
# path                     options (all optional)
GS010123-png_ovr.mp4       head=2.5 fade=1.5
GS010124-png_ovr.mp4       tail=12f fade=0
master.mp4                 in=00:01:24 dur=24
"D:\flights\GS010125.mp4"
```

`fade` on a line is the transition **into the next** clip. `in`/`dur`/`out` pick
a stretch of the file (see below). Relative paths are relative to the list file,
and the order is exactly as written.

### Playback speed

`speed=` plays a clip faster or slower:

```text
master.mp4   in=00:12:30  dur=00:04:00  speed=6      # cruise, compressed
master.mp4   in=00:41:10  dur=00:00:08  speed=0.25   # the landing, in detail
```

Above 1 it runs faster (`1.5`, `2`, `3`, `4`, `6`), below 1 slower (`0.67`, `0.5`,
`0.33`, `0.25`, `0.12`); fractions like `1/3` work too. A 4 minute stretch at
`speed=6` becomes 40 seconds of output.

Everything else stays in the time the viewer sees: `--fade 1.5` is still 1.5
seconds on screen, whatever speeds meet at that junction, and two clips at
different speeds cross-fade correctly. `in`/`dur`/`head`/`tail` stay in source
time — you pick the stretch first, and the speed applies to it.

Speeding up drops frames and slowing down repeats them; there is no motion
interpolation, which at 8K would cost far more than the encode itself. Audio is
retimed with `atempo`, chained for the extreme ratios; stretched far enough it
will sound like it, so `--no-audio` or a music bed is often the better answer for
big slow-motion sections. A clip with `speed=` is always re-encoded, since there
is nothing to copy when every frame moves.

### GoPro chapters

When all inputs have GoPro names (`GS`/`GX`/`GH` + `ccnnnn`, suffixes like
`-png_ovr` are fine), they are sorted by (file number, chapter). Consecutive
chapters of one recording (`GS01xxxx → GS02xxxx`) are joined seamlessly with no
trim, and in smart mode without any re-encoding. `--head`/`--tail` then apply to
the recording as a whole. Disable with `--no-sort` / `--no-group`.

### Taking highlights straight from a master (recommended)

Instead of pre-cutting segments with `ffmpeg -ss .. -t .. -c copy` and feeding
those in, point the list file at the master and give each highlight a range:

```text
# path                        range                     options
../GS00080-85_png_ovr.mp4     in=00:00:15  dur=12
../GS00080-85_png_ovr.mp4     in=00:01:24  dur=24        fade=1
../GS00080-85_png_ovr.mp4     in=00:03:24  out=00:03:36
```

`in=` is where the highlight starts and `dur=` how long it runs. Times are `12`,
`1:30` or `00:04:30.5`, and the same file may be listed as many times as you like.

`out=` is the end point. Give it **together with** `dur=` and it acts as a typo
guard: the run stops before anything is encoded if the two disagree.

```text
../GS00080-85_png_ovr.mp4     in=00:29:04  dur=00:00:20  out=00:29:24
```

```
error: clips.txt:7: in=0:29:04.000 + dur=0:00:20.000 ends at 0:29:24.000,
       but out=0:29:14.000 - fix the line (nothing has been encoded yet)
```

Given on its own, `out=` simply sets the end instead of `dur=`.

This is worth preferring, because pre-cutting with `-c copy` costs real quality
of life:

- a cut that doesn't land exactly on a keyframe makes ffmpeg keep the whole
  preceding GOP as hidden pre-roll behind an edit list, which the tool then has
  to detect and work around;
- each pre-cut segment is usually too short to contain two keyframes, so smart
  mode has nothing to copy and re-encodes almost everything;
- ranges taken from the master keep the master's own keyframes, so far more of
  the footage is copied instead of re-encoded — usually the difference between
  re-encoding nearly all of it and re-encoding only the transitions.

Pre-cut segments still work, and ranges and pre-cut files can be mixed freely.
If you do pre-cut, pass `-t` for a duration — **not `-to`**, which is an
absolute end time and silently gives a much shorter clip than intended when the
start isn't 0.

## How it works

### Smart mode (default)

For every clip the body is stream-copied between two **IDR keyframes**; only
`[previous keyframe → transition]`, the transition itself and
`[transition → next keyframe]` are re-encoded. Re-encoded time per junction is
roughly `D + one GOP` on each side. Keyframes are found by scanning a window
(`--gop-window 30` s) around each cut with `trace_headers`, without decoding.

The tricky parts, and how they are handled:

- **Frame-exact cuts.** Copy pieces start and end only on keyframes that have no
  leading pictures (RADL/RASL). Cutting at an open-GOP CRA would drop or
  duplicate frames. Re-encoded pieces seek half a frame early and renumber
  timestamps (`setpts=N/FRAME_RATE/TB`).
- **Pieces are raw Annex B, not MP4.** Each piece is written as a bare `.hevc`
  bitstream, so a piece has no container: no timestamps to rebase, no edit list
  to inherit, no `hvcC` to clash with the next piece. Joining them is plain byte
  concatenation, and one final mux re-derives every timestamp at a constant
  frame rate. This is what makes pieces from three different sources — NVENC
  re-encodes, CPU transitions and the camera's own untouched bitstream — line up
  exactly. Doing it in MP4 instead means fighting B-frame reorder delays that
  can vary *within* a single GOP, and edit lists that silently hide frames; both
  produce "Non-monotonic DTS" and frozen frames at the joins.
- **Different encoders in one file.** NVENC pieces and the source bitstream have
  different VPS/SPS/PPS. Every piece carries its parameter sets **in-band**,
  repeated at each keyframe (`hevc_mp4toannexb` / `dump_extra`), and the output
  uses the `hev1` tag.
- **Real frame count, not container metadata.** When a clip was pre-cut mid-GOP,
  the hidden pre-roll counts towards the container's frame tag *and* towards a
  raw packet count, so both overstate how many frames actually play — and every
  cut computed from that number is wrong. The first packet's timestamp is
  negative by exactly the hidden part, so one cheap probe gives both the true
  count and the shift, without decoding anything.
- **Unambiguous seeking.** With an edit list in play, the same timestamp can
  resolve to the hidden pre-roll keyframe instead of the intended one, which
  silently copies the wrong stretch of video. Copy pieces therefore seek in the
  file's own raw timeline (`-ignore_editlist`), where there is nothing to be
  ambiguous about.
- **Checks.** Every piece is verified for its frame count; the output is verified
  for the frames that actually *play*, not the count the container advertises
  (a frame hidden behind an edit list still shows up in `nb_frames`).

Re-encoded pieces are decoded with NVDEC and encoded with NVENC. Transitions and
black fades go through the CPU (`hwdownload`), because `xfade` has no CUDA
version and 10-bit overlays need the CPU path. Pieces are rendered **serially**:
the RTX 2080 Ti has one NVDEC/NVENC chip, and parallel GPU jobs (e.g. OVRLEY at
the same time) have produced corrupt frames before.

Smart mode needs HEVC 4:2:0 8/10-bit sources; otherwise the tool falls back to full mode.

### Full mode (`--mode full`)

Everything is re-encoded (NVDEC → NVENC, CPU only for transitions) and joined
with identical codec headers (`hvc1`). Works with any source codec. Use it if
a player has trouble with smart-mode output.

### Audio

Always rebuilt in one pass: each clip is trimmed to exactly its video frames, joined with
`acrossfade` (or `concat` at hard cuts), and encoded as AAC.

Pieces are cached in `<output>_work/` under a hash of their settings. An
interrupted run resumes where it stopped (`--keep-work` keeps them after success).

Probing never decodes: clip lengths, keyframes and pre-roll all come from
timestamps and bitstream headers. On 8K footage that is the difference between
seconds and minutes per file. Piece frame counts are likewise read straight from
the Annex B start codes in one pass, rather than by asking ffprobe to parse every
NAL unit (~20x slower, and it adds up over a job).

## Where it fits in the pipeline

```
overlay_map.py  →  OVRLEY (per clip)  →  xfade_concat.py  →  inject360-inplace (always last)
```

Joining **after** OVRLEY is simplest: the telemetry is already burned into each
clip, so shortening the timeline does not affect sync. If you join **before**
OVRLEY, the telemetry SRT/GPX has to be shifted with the timeline JSON (below).

## Requirements

- Windows 10/11, NVIDIA GPU with HEVC NVENC (10-bit needs Turing / RTX 20 or newer)
- ffmpeg with `hevc_nvenc`, e.g. the gyan.dev essentials build in `C:\ffmpeg\bin` on PATH.
  Without NVENC, libx265 is used automatically (CPU, slow).
- Python 3.9+ for Windows, standard library only

## Installation

Developed in WSL, run on Windows:

```bash
git clone https://github.com/zekesixniner/xfade-concat.git ~/dev/xfade-concat
mkdir -p /mnt/c/Users/<you>/bin
cp ~/dev/xfade-concat/xfade_concat.py /mnt/c/Users/<you>/bin/
```

## Usage (PowerShell)

The script expands wildcards itself, since PowerShell does not do it for external programs.

```powershell
cd D:\done\260811_ESMK_ESMS

# 1.5 s crossfades (default), trim 2 s of shaky start/end of every recording
python C:\Users\<you>\bin\xfade_concat.py GS01*-png_ovr.mp4 -o flight.mp4 --head 2 --tail 2

# Check the plan first: what is copied, what is re-encoded
python C:\Users\<you>\bin\xfade_concat.py GS01*-png_ovr.mp4 -o flight.mp4 --fades 2,0,1 --fade-in 1 --fade-out 3 --dry-run

# Per-clip control, Swedish messages
python C:\Users\<you>\bin\xfade_concat.py --list clips.txt -o flight.mp4 --lang sv
```

Messages are English or Swedish: `--lang en|sv`, or `$env:GOPRO_LANG = "sv"`
(same as gopro-max-gpx-pipeline).

### Options

| option | default | |
|---|---|---|
| `--mode` | `smart` | `full` re-encodes everything |
| `--gop-window` | `30` | seconds searched for keyframes around each cut |
| `--min-copy` | `2` | shortest stretch worth stream-copying |
| `--encoder` | `auto` | `nvenc`, or `x265` (CPU fallback/testing) |
| `--cq` / `--preset` | `15` / `p7` | NVENC `-rc vbr -cq … -b:v 0 -tune hq`, as in overlay_map.py |
| `--bframes` | `0` | full mode only |
| `--gop` | 2 × fps | re-encoded pieces |
| `--bit-depth` | `auto` | from the source `pix_fmt` |
| `--cpu-decode` | | decode on CPU instead of NVDEC |
| `--encode-extra` | | e.g. `"-temporal-aq 1"` |
| `--audio-stream` / `--audio-bitrate` / `--audio-curve` | `0` / `192k` / `tri` | |
| `--make-list FILE` / `--rows N` | | write a starter clips list and exit |
| `--work-dir` / `--keep-work` (`--keep-temp`) | `<output>_work` | |
| `--dry-run` / `-v` | | plan and ffmpeg commands |

All clips must share resolution and frame rate. Audio is included only if every
clip has the chosen audio stream.

## Timeline JSON

Written next to the output as `<output>.timeline.json`:

```json
{
  "mode": "smart",
  "fps": "25/1",
  "copied_s": 2394.0,
  "reencoded_s": 38.5,
  "clips": [
    { "file": "...GS010123-png_ovr.mp4", "src_in_s": 2.0, "src_out_s": 598.0,
      "out_start_s": 0.0, "out_end_s": 596.0,
      "fade_from_prev_s": 0.0, "fade_to_next_s": 1.5 }
  ]
}
```

A source time maps to the output as `out_t = out_start_s + (src_t - src_in_s)`.

## 360° video

ffmpeg drops the spherical metadata. Run `inject360-inplace` on the output as
the last step. The output is written moov-last, which is its fast path.

## Limitations

- Smart-mode output switches parameter sets mid-stream at the joins (legal HEVC,
  verified with ffmpeg's decoder). Check playback in your players/OVRLEY once; if
  one has trouble, use `--mode full`.
- Keyframes more than `--gop-window` seconds from a cut are not found; the whole clip
  is then re-encoded (a warning says so).
- A stretch shorter than the source's GOP cannot contain two keyframes, so it is
  re-encoded whole (a note says so). This is the usual reason short pre-cut
  segments copy nothing — taking ranges from the master instead avoids it.

## Testing

`--encoder x265` runs the whole flow on the CPU. Verified with synthetic clips whose
luma encodes the frame number:

- output frames match the expected sequence exactly, including blended transitions and fades
- sources with closed GOP + B-frames, open GOP (CRA with leading pictures) and GoPro chapters
- head/tail/fade/fade-in/fade-out combinations at 25 and 29.97 fps, 8- and 10-bit
- monotonic DTS, audio duration equals video duration
- clips with a hidden pre-roll edit list (`-ss T -c copy` landing mid-GOP),
  including the case where seeking would otherwise copy the wrong stretch
- a source GOP with a variable B-frame reorder depth (reproduced with x265
  `--zones`) — the case that produced frozen frames and a short output when
  pieces were still joined as MP4
- ranges taken straight from a master (`in=`/`dur=`), and piece caching/resume
- every supported speed from 6x down to 0.12x, checked for exact output length
  and for the source frame each output frame lands on (no drift), plus clips at
  different speeds meeting at a transition

Twenty-two cases run as a suite, each checked for exact frame count,
frame-by-frame content against the sources, no frozen runs, and audio matching
video length.

The NVENC/NVDEC path itself needs testing on the GPU machine.
