# Infraestructura RAG — ECS Fargate

Terraform que provisiona el despliegue en AWS del backend + frontend del
subsistema RAG: ALB, ECS Fargate, ECR, EFS (volumen de Postgres). Esta es
la alternativa a un despliegue de un solo host con Docker Compose — ver
[`../docs/DESPLIEGUE.md`](../docs/DESPLIEGUE.md) para esa otra vía.

No incluye ChromaDB ni Ollama: esos servicios se provisionan por separado
en `../../vector-infraestructura/` (EC2). Tampoco incluye el bucket S3 del
pipeline de datos, provisionado en `../../ingesta/infrastructure/`.

---

## Requisitos previos

- Terraform ≥ 1.7, AWS CLI 2.x, credenciales AWS con permisos para ECS/ECR/ALB/EFS/IAM/EC2.
- ChromaDB y Ollama ya desplegados (ver `../../vector-infraestructura/`).
- `terraform.tfvars` (copiar de `terraform.tfvars.example`) con, como mínimo,
  `postgres_password`, `jwt_secret_key`, `chroma_host`, `ollama_base_url`, y
  al menos una API key de LLM (`openai_api_key` o `google_api_key`).

Terraform solo es necesario para **provisionar/desplegar** (`deploy.sh`,
`push_images.sh`, `terraform plan/apply/destroy`). Las operaciones diarias
de encendido/apagado (`start.sh`/`stop.sh`) **no usan Terraform en absoluto**:
descubren los recursos directamente contra la API de AWS por tag/nombre
(EC2 por `tag:Name`, ECS por nombre de clúster/servicio, el ALB por su
nombre), así que funcionan aunque no tengas el state de Terraform
inicializado localmente — solo requieren AWS CLI 2.x (y `curl`, únicamente
`start.sh`).

## Recursos que provisiona

| Archivo | Recursos |
|---|---|
| `ecs.tf` | Clúster ECS, definición de tarea (backend + sidecar Postgres en la misma tarea), servicios `app` y `frontend` |
| `alb.tf` | Application Load Balancer, listeners, target groups |
| `ecr.tf` | Repositorios ECR para las imágenes de backend y frontend (`force_delete = false`) |
| `efs.tf` | Volumen EFS para los datos de Postgres (persisten entre despliegues; `prevent_destroy`). Política opcional de AWS Backup con `enable_efs_backup` (desactivada por defecto) |
| `security_groups.tf` | Grupos de seguridad de ALB/ECS/EFS |
| `iam.tf` | Roles y políticas de ejecución/tarea de ECS |
| `variables.tf` | Todas las variables de entrada (ver `terraform.tfvars.example`) |
| `outputs.tf` | `alb_url`, `ecr_backend_url`, `ecr_frontend_url`, `ecs_cluster_name`, `aws_account_id` |

`DATABASE_URL` del backend apunta a `localhost:5432`: el sidecar de
Postgres corre en la misma tarea Fargate que el backend.

## Scripts (`scripts/`)

| Script | Qué hace |
|---|---|
| `deploy.sh` | Inicializa Terraform (sin `-upgrade`), respalda el state local en `~/tfstate-backups/`, aplica la infraestructura con confirmación humana, construye y sube las imágenes, fuerza el redespliegue de los servicios ECS. Requiere una terminal interactiva, `terraform.tfvars` completo y credenciales AWS en el entorno. No admite `--auto-approve` y no modifica el state si detecta security groups desalineados: se detiene e indica los comandos manuales. |
| `push_images.sh [TAG]` | Construye las imágenes de backend/frontend y las sube a los repositorios ECR (lee las URLs desde los outputs de Terraform). Tag por defecto: `latest`. |
| `start.sh` | Enciende **todo** el cómputo de la aplicación: primero las EC2 de ChromaDB y Ollama (descubiertas por tag, sin Terraform), verificadas por HTTP; luego, solo si responden, los servicios ECS `app` y `frontend`. No termina hasta comprobar por HTTP que el backend (`/api/ready`) y el frontend responden — ver detalle abajo. |
| `stop.sh` | Apaga **todo** el cómputo en el orden inverso: primero ECS (confirmando, con una sola consulta agregada, que AMBOS servicios quedaron simultáneamente en `desired=running=pending=0`), y solo entonces las EC2 de ChromaDB/Ollama. No depende del ALB ni de `curl` en ningún punto. Los datos de Postgres (EFS) y de ChromaDB/Ollama (EBS) se conservan. |
| `export_postgres.sh` | Exporta un volcado de la base de datos Postgres en EFS. |

### Encendido y apagado seguro (`start.sh` / `stop.sh`)

