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
  *" workspace show"*)
    [[ -n "${FAKE_WORKSPACE_FAIL:-}" ]] && { echo "Error: workspace" >&2; exit 1; }
    echo "${FAKE_WORKSPACE-default}" ;;
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

# head-bucket distingue el bucket del state (FAKE_HEAD_BUCKET_FAIL) del bucket
# de datos de ingesta (FAKE_DATA_BUCKET: 404 por defecto, 403 o exists).
# FAKE_AWS_FAIL_ON=<subcadena> hace fallar (exit 255) la llamada que la contenga.
# El orden del case importa: las consultas de reglas del bastion (CidrIp*) y el
# sondeo del guard (SecurityGroups[].GroupId) van antes de la consulta genérica
# de check_sg_drift (SecurityGroups[0].GroupId).
FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "${CALL_LOG}"
if [[ -n "${FAKE_AWS_FAIL_ON:-}" && "$*" == *"${FAKE_AWS_FAIL_ON}"* ]]; then
  echo "An error occurred (AccessDenied)" >&2; exit 255
fi
case "$*" in
  *"sts get-caller-identity"*)
    [[ -n "${FAKE_STS_FAIL:-}" ]] && { echo "ExpiredToken" >&2; exit 255; }
    echo "${FAKE_ACCOUNT:-000000000000}" ;;
  *"s3api head-bucket --bucket rag-playas-tfstate-"*)
    [[ -n "${FAKE_HEAD_BUCKET_FAIL:-}" ]] && { echo "Not Found (404)" >&2; exit 254; }
    exit 0 ;;
  *"s3api head-bucket"*)
    case "${FAKE_DATA_BUCKET:-404}" in
      exists) exit 0 ;;
      403) echo "An error occurred (403) when calling the HeadBucket operation: Forbidden" >&2
           exit 254 ;;
      *) echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2
         exit 254 ;;
    esac ;;
  *"s3api list-object-versions"*) printf '%b' "${FAKE_STATE_VERSIONS-0\\t0}" ;;
  *"ec2 describe-instances"*) printf '%b' "${FAKE_VECTOR_INSTANCES:-}" ;;
  *describe-vpcs*) echo "${FAKE_DEFAULT_VPC-vpc-0123456789abcdef0}" ;;
  *describe-security-groups*CidrIpv6*) echo "${FAKE_SG_V6:-}" ;;
  *describe-security-groups*CidrIp*) echo "${FAKE_SG_V4:-}" ;;
  *describe-security-groups*"SecurityGroups[].GroupId"*) printf '%b' "${FAKE_GUARD_SG:-}" ;;
  *describe-security-groups*) echo "${FAKE_AWS_SG_ID:-None}" ;;
  *"ec2 describe-addresses"*) printf '%b' "${FAKE_EIPS:-}" ;;
  *"ecr describe-repositories"*) printf '%b' "${FAKE_ECR:-}" ;;
  *"logs describe-log-groups"*) printf '%b' "${FAKE_LOG_GROUPS:-}" ;;
  *"elbv2 describe-load-balancers"*) printf '%b' "${FAKE_ALB:-}" ;;
  *"elbv2 describe-target-groups"*) printf '%b' "${FAKE_TG:-}" ;;
  *"efs describe-file-systems"*) printf '%b' "${FAKE_EFS:-}" ;;
  *"ecs describe-clusters"*) printf '%b' "${FAKE_ECS:-}" ;;
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


