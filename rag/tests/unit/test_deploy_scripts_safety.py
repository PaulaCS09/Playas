"""Salvaguardas de los scripts de despliegue (Fase 1, tarea A1).

Comprueba que los scripts de despliegue:

- exigen confirmación humana para ``terraform apply``: sin ``-auto-approve``,
  solo desde una terminal interactiva y sin ``TF_CLI_ARGS*``/``TF_INPUT``;
- ejecutan ``terraform init`` sin ``-upgrade``;
- respaldan el state local fuera del clon antes del apply y lo dejan en 600;
- no modifican el state automáticamente (``state rm`` / ``import``);
- en el bastion, no borran ``~/playas``, exigen un CIDR SSH ``/32`` y no
  reutilizan un security group con SSH abierto a otros orígenes.

Nunca se ejecuta Terraform ni AWS reales: los scripts se copian a un
directorio temporal y ``terraform``/``aws`` se sustituyen por ejecutables
falsos que solo registran sus argumentos.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VECTOR_DEPLOY = REPO_ROOT / "vector-infraestructura" / "scripts" / "deploy.sh"
RAG_DEPLOY = REPO_ROOT / "rag" / "infrastructure" / "scripts" / "deploy.sh"
BASTION = REPO_ROOT / "scripts" / "deploy_bastion_ec2.sh"
STATE_LIB = REPO_ROOT / "scripts" / "lib" / "terraform_state.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_tfstate_bucket.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")

FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "terraform $*" >> "${CALL_LOG}"
case "$*" in
  *" init"*) [[ -n "${FAKE_INIT_FAIL:-}" ]] && exit 1; exit 0 ;;
  *" state pull"*)
    [[ -n "${FAKE_STATE_PULL_FAIL:-}" ]] && { echo "Error: AccessDenied" >&2; exit 1; }
    printf '%s' "${FAKE_STATE_PULL:-}" ;;
  *" state list"*)
    [[ -n "${FAKE_STATE_LIST_FAIL:-}" ]] && { echo "Error: ExpiredToken" >&2; exit 1; }
    printf '%b' "${FAKE_STATE_LIST:-}" ;;
  *" state show"*)
    [[ -n "${FAKE_STATE_SHOW_FAIL:-}" ]] && exit 1
    if [[ -n "${FAKE_STATE_SG_ID:-}" ]]; then echo "    id = \\"${FAKE_STATE_SG_ID}\\""; exit 0; fi
    exit 1 ;;
  *" apply"*) exit 1 ;;  # simula que la persona rechaza el plan
  *" output"*) echo "10.0.0.1" ;;
esac
"""

FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "${CALL_LOG}"
case "$*" in
  *"sts get-caller-identity"*)
    [[ -n "${FAKE_STS_FAIL:-}" ]] && { echo "ExpiredToken" >&2; exit 255; }
    echo "${FAKE_ACCOUNT:-000000000000}" ;;
  *"s3api head-bucket"*)
    [[ -n "${FAKE_HEAD_BUCKET_FAIL:-}" ]] && { echo "Not Found (404)" >&2; exit 254; }
    exit 0 ;;
  *"ec2 describe-instances"*)
    [[ -n "${FAKE_EC2_FAIL:-}" ]] && { echo "UnauthorizedOperation" >&2; exit 254; }
    echo "${FAKE_VECTOR_INSTANCES:-}" ;;
  *describe-vpcs*) echo "vpc-123" ;;
  *describe-security-groups*CidrIpv6*) echo "${FAKE_SG_V6:-}" ;;
  *describe-security-groups*CidrIp*) echo "${FAKE_SG_V4:-}" ;;
  *describe-security-groups*) echo "${FAKE_AWS_SG_ID:-None}" ;;
  *describe-images*) echo "None" ;;  # detiene el bastion tras el paso del SG
  *create-security-group*) echo "sg-nuevo" ;;
  *authorize-security-group-ingress*) echo "AWS-WRITE authorize" >&2 ;;
