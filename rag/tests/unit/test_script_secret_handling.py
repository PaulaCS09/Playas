"""Manejo de secretos en scripts de despliegue (Fase 1, tarea A2).

Comprueba que:

- ningún script del repositorio imprime en consola variables con secretos;
- ``vector-infraestructura/scripts/deploy.sh`` genera los secretos sin
  mostrarlos y deja ``terraform.tfvars`` y ``rag/.env`` con permisos 600;
- ``rag/infrastructure/scripts/export_postgres.sh`` deja el dump local con
  permisos 600 y firma la URL de subida con una validez corta.

Nunca se ejecutan Terraform, AWS ni ECS reales: los scripts se copian a un
directorio temporal y las herramientas externas se sustituyen por
ejecutables falsos que solo registran sus argumentos.
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
EXPORT_POSTGRES = REPO_ROOT / "rag" / "infrastructure" / "scripts" / "export_postgres.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")

_EXCLUDED_DIRS = {".git", ".venv", "node_modules", ".terraform", ".next"}

# Detector heurístico (no es un analizador de bash): busca sentencias que
# imprimen en consola una variable cuyo nombre indica un secreto. Complementa,
# no sustituye, a los tests de comportamiento que buscan el valor real en la salida.
_PRINT_CMDS = r"(?:echo|printf|log|ok|warn|err|fail|step|info)"
_STATEMENT_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\bthen\b|\bdo\b|\belse\b)\s*")
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_SECRET_NAME = re.compile(r"password|passwd|secret|token|api_?key", re.I)
# Nombres que apuntan a un secreto sin contenerlo.
_NOT_SECRET_SUFFIX = re.compile(r"_(?:FILE|PATH|DIR|NAME|ENV|VAR)$", re.I)
# Variables que en estos scripts contienen secretos aunque su nombre no lo diga.
_KNOWN_SECRET_VARS = {"KEY_VALUE", "generated"}
_CONSOLE_REDIRECT = re.compile(r">&\s*[12]\b|>\s*/dev/(?:stdout|stderr|tty)\b|\|\s*tee\b")
_FILE_OR_PIPE = re.compile(r"(?<![<>&])>{1,2}\s*[^&\s]|\|\s*\S")


def _shell_scripts() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.sh")
        if not _EXCLUDED_DIRS.intersection(path.relative_to(REPO_ROOT).parts)
    )


def _is_secret_var(name: str) -> bool:
    if name in _KNOWN_SECRET_VARS:
        return True
    return bool(_SECRET_NAME.search(name)) and not _NOT_SECRET_SUFFIX.search(name)


def _prints_secret_to_console(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return False
    for statement in _STATEMENT_SPLIT.split(stripped):
        if not re.match(rf"{_PRINT_CMDS}\b", statement):
            continue
        if not any(_is_secret_var(name) for name in _VAR_REF.findall(statement)):
            continue
        if _CONSOLE_REDIRECT.search(statement) or not _FILE_OR_PIPE.search(statement):
            return True
    return False


def test_secret_detector_examples() -> None:
    printed = [
        'echo -e "  postgres_password = ${PG_PASSWORD}"',
        'ok "jwt: $JWT_SECRET"',
        'echo "${generated}"',  # la fuga original de auto_generate_secret
        'echo "${PG_PASSWORD}" >&2',
        'err "fallo: ${DB_PASSWORD}"',
        'fail "${JWT_SECRET}"',
        'echo x > out; echo "${API_KEY}"',
        'echo "${OPENAI_API_KEY}" | tee /dev/stderr',
        'echo "${openai_api_key}"',
        'echo "${TOKEN}" > /dev/stderr',
    ]
    not_printed = [
        'echo "openai_api_key = \\"${KEY_VALUE}\\"" >> "${TFVARS}"',
        "printf '%s' \"${OPENAI_API_KEY}\" | ssh host 'cat > f'",
        '# echo "${PG_PASSWORD}"',
        'echo "${CHROMA_HOST_VAL}"',
        'echo "usando ${PASSWORD_FILE}"',
        'ok "${key}: generado"',
    ]
    for line in printed:
        assert _prints_secret_to_console(line), line
    for line in not_printed:
        assert not _prints_secret_to_console(line), line


def test_no_script_prints_secret_variables() -> None:
    scripts = _shell_scripts()
    assert VECTOR_DEPLOY in scripts
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in scripts
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _prints_secret_to_console(line)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Ejecución con herramientas falsas
# ---------------------------------------------------------------------------

FAKE_TOOLS = {
    "terraform": """#!/usr/bin/env bash
echo "terraform $*" >> "${CALL_LOG}"
case "$*" in
  *" output -raw chromadb_public_ip"*) echo "10.0.0.1" ;;
  *" output -raw ollama_public_ip"*) echo "10.0.0.2" ;;
  *" output -raw chromadb_private_ip"*) echo "172.31.1.1" ;;
  *" output -raw ollama_private_ip"*) echo "172.31.1.2" ;;
  *" output -raw vector_clients_security_group_id"*) echo "sg-0123456789abcdef0" ;;
