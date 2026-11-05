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
}

# tf_reject_backend_overrides: falla si hay variables de entorno que desvían el
# backend S3 o la verificación de cuenta (endpoints de AWS) o que cambian la
# key del state (TF_WORKSPACE la convierte en env:/<workspace>/<key>). Solo se
# muestra el nombre de la variable, nunca su valor.
tf_reject_backend_overrides() {
  local var
  for var in AWS_ENDPOINT_URL AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_STS \
    AWS_S3_ENDPOINT AWS_STS_ENDPOINT TF_WORKSPACE; do
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
  dest="${TFSTATE_BACKUP_DIR:-${HOME}/tfstate-backups}/${stack}/$(date +%Y%m%d-%H%M%S)-$$"

  if tf_backend_is_s3 "${tf_dir}"; then
    (umask 077; mkdir -p "${dest}")
    if ! (umask 077; terraform -chdir="${tf_dir}" state pull > "${dest}/remote.tfstate.partial"); then
      rm -f "${dest}/remote.tfstate.partial"
      tf_fail "terraform state pull falló en ${stack}: error del backend o de credenciales. No se continúa."
    fi
    if [[ ! -s "${dest}/remote.tfstate.partial" ]] \
      || grep -Eq '"lineage"[[:space:]]*:[[:space:]]*""' "${dest}/remote.tfstate.partial"; then
      rm -f "${dest}/remote.tfstate.partial"
      rmdir "${dest}" 2>/dev/null || true
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
