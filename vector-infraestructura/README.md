# vector-infraestructura

Infraestructura Terraform para las instancias EC2 que soportan el sistema RAG:

- **Ollama** — servidor de embeddings (`embeddinggemma:latest`) en el puerto 11434
- **ChromaDB** — base de datos vectorial (`chromadb/chroma:1.3.5`) en el puerto 8000

Cada instancia recibe una **Elastic IP estática** que no cambia entre reinicios.

---

## Requisitos previos

| Herramienta | Versión mínima | Verificar |
|---|---|---|
| Terraform | 1.7 | `terraform version` |
| AWS CLI | 2.x | `aws --version` |
| Python + uv | cualquiera | `uv --version` |
| curl | cualquiera | `curl --version` |

**Credenciales AWS** configuradas con permisos para crear EC2, Elastic IP y Security Groups:

```bash
# Opción A — variables de entorno (AWS Academy / voclabs)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...

# Opción B — perfil en ~/.aws/credentials
aws configure --profile rag-playas
export AWS_PROFILE=rag-playas
```

**Key pair de EC2** existente en la región `us-east-1`. El nombre por defecto es `vockey`; se puede cambiar en `variables.tf` o pasando `-var key_pair_name=<nombre>`.

---

## Despliegue con el script

El script `scripts/deploy.sh` automatiza todo el proceso en un solo comando.

### Ejecución

Desde la raíz del repositorio (`playas/`):

```bash
./vector-infraestructura/scripts/deploy.sh
```

El script requiere una terminal interactiva: `terraform apply` siempre pide
confirmación humana y `--auto-approve` no está permitido. `terraform init` se
ejecuta sin `-upgrade` para respetar `.terraform.lock.hcl`, y antes del apply el
state local se copia a `~/tfstate-backups/` (configurable con `TFSTATE_BACKUP_DIR`).

### Qué hace el script

```
0. terraform init           — sin -upgrade; copia el state a ~/tfstate-backups/
1. terraform apply          — crea/actualiza las EC2 con sus Elastic IPs (pide confirmación)
2. terraform output         — lee las IPs estáticas resultantes
3. Actualiza rag/.env       — CHROMA_HOST y OLLAMA_BASE_URL
4. Actualiza rag/infrastructure/terraform.tfvars — chroma_host y ollama_base_url
5. Espera que ChromaDB responda (máx. 15 min)
6. Importa data/*.jsonl.gz  — carga los documentos en ChromaDB
```

### Salida esperada

```
▶ Ejecutando terraform apply en vector-infraestructura...
  ... (plan de Terraform) ...
▶ Leyendo IPs estáticas desde Terraform outputs...
✓ ChromaDB IP: 54.x.x.x
✓ Ollama IP:   3.x.x.x
▶ Actualizando rag/.env...
✓ .env actualizado
▶ Actualizando rag/infrastructure/terraform.tfvars...
✓ terraform.tfvars actualizado
▶ Esperando a que ChromaDB esté disponible en 54.x.x.x:8000...
   (la EC2 necesita instalar Docker y arrancar el contenedor — puede tardar varios minutos)
..........
✓ ChromaDB disponible
▶ Importando chroma-rag_playas_magdalena-20260731-135720.jsonl.gz → ChromaDB 54.x.x.x:8000...
  Importing 1842 documents in batches of 100 ...
  1842/1842 (100.0%)  312 docs/s  errors=0
✓ Importación completada

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Despliegue completado

  CHROMA_HOST     = 54.x.x.x
  OLLAMA_BASE_URL = http://3.x.x.x:11434
  postgres_password y jwt_secret_key: configurados en terraform.tfvars (no se muestran)

  Archivos actualizados:
    • rag/.env
    • rag/infrastructure/terraform.tfvars

  Para aplicar en ECS Fargate:
    cd rag/infrastructure && terraform apply
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Después del script

Si también tienes el stack ECS Fargate desplegado, aplica los nuevos valores:

```bash
cd rag/infrastructure
terraform apply
```

---

## Despliegue manual paso a paso

Si prefieres ejecutar cada paso por separado:

```bash
# 1. Inicializar Terraform (solo la primera vez)
cd vector-infraestructura
terraform init

# 2. Ver el plan
terraform plan

# 3. Aplicar
terraform apply

# 4. Ver las IPs asignadas
terraform output

# 5. Importar datos a ChromaDB manualmente
cd ../rag
uv run python3 ../data/import_to_chromadb.py \
  ../data/chroma-rag_playas_magdalena-20260731-135720.jsonl.gz \
  --host <chromadb_public_ip> \
  --port 8000
