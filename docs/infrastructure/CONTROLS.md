# Matriz de controles de infraestructura

Cada fila enlaza un control con el archivo que lo implementa y con la prueba
automática que lo verifica. Las pruebas no usan credenciales ni AWS real:
`pytest` sustituye `aws`/`terraform` por dobles de prueba, y `terraform test`
usa `mock_provider`. Se ejecutan en la CI (`rag-test` e `infra-static`) y en
local con `scripts/playas.sh test all`.

Los controles que dependen de una cuenta real están en
[AWS_ACADEMY.md](AWS_ACADEMY.md) y en "Pendiente de validar" de
[ARCHITECTURE.md](ARCHITECTURE.md).

## Terraform State

| # | Control | Implementación | Verificación |
|---|---|---|---|
| S1 | State remoto con una key fija por stack | `*/terraform.tf`, `backend.hcl.example` | `test_backend_example_and_stack_keys_are_consistent` |
| S2 | Lock nativo (`use_lockfile`) y versión de Terraform fijada | `*/terraform.tf` (`~> 1.17.1`) | `test_doctor_terraform_version_follows_required_version`, `test_stack_script_stops_when_state_is_locked` |
| S3 | Bucket: versionado, SSE, Block Public Access, BucketOwnerEnforced | `scripts/bootstrap_tfstate_bucket.sh` | `test_creates_and_configures_bucket_in_safe_order`, `test_verification_fails_on_unexpected_configuration` |
| S4 | Política que deniega las peticiones sin TLS | idem | `test_tls_policy_denies_insecure_transport_on_bucket_and_objects` |
| S5 | `backend.hcl` solo admite `bucket`, de la cuenta actual | `scripts/lib/terraform_state.sh` | `test_s3_backend_config_only_allows_bucket`, `test_s3_backend_requires_bucket_of_current_account` |
| S6 | Se rechazan endpoints, workspaces y `TF_CLI_ARGS` que desvíen el State | idem, `scripts/playas.sh` | `test_s3_backend_refuses_endpoint_and_workspace_overrides`, `test_tf_cli_overrides_rejected` |
| S7 | Un State vacío no se sobrescribe sin confirmación explícita | idem | `test_empty_remote_state_guard_*` (7 pruebas), `test_s3_backend_empty_state_needs_explicit_opt_in` |
| S8 | Copia del State antes de cada apply; si falla, se detiene | stacks `deploy.sh` | `test_s3_backend_backs_up_remote_state_before_apply`, `test_s3_backend_backup_stops_when_state_pull_fails` |
| S9 | Respaldo cifrado (GPG) fuera del clon y sin copia en claro | `scripts/playas.sh backup-state` | `test_backup_state_encrypts_without_plaintext_on_disk`, `test_backup_state_refuses_destination_inside_clone` |
| S10 | States, tfvars y `backend.hcl` fuera de Git | `.gitignore` | `test_gitignore_keeps_states_out_but_tracks_backend_code` |

## Operación y despliegue

