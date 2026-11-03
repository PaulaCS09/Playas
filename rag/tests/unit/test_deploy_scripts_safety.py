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

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")

FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "terraform $*" >> "${CALL_LOG}"
case "$*" in
  *" init"*) exit 0 ;;
  *" state show"*)
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
    """Copia el script a la misma ruta relativa dentro de ``root``."""
    dest = root / src.relative_to(REPO_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


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
    "state_sg_id",
    [pytest.param("sg-en-state", id="id-distinto"), pytest.param("", id="ausente-en-state")],
)
def test_rag_deploy_stops_on_sg_drift_without_touching_state(
    state_sg_id: str, tmp_path: Path
) -> None:
    copy = _copy_script(RAG_DEPLOY, tmp_path / "repo")
    (copy.parent.parent / "terraform.tfstate").write_text('{"serial": 3}')
    result, calls = _run(
        copy,
        tmp_path,
        env_extra={"FAKE_AWS_SG_ID": "sg-en-aws", "FAKE_STATE_SG_ID": state_sg_id},
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
