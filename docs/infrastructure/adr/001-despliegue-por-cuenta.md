# ADR-001 — Despliegue nuevo en la cuenta de cada integrante

- **Estado:** Aceptado (2026-11-01)
- **Ámbito:** los tres stacks

## Contexto

El proyecto se despliega en AWS Academy Learner Lab. Cada integrante tiene su
propia cuenta, con credenciales temporales, `LabRole` y sin permisos para crear
roles IAM. Las cuentas de otros integrantes no son accesibles, y una sesión del
lab puede reiniciarse. El State de los despliegues anteriores era local y no
estaba disponible.

## Decisión

- Cada integrante despliega la plataforma completa en **su** cuenta, como
  despliegue nuevo (greenfield). No se importan recursos ni States ajenos.
- Se conservan los tres stacks existentes (`ingesta/infrastructure`,
  `vector-infraestructura`, `rag/infrastructure`). No se mueven archivos
  Terraform ni States.
- Los datos se reconstruyen en cada despliegue: el índice de ChromaDB desde
  `data/*.jsonl.gz` y los usuarios desde `data/seed_users.sql`.
- HTTP en el ALB es un **riesgo aceptado** (sin dominio ni certificado en el lab).

## Alternativas descartadas

- **Importar los recursos de otra cuenta:** imposible sin acceso a esa cuenta.
- **Reorganizar los stacks en módulos nuevos:** obligaría a migrar o mover
  States y a tocar código de otros integrantes, sin beneficio verificable.

## Consecuencias

- Los nombres de los buckets llevan el ID de cuenta (`rag-playas-tfstate-<cuenta>`,
  `rag-playas-data-<cuenta>`), así que no chocan entre integrantes.
- `RAG_EXPECTED_AWS_ACCOUNT_ID` y `doctor` evitan operar en la cuenta equivocada.
- El tráfico de usuario va en claro (HTTP :80); se documenta como riesgo.

## Verificación

- `scripts/playas.sh doctor` comprueba la cuenta, la región y el bucket del State.
- `test_s3_backend_requires_bucket_of_current_account`,
  `test_stops_on_account_mismatch_without_writing`.
