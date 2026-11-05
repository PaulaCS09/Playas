# ATLAS — Orientación normativa y jurisprudencial sobre playas

Sistema agéntico de orientación normativa y jurisprudencial sobre playas en Colombia.

## Organización y documentación

| Directorio | Responsabilidad | Documentación |
|---|---|---|
| `rag/` | API, agente, frontend y evaluación | [RAG](rag/README.md) |
| `ingesta/` | Conversión, seccionado y enriquecimiento de documentos | [Ingesta](ingesta/README.md) |
| `vector-infraestructura/` | EC2 de ChromaDB y Ollama | [Infraestructura vectorial](vector-infraestructura/README.md) |
| `rag/infrastructure/` | ECS, ALB, ECR y EFS de la aplicación | [Infraestructura RAG](rag/infrastructure/README.md) |
| `ingesta/infrastructure/` | Bucket S3 de las capas del pipeline | [Scripts de ingesta](ingesta/docs/SCRIPTS.md#infraestructura-responsabilidades) |
| `data/` | Corpus versionado, respaldo de ChromaDB y datos semilla | [Datos](data/README.md) |
| `scripts/` | Herramientas que coordinan varios componentes | Despliegue desde bastion, descrito abajo |

`rag/` e `ingesta/` son proyectos Python independientes, con sus propios
entornos y locks. El frontend utiliza Bun dentro del workspace de `rag/`.
No se necesita mover carpetas ni compartir un entorno para trabajar en ellos.
Para desarrollo local, empezar por el README del componente. Ver
[CONTRIBUTING.md](CONTRIBUTING.md) para ramas, alcance y controles de cambios.

La guía siguiente describe el despliegue AWS con HTTP. Provisionar o importar
datos no es lo mismo que encender una aplicación ya desplegada: para uso diario
consultar [encendido y apagado](#apagarencender-el-cómputo-ahorro-de-costos).

---

## Requisitos previos

| Herramienta | Versión mínima |
|---|---|
| Terraform | 1.17.1 (`~> 1.17.1`) |
| AWS CLI | 2.x |
| Session Manager Plugin (AWS) | cualquiera |
| Docker | 24.x |
| Python + uv | cualquiera |

**Credenciales AWS** con permisos para EC2, ECS, ECR, ALB, EFS y IAM (rol `LabRole` en AWS Academy):

> No es necesario configurar credenciales si se usa una máquina Cloud9, pero esta debe tener al menos 100 GB en disco.

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...      # solo AWS Academy
```

- **Instalar Terraform (Ubuntu):**

```bash
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(grep -oP '(?<=UBUNTU_CODENAME=).*' /etc/os-release || lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
```

- **Instalar uv:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- **Instalar Session Manager Plugin (Ubuntu):** requerido por `deploy.sh` para el paso de ECS Exec (`aws ecs execute-command`) que importa los usuarios semilla a PostgreSQL.

```bash
# x86_64 (usar ubuntu_arm64 en la URL si `uname -m` devuelve aarch64)
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb
session-manager-plugin   # debe responder "was installed successfully"
```

## Despliegue automático

### 1. Infraestructura vectorial (ChromaDB + Ollama en EC2)

El script crea las EC2 con IP estática, actualiza las variables de entorno del proyecto con las IPs resultantes e importa los datos en ChromaDB:

```bash
./vector-infraestructura/scripts/deploy.sh
```

Los scripts de despliegue requieren una terminal interactiva: `terraform apply`
muestra el plan y espera confirmación humana (`--auto-approve` no está
permitido, y el script se detiene si `TF_CLI_ARGS*` o `TF_INPUT` están
definidas). Antes de cada apply respaldan el state en `~/tfstate-backups/`
(configurable con `TFSTATE_BACKUP_DIR`), fuera del clon y con permisos `600`:
el archivo local o, con backend S3, `terraform state pull` (ver
[Terraform State remoto](#terraform-state-remoto)).

Al finalizar, `rag/.env` y `rag/infrastructure/terraform.tfvars` quedan actualizados con `CHROMA_HOST` y `OLLAMA_BASE_URL`.

### 2. Aplicación RAG en ECS Fargate

El paso anterior generó `rag/infrastructure/terraform.tfvars` con `postgres_password` y `jwt_secret_key` ya completados. Solo falta agregar al menos una API key de LLM:

```hcl
# Al menos una de las siguientes
openai_api_key     = "..."   # OpenAI (prioridad 1)
openrouter_api_key = "..."   # OpenRouter (prioridad 2)
google_api_key     = "..."   # Gemini (prioridad 3)
```

Despliega la infraestructura ECS, construye las imágenes y las sube a ECR:

```bash
./rag/infrastructure/scripts/deploy.sh
```

Si un security group existe en AWS con un ID distinto al del state, el script
se detiene sin modificar el state e indica los comandos `state rm`/`import`
para que una persona los revise y ejecute manualmente.

### 3. Verificar

```bash
# URL pública
terraform -chdir=rag/infrastructure output -raw alb_url

# Logs en tiempo real
aws logs tail /ecs/rag-playas-prod-app      --region us-east-1 --follow
aws logs tail /ecs/rag-playas-prod-frontend --region us-east-1 --follow
```

La salida `alb_url` ya incluye `http://`; no agregar otro prefijo:

- `/` → Frontend Next.js
- `/api/` → Backend FastAPI
- `/api/health` → Healthcheck

### 4. Exportar la base de datos PostgreSQL

Fargate no permite `docker cp` ni montar el volumen EFS directamente, así que
el script sube el dump a un bucket S3 puente (vía URL prefirmada) y luego lo
descarga localmente en `data/backups/`:

```bash
./rag/infrastructure/scripts/export_postgres.sh --bucket <nombre-bucket-s3>
```

Por defecto conserva la copia en el bucket S3; usar `--delete-remote` para
borrarla tras la descarga. Requiere el Session Manager Plugin (mismo requisito
que `deploy.sh`, usa `aws ecs execute-command`).

El dump contiene datos personales: la copia local queda con permisos `600` y la
URL prefirmada de subida caduca a los 10 minutos. Como se firma antes de
`pg_dump`, una base muy grande puede hacer que la subida falle con HTTP 403; en
ese caso el script se detiene sin dejar un dump local incompleto.

### Alternativa: desplegar desde una EC2 bastion

Si no quieres instalar Terraform, Docker, AWS CLI, etc. localmente, `scripts/deploy_bastion_ec2.sh`
crea una EC2 temporal que hace todo el trabajo por ti: instala las dependencias, clona el
repositorio y ejecuta los pasos 1 y 2 del despliegue automático descritos arriba.

Requisitos:

- Key pair `vockey` ya creado en EC2 (estándar en AWS Academy) y su `.pem` descargado localmente
- `rag/.env` con `OPENAI_API_KEY` configurada (se transfiere a la EC2 por stdin, nunca queda
  expuesta en logs ni en el script)
- `backend.hcl` en la raíz del repositorio (bucket del state de tu cuenta; ver
  [Terraform State remoto](#terraform-state-remoto)). El script lo comprueba antes de crear
  nada y lo copia al bastion.
- `BASTION_SSH_CIDR` con la IP pública autorizada para SSH, con máscara `/32`. El script
  rechaza `0.0.0.0/0`. Si reutiliza el security group del bastion, se detiene cuando alguna
  regla que alcance el puerto 22/tcp (IPv4 o IPv6, protocolo `-1` o un rango de puertos)
  admite un origen distinto de `BASTION_SSH_CIDR`, y muestra el comando `revoke` para que una
  persona lo ejecute. Si falta la regla `/32`, muestra el security group, la IP y la regla
  `tcp/22` que va a crear, y la añade.
- Una terminal interactiva: cada `terraform apply` se ejecuta por `ssh -t` y pide confirmación.
  Para que un corte de red no interrumpa un apply, conviene trabajar dentro de `tmux` en la
  máquina local.

```bash
BASTION_SSH_CIDR=203.0.113.10/32 KEY_FILE=/ruta/a/vockey.pem ./scripts/deploy_bastion_ec2.sh
```

La EC2 (`m4.large`, Ubuntu 24.04, 50GB, instance profile `LabInstanceProfile`) queda como bastion
tras el despliegue — puedes conectarte por SSH para depurar o repetir pasos manualmente. El script
imprime cada paso que ejecuta, tanto localmente como dentro de la instancia.

Usa `BOOTSTRAP_ONLY=1` para solo aprovisionar la EC2 e instalar dependencias (git, Docker,
Terraform, AWS CLI, uv, Session Manager Plugin) sin ejecutar los despliegues de Terraform:

```bash
BOOTSTRAP_ONLY=1 BASTION_SSH_CIDR=203.0.113.10/32 KEY_FILE=/ruta/a/vockey.pem ./scripts/deploy_bastion_ec2.sh
```

Si ya existe una instancia bastion (`rag-playas-bastion`) corriendo, el script la reutiliza en
vez de crear una nueva.

> Si el bastion ya tiene `~/playas`, el script no lo borra: copia los
> `terraform.tfstate*` a `~/tfstate-backups/pre-pull-<fecha>/` y actualiza el
> clon con `git pull --ff-only` (los states y tfvars están ignorados por Git).
> Esas copias siguen dentro de la cuenta del Learner Lab: el responsable de
> infraestructura debe resguardar los states fuera de AWS antes de repetir el
> despliegue. No usar este script para encendido/apagado diario.
>
> El script se detiene sin actualizar el clon si `~/playas` no está en `main` o
> tiene cambios en archivos versionados (por ejemplo, un `.terraform.lock.hcl`
> reescrito por un `init -upgrade` antiguo o un `data/seed_users.sql` copiado
> distinto del versionado). Revisarlos con `git -C ~/playas status` y
> `git -C ~/playas diff`, y descartarlos o guardarlos manualmente antes de repetir.
>
> Si la ejecución termina antes de configurar la API key (por ejemplo, porque se
> rechazó el primer apply), el script borra `~/.openai_api_key` del bastion; si no
> lo consigue, lo indica para borrarlo manualmente.

---

## Despliegue manual

### 1. Infraestructura vectorial (ChromaDB + Ollama en EC2)

```bash
cd vector-infraestructura
terraform init -backend-config=../backend.hcl   # ver "Terraform State remoto"
terraform apply

# Ver las IPs asignadas
terraform output
```

Actualiza manualmente `rag/.env` y `rag/infrastructure/terraform.tfvars` con los valores obtenidos:

```bash
# rag/.env
CHROMA_HOST=<chromadb_public_ip>
OLLAMA_BASE_URL=http://<ollama_public_ip>:11434

# rag/infrastructure/terraform.tfvars
chroma_host     = "<chromadb_public_ip>"
ollama_base_url = "http://<ollama_public_ip>:11434"
```

Importa los datos en ChromaDB una vez que la instancia esté disponible:

```bash
cd rag
uv run python3 ../data/import_to_chromadb.py \
  ../data/<archivo>.jsonl.gz \
  --host <chromadb_public_ip> \
  --port 8000
```

### 2. Aplicación RAG en ECS Fargate

Configura las variables de entorno:

```bash
cp rag/infrastructure/terraform.tfvars.example rag/infrastructure/terraform.tfvars
# Editar terraform.tfvars con todos los valores requeridos
```

Inicializa Terraform, aplica la infraestructura, construye las imágenes y fuerza el redespliegue:

```bash
cd rag/infrastructure
terraform init -backend-config=../../backend.hcl
terraform apply

./scripts/push_images.sh

aws ecs update-service --cluster rag-playas-prod --service rag-playas-prod-app \
  --force-new-deployment --region us-east-1

aws ecs update-service --cluster rag-playas-prod --service rag-playas-prod-frontend \
  --force-new-deployment --region us-east-1
```

---

## Terraform State remoto

Los tres stacks (`ingesta/infrastructure`, `vector-infraestructura`,
`rag/infrastructure`) declaran `backend "s3"` con `use_lockfile = true` y una
key fija por stack (`<stack>/terraform.tfstate`). Cada integrante usa **un
bucket en su propia cuenta de Learner Lab**, `rag-playas-tfstate-<account_id>`
(us-east-1), cuyo nombre va en `backend.hcl` en la raíz del repositorio (no
versionado; ver `backend.hcl.example`). Terraform: `~> 1.17.1` en los stacks,
1.17.1 en la CI y en el bastion.

### 1. Crear el bucket (una vez por cuenta)

```bash
RAG_EXPECTED_AWS_ACCOUNT_ID=<12 dígitos> ./scripts/bootstrap_tfstate_bucket.sh
cp backend.hcl.example backend.hcl   # poner el bucket que muestra el script
RAG_EXPECTED_AWS_ACCOUNT_ID=<12 dígitos> ./scripts/bootstrap_tfstate_bucket.sh --verify-only
```

El script comprueba que las credenciales son de esa cuenta, pide confirmación
y deja el bucket con BucketOwnerEnforced, Block Public Access completo,
versionado, SSE-S3 y una política que deniega peticiones sin TLS. No borra
nada ni configura expiración de versiones. `--verify-only` solo lee.

### 2. Migrar el state local existente (manual, una sola persona por stack)

Los scripts **nunca** migran: si el stack usa backend S3 y queda un
`terraform.tfstate` local, `deploy.sh` se detiene. La migración la hace una
persona, desde la copia autoritativa del state (normalmente el bastion), un
stack cada vez y en este orden: `ingesta/infrastructure`,
`vector-infraestructura`, `rag/infrastructure`. Cada paso requiere aprobación.

1. Congelar los applies de ese stack y respaldar el state local fuera del clon
   (y una copia cifrada fuera de AWS). Anotar `lineage`, `serial` y
   `terraform state list`.
2. Desde una terminal interactiva (en el bastion, por `ssh -t`):
   `terraform -chdir=<stack> init -migrate-state -backend-config=<raíz>/backend.hcl`
   y responder `yes` a la copia. Nunca `-force-copy`.
3. Comprobar: mismo `lineage`, `serial` igual (o +1), mismo `state list`,
   `terraform plan` sin cambios y el objeto `<stack>/terraform.tfstate` en S3.
4. Mover (no borrar) el `terraform.tfstate*` local que quede a
   `~/tfstate-backups/<stack>/pre-s3-<fecha>/` con permisos 600.

Los demás clones de ese stack, sin state local, se inicializan una vez con
`terraform -chdir=<stack> init -reconfigure -backend-config=<raíz>/backend.hcl`
(no copia nada). Si un `deploy.sh` informa de que `init` falló porque el clon
estaba inicializado con otro backend, es este paso.

### 3. Comportamiento de los scripts con backend S3

- `deploy.sh` exige `backend.hcl` y ejecuta `init -backend-config=<raíz>/backend.hcl`.
- Antes de cada apply, `terraform state pull` se guarda en
  `~/tfstate-backups/<stack>/<fecha>/remote.tfstate` (600). Si el pull falla
  (backend o credenciales) el despliegue se detiene; si el state está vacío
  también, salvo `ALLOW_EMPTY_STATE=1` para un stack nuevo.
- `check_sg_drift` (rag) lee `terraform state list` una vez: si falla, se
  detiene sin sugerir `import`.
- Los respaldos siguen dentro de la cuenta del lab y en texto plano (600):
  copiarlos cifrados fuera de AWS sigue siendo responsabilidad del integrante.
- Solo se ejecutan a mano, nunca desde scripts: `-migrate-state`,
  `-reconfigure`, `-force-copy`, `terraform state push` y
  `terraform force-unlock`. Un test estático lo comprueba.
- La CI valida con `init -backend=false`: no usa el bucket ni credenciales.

### 4. Rollback de un stack migrado

- Sin applies después de migrar: quitar el bloque `backend` en un cambio
  revisado y `terraform init -migrate-state` hacia local; comprobar `lineage`
  y `serial` contra S3.
- Con applies después de migrar: igual, siempre con `-migrate-state` desde S3
  (restaurar la copia previa dejaría recursos fuera del state).
- Después, retirar el objeto de S3 (y cualquier `.tflock`) para no tener dos
  fuentes de verdad.
- Un lock huérfano (credenciales caducadas a mitad de un apply) se libera con
  `terraform force-unlock <ID>` solo tras confirmar que no hay otro proceso.

## Arquitectura desplegada

```
Internet
   └── ALB (puerto 80)
        ├── /api/*  → ECS Service: backend (FastAPI :8080) + postgres sidecar (:5432)
        └── /*      → ECS Service: frontend (Next.js :3000)

EFS         ←→ postgres  (persistencia)
ChromaDB EC2 ←→ backend  (índice vectorial)
Ollama EC2   ←→ backend  (embeddings)
```

| Servicio | Tipo | Puerto |
|---|---|---|
| Backend (FastAPI) | ECS Fargate 1024 CPU / 2048 MB | 8080 |
| Frontend (Next.js) | ECS Fargate 256 CPU / 512 MB | 3000 |
| ChromaDB | EC2 `t3.large` 30 GB | 8000 |
| Ollama | EC2 `t3.large` 20 GB | 11434 |

---

## Apagar/encender el cómputo (ahorro de costos)

`stop.sh` apaga primero ambos servicios ECS (`app` y `frontend`) y confirma
que quedaron en cero; después detiene las EC2 de ChromaDB y Ollama. No elimina
EFS, EBS, índices ni conversaciones:

```bash
./rag/infrastructure/scripts/stop.sh
```

`start.sh` verifica primero las EC2 y sus servicios HTTP, enciende ECS y solo
declara éxito cuando el backend (`/api/ready`) y el frontend responden a través
del ALB:

```bash
./rag/infrastructure/scripts/start.sh
```

> Apagar no deja el costo en cero: ALB, EFS, EBS, CloudWatch y Elastic IP siguen
> existiendo. No ejecutar ambos scripts simultáneamente ni apagar mientras
> ingesta, evaluación o mantenimiento estén usando ChromaDB/Ollama. Requieren
> credenciales AWS vigentes; iniciar primero el lab si se utiliza AWS Academy.
> Ver [requisitos y comprobaciones](rag/infrastructure/README.md#encendido-y-apagado-seguro-startsh--stopsh).

---

## Destruir la infraestructura

```bash
# ECS Fargate — revisar el plan y exportar PostgreSQL antes de destruir
terraform -chdir=rag/infrastructure destroy

# EC2 vectorial
terraform -chdir=vector-infraestructura destroy
```

> El EFS de PostgreSQL y las EC2 de ChromaDB/Ollama tienen `prevent_destroy`,
> así que estos `destroy` fallan hasta que se retire esa protección en un cambio
> revisado. Antes, exportar PostgreSQL y conservar un respaldo de ChromaDB:
> destruir o reemplazar las EC2 elimina sus discos e índice. Ver los
> procedimientos en [rag/infrastructure/README.md](rag/infrastructure/README.md)
> y [vector-infraestructura/README.md](vector-infraestructura/README.md). Para
> ahorrar cómputo sin destruir datos, usar `stop.sh`.
