"""CLI and network watchdog (no network/hardware needed)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from dyson_pi.cli.main import main
from dyson_pi.cli.netwatch import NetWatch
from dyson_pi.config import AppConfig


def _cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "hardware": {"backend": "mock"},
        "paths": {"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")},
        "web": {"port": 1},  # nothing listens there -> "service not running"
    }))
    return p


def test_config_check_and_get(tmp_path, capsys):
    cfg = _cfg_file(tmp_path)
    assert main(["--config", str(cfg), "config", "check"]) == 0
    assert main(["--config", str(cfg), "config", "get", "hardware.relay_pin"]) == 0
    assert main(["--config", str(cfg), "config", "get", "hardware.relay_active_low"]) == 0
    out = capsys.readouterr().out.split()
    assert out[-2:] == ["17", "true"]


def test_bad_config_exit_code(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("press: {press_duration_s: 9}\n")
    assert main(["--config", str(p), "config", "check"]) == 2


def test_status_without_service(tmp_path):
    assert main(["--config", str(_cfg_file(tmp_path)), "status"]) == 3


def test_direct_press_and_relay_test_on_mock(tmp_path, capsys):
    cfg = str(_cfg_file(tmp_path))
    assert main(["--config", cfg, "test", "relay", "--direct", "--pulse", "0.05"]) == 0
    assert "COMPLETED" in capsys.readouterr().out
    assert main(["--config", cfg, "press", "--mode", "force", "--direct", "--dry-run"]) == 0


def test_set_password_and_tokens(tmp_path, monkeypatch, capsys):
    import io

    cfg = str(_cfg_file(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["--config", cfg, "set-password", "--stdin"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("a-good-password\n"))
    assert main(["--config", cfg, "set-password", "--stdin"]) == 0
    assert main(["--config", cfg, "token", "create", "ha"]) == 0
    assert "dp_" in capsys.readouterr().out
    assert main(["--config", cfg, "token", "list"]) == 0
    assert "ha" in capsys.readouterr().out


class FakeNet:
    def __init__(self) -> None:
        self.up = True
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(cmd)
        if cmd[:2] == ["nmcli", "-t"]:
            return 0, "dyson-hotspot\n" if self.up else ""
        if cmd[0] == "ip" and "route" in cmd:
            return 0, "default via 172.20.10.1 dev wlan0"
        if cmd[0] == "ping":
            return (0 if self.up else 1), ""
        if cmd[0] == "ip":
            return 0, "3: wlan0    inet 172.20.10.2/28 brd ..."
        return 0, "ok"


def test_netwatch_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    net, t = FakeNet(), {"now": 0.0}
    nw = NetWatch(AppConfig(), tmp_path / "nw.json", None, run=net, now=lambda: t["now"])
    assert nw.check() == "up"
    net.up = False
    assert nw.check() == "down"
    t["now"] = 60
    assert nw.check() == "waiting"
    t["now"] = 181  # > 3 min
    assert nw.check() == "action"
    assert ["nmcli", "connection", "up", "id", "dyson-hotspot"] in net.calls
    t["now"] = 240
    assert nw.check() == "waiting"
    t["now"] = 365
    assert nw.check() == "action"
    assert ["nmcli", "radio", "wifi", "off"] in net.calls
    net.up = True
    assert nw.check() == "up"
    assert json.loads((tmp_path / "nw.json").read_text()) == {}
