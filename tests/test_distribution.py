"""Check built artifacts after uv build; normal unit tests do not build packages."""

import csv
import io
import os
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LICENSE_FILES = {name: (ROOT / name).read_bytes() for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")}
DIST_DIR = os.environ.get("RSS_WATCH_DIST_DIR")
pytestmark = pytest.mark.skipif(not DIST_DIR, reason="set RSS_WATCH_DIST_DIR after uv build")


def assert_license_metadata(data):
    metadata = BytesParser().parsebytes(data)
    assert tuple(map(int, metadata["Metadata-Version"].split("."))) >= (2, 4)
    assert metadata.get_all("License-Expression") == ["MIT"]
    assert sorted(metadata.get_all("License-File", [])) == sorted(LICENSE_FILES)


def artifact(pattern):
    paths = list(Path(DIST_DIR).glob(pattern))
    assert len(paths) == 1, f"Expected one {pattern} in {DIST_DIR}, found {paths}"
    return paths[0]


def test_wheel_license_files_and_metadata():
    with zipfile.ZipFile(artifact("*.whl")) as archive:
        metadata_paths = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1
        metadata_path = metadata_paths[0]
        assert_license_metadata(archive.read(metadata_path))
        dist_info = metadata_path.rsplit("/", 1)[0]
        record = {row[0] for row in csv.reader(io.StringIO(
            archive.read(f"{dist_info}/RECORD").decode("utf-8")
        ))}
        for name, expected in LICENSE_FILES.items():
            path = f"{dist_info}/licenses/{name}"
            assert archive.read(path) == expected
            assert path in record


def test_sdist_license_files_and_metadata():
    with tarfile.open(artifact("*.tar.gz")) as archive:
        metadata_paths = [name for name in archive.getnames()
                          if name.count("/") == 1 and name.endswith("/PKG-INFO")]
        assert len(metadata_paths) == 1
        metadata_path = metadata_paths[0]
        assert_license_metadata(archive.extractfile(metadata_path).read())
        root = metadata_path.rsplit("/", 1)[0]
        for name, expected in LICENSE_FILES.items():
            assert archive.extractfile(f"{root}/{name}").read() == expected
