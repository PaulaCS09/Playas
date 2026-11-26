# Despliegue con `scripts/playas.sh`

La CLI no sustituye a los scripts existentes: los ejecuta en orden y añade
comprobaciones de solo lectura. Ningún comando ejecuta `destroy`,
`-auto-approve`, `-migrate-state`, `import`, `state rm/push` ni `force-unlock`. Esto lo verifica una comprobación estática por texto de los `*.sh` versionados
(`terraform … import|destroy|-destroy|state rm|mv|push`, `-migrate-state`, `force-unlock`; con
`-chdir`, continuaciones, pipes y `$(…)`/backticks; ignora mensajes echo/log) que no detecta
invocaciones construidas indirectamente (subcomando en variable o `eval`). `-auto-approve` se
comprueba solo en los `deploy.sh` de los tres stacks, el bastion y `scripts/playas.sh`. Ver O1 en
[CONTROLS.md](CONTROLS.md).

| Comando | Cambia AWS | Notas |
|---|---|---|
| `doctor` | no | Herramientas, Terraform 1.17.1, región, credenciales, cuenta, `backend.hcl`, bucket del state, states locales, permisos de tfvars, contratos (rechaza placeholders; avisa si `legacy_public_service_access=true`) |
| `bootstrap [--verify-only]` | sí (solo el bucket del state) | `scripts/bootstrap_tfstate_bucket.sh`; pide confirmación |
| `plan <stack\|all>` | no (toma y libera el lock) | `init` con backend S3 + `plan -input=false`; no guarda planes |
| `deploy <stack\|all>` | sí | `<stack>/scripts/deploy.sh`; apply interactivo |
| `status` | no | State por stack, locks, EC2, EIP (cuenta y avisa si no son 2 o si alguna no está asociada; no imprime IPs), ECS, ALB, EFS, costes fijos |
| `health` | no | ChromaDB heartbeat, Ollama `/api/tags`, `/api/health`, `/api/ready`, `/` y targets del ALB |
| `backup-state <stack\|all>` | no | `state pull` → GPG, fuera del clon |
| `start` / `stop` | sí | `rag/infrastructure/scripts/{start,stop}.sh`; exige terminal interactiva y escribir literalmente `start` o `stop` (`cmd_compute` en `scripts/playas.sh`) |
| `test [static\|unit\|all]` | no | Las mismas validaciones que la CI, sin credenciales |

## Requisitos

Terraform 1.17.1, AWS CLI v2, curl, openssl, git; además docker (imágenes de
rag), session-manager-plugin (ECS Exec), uv (importación a ChromaDB y tests) y
gpg (respaldos). `doctor` indica cuáles faltan.

Credenciales del Learner Lab en el entorno (*AWS Details → AWS CLI*):

```bash
export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… AWS_SESSION_TOKEN=…
export AWS_REGION=us-east-1
export RAG_EXPECTED_AWS_ACCOUNT_ID=<12 dígitos>   # recomendado: evita operar en otra cuenta
```

## Primer despliegue en una cuenta nueva

```bash
scripts/playas.sh doctor                       # 1. corregir los errores que indique
scripts/playas.sh bootstrap                    # 2. bucket del state (escribir 'si'; ver nota)
cp backend.hcl.example backend.hcl             #    y sustituir <ACCOUNT_ID>
cp vector-infraestructura/terraform.tfvars.example vector-infraestructura/terraform.tfvars
chmod 600 vector-infraestructura/terraform.tfvars   # 3. ajustar operator_cidr_blocks, etc.
scripts/playas.sh plan ingesta                 # 4. revisar
ALLOW_EMPTY_STATE=1 scripts/playas.sh deploy ingesta
#    copiar el s3_bucket_name mostrado a rag/infrastructure/terraform.tfvars
ALLOW_EMPTY_STATE=1 scripts/playas.sh deploy vector
#    añadir al menos una API key de LLM a rag/infrastructure/terraform.tfvars
scripts/playas.sh doctor                       # 5. contratos de rag completos
ALLOW_EMPTY_STATE=1 scripts/playas.sh deploy rag
scripts/playas.sh health                       # 6. comprobar
scripts/playas.sh backup-state all             # 7. copia cifrada fuera de AWS
```

