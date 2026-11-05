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


@pytest.mark.parametrize(("script", "stack"), STACK_SCRIPTS)
def test_s3_backend_empty_state_needs_explicit_opt_in(
    script: Path, stack: str, tmp_path: Path
) -> None:
    copy, _, _ = _s3_stack(script, tmp_path)
    result, calls = _run(copy, tmp_path)
    assert result.returncode != 0
    assert "ALLOW_EMPTY_STATE=1" in result.stderr
    assert not any(" apply" in c for c in calls)

    result, calls = _run(copy, tmp_path, env_extra={"ALLOW_EMPTY_STATE": "1"})
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
