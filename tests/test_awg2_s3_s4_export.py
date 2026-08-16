from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "main.py").read_text()


def test_awg2_export_preserves_s3_and_s4():
    assert 'AWG_OBFUSCATION_FIELDS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4"' in SRC
