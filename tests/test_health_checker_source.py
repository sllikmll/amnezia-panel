from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backend" / "multi_server.py"


def _src() -> str:
    return SRC.read_text()


def test_health_check_finds_awg_binary_from_path():
    src = _src()
    assert "command -v awg" in src
    assert "awg show" in src
    assert "/usr/local/bin/awg", "health must not require only one fixed awg path"


def test_health_ok_requires_container_and_awg_running():
    src = _src()
    assert 'ok = container_status == "running" and awg_status == "running"' in src
    assert '"ok": ok' in src
