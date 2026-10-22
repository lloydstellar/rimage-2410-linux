"""Build audio CDs: measure source files, convert them to CD audio and align them for gapless playback.

- An audio CD is 44.1 kHz, 16 bit, stereo (Red Book). A 24 bit/44.1 kHz master only needs a
  shorter word length. That is done with dither (TPDF by default), because plain truncation
  gives audible distortion in quiet passages.
- Other sample rates go through soxr with precision 28 and a Chebyshev filter, in the same
  aresample step as the dither, so the audio is converted only once.
- An audio sector is 588 samples. A track that does not end on a sector boundary is padded
  with silence. For continuous music (gapless) the boundaries are therefore first moved to
  a multiple of 588 samples.
- Level: do not normalise. Do measure the true peak and warn when it is too high; a fixed
  reduction before the dither is possible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
import wave
from dataclasses import dataclass

CD_RATE = 44100
SAMPLES_PER_SECTOR = 588
SECTORS_PER_SECOND = 75
FIRST_PREGAP_SECTORS = 150          # track 1 always gets a 2 s pregap
CD_SECTORS_MAX = 360_000            # 80 minutes
CD_SECTORS_74 = 74 * 60 * 75        # original playing time; some old players cannot go beyond it
MIN_TRACK_S = 4.0                   # cdrskin refuses shorter tracks, cdrdao warns
MAX_TRACKS = 99
MAX_PAUSE_S = 10.0
TRUE_PEAK_WARN_DB = 0.0            # above 0 dBTP: the waveform goes above full scale (warning)
TRUE_PEAK_NOTE_DB = -1.0           # between -1 and 0 dBTP: only a note (matters mainly for mp3/aac)
CDTEXT_BYTES_MAX = 3000             # approximate, per language block (253 packs x 12 bytes)

DITHER_METHODS = {
    "triangular": "TPDF (recommended)",
    "lipshitz": "Noise shaping, mild (Lipshitz)",
    "low_shibata": "Noise shaping (low Shibata)",
}
ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
UPC_RE = re.compile(r"^\d{13}$")


class AudioError(RuntimeError):
    pass


@dataclass
class AudioInfo:
    path: str
    codec: str
    sample_rate: int
    channels: int
    bits: int | None                 # bit depth of the source (None for mp3 and similar)
    sample_fmt: str
    duration_s: float
    sample_peak_db: float | None = None
    true_peak_db: float | None = None
    loudness_lufs: float | None = None
    lead_silence_s: float | None = None    # silence (below -60 dBFS) at the start
    tail_silence_s: float | None = None    # silence at the end

    @property
    def is_float(self) -> bool:
        return self.sample_fmt.startswith(("flt", "dbl"))

    @property
    def is_cd_native(self) -> bool:
        return (self.sample_rate == CD_RATE and self.channels == 2 and self.bits == 16
                and self.codec.startswith("pcm_s16"))

    @property
    def needs_resample(self) -> bool:
        return self.sample_rate != CD_RATE

    def describe(self) -> str:
        rate = f"{self.sample_rate / 1000:g}" + " kHz"
        depth = "32 bit float" if self.is_float and self.bits in (None, 32) else (
            f"{self.bits} bit" if self.bits else self.codec)
        ch = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} channels")
        return f"{rate}, {depth}, {ch}"

    def conversion(self) -> str:
        """What the conversion does, in plain language."""
        if self.is_cd_native:
            return "already CD format, left unchanged"
        steps = []
        if self.needs_resample:
            steps.append("sample rate to 44.1 kHz (soxr)")
        if self.channels != 2:
            steps.append("to stereo")
        steps.append("to 16 bit with dither")
        return ", ".join(steps)

    def as_dict(self) -> dict:
        return {"file": os.path.basename(self.path), "codec": self.codec, "sample_rate": self.sample_rate,
                "channels": self.channels, "bits": self.bits, "duration_s": round(self.duration_s, 3),
                "description": self.describe(), "conversion": self.conversion(),
                "sample_peak_db": self.sample_peak_db, "true_peak_db": self.true_peak_db,
                "loudness_lufs": self.loudness_lufs, "lead_silence_s": self.lead_silence_s,
                "tail_silence_s": self.tail_silence_s, "warnings": track_warnings(self), "notes": track_notes(self)}


def tools_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: str) -> AudioInfo:
    """Format and length of an audio file (ffprobe; without ffprobe only WAV)."""
    if shutil.which("ffprobe"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample,sample_fmt,"
             "duration_ts,duration:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        try:
            data = json.loads(out.stdout or "{}")
            st = data["streams"][0]
        except (ValueError, KeyError, IndexError):
            raise AudioError(f"{os.path.basename(path)} is not a readable audio file") from None
        rate = int(st.get("sample_rate") or 0)
        bits = int(st.get("bits_per_raw_sample") or 0) or int(st.get("bits_per_sample") or 0) or None
        dur = float(st.get("duration") or data.get("format", {}).get("duration") or 0)
        if st.get("duration_ts") and rate and st.get("codec_name", "").startswith("pcm"):
            dur = int(st["duration_ts"]) / rate
        return AudioInfo(path, st.get("codec_name", "?"), rate, int(st.get("channels") or 0), bits,
                         st.get("sample_fmt", ""), dur)
    try:
        with wave.open(path, "rb") as w:
            return AudioInfo(path, f"pcm_s{8 * w.getsampwidth()}le", w.getframerate(), w.getnchannels(),
                             8 * w.getsampwidth(), "s16" if w.getsampwidth() == 2 else "s32",
                             w.getnframes() / float(w.getframerate()))
    except (wave.Error, EOFError):
        raise AudioError(f"{os.path.basename(path)}: cannot read the format (ffprobe is missing)") from None


_PEAK_RE = re.compile(r"(Sample peak|True peak):\s*\n\s*Peak:\s*(-?inf|-?[\d.]+)\s*dBFS")
_LUFS_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?inf|-?[\d.]+)\s*LUFS")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")
SILENCE_DB = -60
SILENCE_MIN_S = 0.3


def measure(info: AudioInfo) -> AudioInfo:
    """Sample peak, true peak (ITU-R BS.1770), integrated loudness (ebur128) and silence at the
    start and end (silencedetect, below -60 dBFS), in a single ffmpeg pass."""
    if not shutil.which("ffmpeg"):
        return info
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", info.path, "-af",
                          f"ebur128=peak=true+sample,silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN_S}",
                          "-f", "null", "-"],
                         capture_output=True, text=True, timeout=600)
    text = out.stderr
    starts = [float(v) for v in _SIL_START_RE.findall(text)]
    ends = [float(v) for v in _SIL_END_RE.findall(text)]
    info.lead_silence_s = ends[0] if starts and starts[0] <= 0.01 and ends else 0.0
    if starts and len(starts) > len(ends):           # silence runs to the end
        info.tail_silence_s = max(0.0, info.duration_s - starts[-1])
    elif starts and ends and ends[-1] >= info.duration_s - 0.05:
        info.tail_silence_s = max(0.0, info.duration_s - starts[-1])
    else:
        info.tail_silence_s = 0.0
    for name, val in _PEAK_RE.findall(text):
        v = float("-inf") if "inf" in val else float(val)
        v = None if v == float("-inf") else v
        if name == "Sample peak":
            info.sample_peak_db = v
        else:
            info.true_peak_db = v
    m = _LUFS_RE.search(text)
    if m and "inf" not in m.group(1):
        info.loudness_lufs = float(m.group(1))
    return info


def track_warnings(info: AudioInfo) -> list[str]:
    w = []
    if info.duration_s < MIN_TRACK_S:
        w.append(f"shorter than {MIN_TRACK_S:.0f} seconds: the CD standard does not allow that")
    if info.sample_peak_db is not None and info.sample_peak_db > 0:
        w.append(f"peak above 0 dBFS ({_db(info.sample_peak_db)}): this clips when converted to 16 bit; "
                 "lower the volume")
    elif info.true_peak_db is not None and info.true_peak_db > TRUE_PEAK_WARN_DB:
        w.append(f"true peak {_db(info.true_peak_db)} dBTP: the waveform goes above full scale between samples; "
                 "a CD player may distort briefly there. Lower the volume by at least "
                 f"{_db(-info.true_peak_db - 0.1, sign=False)} dB if you want to avoid that")
    if info.channels > 2:
        w.append("more than two channels: will be mixed down to stereo")
    if info.bits == 8:
        w.append("8 bit source: the quality will not be better than the original")
    return w


def track_notes(info: AudioInfo) -> list[str]:
    """Notes that need no action: the musician chooses the ceiling of their own master."""
    n = []
    if info.tail_silence_s and info.tail_silence_s >= 1.0:
        n.append(f"ends with {_db(info.tail_silence_s, sign=False)} s of silence")
    tp = info.true_peak_db
    if tp is not None and TRUE_PEAK_NOTE_DB < tp <= TRUE_PEAK_WARN_DB:
        n.append(f"true peak {_db(tp)} dBTP: no problem for the CD itself. If someone rips the CD to mp3 or aac, "
                 "peaks above 0 can appear there (those formats often use -1 dBTP)")
    return n


def _db(v: float, sign: bool = True) -> str:
    return f"{v:+.1f}" if sign else f"{v:.1f}"


def convert_filter(dither: str = "triangular", gain_db: float = 0.0) -> str:
    """One aresample step for both sample rate and word length."""
    if dither not in DITHER_METHODS:
        raise AudioError(f"unknown dither method: {dither}")
    chain = []
    if gain_db:
        chain.append(f"volume={gain_db:.2f}dB")
    chain.append(f"aresample={CD_RATE}:resampler=soxr:precision=28:cheby=1:osf=s16:dither_method={dither}")
    return ",".join(chain)


def convert_command(src: str, dst: str, dither: str = "triangular", gain_db: float = 0.0) -> list[str]:
    return ["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:a:0", "-ac", "2",
            "-af", convert_filter(dither, gain_db), "-c:a", "pcm_s16le", "-f", "wav", dst]


def sectors(frames: int) -> int:
    return -(-frames // SAMPLES_PER_SECTOR)


def wav_frames(path: str) -> int:
    with wave.open(path, "rb") as w:
        return w.getnframes()


def gapless_align(wav_paths: list[str]) -> list[int]:
    """Move the boundaries between tracks to the nearest multiple of 588 samples.

    The tracks are read one after another and cut again, so every track except the last
    ends exactly on a sector boundary and no silence is added between them when burning.
    A boundary moves by at most 294 samples (6.7 ms). Works in place on 16-bit stereo WAVs
    (the output of convert_command). Returns the new number of samples per track.
    """
    lengths = [wav_frames(p) for p in wav_paths]
    total = sum(lengths)
    bounds, acc = [], 0
    for n in lengths[:-1]:
        acc += n
        b = int(round(acc / SAMPLES_PER_SECTOR)) * SAMPLES_PER_SECTOR
        bounds.append(min(max(b, (bounds[-1] if bounds else 0) + SAMPLES_PER_SECTOR), total))
    new_lengths = [b - a for a, b in zip([0] + bounds, bounds + [total])]
    if new_lengths == lengths:
        return lengths
    tmp_paths = [p + ".gapless" for p in wav_paths]
    readers = iter(wav_paths)
    src = wave.open(next(readers), "rb")
    params = src.getparams()
    try:
        for out_path, want in zip(tmp_paths, new_lengths):
            with wave.open(out_path, "wb") as out:
                out.setparams(params)
                left = want
                while left > 0:
                    chunk = src.readframes(min(left, 1 << 16))
                    if not chunk:
                        src.close()
                        src = wave.open(next(readers), "rb")
                        continue
                    out.writeframes(chunk)
                    left -= len(chunk) // params.sampwidth // params.nchannels
    finally:
        src.close()
    for tmp, p in zip(tmp_paths, wav_paths):
        os.replace(tmp, p)
    return new_lengths


# ---- CD-TEXT, ISRC and UPC -------------------------------------------------------

_TYPO = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
                       "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " "})


def cdtext_clean(text: str) -> tuple[str, bool]:
    """Convert text to ISO-8859-1 for CD-TEXT. Returns (text, changed?).

    Characters that exist in ISO-8859-1 (é, ü, ß) stay; others are simplified
    (ő to o, curly quotes to straight ones) or, as a last resort, become '?'.
    """
    t = text.translate(_TYPO)
    out = []
    for ch in t:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            try:
                base.encode("latin-1")
                out.append(base or "?")
            except UnicodeEncodeError:
                out.append("?")
    res = "".join(out)
    return res, res != text


def cdtext_size(texts: list[str]) -> int:
    """Rough estimate of the CD-TEXT space in bytes (text plus terminating zero)."""
    return sum(len(t.encode("latin-1", "replace")) + 1 for t in texts if t)


def check_isrc(code: str) -> str:
    c = code.strip().upper().replace("-", "")
    if c and not ISRC_RE.match(c):
        raise AudioError(f"ISRC '{code}' is invalid: expected 12 characters, for example NLA1B2600001")
    return c


def check_upc(code: str) -> str:
    c = code.strip().replace(" ", "")
    if c and not UPC_RE.match(c):
        raise AudioError(f"UPC/EAN '{code}' is invalid: expected 13 digits")
    return c


def msf(sectors_: int) -> str:
    """Sectors as MM:SS:FF for a cdrdao TOC."""
    m, rest = divmod(sectors_, 60 * SECTORS_PER_SECOND)
    s, f = divmod(rest, SECTORS_PER_SECOND)
    return f"{m}:{s:02d}:{f:02d}"


def suggested_pause(infos: list[AudioInfo]) -> float | None:
    """Suggest a pause between tracks when the masters already end with silence.

    If every track (except the last) ends with at least 2 s of silence, an extra pause of
    2 s doubles it, so 0 s is the obvious choice. Returns None if there is nothing to suggest.
    """
    tails = [i.tail_silence_s for i in infos[:-1]]
    if len(infos) < 2 or any(t is None for t in tails):
        return None
    return 0.0 if min(tails) >= 2.0 else None