esac
"""

TF_ENV_VARS = [
    "TF_CLI_ARGS",
    "TF_CLI_ARGS_init",
    "TF_CLI_ARGS_plan",
    "TF_CLI_ARGS_apply",
    "TF_INPUT",
]


def _code_lines(path: Path) -> list[str]:
    """Líneas ejecutables: sin comentarios completos."""
    return [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]


def _logical_commands(text: str) -> list[str]:
    """Une las continuaciones con barra invertida en un solo comando."""
    return [cmd.strip() for cmd in re.sub(r"\\\n\s*", " ", text).splitlines() if cmd.strip()]


def _install_fakes(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS)):
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(0o755)


def _copy_script(src: Path, root: Path) -> Path:
    """Copia el script (y la biblioteca compartida que cargan los deploy.sh) a
    la misma ruta relativa dentro de ``root``."""
    for path in (src, STATE_LIB):
        dest = root / path.relative_to(REPO_ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    return root / src.relative_to(REPO_ROOT)


def _run(
    script: Path,
    tmp_path: Path,
    *args: str,
    tty: bool = True,
    env_extra: dict[str, str] | None = None,
    merge_stderr: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    _install_fakes(bin_dir)
    call_log = tmp_path / "calls.log"
    call_log.touch()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # Entorno mínimo construido desde cero: no hereda TF_* del proceso de pytest.
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "CALL_LOG": str(call_log),
        "TFSTATE_BACKUP_DIR": str(tmp_path / "backups"),
        # A5: el deploy de vector exige fijar el acceso público transitorio.
        "TF_VAR_legacy_public_service_access": "false",
        **(env_extra or {}),
    }
    stdin_master: int | None = None
    if tty:
        stdin_master, stdin = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(script), *args],
            stdin=stdin if tty else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Unir stderr permite comprobar el orden entre mensajes y escrituras en AWS.
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        if stdin_master is not None:
            os.close(stdin)
            os.close(stdin_master)
    return result, call_log.read_text().splitlines()


ALL_SGS = "\\n".join(f"aws_security_group.{name}" for name in ("alb", "app", "efs", "frontend"))

STACK_SCRIPTS = [
    pytest.param(VECTOR_DEPLOY, "vector-infraestructura", id="vector"),
    pytest.param(RAG_DEPLOY, "rag-infrastructure", id="rag"),
]


# ---------------------------------------------------------------------------
# Comprobaciones estáticas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [VECTOR_DEPLOY, RAG_DEPLOY, BASTION], ids=lambda p: p.name)
def test_scripts_never_pass_auto_approve_or_upgrade(script: Path) -> None:
    code = _code_lines(script)
    assert '"-auto-approve"' not in "\n".join(code)
    # Solo las líneas que invocan terraform o un deploy.sh, no los mensajes.
    invocations = [
        line.strip()
        for line in code
        if re.match(r"\s*terraform\s", line) or re.search(r"(^|[\s\"])\./\S*deploy\.sh", line)
    ]
    for line in invocations:
        assert "auto-approve" not in line, line
        assert "init -upgrade" not in line, line


def test_rag_deploy_never_mutates_state_automatically() -> None:
    for line in _code_lines(RAG_DEPLOY):
        assert not re.match(r"\s*terraform\b.*\b(state rm|import)\b", line), line


def test_bastion_does_not_delete_checkout() -> None:
    code = "\n".join(_code_lines(BASTION))
    assert not re.search(r"rm\s+-rf\s+(~|\"?\$\{?HOME\}?)/playas", code)


def test_bastion_authorizes_ssh_only_from_operator_cidr() -> None:
    commands = _logical_commands("\n".join(_code_lines(BASTION)))
    authorize = [cmd for cmd in commands if "authorize-security-group-ingress" in cmd]
    assert len(authorize) == 2  # SG nuevo y SG reutilizado sin la regla /32
    for cmd in authorize:
        assert '--cidr "${BASTION_SSH_CIDR}"' in cmd
        assert "0.0.0.0/0" not in cmd


def test_bastion_runs_terraform_only_through_interactive_ssh() -> None:
    """Ningún deploy/apply dentro de un heredoc: leería el script por stdin."""
    text = BASTION.read_text()
    heredocs = re.findall(r"<<'REMOTE_SCRIPT'\n(.*?)\nREMOTE_SCRIPT\n", text, flags=re.S)
    assert len(heredocs) == 2
    for block in heredocs:
        assert "deploy.sh" not in block
        assert "terraform apply" not in block
    deploy_calls = [
        cmd
        for cmd in _logical_commands("\n".join(_code_lines(BASTION)))
        if re.search(r"\./\S*scripts/deploy\.sh", cmd)
    ]
    assert len(deploy_calls) == 2
    for cmd in deploy_calls:
        assert cmd.startswith("ssh -t "), cmd


# ---------------------------------------------------------------------------
# Comportamiento de los deploy con terraform/aws falsos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_rejects_auto_approve(script: Path, stack: str, tmp_path: Path) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    result, calls = _run(copy, tmp_path, "--auto-approve")
    assert result.returncode != 0
    assert "auto-approve" in result.stderr
    assert calls == []


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_requires_interactive_terminal(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    result, calls = _run(copy, tmp_path, tty=False)
    assert result.returncode != 0
    assert "terminal interactiva" in result.stderr
    assert calls == []


@pytest.mark.parametrize("tf_var", TF_ENV_VARS)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_refuses_terraform_env_overrides(
    script: Path, stack: str, tf_var: str, tmp_path: Path
) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    value = "0" if tf_var == "TF_INPUT" else "-auto-approve"
    result, calls = _run(copy, tmp_path, env_extra={tf_var: value})
    assert result.returncode != 0
    assert tf_var in result.stderr
    assert calls == []


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_backs_up_state_before_interactive_apply(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    tf_dir = copy.parent.parent
    live_state = tf_dir / "terraform.tfstate"
    live_state.write_text('{"serial": 7}')
    live_state.chmod(0o644)
    (tf_dir / "terraform.tfstate.backup").write_text('{"serial": 6}')

    result, calls = _run(copy, tmp_path)

    # El apply falso devuelve 1 (la persona rechaza el plan): el script se detiene.
    assert result.returncode != 0
    tf_calls = [c for c in calls if c.startswith("terraform")]
    init_calls = [c for c in tf_calls if " init" in c]
    apply_calls = [c for c in tf_calls if " apply" in c]
    assert init_calls and all("-upgrade" not in c for c in init_calls)
    assert len(apply_calls) == 1
    assert "-auto-approve" not in apply_calls[0]
    assert "-input=true" in apply_calls[0]

    backups = list((tmp_path / "backups" / stack).glob("*/terraform.tfstate"))
    assert len(backups) == 1
    assert backups[0].read_text() == '{"serial": 7}'
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert (backups[0].parent / "terraform.tfstate.backup").exists()
    # El state original también queda legible solo por el dueño.
    assert stat.S_IMODE(live_state.stat().st_mode) == 0o600


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_without_local_state_still_asks_for_confirmation(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    result, calls = _run(copy, tmp_path)
    assert "No hay terraform.tfstate local" in result.stdout
    assert any(" apply" in c and "-auto-approve" not in c for c in calls)
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_stack_script_stops_when_state_is_locked(script: Path, stack: str, tmp_path: Path) -> None:
    copy = _copy_script(script, tmp_path / "repo")
    tf_dir = copy.parent.parent
    (tf_dir / "terraform.tfstate").write_text("{}")
    (tf_dir / ".terraform.tfstate.lock.info").write_text("{}")
    result, calls = _run(copy, tmp_path)
    assert result.returncode != 0
    assert "lock.info" in result.stderr
    assert not any(" apply" in c for c in calls)


@pytest.mark.parametrize(
    ("state_sg_id", "state_list"),
    [
        pytest.param("sg-en-state", ALL_SGS, id="id-distinto"),
        pytest.param("", "", id="ausente-en-state"),
        # Una dirección que solo comparte prefijo no cuenta como presente.
        pytest.param("", "aws_security_group.alb_x\\naws_security_group_app", id="prefijo"),
    ],
)
def test_rag_deploy_stops_on_sg_drift_without_touching_state(
    state_sg_id: str, state_list: str, tmp_path: Path
) -> None:
    copy = _copy_script(RAG_DEPLOY, tmp_path / "repo")
    (copy.parent.parent / "terraform.tfstate").write_text('{"serial": 3}')
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_AWS_SG_ID": "sg-en-aws",
            "FAKE_STATE_SG_ID": state_sg_id,
            "FAKE_STATE_LIST": state_list,
        },
    )
    assert result.returncode != 0
    assert "Despliegue detenido sin cambios" in result.stderr
    assert "import aws_security_group.alb sg-en-aws" in result.stdout
    tf_calls = [c for c in calls if c.startswith("terraform")]
    assert not any(" state rm" in c or " import" in c or " apply" in c for c in tf_calls)
    assert all("-no-color" in c for c in tf_calls if " state show" in c)
    # El respaldo existe antes de que una persona corrija el state a mano.
    assert list((tmp_path / "backups" / "rag-infrastructure").glob("*/terraform.tfstate"))


# ---------------------------------------------------------------------------
# Bastion: CIDR SSH /32 y security group reutilizado
# ---------------------------------------------------------------------------


def _bastion_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    copy = _copy_script(BASTION, tmp_path / "repo")
    rag_dir = tmp_path / "repo" / "rag"
    rag_dir.mkdir(parents=True, exist_ok=True)
    (rag_dir / ".env").write_text("OPENAI_API_KEY=clave-de-prueba\n")
    key_file = tmp_path / "vockey.pem"
    key_file.write_text("no-es-una-clave")
    return copy, {"KEY_FILE": str(key_file)}


@pytest.mark.parametrize(
    "cidr",
    [
        "",
        "0.0.0.0/0",
        "0.0.0.0/32",
        "000.000.000.000/32",
        "203.0.113.10/24",
        "203.0.113.10",
        "300.1.1.1/32",
        "a.b.c.d/32",
    ],
)
def test_bastion_rejects_missing_or_open_ssh_cidr(cidr: str, tmp_path: Path) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={**env, "BASTION_SSH_CIDR": cidr})
    assert result.returncode != 0
    assert "BASTION_SSH_CIDR" in result.stdout
    assert calls == []


def test_bastion_requires_interactive_terminal(tmp_path: Path) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy, tmp_path, tty=False, env_extra={**env, "BASTION_SSH_CIDR": "203.0.113.10/32"}
    )
    assert result.returncode != 0
    assert "terminal interactiva" in result.stdout
    assert calls == []


@pytest.mark.parametrize(
    ("v4", "v6"),
    [
        pytest.param("0.0.0.0/0", "", id="ipv4-abierto"),
        pytest.param("203.0.113.10/32\t0.0.0.0/0", "", id="ipv4-mixto"),
        pytest.param("", "::/0", id="ipv6-abierto"),
        pytest.param("10.0.0.0/8", "", id="otro-rango"),
    ],
)
def test_bastion_refuses_reused_sg_with_unexpected_ssh_sources(
    v4: str, v6: str, tmp_path: Path
) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            **env,
            "BASTION_SSH_CIDR": "203.0.113.10/32",
            "FAKE_AWS_SG_ID": "sg-bastion",
            "FAKE_SG_V4": v4,
            "FAKE_SG_V6": v6,
        },
    )
    assert result.returncode != 0
    assert "orígenes no autorizados" in result.stdout
    assert "revoke-security-group-ingress" in result.stdout
    assert not any("authorize-security-group-ingress" in c for c in calls)
    assert not any("revoke-security-group-ingress" in c for c in calls)
    # El filtro incluye protocolo -1 y rangos tcp que contienen el 22.
    queries = [c for c in calls if "--group-ids sg-bastion" in c]
    assert queries and all("IpProtocol=='-1'" in c and "FromPort<=`22`" in c for c in queries)


@pytest.mark.parametrize(
    ("given", "normalized", "existing", "authorized"),
    [
        pytest.param("203.0.113.10/32", "203.0.113.10/32", "", True, id="sin-regla"),
        pytest.param("203.0.113.10/32", "203.0.113.10/32", "203.0.113.10/32", False, id="ya-ok"),
        pytest.param("010.001.002.003/32", "10.1.2.3/32", "", True, id="ceros-izquierda"),
    ],
)
def test_bastion_reuses_sg_and_adds_only_operator_cidr(
    given: str, normalized: str, existing: str, authorized: bool, tmp_path: Path
) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            **env,
            "BASTION_SSH_CIDR": given,
            "FAKE_AWS_SG_ID": "sg-bastion",
            "FAKE_SG_V4": existing,
        },
        merge_stderr=True,
    )
    # Supera el paso del SG y se detiene al no encontrar AMI (aws falso).
    assert result.returncode != 0
    assert "No se encontró una AMI" in result.stdout
    assert f"SSH solo desde {normalized}" in result.stdout
    authorize = [c for c in calls if "authorize-security-group-ingress" in c]
    if authorized:
        assert len(authorize) == 1
        assert f"--cidr {normalized}" in authorize[0]
        _assert_rule_announced_before_write(result.stdout, "sg-bastion", normalized)
    else:
        assert authorize == []
        assert "Se va a crear esta regla" not in result.stdout
    assert not any("create-security-group" in c for c in calls)


def _assert_rule_announced_before_write(stdout: str, sg_id: str, cidr: str) -> None:
    announce = stdout.index("Se va a crear esta regla de entrada en AWS")
    write = stdout.index("AWS-WRITE authorize")
    assert announce < write
    block = stdout[announce:write]
    assert f"Security group: {sg_id}" in block
    assert f"IP autorizada:  {cidr.removesuffix('/32')}" in block
    assert f"tcp/22 desde {cidr}" in block


def test_bastion_new_sg_announces_rule_before_creating_it(tmp_path: Path) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy, tmp_path, env_extra={**env, "BASTION_SSH_CIDR": "203.0.113.10/32"}, merge_stderr=True
    )
    assert "No se encontró una AMI" in result.stdout
    assert any("create-security-group" in c for c in calls)
    authorize = [c for c in calls if "authorize-security-group-ingress" in c]
    assert len(authorize) == 1 and "--cidr 203.0.113.10/32" in authorize[0]
    _assert_rule_announced_before_write(result.stdout, "sg-nuevo", "203.0.113.10/32")


# ---------------------------------------------------------------------------
# Backend S3: inicialización y respaldo sin fallos silenciosos
# ---------------------------------------------------------------------------

# Lo que devuelve Terraform 1.17.1 con `state pull` contra una key S3 que aún no
# existe (verificado con Terraform real y un S3 simulado local): no es una
# salida vacía, sino un state vacío con lineage "".
EMPTY_REMOTE_STATE = (
    '{\n  "version": 4,\n  "terraform_version": "1.17.1",\n  "serial": 0,\n'
    '  "lineage": "",\n  "outputs": {},\n  "resources": [],\n  "check_results": null\n}\n'
)

S3_BACKEND_TF = """terraform {
  backend "s3" {
    key          = "stack/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
"""


def _s3_stack(script: Path, tmp_path: Path, backend_hcl: bool = True) -> tuple[Path, Path, Path]:
    """Copia el deploy.sh a un repo temporal cuyo stack declara backend "s3"."""
    repo = tmp_path / "repo"
    copy = _copy_script(script, repo)
    tf_dir = copy.parent.parent
    (tf_dir / "terraform.tf").write_text(S3_BACKEND_TF)
    backend = repo / "backend.hcl"
    if backend_hcl:
        backend.write_text('bucket = "rag-playas-tfstate-000000000000"\n')
    return copy, tf_dir, backend


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_requires_backend_hcl(script: Path, stack: str, tmp_path: Path) -> None:
    copy, _, backend = _s3_stack(script, tmp_path, backend_hcl=False)
    result, calls = _run(copy, tmp_path)
    assert result.returncode != 0
    assert str(backend) in result.stderr
    assert not any(c.startswith("terraform") for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_refuses_leftover_local_state(script: Path, stack: str, tmp_path: Path) -> None:
    """Un state local junto al backend S3 serían dos fuentes de verdad: no se
    migra automáticamente."""
    copy, tf_dir, _ = _s3_stack(script, tmp_path)
    (tf_dir / "terraform.tfstate").write_text('{"serial": 9}')
    result, calls = _run(copy, tmp_path)
    assert result.returncode != 0
    assert "No se migra automáticamente" in result.stderr
    assert not any(c.startswith("terraform") for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_init_uses_backend_config_and_reports_failures(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, backend = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_INIT_FAIL": "1"})
    assert result.returncode != 0
    assert "terraform init falló con el backend S3" in result.stderr
    init_calls = [c for c in calls if " init" in c]
    assert len(init_calls) == 1
    assert f"-backend-config={backend}" in init_calls[0]
    # Ninguna pregunta de migración puede contestarse dentro del script.
    assert "-input=false" in init_calls[0].split()
    for forbidden in ("-migrate-state", "-reconfigure", "-force-copy", "-upgrade"):
        assert forbidden not in init_calls[0]
    assert not any(" apply" in c for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_backup_stops_when_state_pull_fails(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL_FAIL": "1"})
    assert result.returncode != 0
    assert "state pull falló" in result.stderr
    assert not any(" apply" in c for c in calls)
    assert not list((tmp_path / "backups").rglob("*.partial"))


@pytest.mark.parametrize(
    "pulled",
    [
        pytest.param("", id="salida-vacia"),
        pytest.param(EMPTY_REMOTE_STATE, id="json-terraform-1.17"),
        pytest.param('{"serial":0,"lineage":"","resources":[]}', id="json-compacto"),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_empty_state_needs_explicit_opt_in(
    script: Path, stack: str, pulled: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL": pulled})
    assert result.returncode != 0
    assert "ALLOW_EMPTY_STATE=1" in result.stderr
    assert not any(" apply" in c for c in calls)
    # Un state vacío no se guarda como si fuera un respaldo válido.
    assert not list((tmp_path / "backups").rglob("remote.tfstate"))

    result, calls = _run(
        copy, tmp_path, env_extra={"FAKE_STATE_PULL": pulled, "ALLOW_EMPTY_STATE": "1"}
    )
    assert "stack nuevo" in result.stdout
    assert any(" apply" in c and "-auto-approve" not in c for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_backs_up_remote_state_before_apply(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    remote_state = '{"version": 4, "serial": 12, "lineage": "abc"}'
    result, calls = _run(
        copy, tmp_path, env_extra={"FAKE_STATE_PULL": remote_state, "FAKE_STATE_LIST": ""}
    )
    pulls = [i for i, c in enumerate(calls) if " state pull" in c]
    applies = [i for i, c in enumerate(calls) if " apply" in c]
    assert pulls and applies and pulls[0] < applies[0]
    backups = list((tmp_path / "backups" / stack).glob("*/remote.tfstate"))
    assert len(backups) == 1
    assert backups[0].read_text() == remote_state
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# check_sg_drift: un error del backend nunca se toma como "state vacío"
# ---------------------------------------------------------------------------


def test_rag_drift_check_stops_when_state_list_fails(tmp_path: Path) -> None:
    copy = _copy_script(RAG_DEPLOY, tmp_path / "repo")
    (copy.parent.parent / "terraform.tfstate").write_text('{"serial": 3}')
    result, calls = _run(
        copy, tmp_path, env_extra={"FAKE_AWS_SG_ID": "sg-en-aws", "FAKE_STATE_LIST_FAIL": "1"}
    )
    assert result.returncode != 0
    assert "state list falló" in result.stderr
    assert "import" not in result.stdout  # sin sugerencia de import engañosa
    assert not any(" apply" in c for c in calls)


def test_rag_drift_check_stops_when_listed_resource_cannot_be_shown(tmp_path: Path) -> None:
    copy = _copy_script(RAG_DEPLOY, tmp_path / "repo")
    (copy.parent.parent / "terraform.tfstate").write_text('{"serial": 3}')
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_AWS_SG_ID": "sg-en-aws",
            "FAKE_STATE_LIST": ALL_SGS,
            "FAKE_STATE_SHOW_FAIL": "1",
        },
    )
    assert result.returncode != 0
    assert "state show aws_security_group.alb falló" in result.stderr
    assert not any(" apply" in c for c in calls)


def test_rag_drift_check_passes_when_ids_match(tmp_path: Path) -> None:
    copy = _copy_script(RAG_DEPLOY, tmp_path / "repo")
    (copy.parent.parent / "terraform.tfstate").write_text('{"serial": 3}')
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_AWS_SG_ID": "sg-igual",
            "FAKE_STATE_SG_ID": "sg-igual",
            "FAKE_STATE_LIST": ALL_SGS,
        },
    )
    assert "Despliegue detenido" not in result.stderr
    assert sum(" state list" in c for c in calls) == 1  # se lee una sola vez
    assert any(" apply" in c for c in calls)


# ---------------------------------------------------------------------------
# Operaciones de state que solo se hacen a mano
# ---------------------------------------------------------------------------

_FORBIDDEN_IN_SCRIPTS = (
    "-force-copy",
    "-migrate-state",
    "-reconfigure",
    "state push",
    "force-unlock",
)
_MESSAGE_CMDS = re.compile(r"^\s*(echo|printf|log|ok|warn|fail|tf_fail|step)\b")


def _all_shell_scripts() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.sh")
        if not {".git", ".venv", "node_modules", ".terraform"}.intersection(p.parts)
    )


def test_scripts_never_run_manual_only_state_operations() -> None:
    scripts = _all_shell_scripts()
    assert STATE_LIB in scripts and BOOTSTRAP in scripts
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {cmd}"
        for path in scripts
        for cmd in _logical_commands("\n".join(_code_lines(path)))
        if not _MESSAGE_CMDS.match(cmd) and any(token in cmd for token in _FORBIDDEN_IN_SCRIPTS)
    ]
    assert offenders == []


def test_bastion_ships_backend_config_without_extra_heredoc() -> None:
    commands = _logical_commands("\n".join(_code_lines(BASTION)))
    ship = [c for c in commands if "backend.hcl" in c and c.startswith("ssh ")]
    assert len(ship) == 1
    assert "umask 077" in ship[0]
    assert '< "${BACKEND_HCL}"' in ship[0]


def test_bastion_pins_terraform_with_checksum() -> None:
    text = BASTION.read_text()
    assert 'TERRAFORM_VERSION="1.17.1"' in text
    assert re.search(r'TERRAFORM_SHA256="[0-9a-f]{64}"', text)
    assert "sha256sum -c -" in text
    assert "apt-get install -y terraform" not in text
    for stack in ("vector-infraestructura", "rag/infrastructure", "ingesta/infrastructure"):
        tf = (REPO_ROOT / stack / "terraform.tf").read_text()
        assert 'required_version = "~> 1.17.1"' in tf
        assert re.search(r'backend\s+"s3"\s*\{[^}]*use_lockfile\s*=\s*true', tf, flags=re.S)


@pytest.mark.skipif(shutil.which("git") is None, reason="requiere git")
def test_gitignore_keeps_states_out_but_tracks_backend_code() -> None:
    def ignored(path: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", path],
                capture_output=True,
            ).returncode
            == 0
        )

    for path in (
        "terraform.tfstate",
        "vector-infraestructura/terraform.tfstate.backup",
        "rag/infrastructure/errored.tfstate",
        "backups/rag_stack_s1.tfstate.age",
        "backend.hcl",
        "rag/infrastructure/backend.hcl",
    ):
        assert ignored(path), path
    for path in (
        "scripts/bootstrap_tfstate_bucket.sh",
        "rag/tests/unit/test_bootstrap_tfstate_bucket.py",
        "scripts/lib/terraform_state.sh",
        "backend.hcl.example",
        "vector-infraestructura/.terraform.lock.hcl",
    ):
        assert not ignored(path), path


# ---------------------------------------------------------------------------
# Bastion: ALLOW_EMPTY_STATE solo se transmite cuando se pide expresamente
# ---------------------------------------------------------------------------

FULL_FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "${CALL_LOG}"
case "$*" in
  *"sts get-caller-identity"*) echo "${FAKE_ACCOUNT:-000000000000}" ;;
  *describe-vpcs*) echo "vpc-123" ;;
  *describe-security-groups*CidrIpv6*) echo "" ;;
  *describe-security-groups*CidrIp*) echo "${BASTION_SSH_CIDR}" ;;
  *describe-security-groups*) echo "sg-bastion" ;;
  *describe-images*) echo "ami-123" ;;
  *describe-instances*PublicIpAddress*) echo "203.0.113.50" ;;
  *describe-instances*) echo "None" ;;
  *run-instances*) echo "i-123" ;;
esac
exit 0
"""

# Registra cada comando remoto en una línea; consume el heredoc solo si stdin
# no es una terminal (los `ssh -t` reciben la pseudo-terminal del test).
FAKE_SSH = """#!/usr/bin/env bash
all="$*"
echo "ssh ${all//$'\\n'/ }" >> "${SSH_LOG}"
[[ -t 0 ]] || cat > /dev/null
exit 0
"""


def _run_full_bastion(tmp_path: Path, env_extra: dict[str, str] | None = None):
    copy, env = _bastion_repo(tmp_path)
    # Directorio propio, por delante en el PATH: _run reinstala los falsos por defecto.
    full_bin = tmp_path / "bin-full"
    full_bin.mkdir()
    for name, body in (
        ("aws", FULL_FAKE_AWS),
        ("ssh", FAKE_SSH),
        ("scp", "#!/usr/bin/env bash\nexit 0\n"),
    ):
        (full_bin / name).write_text(body)
        (full_bin / name).chmod(0o755)
    ssh_log = tmp_path / "ssh.log"
    ssh_log.touch()
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            **env,
            "PATH": f"{full_bin}{os.pathsep}{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "BASTION_SSH_CIDR": "203.0.113.10/32",
            "SSH_LOG": str(ssh_log),
            **(env_extra or {}),
        },
    )
    deploys = [
        line
        for line in ssh_log.read_text().splitlines()
        if line.startswith("ssh -t ") and "scripts/deploy.sh" in line
    ]
    return result, calls, deploys