def _exec(
    argv: list[str],
    tmp_path: Path,
    *,
    tty: bool = True,
    env_extra: dict[str, str] | None = None,
    merge_stderr: bool = False,
    stdin_text: str = "\x04",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    _install_fakes(bin_dir)
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
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
        # Lo que "teclea" la persona. Por defecto Ctrl-D (EOF): un `read` sobre
        # la pseudo-terminal sin datos se bloquearía hasta el timeout.
        os.write(stdin_master, stdin_text.encode())
    try:
        result = subprocess.run(
            argv,
            stdin=stdin if tty else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Unir stderr permite comprobar el orden entre mensajes y escrituras en AWS.
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            env=env,
            timeout=30,
        )
    finally:
        if stdin_master is not None:
            os.close(stdin)
            os.close(stdin_master)
    return result, call_log.read_text().splitlines()


def _run(
    script: Path, tmp_path: Path, *args: str, **kwargs
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    return _exec(["bash", str(script), *args], tmp_path, **kwargs)


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


def _s3_backend_tf(stack: str) -> str:
    """terraform.tf con backend "s3" y la key real del stack (la exige la
    protección de state vacío)."""
    return (
        "terraform {\n"
        '  backend "s3" {\n'
        f'    key          = "{stack}/terraform.tfstate"\n'
        '    region       = "us-east-1"\n'
        "    encrypt      = true\n"
        "    use_lockfile = true\n"
        "  }\n"
        "}\n"
    )


_SCRIPT_STACK = {VECTOR_DEPLOY: "vector-infraestructura", RAG_DEPLOY: "rag-infrastructure"}


def _s3_stack(script: Path, tmp_path: Path, backend_hcl: bool = True) -> tuple[Path, Path, Path]:
    """Copia el deploy.sh a un repo temporal cuyo stack declara backend "s3"."""
    repo = tmp_path / "repo"
    copy = _copy_script(script, repo)
    tf_dir = copy.parent.parent
    (tf_dir / "terraform.tf").write_text(_s3_backend_tf(_SCRIPT_STACK[script]))
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
        copy,
        tmp_path,
        env_extra={"FAKE_STATE_PULL": pulled, "ALLOW_EMPTY_STATE": "1"},
        stdin_text=f"{stack}\n",
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
    for stack, key in (
        ("vector-infraestructura", "vector-infraestructura"),
        ("rag/infrastructure", "rag-infrastructure"),
    ):
        (repo / stack).mkdir(parents=True, exist_ok=True)
        (repo / stack / "terraform.tf").write_text(_s3_backend_tf(key))
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


# ---------------------------------------------------------------------------
# Protección ante un state remoto vacío (tf_guard_empty_remote_state)
# ---------------------------------------------------------------------------

STATE_BUCKET = "rag-playas-tfstate-000000000000"
ACCOUNT = "000000000000"
EMPTY_ENV = {"FAKE_STATE_PULL": EMPTY_REMOTE_STATE, "ALLOW_EMPTY_STATE": "1"}

# (llamada aws, variable del aws falso, descripción mostrada, id de ejemplo)
GUARD_PROBES = {
    "vector-infraestructura": [
        ("ec2 describe-instances", "FAKE_VECTOR_INSTANCES", "EC2 de ChromaDB/Ollama", "i-0abc"),
        (
            "ec2 describe-security-groups",
            "FAKE_GUARD_SG",
            "security groups de vector-infraestructura",
            "sg-0vec",
        ),
        ("ec2 describe-addresses", "FAKE_EIPS", "Elastic IP de ChromaDB/Ollama", "eipalloc-0a"),
    ],
    "rag-infrastructure": [
        (
            "ec2 describe-security-groups",
            "FAKE_GUARD_SG",
            "security groups de rag/infrastructure",
            "sg-0rag",
        ),
        ("ecr describe-repositories", "FAKE_ECR", "repositorios ECR", "rag-playas-prod-backend"),
        (
            "logs describe-log-groups",
            "FAKE_LOG_GROUPS",
            "log groups de CloudWatch",
            "/ecs/rag-playas-prod-app",
        ),
        ("elbv2 describe-load-balancers", "FAKE_ALB", "ALB", "arn:aws:alb/rag-playas-prod-alb"),
        ("elbv2 describe-target-groups", "FAKE_TG", "target groups", "arn:aws:tg/backend"),
        ("efs describe-file-systems", "FAKE_EFS", "EFS de PostgreSQL", "fs-0abc"),
        ("ecs describe-clusters", "FAKE_ECS", "cluster ECS", "arn:aws:ecs:cluster/rag-playas-prod"),
    ],
}

# Argumentos que identifican los recursos del stack en cada sondeo.
GUARD_PROBE_ARGS = {
    "vector-infraestructura": {
        "ec2 describe-instances": [
            "Name=tag:Name,Values=rag-playas-chromadb,rag-playas-ollama",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
        ],
        "ec2 describe-security-groups": [
            "Name=vpc-id,Values=vpc-0123456789abcdef0",
            "Name=group-name,Values=rag-playas-chromadb,rag-playas-ollama,rag-playas-vector-clients",
        ],
        "ec2 describe-addresses": [
            "Name=tag:Name,Values=rag-playas-chromadb-eip,rag-playas-ollama-eip"
        ],
    },
    "rag-infrastructure": {
        "ec2 describe-security-groups": [
            "Name=vpc-id,Values=vpc-0123456789abcdef0",
            "Name=group-name,Values=rag-playas-prod-alb,rag-playas-prod-app,"
            "rag-playas-prod-efs,rag-playas-prod-frontend",
        ],
        "ecr describe-repositories": [
            "repositoryName=='rag-playas-prod-backend'",
            "repositoryName=='rag-playas-prod-frontend'",
        ],
        "logs describe-log-groups": [
            "--log-group-name-prefix /ecs/rag-playas-prod-",
            "logGroupName=='/ecs/rag-playas-prod-app'",
            "logGroupName=='/ecs/rag-playas-prod-frontend'",
        ],
        "elbv2 describe-load-balancers": ["LoadBalancerName=='rag-playas-prod-alb'"],
        "elbv2 describe-target-groups": [
            "TargetGroupName=='rag-playas-prod-backend'",
            "TargetGroupName=='rag-playas-prod-frontend'",
        ],
        "efs describe-file-systems": ["--creation-token rag-playas-prod-postgres"],
        "ecs describe-clusters": ["--clusters rag-playas-prod ", "status=='ACTIVE'"],
    },
}

_STACK_IDS = {"vector-infraestructura": "vector", "rag-infrastructure": "rag"}
_STACK_SCRIPT = {stack: script for script, stack in _SCRIPT_STACK.items()}

PROBE_CASES = [
    pytest.param(
        _STACK_SCRIPT[stack], stack, *probe, id=f"{_STACK_IDS[stack]}-{probe[0].split()[1]}"
    )
    for stack, probes in GUARD_PROBES.items()
    for probe in probes
]

_READ_ONLY_AWS = ("sts get-caller-identity", "s3api head-bucket", "s3api list-object-versions")


def _first(calls: list[str], needle: str) -> int:
    matches = [i for i, c in enumerate(calls) if needle in c]
    assert matches, f"falta la llamada {needle!r}: {calls}"
    return matches[0]


def _assert_read_only(calls: list[str]) -> None:
    for call in calls:
        if call.startswith("aws "):
            op = call.split()[2] if len(call.split()) > 2 else ""
            assert any(r in call for r in _READ_ONLY_AWS) or op.startswith("describe-"), call
        assert not any(t in call for t in (" import", " state rm", " state push")), call


def _assert_no_apply(calls: list[str]) -> None:
    assert not any(c.startswith("terraform") and " apply" in c for c in calls)


@pytest.mark.parametrize(
    "absent",
    [
        pytest.param("", id="vacio"),
        pytest.param("None", id="none"),
        pytest.param("None\\tNone\\n \\t", id="none-y-espacios"),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_empty_remote_state_guard_happy_path(
    script: Path, stack: str, absent: str, tmp_path: Path
) -> None:
    """Key sin historial, sin recursos del stack y nombre tecleado: apply
    interactivo, tras todas las comprobaciones de solo lectura."""
    copy, _, _ = _s3_stack(script, tmp_path)
    env = {**EMPTY_ENV, **{var: absent for _, var, _, _ in GUARD_PROBES[stack]}}
    result, calls = _run(copy, tmp_path, env_extra=env, stdin_text=f"{stack}\n")

    assert f"Escribe {stack} para continuar" in result.stderr
    assert "creará el stack desde cero" in result.stdout
    assert "✗" not in result.stderr
    applies = [c for c in calls if c.startswith("terraform") and " apply" in c]
    assert len(applies) == 1
    assert "-input=true" in applies[0].split() and "-auto-approve" not in applies[0]

    steps = [
        "sts get-caller-identity",
        "s3api head-bucket",
        " init -input=false",
        " workspace show",
        " state pull",
        "s3api list-object-versions",
    ]
    order = [_first(calls, s) for s in steps]
    assert order == sorted(order)
    probes = [_first(calls, op) for op, _, _, _ in GUARD_PROBES[stack]]
    assert order[-1] < min(probes) and max(probes) < _first(calls, " apply")
    for op, expected in GUARD_PROBE_ARGS[stack].items():
        call = calls[_first(calls, op)]
        assert "--region us-east-1" in call
        for arg in expected:
            assert arg in call, (op, arg)
    _assert_read_only(calls)


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        pytest.param("1\\t0", "tiene versiones anteriores", id="versiones"),
        pytest.param("0\\t1", "tiene versiones anteriores", id="delete-marker"),
        pytest.param("0\\t0\\n2\\t0", "tiene versiones anteriores", id="segunda-pagina"),
        pytest.param("", "Respuesta inesperada", id="vacio"),
        pytest.param("None", "Respuesta inesperada", id="none"),
        pytest.param("abc", "Respuesta inesperada", id="texto"),
        pytest.param("0", "Respuesta inesperada", id="un-solo-numero"),
        pytest.param("0\\tNone", "Respuesta inesperada", id="numero-y-none"),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_empty_remote_state_guard_stops_on_key_history(
    script: Path, stack: str, versions: str, message: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**EMPTY_ENV, "FAKE_STATE_VERSIONS": versions},
        stdin_text=f"{stack}\n",
    )
    assert result.returncode != 0
    assert message in result.stderr
    key = f"{stack}/terraform.tfstate"
    # La consulta de versiones es la última llamada: ni sondeos, ni prompt, ni apply.
    assert "s3api list-object-versions" in calls[-1]
    lov = calls[-1] + " "
    assert f"--bucket {STATE_BUCKET} " in lov
    assert f"--expected-bucket-owner {ACCOUNT} " in lov
    assert f"--prefix {key} " in lov
    assert f"Versions[?Key=='{key}']" in lov and f"DeleteMarkers[?Key=='{key}']" in lov
    assert "--region us-east-1 " in lov
    assert "Escribe" not in result.stderr
    _assert_no_apply(calls)


@pytest.mark.parametrize(("script", "stack", "op", "var", "desc", "rid"), PROBE_CASES)
def test_empty_remote_state_guard_stops_when_stack_resources_exist(
    script: Path, stack: str, op: str, var: str, desc: str, rid: str, tmp_path: Path
) -> None:
    """Un recurso del stack ya en AWS (state local sin migrar, otra key o state
    borrado): se lista y no se importa ni se despliega, aunque se teclee el
    nombre."""
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**EMPTY_ENV, var: f"{rid}\\tNone\\n{rid}-2"},
        stdin_text=f"{stack}\n",
    )
    assert result.returncode != 0
    assert (
        f"✗ El state remoto de {stack} está vacío, pero ya existen en AWS recursos del stack:\n"
        in result.stderr
    )
    assert f"\n  - {desc}: {rid} {rid}-2\n" in result.stderr
    assert result.stderr.count("  - ") == 1
    assert "No se importa ni se despliega nada" in result.stderr
    assert "Escribe" not in result.stderr
    # Se consultan todos los sondeos antes de decidir.
    for probe_op, _, _, _ in GUARD_PROBES[stack]:
        _first(calls, probe_op)
    _assert_no_apply(calls)
    _assert_read_only(calls)
    assert "terraform import" not in result.stdout + result.stderr


PROBE_FAILURE_CASES = [
    pytest.param(
        _STACK_SCRIPT[stack],
        stack,
        {"FAKE_AWS_FAIL_ON": op.split()[1]},
        f"No se pudo comprobar {desc} (credenciales, permisos o red). No se continúa.",
        id=f"{_STACK_IDS[stack]}-{op.split()[1]}",
    )
    for stack, probes in GUARD_PROBES.items()
    for op, _, desc, _ in probes
] + [
    pytest.param(
        _STACK_SCRIPT[stack],
        stack,
        env,
        message,
        id=f"{_STACK_IDS[stack]}-{case}",
    )
    for stack in GUARD_PROBES
    for case, env, message in (
        (
            "list-object-versions-falla",
            {"FAKE_AWS_FAIL_ON": "list-object-versions"},
            "No se pudo consultar el historial de versiones",
        ),
        (
            "vpc-falla",
            {"FAKE_AWS_FAIL_ON": "describe-vpcs"},
            "No se pudo consultar la VPC por defecto",
        ),
        ("sin-vpc-none", {"FAKE_DEFAULT_VPC": "None"}, "No se encontró la VPC por defecto"),
        ("sin-vpc-vacia", {"FAKE_DEFAULT_VPC": ""}, "No se encontró la VPC por defecto"),
    )
]


@pytest.mark.parametrize(("script", "stack", "env", "message"), PROBE_FAILURE_CASES)
def test_empty_remote_state_guard_stops_when_a_check_fails(
    script: Path, stack: str, env: dict[str, str], message: str, tmp_path: Path
) -> None:
    """Un fallo de AWS nunca se toma como "no existe"."""
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={**EMPTY_ENV, **env}, stdin_text=f"{stack}\n")
    assert result.returncode != 0
    assert message in result.stderr
    # Se detiene en la consulta que falló (o justo tras la VPC sin id).
    assert calls[-1].startswith("aws ")
    if "FAKE_AWS_FAIL_ON" in env:
        assert env["FAKE_AWS_FAIL_ON"] in calls[-1]
    assert "Escribe" not in result.stderr
    _assert_no_apply(calls)


