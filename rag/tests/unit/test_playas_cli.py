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
  "version") echo "Terraform v${FAKE_TF_VERSION:-1.17.1}"; echo "on linux_amd64" ;;
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
  *get-bucket-versioning*) echo Enabled ;;
  *get-bucket-encryption*) echo AES256 ;;
  *get-public-access-block*) printf 'True\tTrue\tTrue\tTrue\n' ;;
  *get-bucket-ownership-controls*) echo BucketOwnerEnforced ;;
  *get-bucket-policy*)
    echo '{"Statement":[{"Effect":"Deny","Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}' ;;
  *"ec2 describe-instances"*) echo "127.0.0.1" ;;
  *"elbv2 describe-load-balancers"*) printf 'arn:aws:lb/x\tdns.example\n' ;;
  *"elbv2 describe-target-groups"*) echo "arn:aws:elasticloadbalancing:x:targetgroup/backend/1" ;;
  *"elbv2 describe-target-health"*) printf '%b\n' "${FAKE_TG_HEALTH-healthy\thealthy}" ;;
  *"ec2 describe-addresses"*)
    [[ -n "${FAKE_EIPS_FAIL:-}" ]] && { echo "An error occurred (AccessDenied)" >&2; exit 255; }
    # Si la consulta pidiera la IP pública, cada fila la incluiría: la prueba
    # comprueba que nunca llega a la salida.
    if [[ "$*" == *PublicIp* ]]; then
      printf '%b' "${FAKE_EIPS-}" | sed 's/$/\t203.0.113.77/'
    else
      printf '%b' "${FAKE_EIPS-}"
    fi ;;
esac
exit 0
"""

# gpg falso: "cifra" invirtiendo el texto para poder comprobar que el
# contenido en claro nunca aparece en disco.
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "${CALL_LOG}"
exit 0
"""

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
    cwd: Path | None = None,
    stdin_text: str = "\x04",
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    for name, body in (
        ("terraform", FAKE_TERRAFORM),
        ("aws", FAKE_AWS),
        ("gpg", FAKE_GPG),
        ("curl", FAKE_CURL),
    ):
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
            cwd=cwd,
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


@pytest.mark.parametrize("kind", ["directo", "padre-inexistente", "symlink", "relativo"])
def test_backup_state_refuses_destination_inside_clone(tmp_path: Path, kind: str) -> None:
    root = _tree(tmp_path)
    cwd = None
    if kind == "directo":
        dest = str(root / "backups")
    elif kind == "padre-inexistente":
        dest = str(root / "nuevo" / "backups")
    elif kind == "symlink":
        link = tmp_path / "parece-fuera"
        link.symlink_to(root / "rag", target_is_directory=True)
        (root / "rag").mkdir(exist_ok=True)
        dest = str(link / "backups")
    else:
        (root / "rag").mkdir(exist_ok=True)
        cwd, dest = root / "rag", "backups/sub"
    result, calls = _run(
        root,
        tmp_path,
        "backup-state",
        "vector",
        cwd=cwd,
        env_extra={
            "FAKE_STATE_PULL": REMOTE_STATE,
            "PLAYAS_BACKUP_GPG_RECIPIENT": "ops@example.invalid",
            "PLAYAS_STATE_BACKUP_DIR": dest,
        },
    )
    assert result.returncode != 0
    assert "dentro del clon" in result.stderr
    assert not any("state pull" in c for c in calls)
    assert not list(root.rglob("*.gpg"))


def test_deploy_backup_dir_inside_clone_stops_before_apply(tmp_path: Path) -> None:
    root = _ingesta_tree(tmp_path)
    result, calls = _run(
        root,
        tmp_path,
        "deploy",
        "ingesta",
        tty=True,
        env_extra={"FAKE_STATE_PULL": REMOTE_STATE, "TFSTATE_BACKUP_DIR": str(root / "bk")},
    )
    assert result.returncode != 0
    assert "dentro del clon" in result.stderr
    assert not any("state pull" in c or " apply" in c for c in calls)
    assert not list(root.rglob("remote.tfstate*"))


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


