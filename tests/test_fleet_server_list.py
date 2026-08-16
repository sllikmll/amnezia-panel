from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "main.py").read_text()


def test_local_pseudo_server_can_be_hidden_for_fleet_mode():
    assert "PANEL_SHOW_LOCAL_SERVER" in SRC
    section = SRC[SRC.index("def list_servers"):SRC.index('@app.post("/api/servers"')]
    assert "SHOW_LOCAL_SERVER" in section
