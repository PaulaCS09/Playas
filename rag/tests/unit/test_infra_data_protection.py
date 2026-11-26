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
