"""The ./muse tool without Docker: the Radarr/Sonarr step's helpers, .env writing, pairing links."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_tool(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("muse_tool", str(ROOT / "muse"))
    spec = importlib.util.spec_from_loader("muse_tool", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(mod, "ENV", tmp_path / ".env")
    calls = []
    monkeypatch.setattr(mod, "compose", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, b"", b""))
    monkeypatch.setattr(mod, "wait_healthy", lambda *a, **k: None)
    return mod, calls


def test_budget_suggestion():
    loader = importlib.machinery.SourceFileLoader("muse_tool2", str(ROOT / "muse"))
    spec = importlib.util.spec_from_loader("muse_tool2", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    assert m.suggest_budget(None) == 300
    assert m.suggest_budget(150) == 100
    assert m.suggest_budget(1800) == 600
    assert m.suggest_budget(20000) == 1000


def test_apply_setup_enables_curator_and_keeps_secrets(tmp_path, monkeypatch):
    m, calls = load_tool(tmp_path, monkeypatch)
    ans = {"mode": "lan", "tls_host": "muse.lan", "tls_bind": "192.168.1.5", "jellyfin_url": "http://192.168.1.4:8096",
           "arr": {"RADARR_URL": "http://192.168.1.4:7878", "RADARR_ROOT_FOLDER": "/movies", "MUSE_CURATOR": "on", "MUSE_CURATOR_BUDGET_GB": "500"}}
    m.apply_setup(ans, {"radarr_api_key": "rk", "jellyfin_api_key": "jk"}, start=True, log=lambda kind, msg: None)
    env = m.env_values()
    assert set(env["COMPOSE_PROFILES"].split(",")) == {"tls", "arr", "curator"}
    assert env["RADARR_URL"] == "http://192.168.1.4:7878" and env["MUSE_CURATOR_BUDGET_GB"] == "500" and "MUSE_CURATOR" not in env
    assert (tmp_path / "secrets" / "radarr_api_key").read_text().strip() == "rk"
    key = (tmp_path / "secrets" / "muse_master_key").read_text()
    assert ("up", "-d") in calls
    # running it again never replaces the master key; turning the curator off keeps the arr signal
    ans["arr"]["MUSE_CURATOR"] = "off"
    m.apply_setup(ans, {}, start=False, log=lambda kind, msg: None)
    assert (tmp_path / "secrets" / "muse_master_key").read_text() == key
    assert set(m.env_values()["COMPOSE_PROFILES"].split(",")) == {"tls", "arr"}


def test_pairing_link_and_terminal_qr(tmp_path, monkeypatch, capsys):
    m, _ = load_tool(tmp_path, monkeypatch)
    link = m.pairing_link("CODE123456", {"query_url": "https://muse.lan:8443/query", "ca_sha256": "ab" * 32})
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    assert link.startswith("muse://pair?") and q == {"u": ["https://muse.lan:8443/query"], "c": ["CODE123456"], "fp": ["ab" * 32]}
    m.show_qr(link)
    out = capsys.readouterr().out
    assert "█" in out and len(out.splitlines()) > 20


def test_terminal_qr_decodes():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    sys.path.insert(0, str(ROOT / "installer"))
    import qr

    text = "muse://pair?u=https%3A%2F%2Fmuse.lan%3A8443%2Fquery&c=abcdefghijklmnopqrstuvwx&fp=" + "ab" * 32
    code = qr.encode(text, "M")
    s, b = 8, 4
    img = np.full(((code.size + 2 * b) * s,) * 2, 255, np.uint8)
    for y in range(code.size):
        for x in range(code.size):
            if code.modules[y][x]:
                img[(y + b) * s:(y + b + 1) * s, (x + b) * s:(x + b + 1) * s] = 0
    assert cv2.QRCodeDetector().detectAndDecode(img)[0] == text
    assert qr.to_svg(code).startswith("<svg")