def test_bastion_does_not_forward_allow_empty_state_by_default(tmp_path: Path) -> None:
    result, _, deploys = _run_full_bastion(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(deploys) == 2
    assert all("ALLOW_EMPTY_STATE" not in d for d in deploys)


def test_bastion_forwards_allow_empty_state_only_when_requested(tmp_path: Path) -> None:
    result, _, deploys = _run_full_bastion(tmp_path, {"ALLOW_EMPTY_STATE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "solo en esta ejecución" in result.stdout
    vector, rag = deploys
    # Asignación del propio comando, nunca `export`.
    assert "&& ALLOW_EMPTY_STATE=1 ./vector-infraestructura/scripts/deploy.sh" in vector
    assert "sg docker -c 'ALLOW_EMPTY_STATE=1 ./rag/infrastructure/scripts/deploy.sh'" in rag
    assert all("export ALLOW_EMPTY_STATE" not in d for d in deploys)


@pytest.mark.parametrize("value", ["yes", "true", "2", " 1"])
def test_bastion_rejects_ambiguous_allow_empty_state(value: str, tmp_path: Path) -> None:
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**env, "BASTION_SSH_CIDR": "203.0.113.10/32", "ALLOW_EMPTY_STATE": value},
    )
    assert result.returncode != 0
    assert "ALLOW_EMPTY_STATE solo admite 1" in result.stdout
    assert calls == []


def test_bastion_never_exports_allow_empty_state() -> None:
    code = "\n".join(_code_lines(BASTION))
    assert not re.search(r"\bexport\s+ALLOW_EMPTY_STATE\b", code)
    assignments = re.findall(r'REMOTE_DEPLOY_ENV="([^"]*)"', code)
    assert sorted(set(assignments)) == ["", "ALLOW_EMPTY_STATE=1 "]
    deploy_lines = [c for c in _logical_commands(code) if re.search(r"\./\S*scripts/deploy\.sh", c)]
    assert len(deploy_lines) == 2
    assert all("${REMOTE_DEPLOY_ENV}./" in c for c in deploy_lines)


# ---------------------------------------------------------------------------
# Backend S3: la cuenta del bucket debe ser la de las credenciales
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "env", "message"),
    [
        pytest.param(
            "rag-playas-tfstate-111111111111", {}, "no es de la cuenta actual", id="otra-cuenta"
        ),
        pytest.param("mi-bucket-del-state", {}, "debe contener bucket", id="nombre-no-convenido"),
        pytest.param(
            "rag-playas-tfstate-000000000000",
            {"FAKE_STS_FAIL": "1"},
            "No se pudo verificar la cuenta",
            id="sts-falla",
        ),
        pytest.param(
            "rag-playas-tfstate-000000000000",
            {"RAG_EXPECTED_AWS_ACCOUNT_ID": "222222222222"},
            "RAG_EXPECTED_AWS_ACCOUNT_ID",
            id="cuenta-esperada-distinta",
        ),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_requires_bucket_of_current_account(
    script: Path, stack: str, bucket: str, env: dict[str, str], message: str, tmp_path: Path
) -> None:
    copy, _, backend = _s3_stack(script, tmp_path)
    backend.write_text(f'bucket = "{bucket}"\n')
    result, calls = _run(copy, tmp_path, env_extra={"ALLOW_EMPTY_STATE": "1", **env})
    assert result.returncode != 0
    assert message in result.stderr
    # Se detiene antes de inicializar el backend.
    assert not any(c.startswith("terraform") for c in calls)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nkey = "rag-infrastructure/terraform.tfstate"\n',
            "línea 2 (key)",
            id="key",
        ),
        pytest.param(
            'region = "eu-west-1"\nbucket = "rag-playas-tfstate-000000000000"\n',
            "línea 1 (region)",
            id="region",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nendpoints = { s3 = "http://x" }\n',
            "(endpoints)",
            id="endpoints",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nprofile = "otro"\n',
            "(profile)",
            id="profile",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\naccess_key = "AKIAEXAMPLESECRET"\n',
            "(access_key)",
            id="credencial",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nbucket = "rag-playas-tfstate-000000000000"\n',
            "exactamente una línea bucket (hay 2)",
            id="bucket-duplicado",
        ),
        pytest.param(
            "# solo un comentario\n", "exactamente una línea bucket (hay 0)", id="sin-bucket"
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000" key = "x"\n',
            "línea 1 (bucket)",
            id="dos-atributos-en-una-linea",
        ),
        # Una línea sin forma `nombre =` no expone su contenido.
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nAKIAEXAMPLESECRET\n',
            "línea 2 (?)",
            id="linea-suelta",
        ),
        pytest.param(
            "bucket = 'rag-playas-tfstate-000000000000'\n",
            "línea 1 (bucket): formato no admitido",
            id="comillas-simples",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000" # bucket del lab\n',
            "línea 1 (bucket): formato no admitido",
            id="comentario-final",
        ),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_config_only_allows_bucket(
    script: Path, stack: str, content: str, message: str, tmp_path: Path
) -> None:
    """-backend-config tiene prioridad sobre el bloque backend: una key en el
    backend.hcl compartido haría que los tres stacks usaran el mismo state."""
    copy, _, backend = _s3_stack(script, tmp_path)
    backend.write_text(content)
    result, calls = _run(copy, tmp_path, env_extra={"ALLOW_EMPTY_STATE": "1"})
    assert result.returncode != 0
    assert message in result.stderr
    assert "AKIAEXAMPLESECRET" not in result.stdout + result.stderr
    # Se detiene antes de consultar AWS o inicializar Terraform.
    assert not any(c.startswith(("terraform", "aws")) for c in calls)