@pytest.mark.parametrize(
    "typed",
    [
        pytest.param("otro\n", id="otro-nombre"),
        pytest.param("\n", id="vacia"),
        pytest.param("\x04", id="eof"),
        pytest.param("{stack}-x\n", id="sufijo"),
        pytest.param("{upper}\n", id="mayusculas"),
        pytest.param("{prefix}\n", id="prefijo"),
        pytest.param("{stack}\x04\x04", id="sin-enter"),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_empty_remote_state_guard_requires_exact_stack_name(
    script: Path, stack: str, typed: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    text = typed.format(stack=stack, upper=stack.upper(), prefix=stack.split("-")[0])
    result, calls = _run(copy, tmp_path, env_extra=EMPTY_ENV, stdin_text=text)
    assert result.returncode != 0
    assert f"Escribe {stack} para continuar" in result.stderr
    assert f"✗ Confirmación no recibida para {stack}. No se despliega." in result.stderr
    _assert_no_apply(calls)


SECRET_SENTINEL = "SENTINEL-S3CRET-9f1c"
_GUARD_VARS = {
    "vector-infraestructura": ["project"],
    "rag-infrastructure": ["project", "environment", "aws_region"],
}


def _defaults_cases() -> list:
    cases = []
    for stack, variables in _GUARD_VARS.items():
        script, sid = _STACK_SCRIPT[stack], _STACK_IDS[stack]
        for var in variables:
            cases.append(
                pytest.param(
                    script,
                    stack,
                    {f"TF_VAR_{var}": ""},
                    {},
                    f"TF_VAR_{var} está definida",
                    id=f"{sid}-env-vacia-{var}",
                )
            )
            cases.append(
                pytest.param(
                    script,
                    stack,
                    {},
                    {
                        "terraform.tfvars": f'postgres_password = "{SECRET_SENTINEL}"\n'
                        f'  {var}="{SECRET_SENTINEL}"\n'
                    },
                    f"terraform.tfvars fija {var}.",
                    id=f"{sid}-tfvars-{var}",
                )
            )
        cases += [
            pytest.param(
                script,
                stack,
                {"TF_VAR_project": SECRET_SENTINEL},
                {},
                "TF_VAR_project está definida",
                id=f"{sid}-env-project",
            ),
            pytest.param(
                script,
                stack,
                {},
                {"zz.auto.tfvars": f'project = "{SECRET_SENTINEL}"\n'},
                "zz.auto.tfvars fija project.",
                id=f"{sid}-auto-tfvars",
            ),
            pytest.param(
                script,
                stack,
                {},
                {"x.tfvars.json": f'{{"api_key": "{SECRET_SENTINEL}"}}'},
                "Hay archivos *.tfvars.json",
                id=f"{sid}-tfvars-json",
            ),
        ]
    return cases


@pytest.mark.parametrize(("script", "stack", "env", "files", "message"), _defaults_cases())
def test_empty_remote_state_guard_requires_default_names(
    script: Path,
    stack: str,
    env: dict[str, str],
    files: dict[str, str],
    message: str,
    tmp_path: Path,
) -> None:
    """Los sondeos buscan los nombres por defecto: si una variable podría
    cambiarlos, se detiene antes de buscar nombres equivocados."""
    copy, tf_dir, _ = _s3_stack(script, tmp_path)
    for name, content in files.items():
        (tf_dir / name).write_text(content)
    result, calls = _run(copy, tmp_path, env_extra={**EMPTY_ENV, **env}, stdin_text=f"{stack}\n")
    assert result.returncode != 0
    assert message in result.stderr
    assert SECRET_SENTINEL not in result.stdout + result.stderr
    assert "s3api list-object-versions" in calls[-1]  # antes de cualquier sondeo
    assert not any("describe-" in c for c in calls)
    _assert_no_apply(calls)


def test_empty_remote_state_guard_ignores_unrelated_tfvars_lines(tmp_path: Path) -> None:
    copy, tf_dir, _ = _s3_stack(RAG_DEPLOY, tmp_path)
    (tf_dir / "terraform.tfvars").write_text(
        f'# project = "otro"\nproject_alias = "x"\npostgres_password = "{SECRET_SENTINEL}"\n'
    )
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**EMPTY_ENV, "TF_VAR_other": "1"},
        stdin_text="rag-infrastructure\n",
    )
    assert "fija" not in result.stderr
    assert SECRET_SENTINEL not in result.stdout + result.stderr
    assert any(c.startswith("terraform") and " apply" in c for c in calls)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        pytest.param({"FAKE_WORKSPACE": "dev"}, "no es 'default'", id="otro-workspace"),
        pytest.param({"FAKE_WORKSPACE": ""}, "no es 'default'", id="salida-vacia"),
        pytest.param({"FAKE_WORKSPACE_FAIL": "1"}, "workspace show falló", id="falla"),
    ],
)
@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_requires_default_workspace(
    script: Path, stack: str, env: dict[str, str], message: str, tmp_path: Path
) -> None:
    """Un workspace seleccionado cambia la key a env:/<workspace>/<key>."""
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path, env_extra={**EMPTY_ENV, **env}, stdin_text=f"{stack}\n")
    assert result.returncode != 0
    assert message in result.stderr
    assert " workspace show" in calls[-1]
    assert _first(calls, " init -input=false") < len(calls) - 1
    assert not any(" state pull" in c or " workspace select" in c for c in calls)
    _assert_no_apply(calls)


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_allow_empty_state_with_existing_state_skips_guard(
    script: Path, stack: str, tmp_path: Path
) -> None:
    """Regresión del despliegue normal: con state real, ALLOW_EMPTY_STATE=1 no
    activa la protección (ni historial, ni sondeos, ni prompt)."""
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={
            "FAKE_STATE_PULL": REAL_REMOTE_STATE,
            "ALLOW_EMPTY_STATE": "1",
            "FAKE_STATE_VERSIONS": "5\\t0",
            "FAKE_VECTOR_INSTANCES": "i-0abc",
            "FAKE_GUARD_SG": "sg-0abc",
        },
    )
    assert not any("list-object-versions" in c for c in calls)
    assert "Escribe" not in result.stderr
    assert "stack nuevo" not in result.stdout
    assert any(c.startswith("terraform") and " apply" in c for c in calls)
    assert list((tmp_path / "backups" / stack).glob("*/remote.tfstate"))


