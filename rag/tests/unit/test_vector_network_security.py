"""Seguridad de red de ChromaDB y Ollama (Fase 1, A5).

Comprobaciones estáticas sobre los security groups de
``vector-infraestructura`` y la conexión con ``rag/infrastructure``, y
ejecución de ``vector-infraestructura/scripts/deploy.sh`` con herramientas
falsas. No se ejecuta Terraform ni AWS reales.
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
VECTOR_DIR = REPO_ROOT / "vector-infraestructura"
VECTOR_DEPLOY = VECTOR_DIR / "scripts" / "deploy.sh"
RAG_INFRA = REPO_ROOT / "rag" / "infrastructure"

SERVICE_SGS = [
    pytest.param("chromadb.tf", "chromadb", 8000, id="chromadb"),
    pytest.param("ollama.tf", "ollama", 11434, id="ollama"),
]


def _block(text: str, start: int) -> str:
    """Devuelve el bloque HCL entre llaves que empieza en la primera ``{``
    desde ``start`` (los .tf de este stack no tienen llaves en cadenas)."""
    open_at = text.index("{", start)
    depth = 0
    for idx in range(open_at, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : idx + 1]
    raise AssertionError("bloque sin cerrar")


def _security_group(filename: str, name: str) -> str:
    text = (VECTOR_DIR / filename).read_text()
    match = re.search(rf'resource "aws_security_group" "{name}"', text)
    assert match, name
    return _block(text, match.end())


def _ingress_blocks(sg: str) -> list[tuple[str, str]]:
    """Lista de (tipo, cuerpo): 'static' para ``ingress {}``, 'dynamic' para
    ``dynamic "ingress" {}``."""
    blocks = []
    for match in re.finditer(r'(dynamic\s+"ingress"|(?<![\w"])ingress)\s*\{', sg):
        kind = "dynamic" if match.group(1).startswith("dynamic") else "static"
        body = _block(sg, match.start())
        # Un ``ingress`` estático dentro de un dynamic no existe en HCL, pero el
        # contenido (``content {}``) sí: se descartan coincidencias anidadas.
        if any(body in outer for _, outer in blocks):
            continue
        blocks.append((kind, body))
    return blocks


def _port(body: str) -> int:
    match = re.search(r"from_port\s*=\s*(\d+)", body)
    assert match, body
    return int(match.group(1))


@pytest.mark.parametrize(("filename", "name", "port"), SERVICE_SGS)
def test_world_access_only_through_transitional_flag(filename: str, name: str, port: int) -> None:
    for kind, body in _ingress_blocks(_security_group(filename, name)):
        if "0.0.0.0/0" in body:
            assert kind == "dynamic", body
            assert "var.legacy_public_service_access" in body, body
            assert _port(body) == port, "solo el puerto del servicio puede ser transitorio"
            # Misma descripción que la regla original: el apply de la fase 1 no
            # la revoca y vuelve a autorizar.
            original = {"chromadb": "ChromaDB API", "ollama": "Ollama API"}[name]
            assert re.search(rf'description\s*=\s*"{original}"\n', body), body


@pytest.mark.parametrize(("filename", "name", "port"), SERVICE_SGS)
def test_ssh_only_from_explicit_cidrs(filename: str, name: str, port: int) -> None:
    ssh = [(k, b) for k, b in _ingress_blocks(_security_group(filename, name)) if _port(b) == 22]
    assert len(ssh) == 1
    kind, body = ssh[0]
    assert kind == "dynamic"
    assert "length(var.ssh_cidr_blocks) > 0" in body
    assert re.search(r"cidr_blocks\s*=\s*var\.ssh_cidr_blocks", body)


@pytest.mark.parametrize(("filename", "name", "port"), SERVICE_SGS)
def test_service_port_open_to_vector_clients_and_operator(
    filename: str, name: str, port: int
) -> None:
    blocks = [
        (k, b) for k, b in _ingress_blocks(_security_group(filename, name)) if _port(b) == port
    ]
    static = [b for k, b in blocks if k == "static"]
    assert len(static) == 1
    assert re.search(r"security_groups\s*=\s*\[aws_security_group\.vector_clients\.id\]", static[0])
    assert "cidr_blocks" not in static[0]
    operator = [b for k, b in blocks if "var.operator_cidr_blocks" in b]
    assert len(operator) == 1 and operator[0] in [b for k, b in blocks if k == "dynamic"]
    # Nada más abre este puerto.
    assert len(blocks) == 3


def test_operator_and_ssh_variables_require_single_host_cidrs() -> None:
    text = (VECTOR_DIR / "variables.tf").read_text()
    for name in ("operator_cidr_blocks", "ssh_cidr_blocks"):
        match = re.search(rf'variable "{name}"', text)
        assert match
        block = _block(text, match.end())
        assert re.search(r"default\s*=\s*\[\]", block)
        assert '/32$", c)' in block  # regex solo IPv4 con máscara /32
        assert '!startswith(c, "0.0.0.0/")' in block


def test_vector_stack_exposes_private_ips_and_clients_sg() -> None:
    outputs = (VECTOR_DIR / "outputs.tf").read_text()
    for name, value in (
        ("chromadb_private_ip", "aws_instance.chromadb.private_ip"),
        ("ollama_private_ip", "aws_instance.ollama.private_ip"),
        ("vector_clients_security_group_id", "aws_security_group.vector_clients.id"),
    ):
        match = re.search(rf'output "{name}"', outputs)
        assert match, name
        assert value in _block(outputs, match.end())


def test_backend_service_attaches_vector_clients_sg() -> None:
    ecs = (RAG_INFRA / "ecs.tf").read_text()
    match = re.search(r'resource "aws_ecs_service" "app"', ecs)
    assert match
    service = _block(ecs, match.end())
    assert "var.vector_clients_security_group_id" in service
    assert "data.aws_security_group.vector_clients[0].id" in service
    data = (RAG_INFRA / "data.tf").read_text()
    block = _block(data, re.search(r'data "aws_security_group" "vector_clients"', data).end())
    assert "vpc_id = data.aws_vpc.default.id" in re.sub(r"\s+", " ", block)
    frontend = _block(ecs, re.search(r'resource "aws_ecs_service" "frontend"', ecs).end())
    assert "vector_clients" not in frontend  # el frontend no necesita llegar a Chroma/Ollama


# ---------------------------------------------------------------------------
# deploy.sh: IP privada para ECS, IP pública para el operador
# ---------------------------------------------------------------------------

FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "terraform $*" >> "${CALL_LOG:-/dev/null}"
case "$*" in
  *" output -raw chromadb_public_ip"*) echo "54.0.0.1" ;;
  *" output -raw ollama_public_ip"*) echo "54.0.0.2" ;;
  *" output -raw chromadb_private_ip"*) echo "${FAKE_CHROMA_PRIVATE-172.31.1.1}" ;;
  *" output -raw ollama_private_ip"*) echo "172.31.1.2" ;;
  *" output -raw vector_clients_security_group_id"*) echo "sg-0123456789abcdef0" ;;
  *" output -raw legacy_public_service_access"*) echo "${FAKE_LEGACY-false}" ;;
  *" output -raw operator_cidr_blocks_count"*) echo "1" ;;
esac
exit 0
"""


def _run_vector_deploy(
    tmp_path: Path,
    env_extra: dict[str, str] | None = None,
    vector_tfvars: str | None = "legacy_public_service_access = false\n",
) -> tuple:
    repo = tmp_path / "repo"
    script = repo / VECTOR_DEPLOY.relative_to(REPO_ROOT)
    script.parent.mkdir(parents=True)
    shutil.copy2(VECTOR_DEPLOY, script)
    # deploy.sh carga la biblioteca compartida de scripts/lib/.
    lib = repo / "scripts" / "lib" / "terraform_state.sh"
    lib.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "lib" / "terraform_state.sh", lib)
    if vector_tfvars is not None:
        (script.parent.parent / "terraform.tfvars").write_text(vector_tfvars)
    infra = repo / "rag" / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "terraform.tfvars").write_text('postgres_password = "a"\njwt_secret_key = "b"\n')
    (repo / "rag" / ".env").write_text("CHROMA_HOST=localhost\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("terraform", FAKE_TERRAFORM), ("curl", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "TFSTATE_BACKUP_DIR": str(tmp_path / "backups"),
        "CALL_LOG": str(tmp_path / "calls.log"),
        **(env_extra or {}),
    }
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(script)],
            stdin=slave,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        os.close(slave)
        os.close(master)
    return result, (infra / "terraform.tfvars").read_text(), (repo / "rag" / ".env").read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_deploy_writes_private_ips_for_ecs_and_public_for_operator(tmp_path: Path) -> None:
    result, tfvars, env_file = _run_vector_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'chroma_host = "172.31.1.1"' in tfvars
    assert 'ollama_base_url = "http://172.31.1.2:11434"' in tfvars
    assert 'vector_clients_security_group_id = "sg-0123456789abcdef0"' in tfvars
    assert "54.0.0." not in tfvars
    assert "CHROMA_HOST=54.0.0.1" in env_file
    assert "OLLAMA_BASE_URL=http://54.0.0.2:11434" in env_file


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_deploy_stops_if_private_ip_output_is_missing(tmp_path: Path) -> None:
    result, tfvars, _ = _run_vector_deploy(tmp_path, {"FAKE_CHROMA_PRIVATE": ""})
    assert result.returncode != 0
    assert "CHROMA_PRIVATE_IP" in result.stderr
    assert "chroma_host" not in tfvars