def test_s3_backend_config_accepts_comments_and_blank_lines(tmp_path: Path) -> None:
    copy, _, backend = _s3_stack(VECTOR_DEPLOY, tmp_path)
    backend.write_text(
        '# Bucket del state\n\n  # sangrado\nbucket = "rag-playas-tfstate-000000000000"  \n'
    )
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL": EMPTY_REMOTE_STATE})
    assert "solo se admite" not in result.stderr
    assert any(" init -input=false" in c for c in calls)


def test_backend_example_and_stack_keys_are_consistent() -> None:
    """backend.hcl.example solo lleva bucket y cada stack fija una key distinta."""
    example = [
        line
        for line in (REPO_ROOT / "backend.hcl.example").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert example == ['bucket = "rag-playas-tfstate-<ACCOUNT_ID>"']
    expected = {
        "ingesta/infrastructure": "ingesta/terraform.tfstate",
        "vector-infraestructura": "vector-infraestructura/terraform.tfstate",
        "rag/infrastructure": "rag-infrastructure/terraform.tfstate",
    }
    for stack, key in expected.items():
        keys = re.findall(
            r'^\s*key\s*=\s*"([^"]+)"', (REPO_ROOT / stack / "terraform.tf").read_text(), re.M
        )
        assert keys == [key], stack


def test_vector_empty_remote_state_refuses_existing_instances(tmp_path: Path) -> None:
    """State remoto vacío + EC2 ya existentes = bucket o key equivocados: no
    se crean duplicados aunque se haya pedido ALLOW_EMPTY_STATE=1."""
    copy, _, _ = _s3_stack(VECTOR_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": EMPTY_REMOTE_STATE,
            "ALLOW_EMPTY_STATE": "1",
            "FAKE_VECTOR_INSTANCES": "i-0abc\ti-0def",
        },
    )
    assert result.returncode != 0
    assert "ya existen EC2" in result.stderr
    assert not any(" apply" in c for c in calls)
    # Solo consultas de lectura a AWS.
    aws_calls = [c for c in calls if c.startswith("aws")]
    assert any("ec2 describe-instances" in c for c in aws_calls)
    assert all(
        any(
            op in c for op in ("sts get-caller-identity", "s3api head-bucket", "describe-instances")
        )
        for c in aws_calls
    )
    # Busca las dos EC2 del stack en cualquier estado no terminado.
    describe = [c for c in aws_calls if "ec2 describe-instances" in c]
    assert len(describe) == 1
    assert "Name=tag:Name,Values=rag-playas-chromadb,rag-playas-ollama" in describe[0]
    assert "Name=instance-state-name,Values=pending,running,stopping,stopped" in describe[0]


