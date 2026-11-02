# Plan de implementación greenfield

- Cada fase requiere la aprobación de la responsable del proyecto antes de ejecutarse.
- Los riesgos citados (R*) están en `risk-register.md`. Las mitigaciones concretas se deciden en cada fase.

| Fase | Entregable | Depende de | Resultado esperado | Aprobación |
|---|---|---|---|---|
| 0 | Documentación de arquitectura (`ADR-001-greenfield.md`, `architecture.md`, `risk-register.md`, este plan) | — | Documentos revisados | Revisión de los documentos |
| 1 | Scripts de despliegue endurecidos (R1, R8–R11) | 0 | Los scripts no destruyen state ni aplican cambios sin revisión | Cambio de código |
| 2 | Backend remoto del state para los tres stacks (R2) | 1 | State persistente y con bloqueo en cada cuenta | Diseño del backend y cambio de Terraform |
| 3 | Protección de datos persistentes (R4, R12) | 2 | Ningún cambio de infraestructura reemplaza ni borra datos sin decisión explícita | Cambio de Terraform |
| 4 | Restricción de red de los servicios vectoriales y de SSH (R3, R7) | 2 | ChromaDB, Ollama y SSH no expuestos a Internet | Cambio de Terraform y red |
| 5 | Informe de permisos de LabRole (R17) | — | Lista verificada de servicios permitidos en la cuenta | Inventario de solo lectura |
| 6 | Gestión de secretos fuera de la task definition (R5) | 5 | Secretos no visibles en la definición de tareas ni en `environment` | Cambio de Terraform |
| 7 | Primer despliegue en una cuenta de estudiante | 1–4 (6 opcional) | Stack completo operativo y comprobado | Despliegue y coste |
| 8 | Datos iniciales: índice vectorial y usuarios (R13, R14) | 7 | Índice coherente con el Gold actual; usuarios propios del despliegue | Operación sobre datos |
| 9 | Runbook de operación de sesiones y presupuesto (R15, R16) | 7 | Procedimiento de inicio, cierre y control de costes validado | Revisión |

## Pendiente de decisión

- **Backend:** bucket y convención del state remoto (fase 2).
- **Secretos:** mecanismo, según el resultado de la fase 5 (fase 6).
- **Bastion:** uso o retirada de `scripts/deploy_bastion_ec2.sh` tras la fase 1.