@pytest.mark.parametrize(
    ("guard_sg", "drift_sg", "message"),
    [
        pytest.param(
            "sg-existente",
            "None",
            "  - security groups de rag/infrastructure: sg-existente",
            id="guard",
        ),
        # Defensa en profundidad: si el SG aparece después del sondeo, la
        # comprobación de drift tampoco sugiere import en un state vacío.
        pytest.param(
            "",
            "sg-existente",
            "El state remoto está vacío pero ya existe el SG",
            id="drift-tras-confirmar",
        ),
    ],
)
def test_rag_empty_remote_state_with_existing_sgs_stops(
    guard_sg: str, drift_sg: str, message: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(RAG_DEPLOY, tmp_path)
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={**EMPTY_ENV, "FAKE_GUARD_SG": guard_sg, "FAKE_AWS_SG_ID": drift_sg},
        stdin_text="rag-infrastructure\n",
    )
    assert result.returncode != 0
    assert message in result.stderr
    output = result.stdout + result.stderr
    assert "state rm" not in output
    assert "import aws_security_group" not in output
    assert not any(" state list" in c for c in calls)  # vacío conocido: sin state list
    _assert_read_only(calls)
    _assert_no_apply(calls)


# La biblioteca cargada directamente: stack ingesta (sin deploy.sh propio) y
# precondiciones del guard que los deploy.sh no permiten alcanzar.

