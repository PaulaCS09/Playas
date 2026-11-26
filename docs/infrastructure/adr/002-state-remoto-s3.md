# ADR-002 — Terraform State remoto en S3, una key por stack

- **Estado:** Aceptado (2026-11-04)
- **Ámbito:** los tres stacks, `scripts/lib/terraform_state.sh`, `scripts/bootstrap_tfstate_bucket.sh`

## Contexto

Antes, los tres stacks guardaban el State en local (`terraform.tfstate` en el
clon), sin bloqueo ni versionado, y fijaban `required_version = ">= 1.7"`.
Perder el clon o el portátil significaba perder el State; dos ejecuciones
simultáneas podían corromperlo. El State puede contener secretos aunque los
outputs estén marcados como `sensitive`.

## Decisión

- Backend `s3` en los tres `terraform.tf`, región `us-east-1`, `encrypt = true`.
- **Un bucket por cuenta** (`rag-playas-tfstate-<cuenta>`) y **una key fija por
  stack** en el propio `terraform.tf`: `ingesta/…`, `vector-infraestructura/…`,
  `rag-infrastructure/terraform.tfstate`. `backend.hcl` (no versionado) solo
  aporta `bucket`, así ningún stack puede apuntar a la key de otro.
- **Lock nativo de S3** (`use_lockfile = true`, archivo `<key>.tflock`). Exige
  Terraform ≥ 1.10, y el repo fija `~> 1.17.1`, también en la CI.
- Bucket endurecido por `bootstrap`: versionado, SSE-S3, Block Public Access,
  BucketOwnerEnforced y una política que deniega las peticiones sin TLS.
- Protecciones en los scripts: se rechazan `AWS_ENDPOINT_URL*`, `TF_WORKSPACE`
  y los workspaces distintos de `default`. Un State vacío exige
  `ALLOW_EMPTY_STATE=1` y se comprueba que no tuvo versiones anteriores ni que
  existan ya recursos del stack. Se hace una copia (600) antes de cada apply.
- Respaldo **cifrado con GPG fuera de AWS** (`playas.sh backup-state`), sin
  escribir nunca el State en claro en disco.

## Alternativas descartadas

- **Tabla DynamoDB para el lock:** un recurso más que crear y mantener por
  cuenta; el lock nativo de S3 cubre el caso con la versión fijada.
- **Un bucket por stack:** triplica el bootstrap sin aislar mejor; las keys
  fijas ya separan los stacks.
- **Terraform Cloud / OIDC:** OIDC no está disponible en el Learner Lab.

## Consecuencias

- La migración del State local al bucket es manual y se documenta en el README
  raíz. Ningún script ejecuta `-migrate-state`, `state push/rm` ni `force-unlock`.
- Si la sesión del lab caduca durante un apply, puede quedar un `.tflock`
  huérfano: `status` lo muestra y liberarlo es manual.

## Verificación

- `rag/tests/unit/test_bootstrap_tfstate_bucket.py`: orden seguro
  de creación, política TLS, confirmación explícita, modo `--verify-only` sin
  escrituras.
- `test_deploy_scripts_safety.py`: `test_s3_backend_*`, `test_empty_remote_state_guard_*`,
  `test_backend_example_and_stack_keys_are_consistent`,
  `test_gitignore_keeps_states_out_but_tracks_backend_code`.