def test_doctor_healthy_setup_passes(tmp_path: Path) -> None:
    result, calls = _run(_tree(tmp_path), tmp_path, "doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "doctor: sin errores" in result.stdout
    assert not any(re.search(r"aws s3api (create|put|delete)", c) for c in calls)


@pytest.mark.parametrize(
    ("version", "accepted"),
    [("1.17.1", True), ("1.17.4", True), ("1.18.0", False), ("1.17.0", False)],
)
def test_doctor_terraform_version_follows_required_version(
    tmp_path: Path, version: str, accepted: bool
) -> None:
    result, _ = _run(_tree(tmp_path), tmp_path, "doctor", env_extra={"FAKE_TF_VERSION": version})
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_doctor_fails_without_backend_config(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "backend.hcl").unlink()
    result, calls = _run(root, tmp_path, "doctor")
    assert result.returncode != 0
    assert "Falta backend.hcl" in result.stderr
    # doctor es de solo lectura.
    assert not any(re.search(r"aws s3api (create|put|delete)", c) for c in calls)
    assert not any(c.startswith("terraform") and c != "terraform version" for c in calls)


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


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_passes_when_everything_responds(tmp_path: Path) -> None:
    result, calls = _run(_tree(tmp_path), tmp_path, "health")
    assert result.returncode == 0, result.stdout + result.stderr
    urls = [c for c in calls if c.startswith("curl")]
    for path in (":8000/api/v2/heartbeat", ":11434/api/tags", "/api/health", "/api/ready"):
        assert any(path in u for u in urls), path


@pytest.mark.parametrize("states", ["healthy\\tunhealthy", "draining", ""])
def test_health_fails_on_unhealthy_targets(tmp_path: Path, states: str) -> None:
    result, _ = _run(_tree(tmp_path), tmp_path, "health", env_extra={"FAKE_TG_HEALTH": states})
    assert result.returncode != 0
    assert "comprobaciones fallidas" in result.stderr


# ---------------------------------------------------------------------------
# start / stop: terminal y confirmación escrita (S2)
# ---------------------------------------------------------------------------


def _compute_tree(tmp_path: Path) -> Path:
    """Repo temporal con start.sh/stop.sh falsos que solo registran la llamada."""
    root = _tree(tmp_path)
    for action in ("start", "stop"):
        _write_exec(
            root / "rag" / "infrastructure" / "scripts" / f"{action}.sh",
            f'#!/usr/bin/env bash\necho "{action}.sh $*" >> "${{CALL_LOG}}"\nexit 0\n',
        )
    return root


def _compute_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith(("start.sh", "stop.sh"))]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_compute_requires_terminal(tmp_path: Path, action: str) -> None:
    result, calls = _run(_compute_tree(tmp_path), tmp_path, action)
    assert result.returncode != 0
    assert "terminal interactiva" in result.stderr
    assert _compute_calls(calls) == []


@pytest.mark.parametrize("action", ["start", "stop"])
def test_compute_runs_script_after_exact_confirmation(tmp_path: Path, action: str) -> None:
    result, calls = _run(
        _compute_tree(tmp_path),
        tmp_path,
        action,
        "--uno",
        "dos tres",
        tty=True,
        stdin_text=f"{action}\n",
    )
    assert result.returncode == 0, result.stderr
    assert _compute_calls(calls) == [f"{action}.sh --uno dos tres"]


@pytest.mark.parametrize(
    ("action", "answer"),
    [
        pytest.param("start", "stop\n", id="start-con-stop"),
        pytest.param("stop", "start\n", id="stop-con-start"),
        pytest.param("start", "START\n", id="mayusculas"),
        pytest.param("start", " start\n", id="espacio-inicial"),
        pytest.param("start", "start \n", id="espacio-final"),
        pytest.param("start", "\n", id="vacio"),
        pytest.param("start", "\x04", id="eof"),
        pytest.param("stop", "\x04", id="eof-stop"),
    ],
)
def test_compute_cancels_without_exact_confirmation(
    tmp_path: Path, action: str, answer: str
) -> None:
    result, calls = _run(_compute_tree(tmp_path), tmp_path, action, tty=True, stdin_text=answer)
    assert result.returncode != 0
    assert "Cancelado." in result.stderr
    assert _compute_calls(calls) == []


# ---------------------------------------------------------------------------
# doctor: contratos de rag/infrastructure/terraform.tfvars (A2, A3, S9)
# ---------------------------------------------------------------------------

