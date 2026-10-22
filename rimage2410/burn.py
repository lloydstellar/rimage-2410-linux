"""Build, check and run burn jobs.

Source kinds:
- 'iso'   : data CD/DVD from an ISO image; burned with xorriso, verified by reading it
            back and comparing the SHA-256.
- 'audio' : audio CD from WAV/FLAC/MP3 (or other formats ffmpeg reads); converted to
            44.1 kHz, 16 bit, stereo (soxr, with dither, see audio.py), then burned in
            SAO mode with a pause (pregap) per track, or continuous (gapless). By default with
            cdrskin and a v07t sheet for CD-TEXT; cdrdao with a TOC file also works (CD-TEXT
            is only reliable from cdrdao 1.2.6).
- 'cue'   : BIN/CUE image; burned with cdrdao.

Note: an audio CD has no ISO file system. There is no such thing as an "ISO of an
audio CD"; use separate audio files or BIN/CUE.

Burning uses standard Linux tools (xorriso, cdrdao, ffmpeg, eject). With
dry_run=True it only describes what would happen.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import wave
from dataclasses import dataclass, field
from typing import Callable

from . import audio as A

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aiff", ".aif"}
CD_SECONDS_MAX = 80 * 60          # 80-minute CD
CD_SECONDS_SAFE = 79 * 60 + 30    # half a minute of margin


class BurnError(RuntimeError):
    pass


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def audio_duration_s(path: str) -> float | None:
    """Playing time in seconds. WAV is read directly; other formats use ffprobe."""
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as w:
                return w.getnframes() / float(w.getframerate())
        except wave.Error:
            pass
    if tool_available("ffprobe"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60)
        try:
            return float(out.stdout.strip())
        except ValueError:
            return None
    return None


def _toc_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class BurnPlan:
    kind: str                          # 'none', 'iso', 'audio', 'cue'
    sources: list[str]
    workdir: str
    titles: list[str] = field(default_factory=list)   # CD-TEXT per track (audio)
    album: str = ""
    artist: str = ""
    performers: list[str] = field(default_factory=list)   # artist per track; empty = album artist
    pauses: list[float] = field(default_factory=list)     # pause in seconds before track i (i >= 2); default 2
    gapless: bool = False              # continuous: no pauses, track boundaries on sector boundaries
    dither: str = "triangular"         # see audio.DITHER_METHODS
    gain_db: float = 0.0               # fixed reduction before the dither (0 = none)
    isrc: list[str] = field(default_factory=list)          # per track, optional
    upc: str = ""                      # UPC/EAN of the disc (Media Catalog Number), optional
    copy_permitted: bool = True        # copy bit: your own work may be copied
    speed: int | None = None           # None = default speed of the recorder
    fifo_mb: int = 64                  # buffer per burner in memory; absorbs storage hiccups
    verify: bool = True
    audio_engine: str = "cdrskin"      # 'cdrskin' or 'cdrdao'
    parallel: bool = False             # two recorders at once (cdrskin: modesty_on_drive)
    prepared: bool = False
    _prep_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # -- checks up front ------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of warnings; raise BurnError on hard errors."""
        warnings: list[str] = []
        if self.kind == "none":
            return warnings
        if not self.sources:
            raise BurnError("no source files")
        for p in self.sources:
            if not os.path.isfile(p):
                raise BurnError(f"file does not exist: {p}")
        if self.kind == "iso":
            if len(self.sources) != 1:
                raise BurnError("a data CD needs exactly one ISO file")
            size = os.path.getsize(self.sources[0])
            if size > 900 * 1024 * 1024:
                warnings.append("ISO is larger than 900 MB: use a DVD")
            elif size > 700 * 1024 * 1024:
                warnings.append("ISO is larger than 700 MB: only fits on an 80-minute CD-R with overburn")
        elif self.kind == "audio":
            warnings += self._validate_audio()
        elif self.kind == "cue":
            cues = [p for p in self.sources if p.lower().endswith(".cue")]
            if len(cues) != 1:
                raise BurnError("BIN/CUE needs exactly one .cue file")
        else:
            raise BurnError(f"unknown source kind: {self.kind}")
        return warnings

    def pregap_sectors(self, i: int) -> int:
        """Pause before track i (0-based) in sectors. Track 1 always gets 2 s from the burn tool itself."""
        if i == 0 or self.gapless:
            return 0
        sec = self.pauses[i] if i < len(self.pauses) and self.pauses[i] is not None else 2.0
        return int(round(max(0.0, min(A.MAX_PAUSE_S, float(sec))) * A.SECTORS_PER_SECOND))

    def _validate_audio(self) -> list[str]:
        warnings: list[str] = []
        n = len(self.sources)
        if n > A.MAX_TRACKS:
            raise BurnError(f"an audio CD has at most {A.MAX_TRACKS} tracks")
        if self.dither not in A.DITHER_METHODS:
            raise BurnError(f"unknown dither method: {self.dither}")
        if not -12.0 <= float(self.gain_db) <= 0.0:
            raise BurnError("volume reduction must be between -12 and 0 dB")
        try:
            self.isrc = [A.check_isrc(c or "") for c in self.isrc]
            self.upc = A.check_upc(self.upc or "")
        except A.AudioError as e:
            raise BurnError(str(e)) from None
        sectors = A.FIRST_PREGAP_SECTORS
        unknown = []
        for i, p in enumerate(self.sources):
            if os.path.splitext(p)[1].lower() not in AUDIO_EXT:
                raise BurnError(f"not an audio file: {os.path.basename(p)}")
            d = audio_duration_s(p)
            if d is None:
                unknown.append(os.path.basename(p))
                continue
            if d < A.MIN_TRACK_S:
                raise BurnError(f"track {i + 1} ({os.path.basename(p)}) lasts {d:.1f} s; "
                                f"a CD track must be at least {A.MIN_TRACK_S:.0f} s")
            sectors += A.sectors(int(round(d * A.CD_RATE))) + self.pregap_sectors(i)
        minutes = sectors / A.SECTORS_PER_SECOND / 60
        if sectors > A.CD_SECTORS_MAX:
            raise BurnError(f"total playing time with pauses is {minutes:.1f} min, more than 80 minutes")
        if sectors > A.CD_SECTORS_74:
            warnings.append(f"playing time {minutes:.1f} min: fits on an 80-minute CD-R, but is longer than "
                            "74 minutes (the original CD length; some old players have trouble with that)")
        if unknown:
            warnings.append("playing time unknown (ffprobe missing?) for: " + ", ".join(unknown))
        texts = [self.album, self.artist, *self.titles, *self.performers]
        changed = [t for t in texts if t and A.cdtext_clean(t)[1]]
        if changed:
            warnings.append("CD-TEXT only supports Western European characters; changed: "
                            + ", ".join(f"'{t}' becomes '{A.cdtext_clean(t)[0]}'" for t in changed[:5]))
        if A.cdtext_size(texts) > A.CDTEXT_BYTES_MAX:
            warnings.append("a lot of CD-TEXT: shorten the titles, or not everything will fit")
        return warnings

    def required_tools(self) -> list[str]:
        return {"none": [], "iso": ["xorriso"], "audio": ["ffmpeg", self.audio_engine],
                "cue": ["cdrdao"]}[self.kind]

    # -- commands ---------------------------------------------------------
    def _wav_path(self, i: int) -> str:
        return os.path.join(self.workdir, f"track{i + 1:02d}.wav")

    def _text(self, value: str) -> str:
        return A.cdtext_clean(value or "")[0]

    def _track_performer(self, i: int) -> str:
        return self._text(self.performers[i] if i < len(self.performers) and self.performers[i] else self.artist)

    def _track_isrc(self, i: int) -> str:
        return self.isrc[i] if i < len(self.isrc) else ""

    def toc_text(self) -> str:
        """TOC file for cdrdao (audio, disc-at-once, pauses, CD-TEXT if there are titles)."""
        cdtext = self.has_cdtext()
        lines = ["CD_DA"]
        if self.upc:
            lines.append(f'CATALOG "{self.upc}"')
        lines.append("")
        if cdtext:
            lines += ["CD_TEXT {", "  LANGUAGE_MAP { 0 : EN }", "  LANGUAGE 0 {",
                      f'    TITLE "{_toc_escape(self._text(self.album))}"',
                      f'    PERFORMER "{_toc_escape(self._text(self.artist))}"', "  }", "}", ""]
        for i, _src in enumerate(self.sources):
            lines.append("TRACK AUDIO")
            lines.append("COPY" if self.copy_permitted else "NO COPY")
            if self._track_isrc(i):
                lines.append(f'ISRC "{self._track_isrc(i)}"')
            if cdtext:
                title = self.titles[i] if i < len(self.titles) else ""
                lines += ["CD_TEXT {", "  LANGUAGE 0 {",
                          f'    TITLE "{_toc_escape(self._text(title))}"',
                          f'    PERFORMER "{_toc_escape(self._track_performer(i))}"', "  }", "}"]
            if self.pregap_sectors(i):
                lines.append(f"PREGAP {A.msf(self.pregap_sectors(i))}")
            lines.append(f'FILE "{self._wav_path(i)}" 0')
            lines.append("")
        return "\n".join(lines)

    def prepare_commands(self) -> list[list[str]]:
        """Commands that run before burning (conversion to CD audio)."""
        if self.kind != "audio":
            return []
        return [A.convert_command(src, self._wav_path(i), self.dither, float(self.gain_db))
                for i, src in enumerate(self.sources)]

    def v07t_text(self) -> str:
        """CD-TEXT as a Sony Input Sheet v0.7T for cdrskin (input_sheet_v07t=). Not tested on hardware yet."""
        n = len(self.sources)
        lines = ["Input Sheet Version = 0.7T", "Text Code = 8859", "Language Code = English",
                 f"Album Title = {self._text(self.album)}", f"Artist Name = {self._text(self.artist)}"]
        if self.upc:
            lines.append(f"UPC / EAN = {self.upc}")
        lines += ["First Track Number = 1", f"Last Track Number = {n}"]
        for i in range(n):
            title = self.titles[i] if i < len(self.titles) else ""
            lines += [f"Track {i + 1:02d} Title = {self._text(title)}",
                      f"Track {i + 1:02d} Artist = {self._track_performer(i)}"]
            if self._track_isrc(i):
                lines.append(f"ISRC {i + 1:02d} = {self._track_isrc(i)}")
        return "\n".join(lines) + "\n"

    def has_cdtext(self) -> bool:
        return bool(any(self.titles) or self.album or self.artist or any(self.performers))

    def burn_commands(self, device: str) -> list[list[str]]:
        speed = [] if self.speed is None else ["--speed", str(self.speed)]
        fs = f"fs={self.fifo_mb}m"
        # cdrdao: each buffer is 1 second of CD audio (176,400 bytes)
        cdrdao_buf = ["--buffers", str(max(32, self.fifo_mb * 1024 * 1024 // 176_400))]
        if self.kind == "iso":
            xs = [] if self.speed is None else [f"speed={self.speed}"]
            return [["xorriso", "-as", "cdrecord", "-v", f"dev={device}", "-dao", fs, *xs, self.sources[0]]]
        if self.kind == "audio" and self.audio_engine == "cdrskin":
            sp = [] if self.speed is None else [f"speed={self.speed}"]
            sheet = ([f"input_sheet_v07t={os.path.join(self.workdir, 'cdtext.v07t')}"]
                     if self.has_cdtext() else [])
            modest = (["modesty_on_drive=1:min_percent=90:max_percent=95:min_usec=5000:max_usec=25000"]
                      if self.parallel else [])
            tracks: list[str] = []
            for i in range(len(self.sources)):
                if self._track_isrc(i):
                    tracks.append(f"isrc={self._track_isrc(i)}")
                if self.pregap_sectors(i):
                    tracks.append(f"sao_pregap={self.pregap_sectors(i)}")   # applies to the next track
                tracks.append(self._wav_path(i))
            # --audio_not_raw: refuse a file that is not a recognisable WAV (do not burn noise)
            return [["cdrskin", "-v", f"dev={device}", *sp, fs, *modest, "-sao", "--audio_not_raw",
                     "-copy" if self.copy_permitted else "-nocopy",
                     *([f"mcn={self.upc}"] if self.upc else []), *sheet, *tracks]]
        if self.kind == "audio":
            return [["cdrdao", "write", "--device", device, *speed, *cdrdao_buf, "-n",
                     os.path.join(self.workdir, "audio.toc")]]
        if self.kind == "cue":
            cue = next(p for p in self.sources if p.lower().endswith(".cue"))
            return [["cdrdao", "write", "--device", device, *speed, *cdrdao_buf, "-n", cue]]
        return []

    def describe(self, device: str) -> list[str]:
        out = [" ".join(c) for c in self.prepare_commands()]
        if self.kind == "audio" and self.gapless:
            out.append("gapless: put track boundaries on a multiple of 588 samples")
        if self.kind == "audio" and self.audio_engine == "cdrdao":
            out.append(f"write {os.path.join(self.workdir, 'audio.toc')}")
        if self.kind == "audio" and self.audio_engine == "cdrskin" and self.has_cdtext():
            out.append(f"write {os.path.join(self.workdir, 'cdtext.v07t')}")
        out += [" ".join(c) for c in self.burn_commands(device)]
        if self.verify and self.kind == "iso":
            out.append(f"verify: read {device} back and compare SHA-256 with the ISO")
        return out


CD_SECTORS_MAX = 360_000   # 80 minutes x 60 s x 75 sectors
SAMPLES_PER_SECTOR = 588


def cd_sectors(wav_paths: list[str], pregaps: list[int] | None = None) -> int:
    """Exact space on the CD: per track ceil(samples / 588), plus 150 sectors before track 1
    and the pauses (pregaps, in sectors) before the other tracks."""
    total = A.FIRST_PREGAP_SECTORS + sum(pregaps or [])
    for p in wav_paths:
        with wave.open(p, "rb") as w:
            total += -(-w.getnframes() // SAMPLES_PER_SECTOR)
    return total


# Progress lines from cdrskin and xorriso that add nothing to the log
_PROGRESS = re.compile(r"thank you for being patient|working pre-track|^Track \d+: +\d+ of +\d+ MB written"
                       r"|^xorriso : UPDATE :")


def useful_output(text: str, keep: int = 20) -> list[str]:
    """Last lines of the output, without empty lines and progress messages (which overwrite with \\r)."""
    lines = [ln.strip() for ln in re.split(r"[\r\n]+| {3,}", text)]   # cdrskin appends messages after progress output
    return [ln for ln in lines if ln and not _PROGRESS.search(ln)][-keep:]


def _run(cmd: list[str], log: Callable[[str], None], timeout_s: int = 3600) -> None:
    log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    for line in useful_output(proc.stdout + proc.stderr):
        log("  " + line)
    if proc.returncode != 0:
        raise BurnError(f"{cmd[0]} returned exit code {proc.returncode}")


def sha256_file(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            want = 1 << 20 if limit is None else min(1 << 20, limit - done)
            if want <= 0:
                break
            chunk = f.read(want)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
    return h.hexdigest()


class LocalBurner:
    """Burns with a recorder on this computer using the standard tools.

    The dry run uses DryRunBurner instead (see runner.py).
    """

    def __init__(self, device: str, element: int):
        self.device = device
        self.element = element

    def burn(self, plan: BurnPlan, log: Callable[[str], None]) -> None:
        missing = [t for t in plan.required_tools() if not tool_available(t)]
        if missing:
            raise BurnError("missing tools: " + ", ".join(missing))
        with plan._prep_lock:              # convert only once when two recorders run at the same time
            if not plan.prepared:
                os.makedirs(plan.workdir, exist_ok=True)
                for cmd in plan.prepare_commands():
                    _run(cmd, log)
                if plan.kind == "audio" and plan.gapless:
                    wavs = [plan._wav_path(i) for i in range(len(plan.sources))]
                    A.gapless_align(wavs)
                    log("gapless: track boundaries moved to sector boundaries (shifted by at most 6.7 ms)")
                self._write_sheets(plan, log)
                plan.prepared = True
        for cmd in plan.burn_commands(self.device):
            _run(cmd, log, timeout_s=3 * 3600)
        if plan.verify and plan.kind == "iso":
            size = os.path.getsize(plan.sources[0])
            want = sha256_file(plan.sources[0])
            got = sha256_file(self.device, limit=size)
            if want != got:
                raise BurnError("verify failed: burned data differs from the ISO")
            log("verify passed: SHA-256 matches")

    @staticmethod
    def _write_sheets(plan: BurnPlan, log: Callable[[str], None]) -> None:
        if plan.kind == "audio":
            sectors = cd_sectors([plan._wav_path(i) for i in range(len(plan.sources))],
                                 [plan.pregap_sectors(i) for i in range(len(plan.sources))])
            log(f"audio: {sectors} sectors ({sectors / 75 / 60:.1f} min) of at most {CD_SECTORS_MAX}")
            if sectors > CD_SECTORS_MAX:
                raise BurnError("audio does not fit on an 80-minute CD")
            if plan.audio_engine == "cdrdao":
                with open(os.path.join(plan.workdir, "audio.toc"), "w") as f:
                    f.write(plan.toc_text())
            elif plan.has_cdtext():
                with open(os.path.join(plan.workdir, "cdtext.v07t"), "w", encoding="latin-1",
                          errors="replace") as f:
                    f.write(plan.v07t_text())

    def eject(self) -> None:
        subprocess.run(["eject", self.device], check=True, timeout=60)

    def close_tray(self) -> None:
        subprocess.run(["eject", "-t", self.device], check=True, timeout=60)

    def tray_open(self) -> bool | None:
        """Tray state via the Linux ioctl CDROM_DRIVE_STATUS (0x5326); None if that fails.

        The kernel answers this with the SCSI MECHANISM STATUS command.
        """
        import fcntl
        CDROM_DRIVE_STATUS, CDSL_CURRENT, CDS_TRAY_OPEN = 0x5326, 0x7FFFFFFF, 2
        try:
            fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT) == CDS_TRAY_OPEN
        except OSError:
            return None
        finally:
            os.close(fd)


# ---- work folder in memory ----------------------------------------------------------

RAM_ROOT = "/dev/shm"              # tmpfs on Linux and Raspberry Pi OS
RAM_RESERVE = 1024 ** 3            # leave at least 1 GB of RAM free for everything else
CD_AUDIO_BYTES_PER_S = 176_400


def _mem_available() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def audio_bytes_needed(sources: list[str]) -> int:
    """Space for the converted CD audio (16 bit, 44.1 kHz, stereo) plus sheets."""
    seconds = sum(audio_duration_s(p) or 0.0 for p in sources)
    return int(seconds * CD_AUDIO_BYTES_PER_S * 1.02) + 16 * 1024 * 1024


def pick_burn_dir(kind: str, sources: list[str], job_id: str, fallback: str, ram_root: str | None = RAM_ROOT,
                  log: Callable[[str], None] = lambda _m: None) -> tuple[str, bool]:
    """Choose where the converted audio goes: in RAM if it fits, otherwise on disk.

    In RAM, storage speed no longer matters during the burn. Slow storage can make burns
    with two recorders at once fail. Returns (folder, in_ram).
    """
    if kind != "audio" or not ram_root or not os.path.isdir(ram_root):
        return fallback, False
    need = audio_bytes_needed(sources)
    st = os.statvfs(ram_root)
    free = st.f_bavail * st.f_frsize
    avail = _mem_available()
    room = min(free, avail) if avail is not None else free
    if need + RAM_RESERVE > room:
        log(f"work files on disk: {need / 1e6:.0f} MB does not fit in free RAM "
            f"({room / 1e6:.0f} MB, 1 GB is kept free)")
        return fallback, False
    d = os.path.join(ram_root, "rimage2410", job_id)
    log(f"work files in RAM ({d}, about {need / 1e6:.0f} MB)")
    return d, True


def cleanup_stale_ram_dirs(ram_root: str | None = RAM_ROOT, keep: str | None = None, max_age_s: float = 3 * 3600) -> None:
    """Remove old work folders from RAM (left behind, for example, by a job that stopped while
    a burner was still running; after a power failure tmpfs is empty anyway)."""
    import time
    base = os.path.join(ram_root or "", "rimage2410")
    if not ram_root or not os.path.isdir(base):
        return
    for name in os.listdir(base):
        p = os.path.join(base, name)
        if p == keep:
            continue
        try:
            if time.time() - os.path.getmtime(p) > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