def test_rag_empty_remote_state_with_existing_sgs_stops(tmp_path: Path) -> None:
    copy, _, _ = _s3_stack(RAG_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": EMPTY_REMOTE_STATE,
            "ALLOW_EMPTY_STATE": "1",
            "FAKE_AWS_SG_ID": "sg-existente",
        },
    )
    assert result.returncode != 0
    assert "Despliegue detenido sin cambios" in result.stderr
    assert "El state remoto está vacío pero ya existe el SG" in result.stderr
    assert (
        "Revisar backend.hcl y la key antes de desplegar: no importar ni crear duplicados."
        in result.stderr
    )
    # Sin sugerencias de state rm/import: se aplicarían a un state equivocado.
    output = result.stdout + result.stderr
    assert "state rm" not in output
    assert "import aws_security_group" not in output
    assert not any(" state list" in c for c in calls)  # vacío conocido: sin state list
    assert not any(" state rm" in c or " import" in c for c in calls)
    assert not any(" apply" in c for c in calls)


# ---------------------------------------------------------------------------
# Backend S3: existencia del bucket y variables que desvían el backend
# ---------------------------------------------------------------------------

BACKEND_OVERRIDE_VARS = [
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL_STS",
    "AWS_S3_ENDPOINT",
    "AWS_STS_ENDPOINT",
    "TF_WORKSPACE",
]