Ambos scripts descubren sus recursos directamente contra AWS CLI (sin leer
outputs ni state de Terraform): las EC2 de ChromaDB/Ollama por su tag
`Name` (fallan si encuentran cero o más de una coincidencia, en vez de
asumir un ID), y el clúster/servicios ECS por nombre. Son idempotentes
(seguros de ejecutar más de una vez o después de una ejecución parcial) y
terminan con código de salida `0` únicamente cuando la operación completa
fue verificada:

- **Región y clúster**: la región se resuelve como `AWS_REGION` >
  `AWS_DEFAULT_REGION` > `us-east-1` (en ese orden de precedencia); el
  clúster ECS se resuelve como `RAG_ECS_CLUSTER` > `rag-playas-prod`. Ambas
  variables son opcionales.
- **Identidad antes de mutar**: antes de encender o apagar nada, ambos
  scripts muestran la cuenta AWS activa, el ARN de las credenciales en uso
  y la región resuelta (vía `aws sts get-caller-identity`). Si defines
  `RAG_EXPECTED_AWS_ACCOUNT_ID` (12 dígitos), el script falla antes de
  cualquier mutación si la cuenta activa no coincide — una salvaguarda
  opcional contra ejecutar el script con las credenciales de la cuenta
  equivocada.
- `start.sh` verifica primero que el ALB exista y esté `active` (antes de
  encender nada), y no enciende ECS si ChromaDB u Ollama no responden por
  HTTP primero (evita que el backend intente arrancar con sus dependencias
  externas apagadas); no declara éxito hasta que `/api/ready` del backend y
  `/` del frontend responden a través del ALB. Es el único de los dos
  scripts que requiere `curl` y que depende del ALB.
- `stop.sh` no depende del ALB en ningún punto (debe poder apagar aunque el
  ALB esté degradado o no responda) y no requiere `curl`. No apaga las EC2
  de ChromaDB/Ollama hasta confirmar, con una sola llamada agregada a
  `describe-services` (no solo esperar `services-stable`, que no distingue
  "estable en 0" de otros fallos), que se devolvieron exactamente los dos
  servicios esperados y que **ambos** están simultáneamente en
  `desiredCount=runningCount=pendingCount=0` — que uno de los dos lo esté y
  el otro no (o que AWS devuelva menos servicios de los pedidos) se trata
  como fallo, sin apagar ninguna EC2.
- Cada `update-service` (encendido o apagado) se protege con una
  comprobación explícita e inmediata del `desiredCount` que AWS devuelve en
  la misma respuesta: si no coincide exactamente con el valor solicitado (1
  al encender, 0 al apagar) — vacío, `None` o cualquier otro valor — el
  script falla de inmediato en vez de seguir esperando un estado que nunca
  pidió.
- Cada operación mutante (`start-instances`, `stop-instances`,
  `update-service`) se marca como **intentada** justo antes de invocarla —
  no después de recibir respuesta. Esto es deliberado: si AWS llega a
  aplicar la operación y la respuesta se pierde (red, timeout), un fallo
  posterior igual se trata como **estado potencialmente parcial**, nunca
  como si no hubiera pasado nada. Ningún fallo hace rollback automático de
  lo que ya se haya intentado: si un script termina con código distinto de
  cero **después de haber intentado alguna mutación**, imprime el estado
  observable de los cuatro componentes (2 EC2 + 2 servicios ECS) y comandos
  de diagnóstico sugeridos. **La forma normal de reconciliar un estado
  potencialmente parcial es volver a ejecutar el mismo script** — cada paso
  comprueba el estado real antes de actuar, así que no repite una mutación
  innecesaria.
- Una interrupción (Ctrl+C / `SIGTERM`) es distinta de un fallo: **no**
  imprime el diagnóstico de los cuatro componentes ni hace ninguna llamada
  adicional a AWS — solo un aviso corto indicando si ya se había intentado
  alguna operación mutante antes de interrumpir (estado potencialmente
  parcial) o no. Para ver el estado real tras una interrupción, usa los
  comandos de diagnóstico del README o vuelve a ejecutar el mismo script.
- Solo usan `start-instances`/`stop-instances` sobre las EC2 (nunca
  `terminate-instances`) y `desired_count` sobre ECS: no destruyen
  Terraform, no liberan las Elastic IP, no borran EBS/EFS, no reindexan ni
  reimportan datos. Un ciclo `stop.sh` → `start.sh` conserva discos,
  identificadores, endpoints y los datos de ChromaDB/Postgres.
- Se mantiene HTTP en todo momento (el ALB de este despliegue solo expone
  `http://...`; `start.sh` falla si el DNS del ALB no está `active`, en vez
  de intentar reescribirlo a HTTPS).
