# shellcheck shell=bash
# Funciones compartidas por vector-infraestructura/scripts/deploy.sh y
# rag/infrastructure/scripts/deploy.sh para inicializar Terraform y respaldar
# el state, con backend local o S3. Se carga con `source`; no es ejecutable.
#
# Fallan de forma cerrada: un error del backend o de credenciales detiene el
# despliegue; nunca se toma como "no hay state".
#
# Variables opcionales:
#   TF_BACKEND_CONFIG   archivo -backend-config (default: <raíz>/backend.hcl)
#   TFSTATE_BACKUP_DIR  destino de los respaldos (default: ~/tfstate-backups)
#   ALLOW_EMPTY_STATE=1 acepta un state remoto vacío (stack nuevo)
#
# Nunca ejecutan -migrate-state, -reconfigure, -force-copy, state push ni
# force-unlock: esas operaciones son manuales (ver README.md, "Terraform State
# remoto").

# Se fija en backup_state cuando el state está vacío (stack nuevo o sin
# despliegue previo) y lo usa tf_state_addresses. TF_REMOTE_STATE_EMPTY indica
# además que el vacío viene del backend S3 (aceptado con ALLOW_EMPTY_STATE=1).
TF_STATE_EMPTY=0
# shellcheck disable=SC2034  # la leen vector-infraestructura y rag/infrastructure (deploy.sh)
TF_REMOTE_STATE_EMPTY=0

# Valores fijos con los que se buscan recursos existentes cuando el state
# remoto está vacío (tf_guard_empty_remote_state). Deben coincidir con los
# defaults de variables.tf y la región de providers.tf de cada stack; si un
# tfvars o TF_VAR_* los cambia, la protección se detiene en vez de buscar
# nombres equivocados.
TF_GUARD_REGION="us-east-1"
TF_DEFAULT_PROJECT="rag-playas"
TF_DEFAULT_ENVIRONMENT="prod"
# Los fija tf_verify_backend_config al terminar con éxito.
TF_STATE_BUCKET=""
TF_AWS_ACCOUNT_ID=""
# Los fijan tf_backend_key, tf_guard_default_vpc y tf_probe.
TF_GUARD_KEY=""
TF_GUARD_VPC=""
TF_GUARD_FOUND=""
# Raíz real del clon (symlinks resueltos), calculada desde esta biblioteca.
TF_LIB_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

tf_fail() {
  echo "✗ $*" >&2
  exit 1
}