REAL_REMOTE_STATE = '{"version": 4, "serial": 1, "lineage": "9f1c-real", "resources": []}'


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_stops_when_bucket_is_missing_or_foreign(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(
        copy, tmp_path, env_extra={"ALLOW_EMPTY_STATE": "1", "FAKE_HEAD_BUCKET_FAIL": "1"}
    )
    assert result.returncode != 0
    assert (
        "✗ El bucket del state rag-playas-tfstate-000000000000 no existe o no pertenece a la"
        " cuenta 000000000000. Crearlo con scripts/bootstrap_tfstate_bucket.sh." in result.stderr
    )
    assert not any(c.startswith("terraform") for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_checks_bucket_owner_before_init(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    _, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL": REAL_REMOTE_STATE})
    sts = [i for i, c in enumerate(calls) if "sts get-caller-identity" in c]
    head = [i for i, c in enumerate(calls) if "s3api head-bucket" in c]
    init = [i for i, c in enumerate(calls) if " init -input=false" in c]
    assert len(sts) == 1 and len(head) == 1 and len(init) == 1
    assert sts[0] < head[0] < init[0]
    assert "--region us-east-1" in calls[sts[0]]
    head_args = calls[head[0]].split()
    assert head_args[head_args.index("--bucket") + 1] == "rag-playas-tfstate-000000000000"
    assert head_args[head_args.index("--expected-bucket-owner") + 1] == "000000000000"
    assert head_args[head_args.index("--region") + 1] == "us-east-1"


@pytest.mark.parametrize("var", BACKEND_OVERRIDE_VARS)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_refuses_endpoint_and_workspace_overrides(
    script: Path, stack: str, var: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(
        copy, tmp_path, env_extra={"ALLOW_EMPTY_STATE": "1", var: "http://127.0.0.1:9000"}
    )
    assert result.returncode != 0
    assert (
        f"✗ {var} está definida y desviaría el backend S3 o la key del state."
        f" Ejecutar 'unset {var}' y repetir." in result.stderr
    )
    assert "127.0.0.1" not in result.stdout + result.stderr  # nunca el valor
    assert calls == []


def test_s3_backend_ignores_empty_override_vars(tmp_path: Path) -> None:
    copy, _, _ = _s3_stack(VECTOR_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": REAL_REMOTE_STATE,
            **dict.fromkeys(BACKEND_OVERRIDE_VARS, ""),
        },
    )
    assert "está definida" not in result.stderr
    assert any(" init -input=false" in c for c in calls)
    assert any(" apply" in c for c in calls)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'bucket = "rag-playas-tfstate-000000000000"\r\n', id="crlf"),
        pytest.param(b'bucket = "rag-playas-tfstate-000000000000"', id="sin-salto-final"),
        pytest.param(b'bucket="rag-playas-tfstate-000000000000"\n', id="sin-espacios"),
    ],
)
def test_s3_backend_config_accepts_equivalent_formats(content: bytes, tmp_path: Path) -> None:
    copy, _, backend = _s3_stack(VECTOR_DEPLOY, tmp_path)
    backend.write_bytes(content)
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL": REAL_REMOTE_STATE})
    assert "solo se admite" not in result.stderr
    assert "formato no admitido" not in result.stderr
    assert any(" init -input=false" in c and f"-backend-config={backend}" in c for c in calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_backs_up_real_state_without_resources(
    script: Path, stack: str, tmp_path: Path
) -> None:
    """Un state con lineage real y sin recursos no es "vacío": se respalda sin
    ALLOW_EMPTY_STATE."""
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={"FAKE_STATE_PULL": REAL_REMOTE_STATE})
    assert "ALLOW_EMPTY_STATE" not in result.stderr
    assert "stack nuevo" not in result.stdout
    backups = list((tmp_path / "backups" / stack).glob("*/remote.tfstate"))
    assert len(backups) == 1
    assert backups[0].read_text() == REAL_REMOTE_STATE
    assert any(" apply" in c for c in calls)


