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
| `--list clips.txt` | per-clip control (see below) |

A transition never uses more than 40 % of either clip; longer values are
shortened with a warning.

### Per-clip control

```text
# path                     options (all optional)
GS010123-png_ovr.mp4       head=2.5 fade=1.5
GS010124-png_ovr.mp4       tail=12f fade=0
"D:\flights\GS010125.mp4"
```

`fade` on a line is the transition **into the next** clip. Relative paths are
relative to the list file, and the order is exactly as written.

### GoPro chapters

When all inputs have GoPro names (`GS`/`GX`/`GH` + `ccnnnn`, suffixes like
`-png_ovr` are fine), they are sorted by (file number, chapter). Consecutive
chapters of one recording (`GS01xxxx → GS02xxxx`) are joined seamlessly with no
trim, and in smart mode without any re-encoding. `--head`/`--tail` then apply to
the recording as a whole. Disable with `--no-sort` / `--no-group`.

### Clips cut from a master with `-c copy`

Splitting highlights out of one long recording with
`ffmpeg -ss T -i master.mp4 -t N -c copy clip.mp4` is fine, but only pass `-t`
for a duration - **not `-to`**, which is an absolute end time and silently
produces a much shorter clip than intended if `T` isn't 0. Either way, if `T`
doesn't land exactly on a keyframe, the clip will contain a hidden pre-roll
GOP behind an edit list; the tool detects and accounts for this on its own
(see "Real frame count" below), so no extra flags are needed for it.

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
- **Real frame count, not container metadata.** A clip cut with
  `ffmpeg -ss T -i master -t N -c copy` where `T` lands mid-GOP forces ffmpeg to
  keep the whole preceding GOP as hidden pre-roll (behind an edit list) so the
  file still decodes correctly - the container then reports more frames than
  are actually meant to be shown (`nb_frames` and even a raw packet count both
  include the hidden part). Trusting that number corrupts every cut computed
  from it. The tool cross-checks the container's own duration against the
  frame-count tag and, only when they disagree, pays for a real decode-based
  count (`-count_frames`) to get the true, playable total - cheap for ordinary
  recordings, since most never need it.
- **Different encoders in one file.** NVENC pieces and the source bitstream have
  different VPS/SPS/PPS. Every piece carries its parameter sets **in-band**
  (`hevc_mp4toannexb` / `dump_extra`), and the output uses the `hev1` tag.
- **Monotonic timestamps at the joins.** Pieces with different B-frame delays
  cannot be joined with plain `-c copy`: this gives "Non-monotonic DTS" and frozen
  frames at the joins. Every piece's DTS is normalised to one common delay with
  `setts`. A source encoder's B-frame reorder depth can still vary *within* one
  GOP (seen with real NVENC/lookahead output) so a single per-keyframe
  correction isn't always enough; each copy piece's full DTS sequence is
  verified after the fact, and if it isn't strictly increasing, the tool
  silently re-encodes that stretch instead of shipping a broken join.
- **Checks.** Every piece is verified for frame count, timestamp delay, and (copy
  pieces) full DTS monotonicity; the join is checked for timestamp warnings; the
  output is checked for its total frame count.

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
- A copy piece that turns out to have an inconsistent B-frame pattern is
  re-encoded instead automatically (a warning says so) - this costs the same
  time as if no safe keyframe had been found there in the first place, just
  discovered one step later.

## Testing

`--encoder x265` runs the whole flow on the CPU. Verified with synthetic clips whose
luma encodes the frame number:

- output frames match the expected sequence exactly, including blended transitions and fades
- sources with closed GOP + B-frames, open GOP (CRA with leading pictures) and GoPro chapters
- head/tail/fade/fade-in/fade-out combinations at 25 and 29.97 fps, 8- and 10-bit
- monotonic DTS, audio duration equals video duration
- clips with a hidden pre-roll edit list (`-ss T -c copy` landing mid-GOP):
  frame count and content verified correct once the real, decode-based count is used
- a source GOP with a variable B-frame reorder depth (reproduced with x265
  `--zones`): the resulting bad-DTS join was reproduced byte-for-byte against a
  real failing run's log, and the copy→re-encode fallback was confirmed to
  both trigger and produce a clean, monotonic join

The NVENC/NVDEC path itself needs testing on the GPU machine.
