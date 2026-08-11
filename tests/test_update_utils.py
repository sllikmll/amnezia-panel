import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from update_utils import normalize_version, is_newer_version, best_release_tag


def test_normalize_version_variants():
    assert normalize_version("v1.2.3") == (1, 2, 3)
    assert normalize_version("1.2") == (1, 2, 0)
    assert normalize_version("release-2.0.1-beta") == (2, 0, 1)
    assert normalize_version(None) == (0, 0, 0)


def test_is_newer_version():
    assert is_newer_version("v1.1.0", "1.0.9") is True
    assert is_newer_version("v1.0.0", "1.0.0") is False
    assert is_newer_version("v0.9.9", "1.0.0") is False


def test_best_release_tag():
    assert best_release_tag(["v1.0.0", "v1.2.0", "v1.1.5"], "1.0.0") == "v1.2.0"
    assert best_release_tag(["v1.0.0"], "1.0.0") is None
