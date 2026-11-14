"""
Management commands started as root in the container must run as the
paperless user, or the files they write are unreadable to the web server.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import find_commands

SRC_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = SRC_DIR.parent
WRAPPER_DIR = REPO_DIR / "docker" / "rootfs" / "usr" / "local" / "bin"
# Commands the image runs itself, as the paperless user, rather than people.
SERVICE_COMMANDS = {"document_consumer"}


def _project_commands() -> list[str]:
    commands = []
    for app in apps.get_app_configs():
        if not Path(app.path).resolve().is_relative_to(SRC_DIR):
            continue
        for name in find_commands(str(Path(app.path) / "management")):
            module = importlib.import_module(f"{app.name}.management.commands.{name}")
            if hasattr(module, "Command"):
                commands.append(name)
    return sorted(commands)


def test_commands_are_found() -> None:
    commands = _project_commands()
    assert "document_ocr" in commands
    assert "mail_fetcher" in commands
    assert "base" not in commands


@pytest.mark.parametrize(
    "command",
    [c for c in _project_commands() if c not in SERVICE_COMMANDS],
)
def test_every_command_has_a_wrapper(command: str) -> None:
    wrapper = WRAPPER_DIR / command
    assert wrapper.is_file(), (
        f"{command} has no wrapper in docker/rootfs/usr/local/bin; copy "
        "document_thumbnails and rename the command in it."
    )
    assert os.access(wrapper, os.X_OK), f"{wrapper} is not executable"
    text = wrapper.read_text()
    assert f's6-setuidgid paperless python3 manage.py {command} "$@"' in text
    assert f'\tpython3 manage.py {command} "$@"' in text


def test_custom_image_installs_wrappers_and_drops_root() -> None:
    dockerfile = (REPO_DIR / "Dockerfile.custom").read_text()
    assert "COPY docker/rootfs/usr/local/bin/ /usr/local/bin/" in dockerfile
    marker = dockerfile.index("ENV PAPERLESS_MANAGE_USER=paperless")
    # Build steps that run manage.py as root must come before the marker.
    assert dockerfile.rindex("manage.py") < marker


@pytest.fixture
def manage(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("manage", SRC_DIR / "manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(
        module.os,
        "execvp",
        lambda file, args: calls.append((file, args)),
    )
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module.sys,
        "orig_argv",
        ["python3", "manage.py", "document_ocr", "--apply"],
    )
    for name in (
        "PAPERLESS_MANAGE_USER",
        "PAPERLESS_MANAGE_USER_SWITCHED",
        "USER_IS_NON_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    module.calls = calls
    return module


def test_root_in_image_reruns_as_paperless(
    manage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPERLESS_MANAGE_USER", "paperless")

    manage.run_as_paperless_user()

    assert manage.calls == [
        (
            "s6-setuidgid",
            [
                "s6-setuidgid",
                "paperless",
                sys.executable,
                "manage.py",
                "document_ocr",
                "--apply",
            ],
        ),
    ]
    assert os.environ["PAPERLESS_MANAGE_USER_SWITCHED"] == "1"


@pytest.mark.parametrize(
    ("env", "euid"),
    [
        ({}, 0),  # outside the image
        ({"PAPERLESS_MANAGE_USER": "paperless"}, 1000),
        ({"PAPERLESS_MANAGE_USER": "paperless", "USER_IS_NON_ROOT": "true"}, 0),
        (
            {
                "PAPERLESS_MANAGE_USER": "paperless",
                "PAPERLESS_MANAGE_USER_SWITCHED": "1",
            },
            0,
        ),
    ],
)
def test_runs_unchanged_otherwise(
    manage,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    euid: int,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(manage.os, "geteuid", lambda: euid)

    manage.run_as_paperless_user()

    assert manage.calls == []