| # | Control | Implementación | Verificación |
|---|---|---|---|
| O1 | Nunca `destroy`, `-auto-approve`, `-migrate-state`, `import`, `state rm/push` ni `force-unlock` | `scripts/playas.sh`, `*/scripts/deploy.sh`, `scripts/lib/terraform_state.sh` | `test_never_uses_destructive_or_unattended_terraform`, `test_scripts_never_pass_auto_approve_or_upgrade`, `test_scripts_never_run_manual_only_state_operations` |
| O2 | El apply exige terminal interactiva y confirmación humana | stacks `deploy.sh`, `scripts/deploy_bastion_ec2.sh` | `test_stack_script_requires_interactive_terminal`, `test_deploy_requires_terminal`, `test_bastion_requires_interactive_terminal` |
| O3 | Orden de dependencias ingesta → vector → rag; se detiene en el primer fallo | `scripts/playas.sh` | `test_deploy_all_follows_dependency_order`, `test_deploy_all_stops_at_first_failure` |
| O4 | `plan` no guarda archivos de plan (pueden contener secretos) | idem | `test_cli_plan_never_saves_plan_files` |
| O5 | Comprobaciones previas de cuenta, región, credenciales y contratos | `scripts/playas.sh doctor` | `test_doctor_*` (5 pruebas) |
| O6 | Health checks de servicios y targets del ALB | `scripts/playas.sh health` | `test_health_passes_when_everything_responds`, `test_health_fails_on_unhealthy_targets` |
| O7 | Detección de drift de SG en rag sin ocultar errores | `rag/infrastructure/scripts/deploy.sh` | `test_rag_deploy_stops_on_sg_drift_without_touching_state`, `test_rag_drift_check_stops_when_state_list_fails` |
| O8 | Bastion: Terraform con checksum y SSH solo desde la IP del operador | `scripts/deploy_bastion_ec2.sh` | `test_bastion_pins_terraform_with_checksum`, `test_bastion_rejects_missing_or_open_ssh_cidr` |

## Secretos

| # | Control | Implementación | Verificación |
|---|---|---|---|
| K1 | Ningún script imprime variables secretas | todos los `*.sh` | `test_no_script_prints_secret_variables`, `test_secret_detector_examples` |
| K2 | Secretos generados sin mostrarse y tfvars con permisos 600 | `vector-infraestructura/scripts/deploy.sh` | `test_vector_deploy_generates_secrets_without_printing_them`, `test_vector_deploy_keeps_existing_secrets_and_tightens_permissions` |
| K3 | Dump de Postgres restringido y URL prefirmada de caducidad corta | `rag/infrastructure/scripts/export_postgres.sh` | `test_export_postgres_restricts_dump_and_presign_expiry` |

## Red y datos persistentes

| # | Control | Implementación | Verificación |
|---|---|---|---|
| N1 | ChromaDB/Ollama solo desde el SG `vector_clients` (IP privada) | `vector-infraestructura/network.tf`, `chromadb.tf`, `ollama.tf` | `test_service_port_open_to_vector_clients_and_operator`, `network.tftest.hcl` |
| N2 | Acceso del operador y SSH solo desde IPv4 /32 | `vector-infraestructura/variables.tf` | `test_operator_and_ssh_variables_require_single_host_cidrs`, runs `cidr_abierto_rechazado`, `ipv6_rechazado`, `ssh_no_32_rechazado` |
| N3 | Exposición pública transitoria, explícita y con aviso | `legacy_public_service_access` | `test_legacy_public_access_has_no_default`, `test_transitional_public_access_is_warned`, run `fase_3_sin_acceso_publico` |
| N4 | ECS adjunta el SG de clientes y usa IP privadas | `rag/infrastructure/ecs.tf`, `variables.tf` | `test_backend_service_attaches_vector_clients_sg`, `vector_clients.tftest.hcl` |
| D1 | `prevent_destroy` en el EFS, las EC2 vectoriales y el bucket de datos | `efs.tf`, `chromadb.tf`, `ollama.tf`, `s3.tf` | `test_persistent_data_resources_keep_prevent_destroy`, `no_replace.tftest.hcl` |
| D2 | Una AMI nueva o un cambio de user-data no fuerzan el reemplazo | `ignore_changes = [ami, user_data]` | run `fase_3_no_reemplaza_instancias` |

## Calidad

| # | Control | Implementación | Verificación |
|---|---|---|---|
| Q1 | `terraform fmt`, `validate` y `test` en los tres stacks | `scripts/playas.sh test static` | job `infra-static` |
| Q2 | ShellCheck 0.10 sobre todos los scripts versionados | idem | job `infra-static`, `test_cli_is_shellchecked_by_ci` |
| Q3 | Permisos mínimos del workflow | `.github/workflows/ci.yml` (`permissions: contents: read`) | revisión del workflow |