_LIB_SNIPPET = (
    'source "$LIB"; TF_REMOTE_STATE_EMPTY="${GUARD_EMPTY-1}"; '
    'TF_STATE_BUCKET="${GUARD_BUCKET-rag-playas-tfstate-000000000000}"; '
    'TF_AWS_ACCOUNT_ID="${GUARD_ACCOUNT-000000000000}"; '
    'tf_guard_empty_remote_state "$TFDIR" "$STACK"; echo GUARD-OK'
)


def _run_lib(
    tmp_path: Path,
    stack: str,
    *,
    key: str | None = None,
    env: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    **kwargs,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    tf_dir = tmp_path / "stack"
    tf_dir.mkdir(exist_ok=True)
    (tf_dir / "terraform.tf").write_text(_s3_backend_tf(key or stack))
    for name, content in (files or {}).items():
        (tf_dir / name).write_text(content)
    return _exec(
        ["bash", "-c", _LIB_SNIPPET],
        tmp_path,
        env_extra={"LIB": str(STATE_LIB), "TFDIR": str(tf_dir), "STACK": stack, **(env or {})},
        **kwargs,
    )


def test_guard_ingesta_continues_to_prompt_when_data_bucket_is_absent(tmp_path: Path) -> None:
    result, calls = _run_lib(tmp_path, "ingesta", stdin_text="ingesta\n")
    assert result.returncode == 0, result.stderr
    assert "GUARD-OK" in result.stdout
    assert "Escribe ingesta para continuar" in result.stderr
    head = calls[_first(calls, "s3api head-bucket")] + " "
    assert "--bucket rag-playas-data-000000000000 " in head
    assert f"--expected-bucket-owner {ACCOUNT} " in head
    assert "--prefix ingesta/terraform.tfstate " in calls[_first(calls, "list-object-versions")]
    _assert_read_only(calls)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        pytest.param(
            {"FAKE_DATA_BUCKET": "exists"},
            "  - bucket S3: rag-playas-data-000000000000",
            id="bucket-existe",
        ),
        pytest.param(
            {"FAKE_DATA_BUCKET": "403"},
            "No se pudo comprobar el bucket rag-playas-data-000000000000",
            id="bucket-403",
        ),
        pytest.param(
            {"FAKE_AWS_FAIL_ON": "head-bucket"},
            "No se pudo comprobar el bucket rag-playas-data-000000000000",
            id="head-bucket-falla",
        ),
    ],
)
def test_guard_ingesta_stops_when_data_bucket_exists_or_is_unknown(
    env: dict[str, str], message: str, tmp_path: Path
) -> None:
    result, calls = _run_lib(tmp_path, "ingesta", env=env, stdin_text="ingesta\n")
    assert result.returncode != 0
    assert message in result.stderr
    assert "GUARD-OK" not in result.stdout
    assert "Escribe" not in result.stderr
    _assert_read_only(calls)