- **Apagar no deja el costo en cero**: el ALB, EFS, EBS, CloudWatch y las
  direcciones Elastic IP de ChromaDB/Ollama siguen existiendo y pueden
  seguir generando costo aunque ECS y las EC2 estén apagados.

Precondiciones operativas que los scripts **no pueden verificar por sí
solos** — son responsabilidad de quien los ejecuta:

- No ejecutar `start.sh` y `stop.sh` al mismo tiempo, ni desde dos
  terminales u operadores distintos.
- No ejecutar `stop.sh` mientras un flujo autorizado (ingesta, evaluación,
  mantenimiento) esté usando ChromaDB u Ollama — estas EC2 son recursos
  compartidos por otros componentes del repositorio, y el script no tiene
  forma fiable de detectar esa actividad desde afuera.

## Despliegue manual paso a paso

```bash
cd rag/infrastructure
cp terraform.tfvars.example terraform.tfvars   # completar con valores reales
terraform init
terraform plan
terraform apply

# Construir y subir imágenes, y forzar redespliegue
./scripts/push_images.sh
```

O con el script que encadena todo:

```bash
./scripts/deploy.sh   # pide confirmación antes del apply
```

## Security groups desalineados con el state

Antes, `deploy.sh` reimportaba automáticamente un security group que existía en
AWS con un ID distinto al del state (por ejemplo, tras un destroy parcial). Ahora
el script respalda el state, se detiene sin modificarlo e imprime los comandos
`terraform state rm` / `terraform import` correspondientes. Para recuperarse:

1. Confirmar en la consola o con `aws ec2 describe-security-groups` que el SG
   indicado es el que debe gestionar Terraform.
2. Comprobar que existe el respaldo en `~/tfstate-backups/rag-infrastructure/`.
3. Ejecutar manualmente los comandos impresos.
4. Volver a lanzar `./scripts/deploy.sh` y revisar el plan antes de confirmar.

## Límite de intentos de login y proxy de confianza

El backend limita `POST /api/auth/login` por **email + IP real del cliente**
(10 intentos en 300 s por defecto; `AUTH_RATE_LIMIT_REQUESTS` y
`AUTH_RATE_LIMIT_WINDOW_SECONDS`).

- **IP real detrás del ALB.** uvicorn se arranca con `--proxy-headers` y la
  tarea recibe `FORWARDED_ALLOW_IPS` con todos los CIDR de la VPC por defecto
  (primario y secundarios). uvicorn solo acepta `X-Forwarded-For` de conexiones
  desde esos rangos (los nodos del ALB)
  y toma la IP más a la derecha que no sea de confianza, que el ALB añade y el
  cliente no puede falsificar. El SG de la app solo admite el puerto 8080 desde
  el SG del ALB. **Nunca usar `FORWARDED_ALLOW_IPS=*`**: uvicorn tomaría la IP
  más a la izquierda, controlada por el cliente. El backend se niega a arrancar
  con `*` o con cualquier IP/red enrutable públicamente (`0.0.0.0/0`,
  `0.0.0.0/1`, `8.8.8.8`, etc.).
  La variable debe venir del entorno del proceso, no de `.env` ni del flag
  `--forwarded-allow-ips` de uvicorn, que el backend no puede validar.
- **Modo.** La variable `auth_rate_limit_mode` (por defecto `observe`) fija
  `AUTH_RATE_LIMIT_MODE`: `observe` registra los excesos (`scope=login`, sin
  email ni IP) sin bloquear; `enforce` responde 429 con `Retry-After`.

Para pasar a `enforce`, en un despliegue revisado:

1. Desplegar en `observe` y comprobar con
   `aws logs tail /ecs/<cluster>-app --log-stream-name-prefix backend --filter-pattern '"POST /api/auth/login"'`
   que las líneas de acceso de uvicorn muestran IPs públicas de clientes y no
   IPs de la VPC. Las líneas de `/api/health` muestran siempre la IP privada del
   ALB: es lo esperado.
2. Enviar un login con dos cabeceras `X-Forwarded-For` falsas y comprobar que
   la línea de acceso sigue mostrando la IP real del cliente. Revisar los
   eventos `scope=login` durante el periodo de observación.
3. Cambiar `auth_rate_limit_mode = "enforce"` en `terraform.tfvars`, revisar el
   plan (solo cambia la task definition) y desplegar.
4. Comprobar que el intento N+1 desde un mismo cliente devuelve 429 y que el
   mismo email desde otra IP sigue respondiendo 401/200.

Limitaciones:

- El contador vive en memoria de cada tarea: se reinicia en cada despliegue y,
  con N tareas, el límite efectivo es N × el configurado.
