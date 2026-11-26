"""CLI de operación scripts/playas.sh y deploy.sh de ingesta.

Comprueba, sin AWS ni Terraform reales (ejecutables falsos que registran sus
argumentos), que la CLI:

- valida stacks y comandos y respeta el orden ingesta → vector → rag;
- delega en los deploy.sh existentes, exige terminal y se detiene al primer fallo;
- planifica sin aplicar ni guardar planes y rechaza TF_CLI_ARGS*;
- cifra los respaldos del state sin escribirlo nunca en claro y fuera del clon;
- en ``doctor`` detecta backend.hcl ausente y contratos rotos entre stacks.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI = REPO_ROOT / "scripts" / "playas.sh"
STATE_LIB = REPO_ROOT / "scripts" / "lib" / "terraform_state.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_tfstate_bucket.sh"
INGESTA_DEPLOY = REPO_ROOT / "ingesta" / "infrastructure" / "scripts" / "deploy.sh"
ACCOUNT = "123456789012"
STATE_SECRET = "s3cr3t-postgres-password"
REMOTE_STATE = (
    f'{{"version": 4, "serial": 3, "lineage": "abc", "outputs": {{"x": "{STATE_SECRET}"}}}}'
)

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")

STACKS = {
    "ingesta": ("ingesta/infrastructure", "ingesta"),
    "vector": ("vector-infraestructura", "vector-infraestructura"),
    "rag": ("rag/infrastructure", "rag-infrastructure"),
}

FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "terraform $*" >> "${CALL_LOG}"
case "$*" in
  *" workspace show"*) echo default ;;
  *" state pull"*) printf '%s' "${FAKE_STATE_PULL-}" ;;
  *" output -raw s3_bucket_name"*) echo "rag-playas-data-123456789012" ;;
  *" plan"*) echo "No changes." ;;
  *" apply"*) exit 1 ;;
esac
exit 0
"""

FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "${CALL_LOG}"
case "$*" in
  *"sts get-caller-identity --query [Account,Arn]"*)
    printf '%s\\t%s\\n' "${FAKE_ACCOUNT:-123456789012}" "arn:aws:sts::1:assumed-role/voclabs/user" ;;
  *"sts get-caller-identity"*) echo "${FAKE_ACCOUNT:-123456789012}" ;;
  *"configure get region"*) echo "us-east-1" ;;
esac
exit 0
"""

# gpg falso: "cifra" invirtiendo el texto para poder comprobar que el
# contenido en claro nunca aparece en disco.
FAKE_GPG = """#!/usr/bin/env bash
echo "gpg $*" >> "${CALL_LOG}"
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in --output) out="$2"; shift ;; esac
  shift
