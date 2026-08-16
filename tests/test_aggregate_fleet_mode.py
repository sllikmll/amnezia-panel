from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "aggregate.py").read_text()


def test_aggregate_honors_show_local_server_setting():
    assert "PANEL_SHOW_LOCAL_SERVER" in SRC
    section = SRC[SRC.index("def get_db_servers"):SRC.index("def collect_all_stats")]
    assert "show_local" in section
    assert "if show_local" in section