@pytest.mark.parametrize(
    ("stack", "kwargs", "message", "aws_called"),
    [
        pytest.param(
            "desconocido",
            {},
            "Stack desconocido para la protección de state vacío: desconocido.",
            True,
            id="stack-desconocido",
        ),
        pytest.param(
            "ingesta",
            {"key": "otro"},
            'exactamente una línea key = "ingesta/terraform.tfstate"',
            False,
            id="key-de-otro-stack",
        ),
        pytest.param(
            "ingesta",
            {"tty": False},
            "se requiere una terminal interactiva",
            False,
            id="sin-terminal",
        ),
        pytest.param(
            "ingesta",
            {"env": {"GUARD_BUCKET": ""}},
            "No se verificó el backend S3",
            False,
            id="backend-sin-verificar",
        ),
        pytest.param(
            "ingesta",
            {"files": {"terraform.tfvars": 'project = "x"\n'}},
            "terraform.tfvars fija project.",
            True,
            id="ingesta-project-en-tfvars",
        ),
        pytest.param(
            "ingesta",
            {
                "files": {
                    "terraform.tf": _s3_backend_tf("ingesta")
                    + 'locals {\n  key = "ingesta/terraform.tfstate"\n}\n'
                }
            },
            "debe declarar exactamente una línea key",
            False,
            id="key-duplicada",
        ),
    ],
)
def test_guard_library_fails_closed(
    stack: str, kwargs: dict, message: str, aws_called: bool, tmp_path: Path
) -> None:
    result, calls = _run_lib(tmp_path, stack, stdin_text=f"{stack}\n", **kwargs)
    assert result.returncode != 0
    assert message in result.stderr
    assert "GUARD-OK" not in result.stdout
    assert bool(calls) is aws_called
    assert not any("head-bucket" in c or "describe-" in c for c in calls)


