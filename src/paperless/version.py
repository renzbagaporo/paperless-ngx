import re
from pathlib import Path
from typing import Final

__version__: Final[tuple[int, int, int]] = (3, 3, 4)
# Version string like X.Y.Z
__full_version_str__: Final[str] = ".".join(map(str, __version__))
# Version string like X.Y
__major_minor_version_str__: Final[str] = ".".join(map(str, __version__[:-1]))

PERSONAL_RELEASE_FILE = Path(__file__).with_name("personal-release.txt")


def get_display_version(release: str | None = None) -> str:
    """Identify this personal build without changing the upstream data version."""
    if release is None:
        try:
            release = PERSONAL_RELEASE_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            release = "development"
    if release == "development":
        return f"{__full_version_str__} (personal development)"
    match = re.fullmatch(
        rf"paperless-{re.escape(__full_version_str__)}-([1-9][0-9]*)",
        release,
    )
    if match is None:
        raise ValueError(
            "Personal release must match this upstream version and include a positive revision",
        )
    return f"{__full_version_str__}-{match[1]} (personal)"