# ---------------------------------------------------------------------------
# legacy_public_service_access: sin valor por defecto, nunca reabre por omisión
# ---------------------------------------------------------------------------


def test_legacy_public_access_has_no_default() -> None:
    text = (VECTOR_DIR / "variables.tf").read_text()
    block = _block(text, re.search(r'variable "legacy_public_service_access"', text).end())
    assert "default" not in block
    assert re.search(r"nullable\s*=\s*false", block)
    example = (VECTOR_DIR / "terraform.tfvars.example").read_text()
    assert re.search(r"^legacy_public_service_access\s*=\s*false$", example, flags=re.M)


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_clean_clone_without_tfvars_stops_before_terraform(tmp_path: Path) -> None:
    result, tfvars, _ = _run_vector_deploy(tmp_path, vector_tfvars=None)
    assert result.returncode != 0
    assert "legacy_public_service_access" in result.stderr
    assert not (tmp_path / "calls.log").exists() or (tmp_path / "calls.log").read_text() == ""
    assert "chroma_host" not in tfvars


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_after_phase_3_tfvars_keeps_public_access_closed(tmp_path: Path) -> None:
    """Con el tfvars de la fase 3 (false), deploy.sh sigue adelante y no avisa
    de acceso público; el valor llega a Terraform desde ese archivo."""
    result, _, _ = _run_vector_deploy(
        tmp_path, vector_tfvars="legacy_public_service_access = false\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "siguen abiertos a 0.0.0.0/0" not in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_transitional_public_access_is_warned(tmp_path: Path) -> None:
    result, _, _ = _run_vector_deploy(
        tmp_path,
        env_extra={"FAKE_LEGACY": "true"},
        vector_tfvars="legacy_public_service_access = true\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "siguen abiertos a 0.0.0.0/0" in result.stdout
