from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def _html() -> str:
    return HTML.read_text()


def test_header_status_is_awg_health_not_websocket_state():
    html = _html()
    assert 'id="health-status"' in html
    assert 'async function updateAwgHealth()' in html
    assert "updateAwgHealth();" in html
    ws_block = html[html.index("function connectWS()"):html.index("async function updateAwgHealth()")]
    assert "health-status" not in ws_block
    assert "● live" not in ws_block
    assert "○ offline" not in ws_block


def test_health_polling_is_started_from_main_view():
    html = _html()
    show_main = html[html.index("function showMain()"):html.index("function switchTab")]
    assert "updateAwgHealth();" in show_main
    assert "healthPollTimer" in show_main