```

---

## Archivos de importación

El script toma el `.jsonl.gz` más reciente que encuentre en `data/`. Estos archivos incluyen los embeddings, por lo que reimportarlos con `data/import_to_chromadb.py` no requiere Ollama. En `main` no hay todavía un exportador de ChromaDB: antes de cualquier operación que pueda perder el índice, conservar el `.jsonl.gz` usado en la última importación y un snapshot manual del volumen raíz de la EC2.

---

## Persistencia de los datos

ChromaDB guarda el índice en `/opt/chroma-data`, en el **volumen raíz** (EBS gp3
de 30 GB) de su EC2. Ollama guarda los modelos en el volumen raíz de la suya.

| Operación | Datos de ChromaDB / modelos de Ollama |
|---|---|
| `stop.sh` / `start.sh` (detener y arrancar) | Se conservan: el EBS raíz persiste y el contenedor se reinicia solo |
| Reemplazo de la instancia (`-replace`, cambio de AMI, cambio que fuerce reemplazo) | **Se pierden**: el volumen raíz se borra con la instancia |
| `terraform destroy` | **Se pierden** |

Para evitar pérdidas accidentales, `aws_instance.chromadb` y `aws_instance.ollama`
tienen `lifecycle { prevent_destroy = true; ignore_changes = [ami, user_data] }`:

- Una AMI nueva de Canonical (el data source usa `most_recent`) o un cambio en
  los scripts de arranque **no** generan un reemplazo; el plan no los muestra.
  `user_data` solo se ejecuta en el primer arranque, así que un cambio en
  `scripts/ec2_*.sh` solo afecta a instancias nuevas.
- Cualquier plan que destruya o reemplace una de estas EC2 falla con
  `Instance cannot be destroyed`, incluido `terraform destroy`.
- `prevent_destroy` solo actúa sobre planes de Terraform. No impide terminar la
  instancia, borrar su volumen o cambiarla desde la consola o la CLI de AWS, ni
  `terraform state rm`. Con state local, si el state se pierde, las EC2 quedan
  fuera de Terraform y un `apply` posterior intentaría crear todo de nuevo en
  lugar de reconocer lo existente: fallaría por los security groups con el mismo
  nombre o dejaría recursos duplicados (EIPs, instancias vacías). Resguardar el state (los scripts de
  despliegue lo copian a `~/tfstate-backups/`) y no modificar estos recursos
  fuera de Terraform.

### Actualizar deliberadamente la AMI o el `user_data`

Con `ignore_changes = [ami, user_data]`, ni una AMI nueva ni un cambio en
`scripts/ec2_*.sh` aparecen en el plan. Aplicarlos implica recrear la instancia
(la AMI no se cambia in-place y `user_data` solo corre en el primer arranque),
así que se sigue el procedimiento de la sección siguiente: respaldar, retirar
temporalmente `prevent_destroy`, revisar `terraform plan -replace=...` y aplicar.
Con `-replace` no hace falta retirar `ignore_changes`. Alternativa para la AMI:
retirar también `ami` de `ignore_changes` en ese mismo cambio revisado y
comprobar que el plan muestra `must be replaced` solo en la instancia prevista.
No retirar `user_data` para este fin: el provider lo actualiza in-place
(detiene y arranca la instancia) sin volver a ejecutar el script, así que no
aplica el cambio; usar `-replace`. Restaurar las protecciones retiradas en un
cambio posterior.

## Reemplazar o destruir las EC2

Hacerlo solo de forma consciente y revisada:

1. Conservar el índice: el `.jsonl.gz` de la última importación (en `data/`) y,
   si el índice cambió desde entonces, un snapshot manual del volumen raíz.
2. En un cambio revisado, quitar temporalmente `prevent_destroy` del recurso
   afectado. No hace falta tocar `ignore_changes`: con `-replace`, la instancia
   nueva se crea con la AMI más reciente y el `user_data` actuales.
3. `terraform plan -replace=aws_instance.chromadb` (o `plan -destroy`) y
   comprobar que solo se reemplaza o destruye lo previsto. Después
   `terraform apply -replace=aws_instance.chromadb` o `terraform destroy`,
   confirmando en el prompt.
4. Si se recreó ChromaDB: reimportar con `data/import_to_chromadb.py` y comprobar
   el recuento de documentos. Si se recreó Ollama: su `user_data` vuelve a
   descargar `embeddinggemma`, y el RAG no funcionará hasta que termine. La
   Elastic IP se reasocia sola, así que `chroma_host`/`ollama_base_url` no cambian.
5. Restaurar `prevent_destroy` en un cambio posterior: debe estar fuera de la
   configuración durante ese `apply`.

> Las Elastic IPs se liberan con `terraform destroy`.

---

## Variables

| Variable | Por defecto | Descripción |
|---|---|---|
| `project` | `rag-playas` | Prefijo usado en nombres y tags |
| `environment` | `prod` | Entorno (`dev`, `staging`, `prod`) |
| `key_pair_name` | `vockey` | Key pair EC2 para acceso SSH |

Para sobreescribir alguna variable crea un archivo `terraform.tfvars` en este directorio:

```hcl
key_pair_name = "mi-key-pair"
environment   = "dev"
```
