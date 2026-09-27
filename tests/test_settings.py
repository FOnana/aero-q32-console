"""Settings and environment-report tests."""
import os

import aero_q32 as A


def test_config_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AEROQ32_DATA_DIR", str(tmp_path))
    assert A.config_dir() == str(tmp_path)
    assert os.path.isdir(A.config_dir())


def test_settings_roundtrip(temp_data_dir):
    cfg = A.load_settings()
    assert "hotkeys" in cfg
    cfg["low_battery"] = 33
    assert A.save_settings(cfg) is True
    assert A.load_settings()["low_battery"] == 33


def test_load_settings_survives_corrupt_file(temp_data_dir):
    # A corrupt config must never stop the app from starting.
    with open(A.config_path(), "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    cfg = A.load_settings()
    assert cfg == A.DEFAULT_SETTINGS


def test_log_path_is_under_data_dir(temp_data_dir):
    assert A.log_path().startswith(A.config_dir())


def test_env_report_shape():
    rep = A.env_report()
    for key in ("app_version", "os", "python", "pyserial", "data_dir",
                "all_com_ports", "spp_remote", "spp_local", "verdict", "hints"):
        assert key in rep
    assert isinstance(rep["all_com_ports"], list)
    text = A.format_report(rep)
    assert "verdict" not in text  # it is rendered, not dumped raw
    assert rep["verdict"]