done
[[ -n "${out}" ]] && rev > "${out}"
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _tree(tmp_path: Path, *, fake_deploys: bool = True) -> Path:
    """Copia la CLI y la biblioteca a un repo temporal con stacks mínimos."""
    root = tmp_path / "repo"
    for src in (CLI, STATE_LIB, BOOTSTRAP):
        dest = root / src.relative_to(REPO_ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    for stack, (rel, name) in STACKS.items():
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "terraform.tf").write_text(
            f'terraform {{\n  backend "s3" {{\n    key = "{name}/terraform.tfstate"\n  }}\n}}\n'
        )
        if fake_deploys:
            _write_exec(
                d / "scripts" / "deploy.sh",
                f'#!/usr/bin/env bash\necho "deploy {stack} $*" >> "${{CALL_LOG}}"\n'
                f'[[ "${{FAIL_DEPLOY:-}}" == "{stack}" ]] && exit 3\nexit 0\n',
            )
    (root / "backend.hcl").write_text(f'bucket = "rag-playas-tfstate-{ACCOUNT}"\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _run(
    root: Path,
    tmp_path: Path,
    *args: str,
    tty: bool = False,
    stdin_text: str = "\x04",
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    for name, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS), ("gpg", FAKE_GPG)):
        _write_exec(bin_dir / name, body)
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "CALL_LOG": str(call_log),
        "TFSTATE_BACKUP_DIR": str(tmp_path / "tfstate-backups"),
        **(env_extra or {}),
    }
    master = slave = None
    if tty:
        master, slave = pty.openpty()
        os.write(master, stdin_text.encode())
    try:
        result = subprocess.run(
            ["bash", str(root / "scripts" / "playas.sh"), *args],
            stdin=slave if tty else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        if master is not None:
            os.close(slave)
            os.close(master)
    return result, call_log.read_text().splitlines()


# ---------------------------------------------------------------------------
# Comprobaciones estáticas
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script", [CLI, INGESTA_DEPLOY], ids=lambda p: p.name)
def test_never_uses_destructive_or_unattended_terraform(script: Path) -> None:
    # Solo las invocaciones de terraform, no los mensajes que mencionan las opciones.
    code = "\n".join(
        line
        for line in _code(script).splitlines()
        if re.search(r"(^|[\s(|;&])terraform\s", line)
        and not re.match(r"\s*(echo|log|ok|warn|err|die|tf_fail)\b", line)
    )
    assert "terraform -chdir" in code
    for forbidden in (
        "-auto-approve",
        "destroy",
        "-migrate-state",
        "-reconfigure",
        "-force-copy",
        "force-unlock",
        "state push",
        "state rm",
        "init -upgrade",
    ):
        assert forbidden not in code, forbidden


def test_cli_plan_never_saves_plan_files() -> None:
    assert not re.search(r"\bplan\b[^\n]*-out", _code(CLI))


def test_cli_is_shellchecked_by_ci() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/playas.sh test static" in workflow


# ---------------------------------------------------------------------------
# Comandos y stacks
# ---------------------------------------------------------------------------


def test_help_lists_commands(tmp_path: Path) -> None:
    result, _ = _run(_tree(tmp_path), tmp_path, "help")
    assert result.returncode == 0
    for cmd in (
        "doctor",
        "bootstrap",
        "plan",
        "deploy",
        "status",
        "health",
        "test",
        "backup-state",
    ):
        assert cmd in result.stdout


def test_unknown_command_fails(tmp_path: Path) -> None:
    result, _ = _run(_tree(tmp_path), tmp_path, "apply")
    assert result.returncode != 0
    assert "Comando desconocido" in result.stderr


@pytest.mark.parametrize("command", ["plan", "deploy", "backup-state"])
@pytest.mark.parametrize("stack", ["foo", ""])
def test_invalid_stack_stops_before_terraform(tmp_path: Path, command: str, stack: str) -> None:
    args = [command, stack] if stack else [command]
    result, calls = _run(_tree(tmp_path), tmp_path, *args, tty=True)
    assert result.returncode != 0
    assert not any(c.startswith(("terraform", "deploy")) for c in calls)


def test_deploy_requires_terminal(tmp_path: Path) -> None:
    result, calls = _run(_tree(tmp_path), tmp_path, "deploy", "vector")
    assert result.returncode != 0
    assert "terminal interactiva" in result.stderr
    assert not any(c.startswith("deploy") for c in calls)


def test_deploy_all_follows_dependency_order(tmp_path: Path) -> None:
    result, calls = _run(_tree(tmp_path), tmp_path, "deploy", "all", tty=True)
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith("deploy")] == [
        "deploy ingesta ",
        "deploy vector ",
        "deploy rag ",
    ]


def test_deploy_all_stops_at_first_failure(tmp_path: Path) -> None:
    result, calls = _run(
        _tree(tmp_path), tmp_path, "deploy", "all", tty=True, env_extra={"FAIL_DEPLOY": "vector"}
    )
    assert result.returncode != 0
    deploys = [c for c in calls if c.startswith("deploy")]
    assert deploys == ["deploy ingesta ", "deploy vector "]