- Se confía en todo el CIDR de la VPC, no solo en el ALB: otra máquina de la
  VPC que llegara al puerto 8080 podría fijar la IP. Hoy lo impide el SG de la
  app; no abrir ese puerto a otros orígenes.
- Depende de que el ALB añada la IP del cliente. `alb.tf` no fija
  `xff_header_processing_mode`, así que aplica el valor por defecto de AWS
  (`append`); cambiarlo a `preserve` o `remove` invalida esta configuración.
- La clave es email + IP: varios usuarios tras la misma IP (NAT, red
  corporativa) no se bloquean entre sí, pero no hay límite solo por IP (un
  cliente puede probar una contraseña contra muchos emails) ni solo por email
  (un atacante que rote IPs no queda limitado para una misma cuenta).
- En local, `FORWARDED_ALLOW_IPS` debe estar en el entorno del proceso (uvicorn
  la lee antes de cargar `.env`). Sin ella, detrás del Nginx de
  `docker-compose` la clave usa la IP del contenedor Nginx.

### Registro de la IP real en CloudWatch

Durante la validación en `observe` está autorizado registrar la IP real del
cliente, que es un dato personal, con estas condiciones:

- **Retención máxima de 14 días**: los log groups `/ecs/<cluster>-app` y
  `-frontend` tienen `retention_in_days = 14`. No aumentarla.
- **Acceso restringido**: en AWS Academy no se pueden crear políticas IAM
  propias (todo usa `LabRole`), así que la restricción es organizativa: solo
  las personas responsables de la operación acceden a la cuenta del lab y a
  esos logs. No exportarlos fuera de la cuenta.
- **Qué se registra**: la línea de acceso de uvicorn contiene solo
  `IP:puerto`, método, ruta, versión HTTP y código de estado. No incluye
  cabeceras (`Authorization`, JWT), cuerpos (contraseñas) ni la cadena
  `X-Forwarded-For` completa: solo la IP ya resuelta. Ningún endpoint lleva
  tokens en la query string, y la aplicación no registra cabeceras ni cuerpos.
  `test_login_proxy_rate_limit.py` lo comprueba con un uvicorn real.
- Si se deja de aceptar, arrancar uvicorn con `--no-access-log` (se pierde la
  verificación del paso 1).

## Protección de datos

- `aws_efs_file_system.postgres` tiene `prevent_destroy = true`: cualquier plan
  que lo destruya o reemplace falla, incluido `terraform destroy` del stack.
- Los repositorios ECR tienen `force_delete = false`: `destroy` falla si aún
  contienen imágenes, en lugar de borrarlas sin aviso. En un stack existente, el
  primer plan tras este cambio muestra dos updates in-place de `force_delete`
  (`true -> false`): solo cambian el state, no los repositorios.
- `enable_efs_backup` (por defecto `false`) crea una `aws_efs_backup_policy`
  que activa las copias automáticas de EFS: AWS Backup crea el plan y el vault
  `aws/efs/automatic-backup-*` (copia diaria, retención de 35 días) y ejecuta
  los jobs con `AWSBackupDefaultServiceRole`. En AWS Academy `LabRole` no puede
  crear ese rol: el `apply` puede terminar bien y los jobs fallar sin aviso.
  Tras activarla, comprobar `aws backup list-backup-jobs` y probar una
  restauración. Las copias se facturan aunque el lab esté detenido, y los
  recovery points se conservan hasta caducar aunque se desactive la política.
- El respaldo soportado es `scripts/export_postgres.sh`: deja el dump en
  `data/backups/` (S3 se usa como puente y conserva una copia salvo
  `--delete-remote`).
- `prevent_destroy` protege frente a Terraform, no frente a la consola, la CLI
  ni `terraform state rm`. Con state local, perder el state deja el EFS
  huérfano y un `apply` posterior crearía otro: resguardar el state.

## Destruir la infraestructura

`terraform destroy` falla mientras el EFS tenga `prevent_destroy`. Para
destruir de forma consciente:

1. Exportar Postgres con `scripts/export_postgres.sh` y comprobar el dump.
2. En un cambio revisado, quitar temporalmente `prevent_destroy` de `efs.tf`.
3. Vaciar los repositorios ECR. Alternativa: poner `force_delete = true` y
   ejecutar primero un `apply` (el borrado usa el valor guardado en el state, no
   el de la configuración), aceptando que se borren las imágenes; se
   reconstruyen con `scripts/push_images.sh`.
4. `terraform plan -destroy`, revisar el plan y ejecutar `terraform destroy`,
   confirmando en el prompt.
5. Restaurar las protecciones antes del siguiente despliegue.