esac
exit 0
""",
    "curl": """#!/usr/bin/env bash
echo "curl $*" >> "${CALL_LOG}"
exit 0
""",
    "uv": """#!/usr/bin/env bash
printf 'uv %s\\n' "$*" >> "${CALL_LOG}"
echo "https://bucket.example.invalid/firmada"
""",
    "aws": """#!/usr/bin/env bash
echo "aws $*" >> "${CALL_LOG}"
case "$*" in
  *"ecs list-tasks"*) echo "arn:aws:ecs:us-east-1:000000000000:task/c/abc" ;;
  *"ecs execute-command"*) echo "HTTP_STATUS:200" ;;
  *"s3 cp"*) echo "dump" > "$4" ;;
esac
""",
}


def _install_fakes(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in FAKE_TOOLS.items():
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(0o755)


def _copy_script(src: Path, root: Path) -> Path:
    dest = root / src.relative_to(REPO_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _run(script: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """Ejecuta con una pseudo-terminal en stdin (los deploy exigen terminal)."""
    bin_dir = tmp_path / "bin"
    _install_fakes(bin_dir)
    call_log = tmp_path / "calls.log"
    call_log.touch()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "CALL_LOG": str(call_log),
        "TFSTATE_BACKUP_DIR": str(tmp_path / "backups"),
        # A5: el deploy de vector exige fijar el acceso público transitorio.
        "TF_VAR_legacy_public_service_access": "false",
    }
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(script), *args],
            stdin=slave,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        os.close(slave)
        os.close(master)
    return result, call_log.read_text()


def _tfvars_value(text: str, key: str) -> str:
    match = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, flags=re.M)
    assert match, key
    return match.group(1)


@pytest.fixture
def vector_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    script = _copy_script(VECTOR_DEPLOY, repo)
    infra = repo / "rag" / "infrastructure"
    infra.mkdir(parents=True)
    example = infra / "terraform.tfvars.example"
    example.write_text(
        'postgres_password = "cambia-esto"\njwt_secret_key = "cambia-esto"\nchroma_host = ""\n'
    )
    example.chmod(0o644)
    env_file = repo / "rag" / ".env"
    env_file.write_text("CHROMA_HOST=localhost\n")
    env_file.chmod(0o644)
    return script


@pytest.mark.skipif(shutil.which("openssl") is None, reason="requiere openssl")
def test_vector_deploy_generates_secrets_without_printing_them(
    vector_repo: Path, tmp_path: Path
) -> None:
    result, _ = _run(vector_repo, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    repo = tmp_path / "repo"
    tfvars = repo / "rag" / "infrastructure" / "terraform.tfvars"
    text = tfvars.read_text()
    pg_password = _tfvars_value(text, "postgres_password")
    jwt_secret = _tfvars_value(text, "jwt_secret_key")
    assert re.fullmatch(r"[0-9a-f]{32}", pg_password)
    assert re.fullmatch(r"[0-9a-f]{64}", jwt_secret)

    output = result.stdout + result.stderr
    assert pg_password not in output
    assert jwt_secret not in output
    assert "postgres_password: generado" in output
    assert "no se muestran" in output

    assert stat.S_IMODE(tfvars.stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "rag" / ".env").stat().st_mode) == 0o600


@pytest.mark.skipif(shutil.which("openssl") is None, reason="requiere openssl")
def test_vector_deploy_keeps_existing_secrets_and_tightens_permissions(
    vector_repo: Path, tmp_path: Path
) -> None:
    tfvars = tmp_path / "repo" / "rag" / "infrastructure" / "terraform.tfvars"
    existing = "a" * 32
    tfvars.write_text(f'postgres_password = "{existing}"\njwt_secret_key = "{"b" * 64}"\n')
    tfvars.chmod(0o644)

    result, _ = _run(vector_repo, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tfvars_value(tfvars.read_text(), "postgres_password") == existing
    assert existing not in result.stdout + result.stderr
    assert "postgres_password: ya configurado" in result.stdout
    assert stat.S_IMODE(tfvars.stat().st_mode) == 0o600


def test_export_postgres_restricts_dump_and_presign_expiry(tmp_path: Path) -> None:
    script = _copy_script(EXPORT_POSTGRES, tmp_path / "repo")
    out_dir = tmp_path / "out"

    result, calls = _run(
        script, tmp_path, "--bucket", "bucket-prueba", "--output-dir", str(out_dir)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    dumps = list(out_dir.glob("atlas-backup-*.dump"))
    assert len(dumps) == 1
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert "ExpiresIn=600" in calls
    assert "ExpiresIn=1800" not in calls
