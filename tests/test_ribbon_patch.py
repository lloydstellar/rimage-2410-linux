"""Tests for tools/ribbon-patch.

The firmware itself is not in this repository. Most tests use a synthetic
image with the same layout. Set RIMAGE_FW_6033 to the path of
INTEGRA-XX10-6033.bin to also test with the real file.
"""

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ribbon_patch", Path(__file__).parent.parent / "tools" / "ribbon-patch" / "ribbon_patch.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def synthetic_original():
    data = bytearray(b"\xff" * rp.SIZE)
    for i in range(rp.SHA1_START, 0xDBD8):
        data[i] = (i * 7) & 0xFF
    data[rp.BOOT_OFFSET:rp.BOOT_OFFSET + len(rp.BOOT)] = rp.BOOT
    data[rp.VERSION_OFFSET:rp.VERSION_OFFSET + len(rp.VERSION)] = rp.VERSION
    for offset, opcode in rp.OPCODES.items():
        data[offset] = opcode
    for offset, old, _new in rp.PATCH:
        data[offset] = old
    data[rp.SHA1_OFFSET:rp.SHA1_OFFSET + 20] = hashlib.sha1(data[rp.SHA1_START:rp.SHA1_END]).digest()
    return bytes(data)


def expected_patch(data):
    out = bytearray(data)
    for offset, _old, new in rp.PATCH:
        out[offset] = new
    out[rp.SHA1_OFFSET:rp.SHA1_OFFSET + 20] = hashlib.sha1(out[rp.SHA1_START:rp.SHA1_END]).digest()
    return bytes(out)


@pytest.fixture
def fake(monkeypatch):
    original = synthetic_original()
    patched = expected_patch(original)
    monkeypatch.setattr(rp, "ORIGINAL_SHA256", rp.sha256(original))
    monkeypatch.setattr(rp, "PATCHED_SHA256", rp.sha256(patched))
    monkeypatch.setattr(rp, "ORIGINAL_SHA1", rp.computed_sha1(original))
    monkeypatch.setattr(rp, "PATCHED_SHA1", rp.computed_sha1(patched))
    return original, patched


def test_patch_changes_only_the_expected_bytes(fake):
    original, patched = fake
    out = rp.apply_patch(original)
    assert out == patched
    diff = [i for i in range(rp.SIZE) if original[i] != out[i]]
    assert set(diff) <= {o for o, _, _ in rp.PATCH} | set(range(rp.SHA1_OFFSET, rp.SHA1_OFFSET + 20))
    assert rp.identify(out) == "patched"


def test_refuses_patched_and_unknown(fake):
    original, patched = fake
    with pytest.raises(rp.PatchError, match="already patched"):
        rp.apply_patch(patched)
    damaged = bytearray(original)
    damaged[0x5000] ^= 1
    with pytest.raises(rp.PatchError, match="damaged"):
        rp.apply_patch(bytes(damaged))
    with pytest.raises(rp.PatchError, match="too short"):
        rp.apply_patch(original[:20000])
    other = bytearray(original)
    other[rp.VERSION_OFFSET + len(rp.VERSION) - 1] = ord("B")
    with pytest.raises(rp.PatchError, match="not version 6.033A"):
        rp.apply_patch(bytes(other))


def test_modified_with_valid_checksum_is_refused(fake):
    original, _ = fake
    mod = bytearray(original)
    mod[0x9000] ^= 0x55
    mod[rp.SHA1_OFFSET:rp.SHA1_OFFSET + 20] = hashlib.sha1(mod[rp.SHA1_START:rp.SHA1_END]).digest()
    with pytest.raises(rp.PatchError, match="modified by someone else"):
        rp.apply_patch(bytes(mod))


def test_verify_catches_extra_changes(fake):
    original, patched = fake
    bad = bytearray(patched)
    bad[0x8000] ^= 1
    with pytest.raises(rp.PatchError, match="outside the patch"):
        rp.verify_patched(original, bytes(bad))
    bad = bytearray(patched)
    bad[rp.SHA1_OFFSET] ^= 1
    with pytest.raises(rp.PatchError, match="SHA-1"):
        rp.verify_patched(original, bytes(bad))


def test_command_line(fake, tmp_path, capsys):
    original, patched = fake
    src = tmp_path / "INTEGRA-XX10-6033.bin"
    src.write_bytes(original)
    assert rp.main([str(src)]) == 0
    out = tmp_path / "INTEGRA-XX10-6033b.bin"
    assert out.read_bytes() == patched
    assert src.read_bytes() == original
    assert rp.main([str(src)]) == 1  # output exists
    assert rp.main([str(src), "--force"]) == 0
    assert rp.main([str(out)]) == 0  # already patched, nothing to do
    assert rp.main([str(src), "-o", str(src)]) == 1
    assert rp.main(["--check", str(src)]) == 0
    assert rp.main(["--check", str(out)]) == 0
    junk = tmp_path / "junk.bin"
    junk.write_bytes(bytes(rp.SIZE))
    assert rp.main(["--check", str(junk)]) == 1
    assert rp.main([str(junk)]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "INTEGRA-XX10-6033.bin", "INTEGRA-XX10-6033b.bin", "junk.bin"]


@pytest.mark.skipif(not os.environ.get("RIMAGE_FW_6033"), reason="RIMAGE_FW_6033 not set")
def test_real_firmware():
    original = Path(os.environ["RIMAGE_FW_6033"]).read_bytes()
    assert rp.identify(original) == "original"
    assert rp.sha256(rp.apply_patch(original)) == rp.PATCHED_SHA256
