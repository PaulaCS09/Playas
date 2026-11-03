"""Límite de login detrás del ALB: IP real del cliente y proxy de confianza.

Se monta el router real de ``/api/auth`` dentro del mismo
``ProxyHeadersMiddleware`` que uvicorn aplica en producción, con
``trusted_hosts`` igual al CIDR de la VPC. Las peticiones simulan llegar desde
una IP de la VPC (el ALB) o desde fuera de ella. No hay base de datos ni red:
la sesión y la verificación de contraseña se sustituyen por dobles.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from rag.api import rate_limit as rate_limit_module
from rag.api.database import get_session
from rag.api.rate_limit import SlidingWindowRateLimiter
from rag.api.routes import auth as auth_routes

VPC_CIDR = "172.31.0.0/16"
ALB_NODE_IP = "172.31.10.20"  # IP privada de un nodo del ALB dentro de la VPC
OUTSIDE_IP = "198.51.100.9"  # conexión directa desde fuera de la VPC
MAX_ATTEMPTS = 3
EMAIL = "persona@example.com"

_RAG_DIR = Path(__file__).resolve().parents[2]
_INFRA_DIR = _RAG_DIR / "infrastructure"
_BACKEND_DIR = _RAG_DIR / "backend"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()


async def _fake_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> SlidingWindowRateLimiter:
    instance = SlidingWindowRateLimiter(
        mode="enforce",
        max_requests=MAX_ATTEMPTS,
        window_seconds=300,
        scope="login",
        detail="Demasiados intentos de inicio de sesión.",
    )
    monkeypatch.setattr(auth_routes, "login_rate_limiter", instance)

    async def _always_wrong(_password: str, _hashed: str) -> bool:
        return False

    monkeypatch.setattr(auth_routes, "verify_password_async", _always_wrong)
    return instance


def _client(peer_ip: str) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _fake_session
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=VPC_CIDR)
    transport = httpx.ASGITransport(app=wrapped, client=(peer_ip, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://alb.example")


async def _login(client: httpx.AsyncClient, xff: str | None = None, email: str = EMAIL) -> int:
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "incorrecta"}, headers=headers
    )
    return response.status_code


# ---------------------------------------------------------------------------
# IP real del cliente detrás del ALB
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_limit_applies_per_real_client_ip_behind_alb(
    limiter: SlidingWindowRateLimiter,
) -> None:
    async with _client(ALB_NODE_IP) as client:
        codes = [await _login(client, xff="203.0.113.7") for _ in range(MAX_ATTEMPTS + 1)]
        assert codes == [401] * MAX_ATTEMPTS + [429]
        # La misma cuenta desde otro cliente real no queda bloqueada.
        assert await _login(client, xff="203.0.113.8") == 401


@pytest.mark.anyio
async def test_attacker_cannot_lock_out_victim_from_another_ip(
    limiter: SlidingWindowRateLimiter,
) -> None:
    async with _client(ALB_NODE_IP) as client:
        for _ in range(MAX_ATTEMPTS + 2):
            await _login(client, xff="192.0.2.66")  # atacante
        assert await _login(client, xff="203.0.113.7") == 401  # víctima


@pytest.mark.anyio
async def test_spoofed_leftmost_forwarded_for_does_not_bypass_limit(
    limiter: SlidingWindowRateLimiter,
) -> None:
    """El cliente controla el inicio de X-Forwarded-For; el ALB añade al final
    la IP real. Rotar el valor falso no debe dar intentos nuevos."""
    async with _client(ALB_NODE_IP) as client:
        codes = [
            await _login(client, xff=f"10.9.9.{n}, 203.0.113.7") for n in range(MAX_ATTEMPTS + 1)
        ]
        assert codes[-1] == 429


@pytest.mark.anyio
async def test_forwarded_for_from_untrusted_peer_is_ignored(
    limiter: SlidingWindowRateLimiter,
) -> None:
    """Fuera de la VPC, X-Forwarded-For no cuenta: se usa la IP de la conexión."""
    async with _client(OUTSIDE_IP) as client:
        codes = [await _login(client, xff=f"203.0.113.{n}") for n in range(MAX_ATTEMPTS + 1)]
        assert codes[-1] == 429


@pytest.mark.anyio
async def test_rate_limit_key_uses_client_ip_resolved_by_proxy_middleware(
    limiter: SlidingWindowRateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = auth_routes._login_rate_limit_key

    def _spy(email: str, request: Any) -> str:
        seen.append(request.client.host)
        return original(email, request)

    monkeypatch.setattr(auth_routes, "_login_rate_limit_key", _spy)
    async with _client(ALB_NODE_IP) as client:
        await _login(client, xff="198.18.0.1, 203.0.113.7")
    async with _client(OUTSIDE_IP) as client:
        await _login(client, xff="203.0.113.7")
    assert seen == ["203.0.113.7", OUTSIDE_IP]


@pytest.mark.anyio
async def test_trusted_peer_without_forwarded_for_keeps_the_alb_ip(
    limiter: SlidingWindowRateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin X-Forwarded-For (p. ej. el health check) no hay IP de cliente que
    resolver: se usa la del par. El login real siempre llega con la cabecera
    que añade el ALB; este caso queda explícito."""
    seen: list[str] = []
    original = auth_routes._login_rate_limit_key

    def _spy(email: str, request: Any) -> str:
        seen.append(request.client.host)
        return original(email, request)

    monkeypatch.setattr(auth_routes, "_login_rate_limit_key", _spy)
    async with _client(ALB_NODE_IP) as client:
        await _login(client)
        await _login(client, xff="")
    assert seen == [ALB_NODE_IP, ALB_NODE_IP]


