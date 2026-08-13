from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
HTML = ROOT / "frontend" / "index.html"
STATIC_HTML = ROOT / "backend" / "static" / "index.html"
README = ROOT / "README.md"


def _main() -> str:
    return MAIN.read_text()


def _html() -> str:
    return HTML.read_text()


def test_backend_imports_existing_clients_from_server_configs():
    src = _main()
    assert "def import_existing_clients_from_server" in src
    assert "/opt/amnezia/state/amnezia-awg2-direct/clients" in src
    assert "/opt/amnezia/state/amnezia-awg2/clients" in src
    assert "execute_on_server(server_row, 'cat ' + shlex.quote(conf_path)" in src
    assert "wg', 'pubkey'" in src


def test_server_create_and_manual_import_endpoint_exist():
    src = _main()
    assert "import_existing_clients_from_server(_resolve_server(server_id))" in src
    assert '@app.post("/api/servers/{server_id}/import-clients"' in src
    assert "ImportClientsResponse" in src
    assert "import_result: Optional[Dict] = None" in src


def test_existing_peer_config_endpoint_exists():
    src = _main()
    assert '@app.get("/api/peers/{peer_id}/config"' in src
    assert "def get_peer_config" in src
    assert "_peer_config_response" in src
    assert "make_qr(config)" in src


def test_frontend_has_import_buttons_and_clickable_clients():
    html = _html()
    assert "refreshAllServerClients" in html
    assert "importServerClients" in html
    assert "Клиенты</button>" in html
    assert "showPeerConfig" in html
    assert "clickable-row" in html
    assert "event.stopPropagation(); showPeerConfig" in html


def test_frontend_static_copy_synced():
    assert HTML.read_text() == STATIC_HTML.read_text()


def test_readme_documents_import_and_config_download():
    readme = README.read_text()
    assert "Импорт существующих клиентов" in readme
    assert "/api/peers/{peer_id}/config" in readme
    assert "/api/servers/{server_id}/import-clients" in readme