# ---------------------------------------------------------------------------
# Vector: comprobación de EC2 existentes con state remoto vacío
# ---------------------------------------------------------------------------


def test_vector_empty_remote_state_stops_when_ec2_check_fails(tmp_path: Path) -> None:
    copy, _, _ = _s3_stack(VECTOR_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": EMPTY_REMOTE_STATE,
            "ALLOW_EMPTY_STATE": "1",
            "FAKE_EC2_FAIL": "1",
        },
    )
    assert result.returncode != 0
    assert "No se pudo comprobar si ya existen las EC2" in result.stderr
    assert not any(" apply" in c for c in calls)


@pytest.mark.parametrize(
    "instances",
    [
        pytest.param("", id="vacio"),
        pytest.param("None", id="none"),
        pytest.param(" \t ", id="espacios"),
    ],
)
def test_vector_empty_remote_state_without_instances_continues(
    instances: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(VECTOR_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": EMPTY_REMOTE_STATE,
            "ALLOW_EMPTY_STATE": "1",
            "FAKE_VECTOR_INSTANCES": instances,
        },
    )
    assert "ya existen EC2" not in result.stderr
    assert any("ec2 describe-instances" in c for c in calls)
    assert any(" apply" in c and "-auto-approve" not in c for c in calls)


# ---------------------------------------------------------------------------
# Bastion: el backend S3 se valida antes de crear nada en AWS
# ---------------------------------------------------------------------------

