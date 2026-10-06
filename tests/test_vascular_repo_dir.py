import json

from arteries import setup_cli


def _commands(settings_path):
    hooks = json.loads(settings_path.read_text())["hooks"]
    return [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]


def test_sync_migrates_legacy_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    parent = tmp_path / "parent"
    repo = parent / "repo"
    legacy = repo / ".arteries"  # legacy layout under test
    (legacy / "hooks").mkdir(parents=True)
    (legacy / "config.json").write_text("{}\n")
    settings = repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    old_hooks = f"{repo}/.arteries/hooks"  # legacy hook path
    settings.write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"bash {old_hooks}/hook-observe.sh"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": f"bash {old_hooks}/activate.sh"}]}],
    }}))
    target = repo / ".vascular" / "arteries"

    setup_cli.main(["sync", str(parent), "--check"])
    assert legacy.is_dir() and not target.exists()

    setup_cli.main(["sync", str(parent)])
    assert target.is_dir() and not legacy.exists()
    commands = _commands(settings)
    assert commands
    assert all(".vascular/arteries/hooks/" in c for c in commands)
    assert not any("/.arteries/" in c for c in commands)  # no legacy path left

    before = settings.read_bytes()
    assert not setup_cli.migrate_repo(repo)
    setup_cli.main(["sync", str(parent)])
    assert settings.read_bytes() == before
    assert target.is_dir() and not legacy.exists()
