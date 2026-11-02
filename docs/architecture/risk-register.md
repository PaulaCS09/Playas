# Registro de riesgos (A4)

**Estado de cada riesgo:**
- **C:** confirmado en el código.
- **P:** pendiente de verificar en una cuenta.

Las mitigaciones son propuestas; ninguna está aprobada ni implementada.

| # | Riesgo | Evidencia | Estado | Nivel | Mitigación propuesta |
|---|---|---|---|---|---|
| R1 | Pérdida del state local al reejecutar el bastion | `scripts/deploy_bastion_ec2.sh:234` (`rm -rf ~/playas`) | C | Alto | No usar el script tal como está; backend S3 por cuenta |
| R2 | Sin backend remoto en ningún stack | `*/terraform.tf`; backend perdido en el merge `bf9305a` | C | Alto | Backend S3 con `use_lockfile` (Terraform ≥ 1.10) |
| R3 | ChromaDB y Ollama abiertos a Internet sin autenticación | `vector-infraestructura/chromadb.tf:34-40`, `ollama.tf:16-22` | C | Alto | Acceso solo desde ECS por red privada |
| R4 | Reemplazo de las EC2 al cambiar la AMI, con pérdida del índice | `chromadb.tf:5` (`most_recent`), datos en el volumen raíz | C | Alto | `ignore_changes = [ami]` y volumen de datos aparte |
| R5 | Secretos en texto plano en la task definition y en el state | `rag/infrastructure/ecs.tf:78,110-118` | C | Alto | `secrets`/`valueFrom` si el laboratorio lo permite (P) |
| R6 | Credenciales y JWT en claro por HTTP | `rag/infrastructure/alb.tf:53-63` | C | Medio (aceptado) | Sin acción por ahora (`CLAUDE.md`) |
| R7 | SSH abierto a `0.0.0.0/0` | `chromadb.tf:26-32`, `ollama.tf:8-14`, `deploy_bastion_ec2.sh:86` | C | Medio | Limitar a la IP del operador |
| R8 | Manipulación automática del state | `rag/infrastructure/scripts/deploy.sh:49-50` | C | Medio | Paso manual revisado |
| R9 | Versiones de provider no fijadas | `init -upgrade` en los dos `deploy.sh` | C | Medio | Quitar `-upgrade` |
| R10 | Apply sin revisión | `--auto-approve` en `deploy_bastion_ec2.sh:249,264` | C | Medio | `plan` revisado antes de cada `apply` |
| R11 | Secretos impresos en la terminal | `vector-infraestructura/scripts/deploy.sh:196-197` | C | Medio | No imprimirlos |
| R12 | Borrado de PostgreSQL con `destroy` | `rag/infrastructure/efs.tf` sin `prevent_destroy` | C | Medio | `prevent_destroy` y `export_postgres.sh` antes de destruir |
| R13 | Export de ChromaDB desfasado y con colección y host antiguos | Manifest del `data/*.jsonl.gz`; `data/import_to_chromadb.py:88-90` | C | Medio | Reindexar desde Gold; `--collection` y `--host` explícitos |
| R14 | Hashes de usuarios de prueba en un repositorio público | `data/seed_users.sql`; `rag/infrastructure/scripts/deploy.sh:107-146` | C | Medio | Usuarios nuevos por despliegue |
| R15 | Costes con el laboratorio detenido (ALB, EFS, EBS, EIP, IPv4, Insights) | `alb.tf`, `efs.tf`, EIP en `vector-infraestructura/` | C | Medio | `stop.sh` al cerrar; destruir entre periodos largos |
| R16 | Sesión de ~4 h interrumpe applies largos | Esperas de hasta 900 s en `start.sh` y `deploy.sh` | P | Medio | No iniciar applies con menos de 90 min de sesión |
| R17 | ECS Exec o EFS IAM no permitidos por LabRole | `ecs.tf:46-49,195` | P | Medio | Verificar antes del primer despliegue |
| R18 | Imágenes mutables (`latest`) | `rag/infrastructure/variables.tf:26,32`, `ecr.tf:3,13` | C | Bajo | Tag por SHA de Git |