def test_guard_library_is_noop_when_state_is_not_empty(tmp_path: Path) -> None:
    result, calls = _run_lib(tmp_path, "ingesta", env={"GUARD_EMPTY": "0"}, tty=False)
    assert result.returncode == 0
    assert "GUARD-OK" in result.stdout
    assert calls == []


# Comprobaciones estáticas de la biblioteca.

_ALLOWED_LIB_AWS = {
    ("sts", "get-caller-identity"),
    ("s3api", "head-bucket"),
    ("s3api", "list-object-versions"),
}


def test_state_lib_only_runs_read_only_aws_and_terraform_commands() -> None:
    commands = [
        cmd
        for cmd in _logical_commands("\n".join(_code_lines(STATE_LIB)))
        if not _MESSAGE_CMDS.match(cmd)
    ]
    aws_ops = {
        (svc, op)
        for cmd in commands
        for svc, op in re.findall(r"(?:^|[\s(=])aws\s+([a-z0-9-]+)\s+([a-z0-9-]+)", cmd)
    }
    assert _ALLOWED_LIB_AWS <= aws_ops
    for svc, op in aws_ops:
        assert (svc, op) in _ALLOWED_LIB_AWS or op.startswith("describe-"), (svc, op)
    assert {svc for svc, op in aws_ops if op.startswith("describe-")} == {
        "ec2",
        "ecr",
        "logs",
        "elbv2",
        "efs",
        "ecs",
    }
    code = "\n".join(commands)
    for forbidden in ("terraform console", "-lock=false", "plan -out"):
        assert forbidden not in STATE_LIB.read_text(), forbidden
    # Subcomandos de Terraform que ejecuta: init y lecturas del state/workspace.
    tf_subcommands = set(re.findall(r'terraform -chdir="\$\{tf_dir\}" ([a-z]+(?: [a-z]+)?)', code))
    assert tf_subcommands == {"init", "state pull", "state list", "workspace show"}