RAG_TFVARS_OK = (
    'chroma_host     = "172.31.10.20"\n'
    'ollama_base_url = "http://172.31.10.21:11434"\n'
    f's3_bucket_name  = "rag-playas-data-{ACCOUNT}"\n'
)
SG_OK = 'vector_clients_security_group_id = "sg-0123456789abcdef0"\n'
SG_EMPTY_MSG = "vacío con legacy_public_service_access = false"
SG_LEGACY_MSG = "hoy funciona porque"
SG_UNKNOWN_MSG = "no se puede saber si es obligatorio"


def _write_tfvars(root: Path, stack_rel: str, content: str | bytes) -> Path:
    path = root / stack_rel / "terraform.tfvars"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    path.chmod(0o600)
    return path


def _doctor(
    tmp_path: Path, *, rag: str | bytes | None, vector: str | bytes | None
) -> subprocess.CompletedProcess[str]:
    root = _tree(tmp_path)
    if rag is not None:
        _write_tfvars(root, "rag/infrastructure", rag)
    if vector is not None:
        _write_tfvars(root, "vector-infraestructura", vector)
    result, calls = _run(root, tmp_path, "doctor")
    # doctor es de solo lectura.
    assert not any(re.search(r"aws s3api (create|put|delete)", c) for c in calls)
    return result


@pytest.mark.parametrize(
    "sg_line",
    [
        pytest.param("", id="sg-ausente"),
        pytest.param('vector_clients_security_group_id = ""\n', id="sg-vacio"),
    ],
)
@pytest.mark.parametrize(
    "vector",
    [
        pytest.param(b"legacy_public_service_access = false\n", id="espacios"),
        pytest.param(b"legacy_public_service_access\t=\tfalse\n", id="tabs"),
        pytest.param(b"legacy_public_service_access = false\r\n", id="crlf"),
        pytest.param(b"legacy_public_service_access = false # fase 3\n", id="comentario-hash"),
        pytest.param(b"legacy_public_service_access = false // fase 3\n", id="comentario-slash"),
        pytest.param(b"legacy_public_service_access=false#x\n", id="sin-espacios"),
        pytest.param(
            b"# legacy_public_service_access = true\nlegacy_public_service_access = false\n",
            id="comentada-antes",
        ),
    ],
)
def test_doctor_empty_vector_sg_fails_when_legacy_is_false(
    tmp_path: Path, sg_line: str, vector: bytes
) -> None:
    result = _doctor(tmp_path, rag=RAG_TFVARS_OK + sg_line, vector=vector)
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"vector_clients_security_group_id {SG_EMPTY_MSG}" in result.stderr


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param(b"legacy_public_service_access = true\n", id="espacios"),
        pytest.param(b"legacy_public_service_access\t=\ttrue\r\n", id="tabs-crlf"),
        pytest.param(b"legacy_public_service_access = true // fase 1\n", id="comentario"),
        pytest.param(
            b"# legacy_public_service_access = false\nlegacy_public_service_access = true\n",
            id="comentada-antes",
        ),
    ],
)
def test_doctor_empty_vector_sg_only_warns_when_legacy_is_true(
    tmp_path: Path, vector: bytes
) -> None:
    result = _doctor(tmp_path, rag=RAG_TFVARS_OK, vector=vector)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert SG_EMPTY_MSG not in out
    assert SG_LEGACY_MSG in result.stdout


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param(None, id="tfvars-ausente"),
        pytest.param("ssh_cidr_blocks = []\n", id="sin-variable"),
        pytest.param("legacy_public_service_access = maybe\n", id="valor-invalido"),
        pytest.param("# legacy_public_service_access = false\n", id="solo-comentada"),
    ],
)
def test_doctor_empty_vector_sg_warns_when_legacy_is_unknown(
    tmp_path: Path, vector: str | None
) -> None:
    result = _doctor(tmp_path, rag=RAG_TFVARS_OK, vector=vector)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert SG_EMPTY_MSG not in out
    assert SG_UNKNOWN_MSG in result.stdout


