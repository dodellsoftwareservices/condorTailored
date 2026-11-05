"""Configuration storage must not follow cwd or escape the shared fixture."""

from config_manager import ConfigManager, get_config_manager


def test_collection_time_access_cannot_rewrite_an_install(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    project = Path(__file__).resolve().parents[1]
    sentinels = {
        "config.yml": b"servers: {}\nusers: {}\nversion: 1\n",
        "config.yml.bak": b"backup sentinel\n",
        "audit_log.yml": b"audit sentinel\n",
    }
    for name, content in sentinels.items():
        (tmp_path / name).write_bytes(content)
    probe = tmp_path / "test_collection_probe.py"
    probe.write_text(
        "from config_manager import get_config_manager\n"
        "cm = get_config_manager()\n"
        "cm.set_user_preference(7, 'theme', 'test')\n"
        "cm._audit('test', 'user', '7', 7)\n"
        "def test_placeholder(): pass\n"
    )
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(project / "tests"), str(project)])
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "--collect-only",
            "-q",
            str(probe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name, content in sentinels.items():
        assert (tmp_path / name).read_bytes() == content


def test_default_manager_and_audit_are_inside_the_test_root(tmp_path):
    cm = get_config_manager()
    assert cm.config_path == tmp_path / "config.yml"
    assert cm.audit_log_path == tmp_path / "audit_log.yml"


def test_manager_keeps_its_paths_when_cwd_changes(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.chdir(original)
    cm = ConfigManager("config.yml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cm.set_user_preference(7, "theme", "dark")
    cm._audit("test", "user", "7", 7)
    assert cm.config_path == original / "config.yml"
    assert cm.audit_log_path == original / "audit_log.yml"
    assert not (elsewhere / "config.yml").exists()
    assert not (elsewhere / "audit_log.yml").exists()
