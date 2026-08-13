import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"


AWG2_CLIENT_CONF = """[Interface]
PrivateKey = CLIENT_PRIVATE
Address = 10.248.118.2/32
DNS = 1.1.1.1, 8.8.8.8
Jc = 7
Jmin = 50
Jmax = 1000
S1 = 82
S2 = 210
H1 = 123456789
H2 = 987654321
H3 = 192837465
H4 = 564738291

[Peer]
PublicKey = SERVER_PUBLIC
PresharedKey = CLIENT_PSK
Endpoint = veesplv.dogonin.ru:43018
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""


def _load_config_helpers():
    tree = ast.parse(MAIN.read_text())
    names = {
        "AWG_OBFUSCATION_FIELDS",
        "_format_awg_fields",
        "_parse_client_conf_text",
        "gen_client_config_remote",
    }
    module = ast.Module(
        body=[node for node in tree.body if isinstance(node, (ast.Assign, ast.FunctionDef)) and getattr(node, "name", None) in names or (isinstance(node, ast.Assign) and any(getattr(t, "id", None) in names for t in node.targets))],
        type_ignores=[],
    )
    ns = {"Optional": object}
    exec(compile(module, str(MAIN), "exec"), ns)
    return ns


def test_parse_client_conf_preserves_awg2_obfuscation_fields():
    ns = _load_config_helpers()
    parsed = ns["_parse_client_conf_text"](AWG2_CLIENT_CONF)

    assert parsed["awg_fields"] == {
        "Jc": "7",
        "Jmin": "50",
        "Jmax": "1000",
        "S1": "82",
        "S2": "210",
        "H1": "123456789",
        "H2": "987654321",
        "H3": "192837465",
        "H4": "564738291",
    }


def test_generated_remote_config_includes_awg2_obfuscation_fields():
    ns = _load_config_helpers()
    config = ns["gen_client_config_remote"](
        "CLIENT_PRIVATE",
        "10.248.118.2",
        "CLIENT_PSK",
        "SERVER_PUBLIC",
        "veesplv.dogonin.ru:43018",
        awg_fields={
            "Jc": "7",
            "Jmin": "50",
            "Jmax": "1000",
            "S1": "82",
            "S2": "210",
            "H1": "123456789",
            "H2": "987654321",
            "H3": "192837465",
            "H4": "564738291",
        },
    )

    interface_block = config.split("\n[Peer]\n", 1)[0]
    for field in ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"]:
        assert f"{field} = " in interface_block
    assert "Jc = 7" in interface_block
    assert "H4 = 564738291" in interface_block
    assert "AllowedIPs = 0.0.0.0/0" in config