Antes de escribir `si`, si `RAG_EXPECTED_AWS_ACCOUNT_ID` no está exportada,
`bootstrap` pide escribir el ID de la cuenta detectada (`cmd_bootstrap` en `scripts/playas.sh`);
conviene exportarla para evitar operar en otra cuenta.

`ALLOW_EMPTY_STATE=1` se pone solo como prefijo de esa ejecución. Aun con ella,
el deploy se detiene si la key del stack tuvo versiones en el bucket, si ya
existen recursos del stack o si no se escribe el nombre del stack.

`plan rag` antes de `deploy vector` falla por falta de variables: es el
orden de dependencias, no un error.

## Actualizaciones

```bash
scripts/playas.sh doctor
scripts/playas.sh plan <stack>        # revisar el plan
scripts/playas.sh deploy <stack>      # Terraform muestra el plan otra vez y pide 'yes'
```

Si el plan que muestra `deploy` difiere del revisado con `plan`, responder
`no` y revisar. Cualquier `destroy` o reemplazo del EFS, las EC2 vectoriales o el
bucket de datos está bloqueado por `prevent_destroy`; no desactivarlo sin una decisión
explícita.

## Alternativa: despliegue desde un bastion

El despliegue oficial es desde WSL con `scripts/playas.sh`. `scripts/deploy_bastion_ec2.sh`
(EC2 temporal con las herramientas instaladas) es una alternativa; su detalle está en el
README raíz, sección "Alternativa: desplegar desde una EC2 bastion". Límites que conviene
conocer (todos verificables en el script):

- **No despliega ingesta.** Solo ejecuta `vector-infraestructura/scripts/deploy.sh` y
  `rag/infrastructure/scripts/deploy.sh` (pasos "Desplegando infraestructura vectorial" y "Desplegando aplicación RAG"). `s3_bucket_name` debe
  venir de un despliegue previo de ingesta (`scripts/playas.sh deploy ingesta`).
- **Clona `https://github.com/jpospinalo/playas.git`** (`REPO_URL`) y, si
  `~/playas` ya existe, se detiene si la rama no es `main` (variable `BRANCH`).
- **No copia `vector-infraestructura/terraform.tfvars`** (copia `backend.hcl`, la clave de
  OpenAI por stdin y, por `scp`, el último export `data/*.jsonl.gz` y `data/seed_users.sql`
  si existen). Paso manual en el bastion, antes de repetir o continuar el despliegue de
  vector (`vector-infraestructura/scripts/deploy.sh` se detiene si falta
  `legacy_public_service_access`):

  ```bash
  ssh -i /ruta/a/vockey.pem ubuntu@<IP-del-bastion>
  cd ~/playas/vector-infraestructura
  umask 077
  cp terraform.tfvars.example terraform.tfvars   # queda con permisos 600
  # editar: legacy_public_service_access (true/false) y
  # operator_cidr_blocks = ["<IP pública del bastion>/32"]
  ```

## Respaldo cifrado del state

```bash
# Con clave pública (recomendado; sin interacción):
PLAYAS_BACKUP_GPG_RECIPIENT=<id o correo de la clave> scripts/playas.sh backup-state all
# Sin clave: gpg pide una frase de paso (AES256), requiere terminal.
scripts/playas.sh backup-state all
```

- El state se lee con `terraform state pull` a memoria y se cifra
  directamente: nunca se escribe en claro en disco.
- Destino: `PLAYAS_STATE_BACKUP_DIR` (default `~/playas-state-backups`, 700),
  archivos `<stack>-<UTC>.tfstate.gpg` (600) con su `.sha256`. Se rechaza un
  destino dentro del clon.
- Debe ejecutarse desde el equipo del integrante (fuera de AWS); en una EC2
  la CLI avisa. Copiar después los `.gpg` a un almacenamiento personal.
- Restaurar es manual: `gpg --decrypt` y seguir "Recuperar una versión
  anterior del state" en el README raíz. Antes, preferir las versiones del
  propio bucket (versionado activado).

## Validación local y CI

```bash
scripts/playas.sh test static   # fmt, validate, terraform test (mock), shellcheck
scripts/playas.sh test unit     # tests de scripts con aws/terraform falsos
```

El job `infra-static` de `.github/workflows/ci.yml` ejecuta
`scripts/playas.sh test static`; `rag-test` ejecuta todos los tests unitarios.
Ninguno usa credenciales ni backend.