_BASTION_READ_ONLY_OPS = ("sts get-caller-identity", "s3api head-bucket")
_BASTION_FORBIDDEN_OPS = (
    "create-security-group",
    "authorize-",
    "run-instances",
    "describe-images",
)


def _bastion_s3_backend(tmp_path: Path, backend: str | None) -> Path:
    """Declara backend "s3" en los dos stacks del repo temporal del bastion."""
    repo = tmp_path / "repo"
    for stack in ("vector-infraestructura", "rag/infrastructure"):
        (repo / stack).mkdir(parents=True, exist_ok=True)
        (repo / stack / "terraform.tf").write_text(S3_BACKEND_TF)
    backend_hcl = repo / "backend.hcl"
    if backend is not None:
        backend_hcl.write_text(backend)
    return backend_hcl


@pytest.mark.parametrize(
    ("backend", "env", "message"),
    [
        pytest.param(None, {}, "Falta {backend_hcl}", id="sin-backend-hcl"),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\nkey = "otro/terraform.tfstate"\n',
            {},
            "línea 2 (key)",
            id="key-extra",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-111111111111"\n',
            {},
            "no es de la cuenta actual",
            id="otra-cuenta",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\n',
            {"AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9000"},
            "AWS_ENDPOINT_URL_S3 está definida",
            id="endpoint-s3",
        ),
        pytest.param(
            'bucket = "rag-playas-tfstate-000000000000"\n',
            {"FAKE_HEAD_BUCKET_FAIL": "1"},
            "no existe o no pertenece a la cuenta 000000000000",
            id="bucket-inexistente",
        ),
    ],
)
def test_bastion_validates_s3_backend_before_any_aws_write(
    backend: str | None, env: dict[str, str], message: str, tmp_path: Path
) -> None:
    backend_hcl = _bastion_s3_backend(tmp_path, backend)
    copy, bastion_env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**bastion_env, "BASTION_SSH_CIDR": "203.0.113.10/32", **env},
    )
    assert result.returncode != 0
    # fail() escribe en stdout y tf_fail() en stderr.
    assert message.format(backend_hcl=backend_hcl) in result.stdout + result.stderr
    aws_calls = [c for c in calls if c.startswith("aws")]
    assert all(any(op in c for op in _BASTION_READ_ONLY_OPS) for c in aws_calls), aws_calls
    assert not any(op in c for c in calls for op in _BASTION_FORBIDDEN_OPS)
    assert "Prerrequisitos OK" not in result.stdout


def test_bastion_valid_s3_backend_continues_after_read_only_checks(tmp_path: Path) -> None:
    _bastion_s3_backend(tmp_path, 'bucket = "rag-playas-tfstate-000000000000"\n')
    copy, env = _bastion_repo(tmp_path)
    result, calls = _run(
        copy, tmp_path, env_extra={**env, "BASTION_SSH_CIDR": "203.0.113.10/32"}, merge_stderr=True
    )
    # Supera la validación y se detiene más adelante al no encontrar AMI (aws falso).
    assert "Prerrequisitos OK" in result.stdout
    assert "No se encontró una AMI" in result.stdout
    sts = [i for i, c in enumerate(calls) if "sts get-caller-identity" in c]
    head = [i for i, c in enumerate(calls) if "s3api head-bucket" in c]
    vpcs = [i for i, c in enumerate(calls) if "describe-vpcs" in c]
    assert sts and head and vpcs and sts[0] < head[0] < vpcs[0]
    assert "--expected-bucket-owner 000000000000" in calls[head[0]]


def test_bastion_valid_s3_backend_ships_backend_hcl(tmp_path: Path) -> None:
    _bastion_s3_backend(tmp_path, 'bucket = "rag-playas-tfstate-000000000000"\n')
    result, calls, deploys = _run_full_bastion(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("s3api head-bucket" in c for c in calls)
    assert "backend.hcl copiado al bastion" in result.stdout
    ships = [
        line for line in (tmp_path / "ssh.log").read_text().splitlines() if "backend.hcl" in line
    ]
    assert len(ships) == 1
    assert len(deploys) == 2
