"""Download pinned upstream releases and verify digests and ARM64 ELF metadata."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import sys
from urllib.request import Request, urlopen
import zipfile

def download(item):
    request = Request(item["url"], headers={"User-Agent": "miho-arm64-build"})
    with urlopen(request, timeout=120) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024:
        raise RuntimeError("upstream archive exceeds size limit")
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise RuntimeError("upstream SHA256 mismatch")
    return data

def main(output):
    versions = json.loads(Path("versions.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    binary = gzip.decompress(download(versions["mihomo"]))
    if binary[:5] != b"\x7fELF\x02" or struct.unpack("<H", binary[18:20])[0] != 183:
        raise RuntimeError("Mihomo binary is not ELF64 AArch64")
    (output / "mihomo").write_bytes(binary)
    (output / "mihomo").chmod(0o755)
    with zipfile.ZipFile(io.BytesIO(download(versions["zashboard"]))) as archive:
        for item in archive.infolist():
            path = Path(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("unsafe dashboard archive")
        extracted = output / "dashboard-unpacked"
        archive.extractall(extracted)
    indexes = list(extracted.rglob("index.html"))
    if not indexes:
        raise RuntimeError("dashboard index.html missing")
    index = min(indexes, key=lambda item: len(item.parts))
    shutil.move(str(index.parent), str(output / "zashboard"))
    if extracted.exists():
        shutil.rmtree(extracted)
    print("Verified Mihomo ARM64 and bundled Zashboard:", versions["mihomo"]["version"], versions["zashboard"]["version"])

if __name__ == "__main__":
    main(Path(sys.argv[1]))

