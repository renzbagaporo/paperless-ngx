import pytest

from paperless import version


def test_release_display_includes_personal_revision():
    assert (
        version.get_display_version(f"paperless-{version.__full_version_str__}-7")
        == f"{version.__full_version_str__}-7 (personal)"
    )


def test_unreleased_build_is_identified_as_development(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "PERSONAL_RELEASE_FILE", tmp_path / "missing")
    assert (
        version.get_display_version()
        == f"{version.__full_version_str__} (personal development)"
    )


def test_packaged_release_is_used_for_display(tmp_path, monkeypatch):
    metadata = tmp_path / "personal-release.txt"
    metadata.write_text(
        f"paperless-{version.__full_version_str__}-12\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(version, "PERSONAL_RELEASE_FILE", metadata)
    assert (
        version.get_display_version() == f"{version.__full_version_str__}-12 (personal)"
    )


@pytest.mark.parametrize(
    "release",
    [
        "paperless-0.0.0-1",
        f"paperless-{version.__full_version_str__}-0",
        f"paperless-{version.__full_version_str__}-01",
        f"paperless-{version.__full_version_str__}-1-extra",
        version.__full_version_str__,
        "",
    ],
)
def test_invalid_or_wrong_upstream_release_cannot_label_a_build(release):
    with pytest.raises(ValueError, match="Personal release must match"):
        version.get_display_version(release)