# ---------------------------------------------------------------------------
# Límite de intentos y modo observe
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_limit_is_isolated_per_email_for_the_same_ip(
    limiter: SlidingWindowRateLimiter,
) -> None:
    async with _client(ALB_NODE_IP) as client:
        for _ in range(MAX_ATTEMPTS + 1):
            await _login(client, xff="203.0.113.7")
        assert await _login(client, xff="203.0.113.7", email="otra@example.com") == 401


@pytest.mark.anyio
async def test_429_includes_retry_after(limiter: SlidingWindowRateLimiter) -> None:
    async with _client(ALB_NODE_IP) as client:
        for _ in range(MAX_ATTEMPTS):
            await _login(client, xff="203.0.113.7")
        response = await client.post(
            "/api/auth/login",
            json={"email": EMAIL, "password": "x"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.anyio
async def test_observe_mode_never_blocks_and_logs_without_personal_data(
    limiter: SlidingWindowRateLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    limiter.mode = "observe"
    with caplog.at_level(logging.WARNING, logger=rate_limit_module.__name__):
        async with _client(ALB_NODE_IP) as client:
            codes = [await _login(client, xff="203.0.113.7") for _ in range(MAX_ATTEMPTS + 2)]
    assert codes == [401] * (MAX_ATTEMPTS + 2)
    records = [r for r in caplog.records if getattr(r, "scope", None) == "login"]
    assert records
    for record in records:
        text = record.getMessage() + str(record.__dict__)
        assert EMAIL not in text
        assert "203.0.113.7" not in text


# ---------------------------------------------------------------------------
# Configuración: FORWARDED_ALLOW_IPS nunca "*"
# ---------------------------------------------------------------------------


def _import_config(env_value: str | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    # .env vacío propio: nunca se carga el .env real del desarrollador.
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    env = {"PATH": "/usr/bin:/bin", "RAG_ENV_FILE": str(env_file)}
    if env_value is not None:
        env["FORWARDED_ALLOW_IPS"] = env_value
    return subprocess.run(
        [sys.executable, "-c", "from rag import config; print(config.FORWARDED_ALLOW_IPS)"],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "value",
    [
        "*",
        " * ",
        "172.31.0.0/16, *",
        "0.0.0.0/0",
        "::/0",
        # Equivalentes o casi equivalentes a "todo Internet" que no son literales.
        "0.0.0.0/1,128.0.0.0/1",
        "0.0.0.0/00",
        "0.0.0.0/0.0.0.0",
        "8.8.8.8",
        "172.31.0.0/16,52.0.0.0/8",
    ],
)
def test_unsafe_forwarded_allow_ips_fails_at_startup(value: str, tmp_path: Path) -> None:
    result = _import_config(value, tmp_path)
    assert result.returncode != 0
    assert "FORWARDED_ALLOW_IPS" in result.stderr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "127.0.0.1"),
        ("172.31.0.0/16", "172.31.0.0/16"),
        ("10.0.0.0/8,127.0.0.1", "10.0.0.0/8,127.0.0.1"),
        # CIDR secundario de VPC en el espacio compartido (no global).
        ("172.31.0.0/16,100.64.0.0/16", "172.31.0.0/16,100.64.0.0/16"),
        ("::1", "::1"),
    ],
)
def test_safe_forwarded_allow_ips_is_accepted(
    value: str | None, expected: str, tmp_path: Path
) -> None:
    result = _import_config(value, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == expected


def test_ecs_task_trusts_only_the_vpc_cidr_and_starts_in_observe() -> None:
    ecs = (_INFRA_DIR / "ecs.tf").read_text()
    forwarded = re.findall(r'name\s*=\s*"FORWARDED_ALLOW_IPS",\s*value\s*=\s*(.+?)\s*},?\n', ecs)
    # Todos los CIDR de la VPC (primario y secundarios), nunca "*".
    assert forwarded == [
        'join(",", [for assoc in data.aws_vpc.default.cidr_block_associations : assoc.cidr_block])'
    ]
    assert re.search(
        r'name\s*=\s*"AUTH_RATE_LIMIT_MODE",\s*value\s*=\s*var\.auth_rate_limit_mode', ecs
    )
    variables = (_INFRA_DIR / "variables.tf").read_text()
    block = re.search(r'variable "auth_rate_limit_mode" \{(.*?)\n\}', variables, flags=re.S)
    assert block and re.search(r'default\s*=\s*"observe"', block.group(1))


def test_backend_image_enables_proxy_headers_without_wildcard() -> None:
    dockerfile = (_RAG_DIR / "docker" / "Dockerfile.backend").read_text()
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert '"--proxy-headers"' in cmd
    assert "*" not in cmd


# ---------------------------------------------------------------------------
# Registro de la IP en logs: solo la IP resuelta, sin secretos ni cuerpos
# ---------------------------------------------------------------------------


def test_backend_logs_retention_is_at_most_14_days() -> None:
    ecs = (_INFRA_DIR / "ecs.tf").read_text()
    retentions = {
        name: int(days)
        for name, days in re.findall(
            r'resource "aws_cloudwatch_log_group" "(\w+)" \{(?:(?!\nresource ).)*?'
            r"retention_in_days\s*=\s*(\d+)",
            ecs,
            flags=re.S,
        )
    }
    assert set(retentions) == {"app", "frontend"}
    assert all(days <= 14 for days in retentions.values()), retentions


def test_real_uvicorn_access_log_has_client_ip_but_no_secrets_or_forwarded_chain(
    limiter: SlidingWindowRateLimiter,
) -> None:
    """Arranca un uvicorn real en localhost (sin AWS) con --proxy-headers y
    127.0.0.1 como proxy de confianza, y revisa la línea de access log."""
    import asyncio
    import socket
    import threading
    import time

    import uvicorn

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _fake_session

    # Socket ya enlazado y entregado a uvicorn: sin carrera por el puerto.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    access_logger = logging.getLogger("uvicorn.access")
    handler = _Collector()
    access_logger.addHandler(handler)
    previous_level = access_logger.level
    access_logger.setLevel(logging.INFO)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=None,
        access_log=True,
        lifespan="off",
        ws="none",
    )
    server = uvicorn.Server(config)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(server.serve(sockets=[sock]))
        except BaseException as exc:  # el test falla rápido y con la causa
            errors.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not errors, errors
        assert server.started

        password = "contraseña-que-no-debe-aparecer"
        bearer = "Bearer jwt.que.no.debe.aparecer"
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/auth/login",
            json={"email": EMAIL, "password": password},
            headers={
                "Authorization": bearer,
                "X-Forwarded-For": "10.66.66.66, 203.0.113.7",
            },
            timeout=10,
            trust_env=False,  # ignora HTTP(S)_PROXY del runner
        )
        assert response.status_code == 401
        deadline = time.monotonic() + 5
        while not records and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        access_logger.removeHandler(handler)
        access_logger.setLevel(previous_level)
    assert not thread.is_alive()

    lines = [record.getMessage() for record in records]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "203.0.113.7" in line  # IP real del cliente
    assert "POST /api/auth/login" in line
    for forbidden in (password, bearer, "jwt.que.no", "10.66.66.66", EMAIL, "Authorization"):
        assert forbidden not in line, (forbidden, line)


@pytest.mark.anyio
async def test_rehash_failure_log_omits_hash_and_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Si falla el commit del re-hash, el log no debe volcar el error de
    SQLAlchemy, que incluiría el nuevo password_hash en sus parámetros."""
    new_hash = "v2:$2b$12$hash-que-no-debe-aparecer-en-logs"

    class _User:
        id = "user-1"
        password_hash = "legacy"

    class _FailingSession:
        rolled_back = False

        async def commit(self) -> None:
            raise RuntimeError(f"[parameters: ('{new_hash}', 'user-1')]")

        async def rollback(self) -> None:
            self.rolled_back = True

    async def _fake_hash(_password: str) -> str:
        return new_hash

    session = _FailingSession()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_routes, "hash_password_async", _fake_hash)
        with caplog.at_level(logging.WARNING, logger=auth_routes.__name__):
            await auth_routes._opportunistically_rehash(_User(), "secreta", session)  # type: ignore[arg-type]

    assert session.rolled_back
    records = [
        r for r in caplog.records if "opportunistic_password_rehash_failed" in r.getMessage()
    ]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is None
    assert "RuntimeError" in record.getMessage()
    assert new_hash not in record.getMessage()
    assert "secreta" not in record.getMessage()
