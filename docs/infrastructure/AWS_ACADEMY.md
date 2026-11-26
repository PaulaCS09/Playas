# AWS Academy Learner Lab

Ningún permiso de esta tabla se ha observado en una ejecución real
registrada en el repositorio. **Probable**: flujo que ya usaba `main`.
**No verificado**: nuevo o señalado como no comprobado.

## Restricciones que condicionan la infraestructura

- **Credenciales temporales** (`AWS_SESSION_TOKEN`) que caducan al terminar la
  sesión del lab. Si caducan durante un apply puede quedar un `<key>.tflock`
  huérfano: `status` lo muestra; liberarlo es manual (`force-unlock`, README raíz).
- **Región fija `us-east-1`** en los backends y providers de ingesta y vector,
  en `scripts/lib/terraform_state.sh` y en el bootstrap. `doctor` falla si la
  región configurada es otra.
- **Sin IAM propio**: ECS usa `LabRole` (`rag/infrastructure/iam.tf`), el
  bastion `LabInstanceProfile`, y las EC2 el key pair `vockey`.
- **Una cuenta por integrante**: el bucket del state y el de datos llevan el
  ID de cuenta en el nombre; `RAG_EXPECTED_AWS_ACCOUNT_ID` evita operar en otra.

## Permisos utilizados

| Servicio | Operaciones | Estado |
|---|---|---|
| STS | GetCallerIdentity | probable |
| S3 (state) | CreateBucket, Put/Get de versionado, cifrado, PAB, ownership y política; HeadBucket; Get/Put/DeleteObject (state y `.tflock`) | no verificado |
| S3 (state) | ListBucketVersions (protección de state vacío) | no verificado — `doctor` lo comprueba |
| S3 (datos) | CreateBucket, versionado, SSE, PAB; `s3 cp/rm` (export de Postgres) | no verificado / probable |
| EC2 | Describe*, RunInstances, SG, EIP, Start/StopInstances | probable |
| IAM | GetRole y PassRole sobre LabRole; PassRole sobre LabInstanceProfile | probable |
| ECS | Cluster, task definitions, servicios, ListTasks, Container Insights | probable |
| ECS Exec | ExecuteCommand + `ssmmessages` en LabRole + session-manager-plugin local | probable (los reintentos del deploy indican fallos intermitentes) |
| ECR | Repositorios, lifecycle, scan on push, push | probable |
| ELBv2 | ALB, target groups, listener y reglas, DescribeTargetHealth | probable |
| EFS | File system cifrado, access point, mount targets; ClientMount/ClientWrite con IAM | probable |
| AWS Backup | Política del EFS | no verificado — desactivado por defecto |
| CloudWatch Logs | Log groups (14 días), `logs tail` | probable |

## Costes

Estimados a partir de Terraform, no de un inventario real.

| Recurso | Sigue costando con el lab detenido |
|---|---|
| 2 EIP (IPv4 pública) | sí, también con la EC2 parada |
| EBS gp3 (30 GB + 20 GB) | sí |
| ALB | sí, por hora, aunque los servicios estén a 0 |
| EFS, ECR, S3 (state versionado y datos), CloudWatch Logs | sí, por GB (bajo) |
| EC2 t3.large ×2, Fargate | solo encendidos |
| Bastion | sí, si se deja encendido |
| NAT Gateway, AWS Backup | no existen / desactivado |

`scripts/playas.sh stop` apaga EC2 y ECS, pero el ALB y las EIP son el coste
fijo principal; eliminarlos sería un cambio de arquitectura que requiere
decisión del equipo. Container Insights (`ecs.tf`) añade métricas de pago.
