"""Bootstrap del bucket del Terraform State (Fase 2).

Ejecuta ``scripts/bootstrap_tfstate_bucket.sh`` con un ``aws`` falso que
registra las llamadas y simula el bucket. Nunca contacta con AWS.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_tfstate_bucket.sh"
ACCOUNT = "123456789012"
BUCKET = f"rag-playas-tfstate-{ACCOUNT}"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")

# Cada llamada se registra como una línea; la política aplicada se guarda aparte.
FAKE_AWS = r"""#!/usr/bin/env bash
all="$*"
echo "aws ${all//$'\n'/ }" >> "${CALL_LOG}"  # una línea por llamada
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == "--policy" ]] && printf '%s' "${args[i+1]}" > "${POLICY_FILE}"
done
case "$*" in
  *"sts get-caller-identity"*) echo "${FAKE_ACCOUNT}" ;;
  *"head-bucket"*) [[ -f "${BUCKET_MARKER}" ]] || exit 254 ;;
  *"create-bucket"*) touch "${BUCKET_MARKER}" ;;
  *"get-bucket-versioning"*) echo "${FAKE_VERSIONING:-Enabled}" ;;
  *"get-bucket-encryption"*) echo "AES256" ;;
  *"get-public-access-block"*) printf 'True\tTrue\tTrue\tTrue\n' ;;
  *"get-bucket-ownership-controls"*) echo "BucketOwnerEnforced" ;;
  *"get-bucket-policy"*) [[ -f "${POLICY_FILE}" ]] && cat "${POLICY_FILE}" || exit 254 ;;
esac
exit 0
"""


def _run(
    tmp_path: Path,
    *args: str,
    answer: str | None = "si",
    tty: bool = True,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "aws").write_text(FAKE_AWS)
    (bin_dir / "aws").chmod(0o755)
    call_log = tmp_path / "calls.log"
    call_log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CALL_LOG": str(call_log),
        "POLICY_FILE": str(tmp_path / "policy.json"),
        "BUCKET_MARKER": str(tmp_path / "bucket.exists"),
        "FAKE_ACCOUNT": ACCOUNT,
        "RAG_EXPECTED_AWS_ACCOUNT_ID": ACCOUNT,
        **(env_extra or {}),
    }
    if tty:
        master, slave = pty.openpty()
        if answer is not None:
            os.write(master, f"{answer}\n".encode())
        try:
            result = subprocess.run(
                ["bash", str(BOOTSTRAP), *args],
                stdin=slave,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
        finally:
            os.close(slave)
            os.close(master)
    else:
        result = subprocess.run(
            ["bash", str(BOOTSTRAP), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    return result, call_log.read_text().splitlines()


def _writes(calls: list[str]) -> list[str]:
    return [c for c in calls if " create-bucket" in c or " put-" in c]


@pytest.mark.parametrize("expected", ["", "12345", "abcdefghijkl"])
def test_requires_a_valid_expected_account(expected: str, tmp_path: Path) -> None:
    result, calls = _run(tmp_path, env_extra={"RAG_EXPECTED_AWS_ACCOUNT_ID": expected})
    assert result.returncode != 0
    assert "RAG_EXPECTED_AWS_ACCOUNT_ID" in result.stderr
    assert calls == []


def test_stops_on_account_mismatch_without_writing(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, env_extra={"FAKE_ACCOUNT": "999999999999"})
    assert result.returncode != 0
    assert "999999999999" in result.stderr
    assert [c for c in calls if "sts" not in c] == []


def test_creates_and_configures_bucket_in_safe_order(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    writes = _writes(calls)
    ops = [next(op for op in w.split() if op.startswith(("create-", "put-"))) for w in writes]
    assert ops == [
        "create-bucket",
        "put-bucket-ownership-controls",
        "put-public-access-block",
        "put-bucket-versioning",
        "put-bucket-encryption",
        "put-bucket-policy",
    ]
    assert "--object-ownership BucketOwnerEnforced" in writes[0]
    assert "LocationConstraint" not in writes[0]  # us-east-1
    assert all(f"--bucket {BUCKET}" in w for w in writes)
    # Fuera de create-bucket, todas exigen que el dueño sea esta cuenta.
    assert all(f"--expected-bucket-owner {ACCOUNT}" in w for w in writes[1:])
    assert (
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
        in writes[2]
    )
    assert "Status=Enabled" in writes[3]
    assert '"SSEAlgorithm":"AES256"' in writes[4]
    assert f'bucket = "{BUCKET}"' in result.stdout


def test_tls_policy_denies_insecure_transport_on_bucket_and_objects(tmp_path: Path) -> None:
    result, _ = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    policy = json.loads((tmp_path / "policy.json").read_text())
    [statement] = policy["Statement"]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert statement["Action"] == "s3:*"
    assert sorted(statement["Resource"]) == sorted(
        [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"]
    )
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_never_runs_destructive_or_lifecycle_operations(tmp_path: Path) -> None:
    _run(tmp_path)
    text = BOOTSTRAP.read_text()
    for forbidden in (
        "delete-",
        "s3 rm",
        "rb ",
        "put-bucket-lifecycle",
        "put-bucket-acl",
        "put-object-acl",
    ):
        assert forbidden not in text, forbidden


def test_existing_bucket_is_completed_not_recreated(tmp_path: Path) -> None:
    (tmp_path / "bucket.exists").touch()
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(" create-bucket" in c for c in calls)
    assert any(" put-bucket-policy" in c for c in calls)


def test_requires_explicit_confirmation(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, answer="no")
    assert result.returncode != 0
    assert "Cancelado" in result.stderr
    assert _writes(calls) == []


def test_requires_interactive_terminal_to_write(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, tty=False)
    assert result.returncode != 0
    assert "terminal interactiva" in result.stderr
    assert _writes(calls) == []


def test_verify_only_never_writes_and_checks_configuration(tmp_path: Path) -> None:
    _run(tmp_path)  # deja un bucket configurado (falso)
    (tmp_path / "calls.log").write_text("")
    result, calls = _run(tmp_path, "--verify-only", tty=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _writes(calls) == []
    for op in (
        "get-bucket-versioning",
        "get-bucket-encryption",
        "get-public-access-block",
        "get-bucket-ownership-controls",
        "get-bucket-policy",
    ):
        assert any(op in c for c in calls), op


def test_verification_fails_on_unexpected_configuration(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, env_extra={"FAKE_VERSIONING": "Suspended"})
    assert result.returncode != 0
    assert "Versionado" in result.stderr
    assert "no es la esperada" in result.stderr


def test_verify_only_fails_without_bucket(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "--verify-only", tty=False)
    assert result.returncode != 0
    assert "no existe" in result.stderr
    assert _writes(calls) == []
