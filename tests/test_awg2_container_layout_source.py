from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
MULTI = ROOT / "backend" / "multi_server.py"


def test_client_import_supports_current_awg2_container_path():
    src = MAIN.read_text()
    assert "'/opt/amnezia/clients'" in src
    assert "docker exec" in src


def test_awg_fields_support_current_server_config_inside_container():
    src = MAIN.read_text()
    assert '"/opt/amnezia/awg/awg0.conf"' in src
    assert "_docker_exec_shell" in src


def test_remote_peer_listing_discovers_awg_binary_instead_of_fixed_usr_local_path():
    src = MULTI.read_text()
    peers_section = src[src.index("def get_server_peers"):src.index("def parse_remote_dump")]
    assert "command -v awg" in peers_section
    assert '"/usr/local/bin/awg"' not in peers_section


def test_server_pubkey_is_derived_from_current_persistent_config():
    src = MULTI.read_text()
    section = src[src.index("def get_server_pubkey"):src.index("def create_remote_peer")]
    assert "/opt/amnezia/awg/awg0.conf" in src
    assert "_config_discovery_script" in section
    assert "pubkey" in section
    assert "/data/server.pub" not in section


def test_peer_mutations_are_persistent_and_use_current_clients_dir():
    src = MULTI.read_text()
    create = src[src.index("def create_remote_peer"):src.index("def delete_remote_peer")]
    delete = src[src.index("def delete_remote_peer"):src.index("def service_action")]
    assert "/opt/amnezia/awg/awg0.conf" in src
    assert "/opt/amnezia/clients" in src
    assert "_config_discovery_script" in create
    assert "docker restart" in create
    assert "_config_discovery_script" in delete
    assert "docker restart" in delete