@pytest.mark.parametrize("legacy", ["false", "true"])
def test_doctor_accepts_vector_sg_when_defined(tmp_path: Path, legacy: str) -> None:
    result = _doctor(
        tmp_path,
        rag=RAG_TFVARS_OK + SG_OK,
        vector=f"legacy_public_service_access = {legacy}\n",
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "vector_clients_security_group_id definido" in result.stdout
    assert "doctor: sin errores" in result.stdout
    for msg in (SG_EMPTY_MSG, SG_LEGACY_MSG, SG_UNKNOWN_MSG):
        assert msg not in out


def test_doctor_warns_that_legacy_opens_services_to_internet(tmp_path: Path) -> None:
    result = _doctor(tmp_path, rag=None, vector="legacy_public_service_access = true\n")
    warning = [
        line for line in result.stdout.splitlines() if "legacy_public_service_access = true" in line
    ]
    assert warning, result.stdout
    assert "0.0.0.0/0" in warning[0]
    assert "005-red-privada-vector" in warning[0]


def test_doctor_does_not_warn_about_internet_when_legacy_is_false(tmp_path: Path) -> None:
    result = _doctor(tmp_path, rag=None, vector="legacy_public_service_access = false\n")
    assert "0.0.0.0/0" not in result.stdout + result.stderr


RAG_EXAMPLE = REPO_ROOT / "rag" / "infrastructure" / "terraform.tfvars.example"


@pytest.mark.parametrize("uncomment_bucket", [False, True], ids=["tal-cual", "con-bucket"])
def test_doctor_rejects_unmodified_rag_example(tmp_path: Path, uncomment_bucket: bool) -> None:
    example = RAG_EXAMPLE.read_text()
    if uncomment_bucket:
        example += 's3_bucket_name = "rag-playas-data-xxxxxxxxx"\n'
    result = _doctor(tmp_path, rag=example, vector="legacy_public_service_access = true\n")
    assert result.returncode != 0
    for key in ("chroma_host", "ollama_base_url"):
        assert f"{key} conserva el valor de ejemplo" in result.stderr, key
    assert ("s3_bucket_name conserva el valor de ejemplo" in result.stderr) is uncomment_bucket
    # Los secretos del ejemplo nunca se imprimen.
    assert "cambia-esta" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chroma_host", "<chromadb_public_ip>"),
        ("chroma_host", "ec2-xxx-xxx-xxx-xxx.compute-1.amazonaws.com"),
        ("ollama_base_url", "http://<ollama_public_ip>:11434"),
        ("ollama_base_url", "cambia-esto"),
        ("vector_clients_security_group_id", "cambia-esto"),
        ("vector_clients_security_group_id", "<vector_clients_security_group_id>"),
        ("s3_bucket_name", "rag-playas-data-xxxxxxxxx"),
        ("s3_bucket_name", "cambia-esto"),
    ],
)
def test_doctor_rejects_placeholder_contract_values(tmp_path: Path, key: str, value: str) -> None:
    lines = [
        line for line in (RAG_TFVARS_OK + SG_OK).splitlines() if not line.startswith(f"{key} ")
    ]
    result = _doctor(
        tmp_path,
        rag="\n".join([*lines, f'{key} = "{value}"']) + "\n",
        vector="legacy_public_service_access = false\n",
    )
    assert result.returncode != 0
    assert f"{key} conserva el valor de ejemplo" in result.stderr


