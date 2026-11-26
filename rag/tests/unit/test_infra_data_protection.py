"""Protección de los datos persistentes de la infraestructura (ADR-005, D1).

Comprobación estática de los .tf: los recursos que guardan datos que no se
reconstruyen solos deben conservar ``prevent_destroy = true``, para que ningún
``destroy`` ni reemplazo accidental llegue a ejecutarse. No se ejecuta
Terraform ni AWS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

PROTECTED_RESOURCES = [
    pytest.param(
        "rag/infrastructure/efs.tf",
        "aws_efs_file_system",
        "postgres",
        id="efs-postgres",
    ),
    pytest.param(
        "vector-infraestructura/chromadb.tf",
        "aws_instance",
        "chromadb",
        id="ec2-chromadb",
    ),
    pytest.param(
        "vector-infraestructura/ollama.tf",
        "aws_instance",
        "ollama",
        id="ec2-ollama",
    ),
    pytest.param(
        "ingesta/infrastructure/s3.tf",
        "aws_s3_bucket",
        "data",
        id="s3-datos",
    ),
]


def _strip_comments(text: str) -> str:
    """Quita comentarios ``#`` y ``//`` de línea completa o al final de línea
    (estos .tf no tienen esas secuencias dentro de cadenas)."""
    return "\n".join(re.split(r"\s(?:#|//)|^\s*(?:#|//)", line)[0] for line in text.splitlines())


def _resource_block(text: str, rtype: str, name: str) -> str:
    match = re.search(rf'resource\s+"{rtype}"\s+"{name}"\s*\{{', text)
    assert match, f'No se encontró resource "{rtype}" "{name}"'
    depth = 0
    for idx in range(match.end() - 1, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : idx + 1]
    raise AssertionError(f'Bloque sin cerrar en resource "{rtype}" "{name}"')


@pytest.mark.parametrize(("path", "rtype", "name"), PROTECTED_RESOURCES)
def test_persistent_data_resources_keep_prevent_destroy(path: str, rtype: str, name: str) -> None:
    text = _strip_comments((REPO_ROOT / path).read_text(encoding="utf-8"))
    block = _resource_block(text, rtype, name)
    lifecycle = re.search(r"lifecycle\s*\{[^}]*\}", block)
    assert lifecycle, f"{path}: {rtype}.{name} no tiene bloque lifecycle"
    assert re.search(r"prevent_destroy\s*=\s*true\b", lifecycle.group(0)), (
        f"{path}: {rtype}.{name} debe conservar prevent_destroy = true"
    )


def test_comment_does_not_count_as_protection() -> None:
    """Un ``prevent_destroy`` comentado no debe contar como protección."""
    text = _strip_comments(
        'resource "aws_s3_bucket" "data" {\n  lifecycle {\n    # prevent_destroy = true\n  }\n}\n'
    )
    block = _resource_block(text, "aws_s3_bucket", "data")
    assert not re.search(r"prevent_destroy\s*=\s*true", block)


# S4: un cambio de AMI o de user_data reemplazaría las EC2 de vector (y con
# ellas los datos de ChromaDB y los modelos de Ollama); con prevent_destroy el
# plan fallaría. Terraform debe ignorar esos dos atributos.
VECTOR_INSTANCES = [
    pytest.param("vector-infraestructura/chromadb.tf", "chromadb", id="ec2-chromadb"),
    pytest.param("vector-infraestructura/ollama.tf", "ollama", id="ec2-ollama"),
]


def _ignore_changes(block: str) -> set[str] | None:
    lifecycle = re.search(r"lifecycle\s*\{[^}]*\}", block)
    if not lifecycle:
        return None
    match = re.search(r"ignore_changes\s*=\s*\[([^\]]*)\]", lifecycle.group(0))
    if not match:
        return None
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


@pytest.mark.parametrize(("path", "name"), VECTOR_INSTANCES)
def test_vector_instances_ignore_ami_and_user_data(path: str, name: str) -> None:
    text = _strip_comments((REPO_ROOT / path).read_text(encoding="utf-8"))
    block = _resource_block(text, "aws_instance", name)
    ignored = _ignore_changes(block)
    assert ignored is not None, f"{path}: aws_instance.{name} sin lifecycle.ignore_changes"
    assert {"ami", "user_data"} <= ignored, (
        f"{path}: aws_instance.{name} debe ignorar ami y user_data (tiene {sorted(ignored)})"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("ignore_changes = [user_data, ami]", {"ami", "user_data"}, id="orden-inverso"),
        pytest.param(
            "ignore_changes = [\n  ami,\n  user_data,\n]", {"ami", "user_data"}, id="multilinea"
        ),
        pytest.param("# ignore_changes = [ami, user_data]", None, id="comentado"),
        pytest.param("ignore_changes = [ami]", {"ami"}, id="solo-ami"),
    ],
)
def test_ignore_changes_parser(body: str, expected: set[str] | None) -> None:
    text = _strip_comments(
        f'resource "aws_instance" "x" {{\n  lifecycle {{\n    prevent_destroy = true\n    {body}\n  }}\n}}\n'
    )
    assert _ignore_changes(_resource_block(text, "aws_instance", "x")) == expected
