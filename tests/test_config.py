import textwrap
from pathlib import Path

import pytest

from karuta.config import KarutaConfig, load_config


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content))
    return path


def test_load_config_ok(tmp_path, monkeypatch):
    cfg_file = _write(
        tmp_path / "config.yaml",
        """
        guild_id: "111"
        channel_id: "222"
        pick_button_index: {a: 0, b: 1, c: 2}
        accounts:
          a: {browser: chrome, profile_dir: ./profiles/a}
          b: {browser: brave, profile_dir: ./profiles/b, executable_path: /usr/bin/brave-browser}
          c: {browser: opera_gx, profile_dir: ./profiles/c, executable_path: /opt/opera}
        """,
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config(cfg_file)
    assert isinstance(cfg, KarutaConfig)
    assert cfg.rotation_order == ["a", "b", "c"]
    assert cfg.accounts["b"].browser == "brave"
    assert cfg.pick_cooldown_seconds == 660  # default


def test_pick_button_index_must_cover_0_1_2(tmp_path):
    cfg_file = _write(
        tmp_path / "config.yaml",
        """
        guild_id: "1"
        channel_id: "2"
        pick_button_index: {a: 0, b: 0, c: 2}
        accounts:
          a: {browser: chrome, profile_dir: ./a}
          b: {browser: brave, profile_dir: ./b}
          c: {browser: opera_gx, profile_dir: ./c}
        """,
    )
    with pytest.raises(Exception):
        load_config(cfg_file)


def test_load_config_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")
