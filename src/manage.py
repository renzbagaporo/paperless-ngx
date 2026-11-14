#!/usr/bin/env python3
import os
import sys


def run_as_paperless_user() -> None:
    """
    In the container image, a command started as root re-runs itself as the
    Paperless user, as the image's command wrappers do, so the files it writes
    stay readable by the web server and workers. The image names that user in
    PAPERLESS_MANAGE_USER; elsewhere nothing changes.
    """
    user = os.environ.get("PAPERLESS_MANAGE_USER")
    if (
        not user
        or os.environ.get("USER_IS_NON_ROOT")
        or os.environ.get("PAPERLESS_MANAGE_USER_SWITCHED")
        or os.geteuid() != 0
    ):
        return
    # Guards against a loop if the user is mapped to uid 0.
    os.environ["PAPERLESS_MANAGE_USER_SWITCHED"] = "1"
    os.execvp(
        "s6-setuidgid",
        ["s6-setuidgid", user, sys.executable, *sys.orig_argv[1:]],
    )


if __name__ == "__main__":
    run_as_paperless_user()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paperless.settings")

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
