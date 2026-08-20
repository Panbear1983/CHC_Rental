"""The repository exposes one shell entry point, independent of the caller's cwd."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_dashboard_shell_is_the_only_public_entrypoint(tmp_path):
    result = subprocess.run(
        [str(REPO / "dashboard.sh"), "help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "single public entry point" in result.stdout
    assert "./dashboard.sh scrape" in result.stdout
    assert "./dashboard.sh deliver" in result.stdout
    assert "./dashboard.sh run" in result.stdout
    assert not (REPO / "chc.sh").exists()


def test_package_does_not_generate_parallel_console_scripts():
    with (REPO / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert "scripts" not in project


def test_launchagent_templates_use_the_shell_entrypoint():
    for filename in ("com.chcrental.daily.plist", "com.chcrental.alerts.plist"):
        text = (REPO / "scripts" / filename).read_text(encoding="utf-8")
        assert "./dashboard.sh" in text
        assert ".venv/bin/chc-rental" not in text


def test_shell_commands_resolve_the_repository_env_file():
    text = (REPO / "dashboard.sh").read_text(encoding="utf-8")
    assert '--env-file "$REPO/.env"' in text