def test_doctor_accepts_real_contract_values(tmp_path: Path) -> None:
    result = _doctor(
        tmp_path,
        rag=RAG_TFVARS_OK + SG_OK,
        vector="legacy_public_service_access = false\n",
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "conserva el valor de ejemplo" not in out
    for key in ("chroma_host", "ollama_base_url", "vector_clients_security_group_id"):
        assert f"{key} definido" in result.stdout, key
    assert "s3_bucket_name coincide con el bucket de ingesta" in result.stdout


def test_doctor_reports_missing_data_bucket_instead_of_aborting(tmp_path: Path) -> None:
    """Regresión de tfvars_value: una clave ausente (grep sin coincidencias)
    es un valor vacío, no un error de lectura ni una salida silenciosa."""
    rag = "".join(
        line + "\n" for line in RAG_TFVARS_OK.splitlines() if "s3_bucket_name" not in line
    )
    result = _doctor(tmp_path, rag=rag + SG_OK, vector="legacy_public_service_access = false\n")
    out = result.stdout + result.stderr
    assert "s3_bucket_name vacío" in result.stdout, out
    assert "no se puede leer" not in out
    assert result.returncode == 0, out
    assert "doctor: sin errores" in result.stdout


# ---------------------------------------------------------------------------
# status: Elastic IP de vector (A14)
# ---------------------------------------------------------------------------

EIP_CHROMA = "eipalloc-0aaa1111\trag-playas-chromadb-eip\teipassoc-0c1"
EIP_OLLAMA = "eipalloc-0bbb2222\trag-playas-ollama-eip\teipassoc-0c2"
EIP_OLLAMA_FREE = "eipalloc-0bbb2222\trag-playas-ollama-eip\tNone"
EIP_EXTRA = "eipalloc-0ccc3333\trag-playas-chromadb-eip\teipassoc-0c3"
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _status_eips(
    tmp_path: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    result, calls = _run(_tree(tmp_path), tmp_path, "status", env_extra=env)
    assert result.returncode == 0, result.stdout + result.stderr
    query = [c for c in calls if "ec2 describe-addresses" in c]
    assert len(query) == 1
    assert "rag-playas-chromadb-eip,rag-playas-ollama-eip" in query[0]
    # Sección de las EIP (la de EC2 sí muestra IP, por diseño).
    start = result.stdout.index("EIP asignadas")
    end = result.stdout.index("rag (ECS", start)
    section = result.stdout[start:end]
    assert "203.0.113.77" not in result.stdout + result.stderr
    assert not _IPV4.search(section), section
    return result, section


def test_status_two_associated_eips(tmp_path: Path) -> None:
    _, section = _status_eips(tmp_path, {"FAKE_EIPS": f"{EIP_CHROMA}\n{EIP_OLLAMA}\n"})
    assert "EIP asignadas: 2" in section
    assert "Se esperan 2" not in section
    assert "sin asociar" not in section


def test_status_reports_unassociated_eip(tmp_path: Path) -> None:
    _, section = _status_eips(tmp_path, {"FAKE_EIPS": f"{EIP_CHROMA}\n{EIP_OLLAMA_FREE}\n"})
    assert "EIP asignadas: 2" in section
    assert "EIP sin asociar" in section
    free = section[section.index("EIP sin asociar") :]
    assert "eipalloc-0bbb2222 (rag-playas-ollama-eip)" in free
    assert "eipalloc-0aaa1111" not in free


@pytest.mark.parametrize(
    ("rows", "count"),
    [
        pytest.param(EIP_CHROMA, 1, id="una"),
        pytest.param(f"{EIP_CHROMA}\n{EIP_OLLAMA}\n{EIP_EXTRA}", 3, id="tres"),
        pytest.param("", 0, id="ninguna"),
    ],
)
def test_status_warns_when_eip_count_is_not_two(tmp_path: Path, rows: str, count: int) -> None:
    _, section = _status_eips(tmp_path, {"FAKE_EIPS": rows})
    assert f"EIP asignadas: {count}" in section
    assert (
        f"Se esperan 2 EIP etiquetadas de vector (chromadb-eip y ollama-eip); hay {count}."
        in section
    )


def test_status_eip_query_failure_shows_unknown(tmp_path: Path) -> None:
    _, section = _status_eips(tmp_path, {"FAKE_EIPS_FAIL": "1"})
    assert "EIP asignadas: ?" in section
    assert "No se pudieron consultar las EIP" in section
    assert "Se esperan 2" not in section


# ---------------------------------------------------------------------------
# test unit: lista de pruebas de infraestructura (A1)
# ---------------------------------------------------------------------------

_EXPECTED_INFRA_TESTS = [
    f"tests/unit/test_{name}.py"
    for name in (
        "deploy_scripts_safety",
        "playas_cli",
        "bootstrap_tfstate_bucket",
        "script_secret_handling",
        "vector_network_security",
        "infra_data_protection",
    )
]


def test_cli_infra_tests_include_every_infra_test_file() -> None:
    match = re.search(r"^INFRA_TESTS=\((.*?)^\)", CLI.read_text(), flags=re.M | re.S)
    assert match, "INFRA_TESTS no encontrado en scripts/playas.sh"
    listed = _code_text(match.group(1)).split()
    existing = [t for t in _EXPECTED_INFRA_TESTS if (REPO_ROOT / "rag" / t).is_file()]
    assert existing, "no se encontró ninguna prueba de infraestructura"
    missing = [t for t in existing if t not in listed]
    assert missing == [], missing
    for t in listed:
        assert (REPO_ROOT / "rag" / t).is_file(), t


def _code_text(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())
