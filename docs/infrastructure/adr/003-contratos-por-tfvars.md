# ADR-003 — Contratos entre stacks por tfvars

- **Estado:** Aceptado (2026-11-03)
- **Ámbito:** `vector-infraestructura/outputs.tf`, `rag/infrastructure/variables.tf`, `scripts/playas.sh doctor`

## Contexto

El stack `rag` necesita datos de los otros dos: las IP privadas de ChromaDB y
Ollama, el Security Group de clientes de vector y el bucket de datos de ingesta.
Antes, esos valores se copiaban a mano y nada comprobaba que fueran correctos o
de la misma cuenta.

## Decisión

- Cada productor expone **outputs explícitos** (`chromadb_private_ip`,
  `ollama_private_ip`, `vector_clients_security_group_id`, `s3_bucket_name`).
- El consumidor (`rag`) los recibe como variables en
  `rag/infrastructure/terraform.tfvars` (permisos 600, no versionado). En
  Terraform solo `vector_clients_security_group_id` tiene un bloque
  `validation` (vacío o con prefijo `sg-`, `rag/infrastructure/variables.tf`);
  `chroma_host`, `ollama_base_url` y `s3_bucket_name` no tienen `validation`.
- `vector-infraestructura/scripts/deploy.sh` escribe automáticamente sus
  valores; `s3_bucket_name` lo muestra `deploy ingesta`.
- `scripts/playas.sh doctor` lee `rag/infrastructure/terraform.tfvars` como texto y
  rechaza placeholders (`ec2-xxx*`, `cambia-*`, `*xxx*`, `*<*`, `*>*`, tras quitar
  `http(s)://`; `tfvars_placeholder`, `scripts/playas.sh`) en `chroma_host`,
  `ollama_base_url`, `vector_clients_security_group_id` y `s3_bucket_name`. Si
  `chroma_host` u `ollama_base_url` están vacíos, avisa. Si
  `vector_clients_security_group_id` está vacío, FALLA solo cuando
  `legacy_public_service_access=false` en `vector-infraestructura/terraform.tfvars`; con
  `true` o si no se puede determinar, solo avisa. `doctor` lee `legacy_public_service_access` solo de `vector-infraestructura/terraform.tfvars` e ignora `TF_VAR_legacy_public_service_access`: si se define solo por variable de entorno, avisa en lugar de fallar (definirlo en el tfvars). Con `legacy_public_service_access=true`
  avisa además de que 8000/11434 están abiertos a `0.0.0.0/0` (remite a
  [ADR-005](005-red-privada-vector.md)). Por último, `s3_bucket_name` debe ser el bucket de
  ingesta **de la cuenta actual** (`rag-playas-data-<cuenta>`).

## Alternativas descartadas

- **`terraform_remote_state`:** acopla los stacks (para planificar `rag` habría
  que leer el State, y con él los secretos, de los otros) y amplía los permisos
  necesarios sobre el bucket del State.
- **SSM Parameter Store:** añade un servicio y permisos que no están verificados
  en el Learner Lab.

## Consecuencias

- Cada stack se planifica de forma aislada; `plan rag` falla con un mensaje
  claro si todavía no se ha desplegado `vector`.
- El orden de despliegue es fijo: **ingesta → vector → rag**.

## Verificación

- `test_vector_stack_exposes_private_ips_and_clients_sg`,
  `test_deploy_writes_private_ips_for_ecs_and_public_for_operator`,
  `test_deploy_stops_if_private_ip_output_is_missing`,
  `test_doctor_detects_wrong_data_bucket_contract`.
- `rag/infrastructure/tests/vector_clients.tftest.hcl` (rechaza un ID de SG sin prefijo `sg-`).
