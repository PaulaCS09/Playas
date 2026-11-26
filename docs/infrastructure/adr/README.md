# Decisiones de arquitectura (ADR)

Cada ADR recoge una decisión de infraestructura vigente: el contexto, lo que se
decidió, las alternativas descartadas y sus consecuencias. Si una decisión
cambia, se añade un ADR nuevo que la sustituye; el anterior se conserva marcado
como *Sustituido*.

| ADR | Decisión | Estado |
|---|---|---|
| [001](001-despliegue-por-cuenta.md) | Despliegue nuevo en la cuenta de Learner Lab de cada integrante | Aceptado |
| [002](002-state-remoto-s3.md) | Terraform State remoto en S3, una key por stack y lock nativo | Aceptado |
| [003](003-contratos-por-tfvars.md) | Contratos entre stacks por tfvars, sin `terraform_remote_state` | Aceptado |
| [004](004-cli-orquestadora.md) | CLI `scripts/playas.sh` que orquesta los scripts existentes | Aceptado |
| [005](005-red-privada-vector.md) | ChromaDB y Ollama accesibles por IP privada y datos protegidos | Aceptado |

Plantilla: **Contexto** → **Decisión** → **Alternativas descartadas** →
**Consecuencias** → **Verificación** (pruebas o comandos que demuestran que se
cumple).