def _tf_default(stack_dir: str, var: str) -> str:
    text = (REPO_ROOT / stack_dir / "variables.tf").read_text()
    block = re.search(rf'variable\s+"{var}"\s*\{{(.*?)\n\}}', text, flags=re.S)
    assert block, (stack_dir, var)
    default = re.search(r'^\s*default\s*=\s*"([^"]*)"', block.group(1), flags=re.M)
    assert default, (stack_dir, var)
    return default.group(1)


def _lib_constant(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"$', STATE_LIB.read_text(), flags=re.M)
    assert match, name
    return match.group(1)


def test_state_lib_defaults_match_terraform_variables() -> None:
    project = _lib_constant("TF_DEFAULT_PROJECT")
    for stack_dir in ("ingesta/infrastructure", "vector-infraestructura", "rag/infrastructure"):
        assert _tf_default(stack_dir, "project") == project, stack_dir
    assert _tf_default("rag/infrastructure", "environment") == _lib_constant(
        "TF_DEFAULT_ENVIRONMENT"
    )
    region = _lib_constant("TF_GUARD_REGION")
    assert _tf_default("rag/infrastructure", "aws_region") == region
    assert re.search(
        r"^\s*region\s*=\s*var\.aws_region\s*$",
        (REPO_ROOT / "rag/infrastructure/providers.tf").read_text(),
        flags=re.M,
    )
    for stack_dir in ("ingesta/infrastructure", "vector-infraestructura"):
        providers = (REPO_ROOT / stack_dir / "providers.tf").read_text()
        assert re.findall(r'^\s*region\s*=\s*"([^"]+)"', providers, flags=re.M) == [region]
    assert re.search(
        r'name_prefix\s*=\s*"\$\{var\.project\}-\$\{var\.environment\}"',
        (REPO_ROOT / "rag/infrastructure/locals.tf").read_text(),
    )


def _lib_case_block(stack: str) -> str:
    text = STATE_LIB.read_text()
    match = re.search(rf"^\s*{re.escape(stack)}\)\n(.*?)^\s*;;", text, flags=re.M | re.S)
    assert match, stack
    return match.group(1)


def _tf_text(stack_dir: str) -> str:
    return "\n".join(p.read_text() for p in sorted((REPO_ROOT / stack_dir).glob("*.tf")))


def test_state_lib_probe_names_exist_in_terraform() -> None:
    vector = _lib_case_block("vector-infraestructura")
    vector_suffixes = set(re.findall(r"\$\{p\}(-[a-z][a-z-]*)", vector))
    assert vector_suffixes == {
        "-chromadb",
        "-ollama",
        "-vector-clients",
        "-chromadb-eip",
        "-ollama-eip",
    }
    vector_tf = _tf_text("vector-infraestructura")
    for suffix in vector_suffixes:
        assert f'"${{var.project}}{suffix}"' in vector_tf, suffix

    rag = _lib_case_block("rag-infrastructure")
    rag_suffixes = set(re.findall(r"\$\{pre\}(-[a-z][a-z-]*)", rag))
    assert rag_suffixes == {"-alb", "-app", "-efs", "-frontend", "-backend", "-postgres"}
    rag_tf = _tf_text("rag/infrastructure")
    for suffix in rag_suffixes:
        assert f'"${{local.name_prefix}}{suffix}"' in rag_tf, suffix
    for log_group in ("app", "frontend"):
        assert f"'/ecs/${{pre}}-{log_group}'" in rag
        assert f'"/ecs/${{local.name_prefix}}-{log_group}"' in rag_tf
    assert '--creation-token "${pre}-postgres"' in rag
    assert 'creation_token   = "${local.name_prefix}-postgres"' in rag_tf
    assert '--clusters "${pre}"' in rag
    assert re.search(
        r'resource\s+"aws_ecs_cluster"\s+"\w+"\s*\{\s*name\s*=\s*local\.name_prefix', rag_tf
    )

    ingesta = _lib_case_block("ingesta")
    assert 'tf_guard_bucket_exists "${p}-data-${TF_AWS_ACCOUNT_ID}"' in ingesta
    assert (
        'bucket_name = "${var.project}-data-${data.aws_caller_identity.current.account_id}"'
        in _tf_text("ingesta/infrastructure")
    )
