import importlib
import sys
import types


def _load_multi_server():
    fake = types.ModuleType("paramiko")
    for name in ("SSHClient", "SFTPFile", "AutoAddPolicy", "RSAKey", "Ed25519Key", "ECDSAKey"):
        setattr(fake, name, type(name, (), {}))
    sys.modules.setdefault("paramiko", fake)
    return importlib.import_module("backend.multi_server")


def test_awg2_dump_skips_interface_and_maps_peer_counters_correctly():
    module = _load_multi_server()
    interface = "\t".join([
        "awg0", "I" * 44, "S" * 44, "33415", "4", "10", "50", "128", "16", "52", "1"
    ])
    peer = "\t".join([
        "awg0", "P" * 44, "K" * 44, "203.0.113.5:5555", "10.8.1.1/32",
        "1700000000", "123", "456", "off"
    ])
    parsed = module.parse_remote_dump(interface + "\n" + peer + "\n")
    assert parsed == [{
        "public_key": "P" * 44,
        "endpoint": "203.0.113.5:5555",
        "transfer_rx": 123,
        "transfer_tx": 456,
        "last_handshake": 1700000000,
        "allowed_ips": "10.8.1.1/32",
    }]