@pytest.mark.parametrize("var", ["TF_CLI_ARGS", "TF_CLI_ARGS_init", "TF_CLI_ARGS_plan", "TF_INPUT"])
@pytest.mark.parametrize("command", ["plan", "deploy"])
def test_tf_cli_overrides_rejected(tmp_path: Path, var: str, command: str) -> None:
    result, calls = _run(
        _tree(tmp_path), tmp_path, command, "rag", tty=True, env_extra={var: "-auto-approve"}
    )
    assert result.returncode != 0
    assert var in result.stderr
    assert not any(c.startswith(("terraform", "deploy")) for c in calls)


def test_plan_all_inits_with_backend_and_never_applies(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    result, calls = _run(root, tmp_path, "plan", "all")
    assert result.returncode == 0, result.stderr
    inits = [c for c in calls if " init" in c]
    plans = [c for c in calls if " plan" in c]
    assert len(inits) == 3 and len(plans) == 3
    assert all(f"-backend-config={root}/backend.hcl" in c and "-input=false" in c for c in inits)
    assert [p.split("-chdir=")[1].split()[0] for p in plans] == [
        str(root / rel) for rel, _ in STACKS.values()
    ]
    assert all("-input=false" in p and "-out" not in p for p in plans)
    assert not any(" apply" in c for c in calls)


def test_plan_stops_without_backend_config(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "backend.hcl").unlink()
    result, calls = _run(root, tmp_path, "plan", "vector")
    assert result.returncode != 0
    assert "backend.hcl" in result.stderr
    assert not any(" plan" in c for c in calls)


# ---------------------------------------------------------------------------
# backup-state: cifrado y fuera del clon
# ---------------------------------------------------------------------------


def test_backup_state_encrypts_without_plaintext_on_disk(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    dest = tmp_path / "offsite"
    result, calls = _run(
        root,
        tmp_path,
        "backup-state",
        "vector",
        env_extra={
            "FAKE_STATE_PULL": REMOTE_STATE,
            "PLAYAS_BACKUP_GPG_RECIPIENT": "ops@example.invalid",
            "PLAYAS_STATE_BACKUP_DIR": str(dest),
        },
    )
    assert result.returncode == 0, result.stderr
    encrypted = list(dest.glob("vector-infraestructura-*.tfstate.gpg"))
    assert len(encrypted) == 1
    assert (encrypted[0].stat().st_mode & 0o777) == 0o600
    assert (dest.stat().st_mode & 0o777) == 0o700
    assert encrypted[0].with_name(encrypted[0].name + ".sha256").exists()
    assert not list(dest.glob("*.partial"))
    # El secreto del state no aparece en claro en ningún archivo ni en la salida.
    for path in [*tmp_path.rglob("*")]:
        if path.is_file() and path.name != "calls.log":
            assert STATE_SECRET not in path.read_text(errors="ignore"), path
    assert STATE_SECRET not in result.stdout + result.stderr
    gpg_calls = [c for c in calls if c.startswith("gpg") and "--encrypt" in c]
    assert len(gpg_calls) == 1 and "--recipient ops@example.invalid" in gpg_calls[0]


def test_backup_state_refuses_destination_inside_clone(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    result, calls = _run(
        root,
        tmp_path,
        "backup-state",
        "vector",
        env_extra={
            "FAKE_STATE_PULL": REMOTE_STATE,
            "PLAYAS_BACKUP_GPG_RECIPIENT": "ops@example.invalid",
            "PLAYAS_STATE_BACKUP_DIR": str(root / "backups"),
        },
    )
    assert result.returncode != 0
    assert "dentro del clon" in result.stderr
    assert not any("state pull" in c for c in calls)


def test_backup_state_skips_empty_state(tmp_path: Path) -> None:
    dest = tmp_path / "offsite"
    result, calls = _run(
        _tree(tmp_path),
        tmp_path,
        "backup-state",
        "rag",
        env_extra={
            "FAKE_STATE_PULL": '{"serial": 0, "lineage": ""}',
            "PLAYAS_BACKUP_GPG_RECIPIENT": "ops@example.invalid",
            "PLAYAS_STATE_BACKUP_DIR": str(dest),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "vacío" in result.stdout + result.stderr
    assert not list(dest.glob("*.gpg"))
    assert not any(c.startswith("gpg") and "--encrypt" in c for c in calls)


def test_backup_state_without_recipient_requires_terminal(tmp_path: Path) -> None:
    result, calls = _run(
        _tree(tmp_path),
        tmp_path,
        "backup-state",
        "rag",
        env_extra={"PLAYAS_STATE_BACKUP_DIR": str(tmp_path / "offsite")},
    )
    assert result.returncode != 0
    assert "terminal interactiva" in result.stderr
    assert not any("state pull" in c for c in calls)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_fails_without_backend_config(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "backend.hcl").unlink()
    result, calls = _run(root, tmp_path, "doctor")
    assert result.returncode != 0
    assert "Falta backend.hcl" in result.stderr
    # doctor es de solo lectura.
    assert not any(re.search(r"aws s3api (create|put|delete)", c) for c in calls)
    assert not any(c.startswith("terraform") and " version" not in c for c in calls)


def test_doctor_detects_wrong_data_bucket_contract(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    tfvars = root / "rag" / "infrastructure" / "terraform.tfvars"
    tfvars.write_text('s3_bucket_name = "rag-playas-data-999999999999"\n')
    tfvars.chmod(0o600)
    result, _ = _run(root, tmp_path, "doctor")
    assert result.returncode != 0
    assert "no es el bucket de ingesta" in result.stderr


def test_doctor_flags_endpoint_override(tmp_path: Path) -> None:
    result, _ = _run(
        _tree(tmp_path), tmp_path, "doctor", env_extra={"AWS_ENDPOINT_URL_S3": "http://x"}
    )
    assert result.returncode != 0
    assert "AWS_ENDPOINT_URL_S3" in result.stderr
    assert "http://x" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# ingesta/infrastructure/scripts/deploy.sh
# ---------------------------------------------------------------------------


def _ingesta_tree(tmp_path: Path) -> Path:
    root = _tree(tmp_path, fake_deploys=False)
    dest = root / INGESTA_DEPLOY.relative_to(REPO_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(INGESTA_DEPLOY, dest)
    return root


def test_ingesta_deploy_rejects_arguments(tmp_path: Path) -> None:
    root = _ingesta_tree(tmp_path)
    result = subprocess.run(
        ["bash", str(root / INGESTA_DEPLOY.relative_to(REPO_ROOT)), "--auto-approve"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--auto-approve no está permitido" in result.stderr


def test_ingesta_deploy_backs_up_and_applies_interactively(tmp_path: Path) -> None:
    root = _ingesta_tree(tmp_path)
    result, calls = _run(
        root, tmp_path, "deploy", "ingesta", tty=True, env_extra={"FAKE_STATE_PULL": REMOTE_STATE}
    )
    tf = [c for c in calls if c.startswith("terraform")]
    order = [
        next(i for i, c in enumerate(tf) if k in c) for k in (" init", " state pull", " apply")
    ]
    assert order == sorted(order)
    applies = [c for c in tf if " apply" in c]
    assert len(applies) == 1 and "-input=true" in applies[0] and "auto-approve" not in applies[0]
    backups = list((tmp_path / "tfstate-backups" / "ingesta").rglob("remote.tfstate"))
    assert len(backups) == 1
    # El apply falso falla (la persona rechaza el plan): el deploy se detiene.
    assert result.returncode != 0


def test_ingesta_deploy_empty_state_needs_opt_in(tmp_path: Path) -> None:
    root = _ingesta_tree(tmp_path)
    result, calls = _run(root, tmp_path, "deploy", "ingesta", tty=True)
    assert result.returncode != 0
    assert "ALLOW_EMPTY_STATE=1" in result.stderr
    assert not any(" apply" in c for c in calls)