# tf_backend_is_s3 <directorio_terraform>: 0 si el stack declara backend "s3".
tf_backend_is_s3() {
  grep -qsE '^[[:space:]]*backend[[:space:]]+"s3"' "$1"/*.tf
}

# tf_verify_backend_config <archivo_backend>
# backend.hcl solo puede contener una línea bucket = "rag-playas-tfstate-<cuenta>"
# (más líneas en blanco y comentarios #). Cualquier otro atributo se rechaza:
# -backend-config tiene prioridad sobre el bloque backend, así que una `key`
# en este archivo, compartido por los tres stacks, haría que todos usaran el
# mismo state (verificado con Terraform 1.17.1).
# Comprueba además, solo con lecturas: que la cuenta del nombre del bucket es
# la de las credenciales actuales (y RAG_EXPECTED_AWS_ACCOUNT_ID, si está
# definida) y que el bucket existe y pertenece a esa cuenta (head-bucket con
# --expected-bucket-owner). No comprueba cifrado, versionado ni políticas del
# bucket: eso lo configura scripts/bootstrap_tfstate_bucket.sh.
tf_verify_backend_config() {
  local backend_config="$1" line n=0 attr bucket="" buckets=0 bucket_account account
  local bucket_re='^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"([^"]*)"[[:space:]]*$'
  while IFS= read -r line || [[ -n "${line}" ]]; do
    n=$((n + 1))
    [[ "${line}" =~ ^[[:space:]]*(#.*)?$ ]] && continue
    if [[ "${line}" =~ ${bucket_re} ]]; then
      bucket="${BASH_REMATCH[1]}"
      buckets=$((buckets + 1))
      continue
    fi
    # Solo el nombre del atributo, y solo si la línea tiene forma `nombre =`:
    # nunca se muestra el valor ni la línea (podría ser una credencial).
    attr="?"
    [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]{0,40})[[:space:]]*= ]] && attr="${BASH_REMATCH[1]}"
    if [[ "${attr}" == "bucket" ]]; then
      tf_fail "${backend_config}, línea ${n} (bucket): formato no admitido; usar exactamente bucket = \"rag-playas-tfstate-<account_id>\" sin comentarios en la misma línea."
    fi
    tf_fail "${backend_config}, línea ${n} (${attr}): solo se admite bucket = \"rag-playas-tfstate-<account_id>\".
  La key de cada stack está fija en su terraform.tf; no añadir key, region ni otros atributos."
  done < "${backend_config}"
  [[ "${buckets}" -eq 1 ]] \
    || tf_fail "${backend_config} debe contener exactamente una línea bucket (hay ${buckets})."
  if [[ "${bucket}" =~ ^rag-playas-tfstate-([0-9]{12})$ ]]; then
    bucket_account="${BASH_REMATCH[1]}"
  else
    tf_fail "${backend_config} debe contener bucket = \"rag-playas-tfstate-<account_id>\" (ver backend.hcl.example)."
  fi
  command -v aws >/dev/null 2>&1 || tf_fail "Se necesita aws CLI para verificar la cuenta del backend S3."
  # Región fija: el backend de los tres stacks está en us-east-1 (terraform.tf).
  if ! account=$(aws sts get-caller-identity --query Account --output text --region us-east-1); then
    tf_fail "No se pudo verificar la cuenta AWS (credenciales ausentes o caducadas)."
  fi
  [[ "${account}" == "${bucket_account}" ]] \
    || tf_fail "El bucket del state (${bucket}) no es de la cuenta actual (${account}). Revisar backend.hcl."
  if [[ -n "${RAG_EXPECTED_AWS_ACCOUNT_ID:-}" && "${account}" != "${RAG_EXPECTED_AWS_ACCOUNT_ID}" ]]; then
    tf_fail "Las credenciales son de la cuenta ${account}, no de RAG_EXPECTED_AWS_ACCOUNT_ID=${RAG_EXPECTED_AWS_ACCOUNT_ID}."
  fi
  # Solo lectura. stderr de aws no se oculta: head-bucket no imprime
  # credenciales y su error (403/404) ayuda a diagnosticar.
  if ! aws s3api head-bucket --bucket "${bucket}" --expected-bucket-owner "${account}" \
    --region us-east-1 >/dev/null; then
    tf_fail "El bucket del state ${bucket} no existe o no pertenece a la cuenta ${account}. Crearlo con scripts/bootstrap_tfstate_bucket.sh."
  fi
  TF_STATE_BUCKET="${bucket}"
  TF_AWS_ACCOUNT_ID="${account}"
}

# tf_reject_backend_overrides: falla si hay variables de entorno que desvían el
# backend S3 o la verificación de cuenta (endpoints de AWS) o que cambian la
# key del state (TF_WORKSPACE la convierte en env:/<workspace>/<key>). Solo se
# muestra el nombre de la variable, nunca su valor.
tf_reject_backend_overrides() {
  local var vars=()
  # AWS_ENDPOINT_URL y todas las AWS_ENDPOINT_URL_<SERVICIO> (S3, STS, EC2, ...).
  mapfile -t vars < <(compgen -v AWS_ENDPOINT_URL || true)
  for var in "${vars[@]}" AWS_S3_ENDPOINT AWS_STS_ENDPOINT TF_WORKSPACE; do
    if [[ -n "${!var:-}" ]]; then
      tf_fail "${var} está definida y desviaría el backend S3 o la key del state. Ejecutar 'unset ${var}' y repetir."
    fi
  done
}

# tf_init <directorio_terraform> <raíz_del_repositorio>
tf_init() {
  local tf_dir="$1" repo_root="$2" backend_config
  if ! tf_backend_is_s3 "${tf_dir}"; then
    # Sin -upgrade: respeta las versiones fijadas en .terraform.lock.hcl.
    terraform -chdir="${tf_dir}" init
    return
  fi

  backend_config="${TF_BACKEND_CONFIG:-${repo_root}/backend.hcl}"
  [[ -f "${backend_config}" ]] || tf_fail "Falta ${backend_config} (bucket del state).
  Crear el bucket con scripts/bootstrap_tfstate_bucket.sh y copiar backend.hcl.example."

  # Un state local junto a un backend S3 serían dos fuentes de verdad, y el
  # init pediría migrarlo. La migración es manual y la hace una sola persona.
  if [[ -s "${tf_dir}/terraform.tfstate" ]]; then
    tf_fail "Hay un terraform.tfstate local en ${tf_dir} y el stack usa backend S3.
  No se migra automáticamente: seguir 'Terraform State remoto' en README.md."
  fi

  tf_reject_backend_overrides
  tf_verify_backend_config "${backend_config}"

  # -input=false: si Terraform encontrara algún state que migrar, fallaría en
  # vez de preguntar "copy existing state?" dentro del script.
  if ! terraform -chdir="${tf_dir}" init -input=false -backend-config="${backend_config}"; then
    tf_fail "terraform init falló con el backend S3 de ${backend_config}.
  Revisar credenciales y bucket. Si este clon se inicializó antes con otro backend,
  seguir 'Terraform State remoto' en README.md (paso manual)."
  fi

  # Un `terraform workspace select` anterior queda guardado en
  # .terraform/environment y, como TF_WORKSPACE, cambia la key del state a
  # env:/<workspace>/<key>. Solo lectura: nunca se cambia de workspace aquí.
  local workspace
  if ! workspace=$(terraform -chdir="${tf_dir}" workspace show); then
    tf_fail "terraform workspace show falló en ${tf_dir}. No se continúa."
  fi
  if [[ "${workspace}" != "default" ]]; then
    tf_fail "El workspace de Terraform en ${tf_dir} no es 'default' y cambiaría la key del state.
  Volver a 'default' manualmente (terraform workspace select default) y repetir."
  fi
}

# tf_require_outside_repo <directorio>: falla si el directorio, con los
# symlinks resueltos, queda dentro del clon: los respaldos del state contienen
# secretos y nunca deben quedar en el repositorio. No crea nada: si aún no
# existe, resuelve su ancestro existente más cercano.
tf_require_outside_repo() {
  local dir="$1" probe rest="" real
  [[ "${dir}" == /* ]] || dir="${PWD}/${dir}"
  probe="${dir}"
  while [[ ! -d "${probe}" ]]; do
    case "$(basename "${probe}")" in
      ..|.) tf_fail "${1}: no se admiten '.' ni '..' en la parte que aún no existe." ;;
    esac
    rest="/$(basename "${probe}")${rest}"
    probe="$(dirname "${probe}")"
  done
  real="$(cd "${probe}" && pwd -P)" || tf_fail "No se pudo resolver ${1}."
  real="${real%/}${rest}"
  case "${real}/" in
    "${TF_LIB_REPO_ROOT}/"*)
      tf_fail "${1} está dentro del clon (${TF_LIB_REPO_ROOT}): los respaldos del state deben quedar fuera del repositorio." ;;
  esac
}

# backup_state <directorio_terraform> <nombre_stack>
# Copia el state fuera del clon con permisos 600 antes del apply.
#   - Backend local: copia terraform.tfstate* y deja el original en 600.
#   - Backend S3: guarda `terraform state pull` en remote.tfstate. Si el pull
#     falla se detiene; si el state no existe se detiene salvo ALLOW_EMPTY_STATE=1.
#     Terraform 1.17.1 no devuelve una salida vacía para una key inexistente,
#     sino un state vacío ({"serial": 0, "lineage": "", ...}): ambos casos
#     cuentan como "no existe".
backup_state() {
  local tf_dir="$1" stack="$2" dest
  local base="${TFSTATE_BACKUP_DIR:-${HOME}/tfstate-backups}"
  tf_require_outside_repo "${base}"
  dest="${base}/${stack}/$(date +%Y%m%d-%H%M%S)-$$"

  if tf_backend_is_s3 "${tf_dir}"; then
    (umask 077; mkdir -p "${dest}")
    # Una interrupción entre el pull y el mv no deja el state en claro a medias.
    # shellcheck disable=SC2064  # se expande ahora a propósito
    trap "rm -f '${dest}/remote.tfstate.partial'" EXIT
    # shellcheck disable=SC2064
    trap "rm -f '${dest}/remote.tfstate.partial'; exit 130" INT TERM
    if ! (umask 077; terraform -chdir="${tf_dir}" state pull > "${dest}/remote.tfstate.partial"); then
      rm -f "${dest}/remote.tfstate.partial"
      tf_fail "terraform state pull falló en ${stack}: error del backend o de credenciales. No se continúa."
    fi
    if [[ ! -s "${dest}/remote.tfstate.partial" ]] \
      || grep -Eq '"lineage"[[:space:]]*:[[:space:]]*""' "${dest}/remote.tfstate.partial"; then
      rm -f "${dest}/remote.tfstate.partial"
      rmdir "${dest}" 2>/dev/null || true
      trap - EXIT INT TERM
      if [[ "${ALLOW_EMPTY_STATE:-0}" == "1" ]]; then
        TF_STATE_EMPTY=1
        # shellcheck disable=SC2034  # la leen vector-infraestructura y rag/infrastructure (deploy.sh)
        TF_REMOTE_STATE_EMPTY=1
        echo "⚠ El state remoto de ${stack} está vacío (ALLOW_EMPTY_STATE=1: stack nuevo)."
        return 0
      fi
      tf_fail "El state remoto de ${stack} está vacío. Si es un stack nuevo, repetir con
  ALLOW_EMPTY_STATE=1; si no, revisar bucket y key antes de cualquier apply."
    fi
    mv "${dest}/remote.tfstate.partial" "${dest}/remote.tfstate"
    trap - EXIT INT TERM
    chmod 600 "${dest}/remote.tfstate"
    echo "✓ State remoto respaldado en ${dest}/remote.tfstate"
    return 0
  fi

  if [[ -e "${tf_dir}/.terraform.tfstate.lock.info" ]]; then
    tf_fail "Existe ${tf_dir}/.terraform.tfstate.lock.info: otro proceso de Terraform puede estar en curso."
  fi
  if [[ ! -f "${tf_dir}/terraform.tfstate" ]]; then
    TF_STATE_EMPTY=1
    echo "⚠ No hay terraform.tfstate local en ${stack}; no hay nada que respaldar"
    return 0
  fi
  (
    umask 077
    mkdir -p "${dest}"
    cp -pL "${tf_dir}"/terraform.tfstate* "${dest}/"
    chmod 600 "${dest}"/terraform.tfstate* "${tf_dir}"/terraform.tfstate*
  )
  echo "✓ State respaldado en ${dest}"
}

# ── Protección ante un state remoto vacío ────────────────────────────────────
# Con ALLOW_EMPTY_STATE=1, un state remoto vacío haría que el apply creara de
# nuevo todo el stack. Antes de aceptarlo se comprueba, solo con lecturas, que
# la key nunca tuvo versiones en el bucket y que no existen ya en AWS recursos
# con los nombres del stack. Nunca se importa ni se modifica nada.

# tf_backend_key <directorio_terraform> <nombre_stack>: exige que terraform.tf
# declare exactamente una key, igual a "<nombre_stack>/terraform.tfstate", y
# la deja en TF_GUARD_KEY.
tf_backend_key() {
  local tf_dir="$1" stack="$2" line keys=0 found=""
  local key_re='^[[:space:]]*key[[:space:]]*=[[:space:]]*"([^"]*)"[[:space:]]*$'
  [[ -f "${tf_dir}/terraform.tf" ]] || tf_fail "Falta ${tf_dir}/terraform.tf."
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" =~ ^[[:space:]]*key[[:space:]]*= ]]; then
      keys=$((keys + 1))
      [[ "${line}" =~ ${key_re} ]] && found="${BASH_REMATCH[1]}"
    fi
  done < "${tf_dir}/terraform.tf"
  if [[ "${keys}" -ne 1 || "${found}" != "${stack}/terraform.tfstate" ]]; then
    tf_fail "${tf_dir}/terraform.tf debe declarar exactamente una línea key = \"${stack}/terraform.tfstate\"."
  fi
  TF_GUARD_KEY="${found}"
}

# tf_guard_require_defaults <directorio_terraform> <variable>...: los nombres
# que se buscan en AWS se calculan con los defaults de variables.tf. Si alguna
# variable podría tener otro valor (TF_VAR_<variable> definida, una línea en
# terraform.tfvars o *.auto.tfvars, o cualquier *.tfvars.json, que no se
# analiza), se detiene. Nunca se muestran valores de los tfvars.
tf_guard_require_defaults() {
  local tf_dir="$1" var env_name file
  shift
  if compgen -G "${tf_dir}/*.tfvars.json" > /dev/null; then
    tf_fail "Hay archivos *.tfvars.json en ${tf_dir}: no se puede comprobar que se usan los valores por defecto. No se continúa."
  fi
  for var in "$@"; do
    env_name="TF_VAR_${var}"
    if [[ -n "${!env_name+x}" ]]; then
      tf_fail "${env_name} está definida. Con el state remoto vacío solo se admite el valor por defecto de ${var}: ejecutar 'unset ${env_name}' y repetir."
    fi
    for file in "${tf_dir}/terraform.tfvars" "${tf_dir}"/*.auto.tfvars; do
      [[ -f "${file}" ]] || continue
      if grep -qE -- "^[[:space:]]*${var}[[:space:]]*=" "${file}"; then
        tf_fail "${file} fija ${var}. Con el state remoto vacío solo se admite el valor por defecto: quitar la línea y repetir."
      fi
    done
  done
}

# tf_probe <descripción> <comando aws de solo lectura...>: ejecuta la consulta;
# si falla, se detiene. Una salida vacía o "None" significa que no existe; si
# no, añade "  - <descripción>: <ids>" a TF_GUARD_FOUND. Se llama directamente
# (no dentro de $(...)): tf_fail debe terminar el script, no una subshell.
tf_probe() {
  local desc="$1" out token ids="" tokens=()
  shift
  if ! out=$("$@"); then
    tf_fail "No se pudo comprobar ${desc} (credenciales, permisos o red). No se continúa."
  fi
  # Normaliza espacios, tabuladores y saltos de línea sin expandir comodines.
  read -r -d '' -a tokens <<< "${out}" || true
  for token in "${tokens[@]}"; do
    [[ "${token}" == "None" ]] && continue
    ids="${ids:+${ids} }${token}"
  done
  if [[ -n "${ids}" ]]; then
    TF_GUARD_FOUND+="  - ${desc}: ${ids}"$'\n'
  fi
  return 0
}

# tf_guard_default_vpc: deja en TF_GUARD_VPC el ID de la VPC por defecto.
tf_guard_default_vpc() {
  local vpc
  if ! vpc=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query 'Vpcs[0].VpcId' --output text --region "${TF_GUARD_REGION}"); then
    tf_fail "No se pudo consultar la VPC por defecto (credenciales, permisos o red). No se continúa."
  fi
  [[ "${vpc}" =~ ^vpc-[0-9a-f]{8,17}$ ]] \
    || tf_fail "No se encontró la VPC por defecto en ${TF_GUARD_REGION}. No se continúa."
  TF_GUARD_VPC="${vpc}"
}

# tf_guard_bucket_exists <bucket>: añade el bucket a TF_GUARD_FOUND si existe
# en la cuenta. head-bucket no tiene salida "no existe": solo un 404 de HeadBucket cuenta
# como ausencia; un 403 (p. ej. bucket de otra cuenta) o un error de red
# detienen el despliegue.
tf_guard_bucket_exists() {
  local bucket="$1" err
  if err=$(aws s3api head-bucket --bucket "${bucket}" \
    --expected-bucket-owner "${TF_AWS_ACCOUNT_ID}" --region "${TF_GUARD_REGION}" 2>&1 >/dev/null); then
    TF_GUARD_FOUND+="  - bucket S3: ${bucket}"$'\n'
    return 0
  fi
  case "${err}" in
    *"(404) when calling the HeadBucket operation"*) return 0 ;;
  esac
  [[ -n "${err}" ]] && printf '%s\n' "${err}" >&2
  tf_fail "No se pudo comprobar el bucket ${bucket}: puede existir sin pertenecer a la cuenta ${TF_AWS_ACCOUNT_ID}, o faltan permisos. No se continúa."
}

# tf_guard_probes <directorio_terraform> <nombre_stack>: busca en AWS los
# recursos con nombre fijo de cada stack (nombres de los .tf, con los defaults
# de variables.tf). Solo lecturas.
tf_guard_probes() {
  local tf_dir="$1" stack="$2" p="${TF_DEFAULT_PROJECT}" pre
  local r=(--region "${TF_GUARD_REGION}")
  case "${stack}" in
    ingesta)
      tf_guard_require_defaults "${tf_dir}" project
      tf_guard_bucket_exists "${p}-data-${TF_AWS_ACCOUNT_ID}"
      ;;
    vector-infraestructura)
      tf_guard_require_defaults "${tf_dir}" project
      tf_guard_default_vpc
      tf_probe "EC2 de ChromaDB/Ollama" aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=${p}-chromadb,${p}-ollama" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text "${r[@]}"
      tf_probe "security groups de vector-infraestructura" aws ec2 describe-security-groups \
        --filters "Name=vpc-id,Values=${TF_GUARD_VPC}" \
                  "Name=group-name,Values=${p}-chromadb,${p}-ollama,${p}-vector-clients" \
        --query 'SecurityGroups[].GroupId' --output text "${r[@]}"
      tf_probe "Elastic IP de ChromaDB/Ollama" aws ec2 describe-addresses \
        --filters "Name=tag:Name,Values=${p}-chromadb-eip,${p}-ollama-eip" \
        --query 'Addresses[].AllocationId' --output text "${r[@]}"
      ;;
    rag-infrastructure)
      tf_guard_require_defaults "${tf_dir}" project environment aws_region
      pre="${p}-${TF_DEFAULT_ENVIRONMENT}"
      tf_guard_default_vpc
      tf_probe "security groups de rag/infrastructure" aws ec2 describe-security-groups \
        --filters "Name=vpc-id,Values=${TF_GUARD_VPC}" \
                  "Name=group-name,Values=${pre}-alb,${pre}-app,${pre}-efs,${pre}-frontend" \
        --query 'SecurityGroups[].GroupId' --output text "${r[@]}"
      tf_probe "repositorios ECR" aws ecr describe-repositories \
        --query "repositories[?repositoryName=='${pre}-backend' || repositoryName=='${pre}-frontend'].repositoryName" \
        --output text "${r[@]}"
      tf_probe "log groups de CloudWatch" aws logs describe-log-groups \
        --log-group-name-prefix "/ecs/${pre}-" \
        --query "logGroups[?logGroupName=='/ecs/${pre}-app' || logGroupName=='/ecs/${pre}-frontend'].logGroupName" \
        --output text "${r[@]}"
      tf_probe "ALB" aws elbv2 describe-load-balancers \
        --query "LoadBalancers[?LoadBalancerName=='${pre}-alb'].LoadBalancerArn" \
        --output text "${r[@]}"
      tf_probe "target groups" aws elbv2 describe-target-groups \
        --query "TargetGroups[?TargetGroupName=='${pre}-backend' || TargetGroupName=='${pre}-frontend'].TargetGroupArn" \
        --output text "${r[@]}"
      tf_probe "EFS de PostgreSQL" aws efs describe-file-systems \
        --creation-token "${pre}-postgres" \
        --query "FileSystems[?LifeCycleState!='deleted'].FileSystemId" \
        --output text "${r[@]}"
      tf_probe "cluster ECS" aws ecs describe-clusters --clusters "${pre}" \
        --query "clusters[?status=='ACTIVE'].clusterArn" --output text "${r[@]}"
      ;;
    *)
      tf_fail "Stack desconocido para la protección de state vacío: ${stack}."
      ;;
  esac
}

# tf_guard_empty_remote_state <directorio_terraform> <nombre_stack>
# Se llama tras backup_state. Sin efecto si el state remoto no está vacío.
# Si lo está (ALLOW_EMPTY_STATE=1), antes del apply exige:
#   a) que la key del stack no tenga versiones ni delete markers en el bucket
#      (si las tiene, el state existió y se borró o sobrescribió: se recupera a
#      mano, no se recrea el stack);
#   b) que no existan en AWS recursos con los nombres del stack (state local
#      sin migrar, otra key o state borrado);
#   c) que una persona escriba el nombre del stack en la terminal.
tf_guard_empty_remote_state() {
  local tf_dir="$1" stack="$2" counts token total=0 answer tokens=()
  [[ "${TF_REMOTE_STATE_EMPTY}" -eq 1 ]] || return 0

  [[ -t 0 ]] || tf_fail "El state remoto de ${stack} está vacío: se requiere una terminal interactiva para confirmarlo."
  [[ -n "${TF_STATE_BUCKET}" && -n "${TF_AWS_ACCOUNT_ID}" ]] \
    || tf_fail "No se verificó el backend S3 de ${stack} (bucket y cuenta desconocidos). No se continúa."
  tf_backend_key "${tf_dir}" "${stack}"

  # a) Historial de la key. Con --output text la consulta se aplica a cada
  #    página: se suman todos los números devueltos.
  if ! counts=$(aws s3api list-object-versions --bucket "${TF_STATE_BUCKET}" \
    --expected-bucket-owner "${TF_AWS_ACCOUNT_ID}" --prefix "${TF_GUARD_KEY}" \
    --region "${TF_GUARD_REGION}" \
    --query "[length(Versions[?Key=='${TF_GUARD_KEY}'] || \`[]\`), length(DeleteMarkers[?Key=='${TF_GUARD_KEY}'] || \`[]\`)]" \
    --output text); then
    tf_fail "No se pudo consultar el historial de versiones de ${TF_GUARD_KEY} en el bucket del state. No se continúa."
  fi
  read -r -d '' -a tokens <<< "${counts}" || true
  for token in "${tokens[@]}"; do
    [[ "${token}" =~ ^[0-9]+$ ]] \
      || tf_fail "Respuesta inesperada al consultar las versiones de ${TF_GUARD_KEY}. No se continúa."
    total=$((total + 10#${token}))
  done
  [[ "${#tokens[@]}" -ge 2 && $(( ${#tokens[@]} % 2 )) -eq 0 ]] \
    || tf_fail "Respuesta inesperada al consultar las versiones de ${TF_GUARD_KEY}. No se continúa."
  if [[ "${total}" -gt 0 ]]; then
    tf_fail "El state remoto de ${stack} está vacío, pero ${TF_GUARD_KEY} tiene versiones anteriores en el bucket: el state existió.
  No se crea el stack de nuevo. Recuperarlo a mano: ver 'Recuperar una versión anterior del state' en README.md."
  fi

  # b) Recursos con los nombres del stack ya presentes en AWS.
  TF_GUARD_FOUND=""
  tf_guard_probes "${tf_dir}" "${stack}"
  if [[ -n "${TF_GUARD_FOUND}" ]]; then
    echo "✗ El state remoto de ${stack} está vacío, pero ya existen en AWS recursos del stack:" >&2
    printf '%s' "${TF_GUARD_FOUND}" >&2
    tf_fail "No se importa ni se despliega nada. Probablemente hay un state local sin migrar, otra key o un state borrado.
  Revisar backend.hcl y la key; ver 'Terraform State remoto' en README.md."
  fi

  # c) Confirmación humana explícita.
  echo "⚠ El state remoto de ${stack} está vacío y no se encontraron recursos del stack en AWS."
  echo "  terraform apply creará el stack desde cero."
  if ! IFS= read -r -p "Escribe ${stack} para continuar: " answer || [[ "${answer}" != "${stack}" ]]; then
    tf_fail "Confirmación no recibida para ${stack}. No se despliega."
  fi
}

# restrict_state <directorio_terraform>: deja el state local escrito por apply
# en 600. Con backend S3 no hay state local.
restrict_state() {
  local tf_dir="$1"
  if compgen -G "${tf_dir}/terraform.tfstate*" > /dev/null; then
    chmod 600 "${tf_dir}"/terraform.tfstate*
  fi
}

# tf_state_addresses <directorio_terraform>: imprime las direcciones del state.
# Vacío solo si backup_state comprobó que el state está vacío; cualquier fallo
# de `state list` detiene el despliegue.
tf_state_addresses() {
  local tf_dir="$1" addresses
  [[ "${TF_STATE_EMPTY}" -eq 1 ]] && return 0
  if ! addresses=$(terraform -chdir="${tf_dir}" state list -no-color); then
    tf_fail "terraform state list falló: error del backend o de credenciales. No se continúa."
  fi
  printf '%s\n' "${addresses}"
}
