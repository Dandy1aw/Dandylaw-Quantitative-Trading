from scripts.check_release_state import release_violations


REQUIRED = {
    ".github/workflows/ci.yml",
    "scripts/check_release_state.py",
    "scripts/check_secrets.py",
}


def test_release_policy_accepts_required_files_without_credentials() -> None:
    assert release_violations(REQUIRED) == []


def test_release_policy_rejects_forbidden_tracked_credential_file() -> None:
    assert release_violations(REQUIRED | {"api.txt"}) == [
        "api.txt: forbidden credential file is tracked"
    ]


def test_release_policy_rejects_missing_ci_and_security_scripts() -> None:
    violations = release_violations({"README.md"})

    assert violations == [
        ".github/workflows/ci.yml: required release file is not tracked",
        "scripts/check_release_state.py: required release file is not tracked",
        "scripts/check_secrets.py: required release file is not tracked",
    ]
