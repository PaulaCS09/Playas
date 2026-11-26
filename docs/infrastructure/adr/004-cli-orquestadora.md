# ADR-004 — CLI `scripts/playas.sh` que orquesta los scripts existentes

- **Estado:** Aceptado (2026-11-26)
- **Ámbito:** `scripts/playas.sh`, `.github/workflows/ci.yml` (job `infra-static`)

## Contexto

Cada stack tenía su propio `deploy.sh`, sin un punto de entrada común ni
comprobaciones previas. Dos scripts aceptaban `--auto-approve` y el bastion lo
usaba, así que un apply podía ejecutarse sin que nadie revisara el plan.
Ingesta no tenía script de despliegue. No había validación de infraestructura
en la CI.

## Decisión

- Una CLI en Bash (`scripts/playas.sh`), la misma herramienta que los scripts
  existentes, con los comandos `doctor`, `bootstrap`, `plan`, `deploy`,
  `status`, `health`, `backup-state`, `start`/`stop` y `test`.
- La CLI **reutiliza** el `deploy.sh` de cada stack en lugar de duplicarlo, y
  añade `ingesta/infrastructure/scripts/deploy.sh` con el mismo patrón.
- **Reglas de seguridad fijas:** nunca `destroy`, `-auto-approve`,
  `-migrate-state`, `import`, `state rm/push` ni `force-unlock`. El apply es
  interactivo (requiere terminal), `plan` no guarda archivos de plan y la CLI
  falla de forma cerrada ante cualquier error.
- `deploy all` y `plan all` siguen el orden **ingesta → vector → rag** y se
  detienen en el primer fallo.
- La CI ejecuta `scripts/playas.sh test static` sin credenciales: así la CI y
  el entorno local validan exactamente lo mismo.

## Alternativas descartadas

- **Makefile o Python:** otra herramienta y otra capa para hacer lo mismo que
  ya hacen los scripts Bash del repo.
- **Reescribir los `deploy.sh`:** riesgo de romper flujos probados y código de
  otros integrantes.

## Consecuencias

- Un solo punto de entrada documentado en `DEPLOYMENT.md`.
- Eliminar recursos protegidos exige una decisión explícita, fuera de la CLI.

## Verificación

- `rag/tests/unit/test_playas_cli.py` (27 pruebas, con `aws`/`terraform` falsos):
  `test_never_uses_destructive_or_unattended_terraform`,
  `test_deploy_all_follows_dependency_order`, `test_deploy_all_stops_at_first_failure`,
  `test_cli_plan_never_saves_plan_files`, `test_cli_is_shellchecked_by_ci`, …
- `test_scripts_never_pass_auto_approve_or_upgrade`, `test_stack_script_rejects_auto_approve`.
