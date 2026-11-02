# ADR-001: Reconstrucción greenfield sobre la arquitectura A4

- **Estado:** aceptada (2026-11-01).
- **Alcance:** infraestructura AWS de PLAYAS en cuentas individuales de AWS Academy Learner Lab.

## Contexto

- El despliegue original no es accesible:
  - El state de Terraform y los recursos de despliegues anteriores no están disponibles desde ninguna cuenta accesible.
- El historial de Git menciona al menos dos cuentas más:
  - Cuenta del bucket de datos: `rag/.env.example` entre `1d7bbd2` y `7754b01`.
  - Cuenta del bucket de state: `10787af`. Es el hash del commit no una cuenta.
- No se pueden solicitar credenciales a otros integrantes.
- Los tres stacks usan backend local:
  - `vector-infraestructura/terraform.tf`
  - `rag/infrastructure/terraform.tf`
  - `ingesta/infrastructure/terraform.tf`
- El backend S3 añadido en `10787af` se perdió en el merge `bf9305a`.
- Ninguna rama contiene Terraform más completo ni más reciente que `main` (`23c6c5b`).

## Decisión

1. Usar la arquitectura A4 de `main` como base. Los tres stacks son:
   - `ingesta/infrastructure/` (S3)
   - `vector-infraestructura/` (EC2 de ChromaDB y Ollama)
   - `rag/infrastructure/` (ECS Fargate, ALB, ECR, EFS)
2. Tratar cada despliegue como **nuevo** en la cuenta del estudiante que lo ejecuta. No se recuperan ni importan recursos ni state de despliegues anteriores.
3. Mantener HTTP en el ALB, como establece `CLAUDE.md`. Es un riesgo aceptado.

Cualquier cambio sobre A4 (backend remoto, red, scripts) requiere una decisión propia y no forma parte de este ADR.

## Alternativas descartadas

| Alternativa | Motivo |
|---|---|
| Recuperar el state o los recursos originales | Las cuentas son inaccesibles y la ubicación del state no es verificable |
| A1: monolito `infrastructure/` (abril–junio) | Sustituida en `main` el 2026-06-17 y sin stack de aplicación |
| A2: rama `chore/reorg-estructura` | Abandonada; reintroduce Firebase, en contra de `CLAUDE.md` |
| Combinar código de varias ramas | Ninguna aporta infraestructura más reciente que `main` |
| Usar `scripts/deploy_bastion_ec2.sh` tal como está | Ejecuta `rm -rf ~/playas` (línea 234) y `--auto-approve` |

## Consecuencias

- Cada estudiante crea y paga su propia infraestructura y es responsable de su state.
- Los datos de despliegues anteriores no se recuperan:
  - El índice de ChromaDB debe reconstruirse.
  - Las cuentas de usuario de `data/seed_users.sql` no se dan por válidas.
- Los riesgos conocidos de A4 se mantienen hasta que se aprueben sus mitigaciones (ver `risk-register.md`).
- Las fases de trabajo están en `deployment-plan.md`.
