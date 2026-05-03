from app.services.artifact_urls import sign_artifact_url, split_artifact_path


def test_split_artifact_path_accepts_known_prefixes() -> None:
    assert split_artifact_path("/pics/a/b.png") == ("pic", "a/b.png")
    assert split_artifact_path("https://example.test/results/r/report.html?x=1") == (
        "results",
        "r/report.html",
    )
    assert split_artifact_path("/api/not-an-artifact") is None


def test_sign_artifact_url_adds_scoped_token() -> None:
    signed = sign_artifact_url("/results/traces/demo.zip")
    assert signed is not None
    assert signed.startswith("/results/traces/demo.zip?")
    assert "artifact_token=" in signed


def test_sign_artifact_url_leaves_non_artifacts_unchanged() -> None:
    assert sign_artifact_url("https://example.test/file.png") == "https://example.test/file.png"
