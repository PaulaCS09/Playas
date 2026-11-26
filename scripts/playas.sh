#!/usr/bin/env bash
# CLI de operación de la infraestructura de PLAYAS (AWS Academy Learner Lab).
#
# Punto de entrada único que reutiliza los scripts existentes; no los sustituye:
#   bootstrap  → scripts/bootstrap_tfstate_bucket.sh
#   deploy     → <stack>/scripts/deploy.sh (confirmación humana del apply)
#   start/stop → rag/infrastructure/scripts/{start,stop}.sh
#   init/state → scripts/lib/terraform_state.sh
#
# Stacks (orden de despliegue por dependencias reales):
#   ingesta  ingesta/infrastructure   bucket S3 de datos     → s3_bucket_name
#   vector   vector-infraestructura   EC2 ChromaDB + Ollama  → IP privadas, SG clientes
#   rag      rag/infrastructure       ECS, ALB, ECR, EFS     (consume los dos anteriores)
#
# Nunca ejecuta terraform destroy, -auto-approve, -migrate-state,
# -reconfigure, state push/rm, import ni force-unlock.
#
# Uso: scripts/playas.sh <comando> [argumentos]   (scripts/playas.sh help)
set -euo pipefail
umask 077
export AWS_PAGER=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/terraform_state.sh
source "${REPO_ROOT}/scripts/lib/terraform_state.sh"

REGION="${TF_GUARD_REGION}"
TERRAFORM_VERSION="1.17.1"
SHELLCHECK_PY_VERSION="0.10.0.1"
STACK_ORDER=(ingesta vector rag)
NAME_PREFIX="${TF_DEFAULT_PROJECT}-${TF_DEFAULT_ENVIRONMENT}"
INFRA_TESTS=(
  tests/unit/test_deploy_scripts_safety.py
  tests/unit/test_bootstrap_tfstate_bucket.py
  tests/unit/test_script_secret_handling.py
  tests/unit/test_vector_network_security.py
  tests/unit/test_playas_cli.py
)

bold='\033[1m'; green='\033[0;32m'; yellow='\033[1;33m'; red='\033[0;31m'; reset='\033[0m'
log()  { echo -e "${bold}▶ $*${reset}"; }
ok()   { echo -e "${green}✓ $*${reset}"; }
warn() { echo -e "${yellow}⚠ $*${reset}"; }
err()  { echo -e "${red}✗ $*${reset}" >&2; }
die()  { err "$*"; exit 1; }

usage() {
  cat <<'EOF'
Uso: scripts/playas.sh <comando> [argumentos]

Solo lectura:
  doctor                 Herramientas, credenciales, cuenta, región, backend.hcl,
                         bucket del state y contratos entre stacks.
  plan <stack|all>       terraform init (backend S3) + plan. Nunca aplica.
  status                 Recursos del proyecto en AWS, state por stack y costes fijos.
  health                 Health checks HTTP de ChromaDB, Ollama, ALB y targets.
  test [static|unit|all] Validaciones locales sin AWS (las mismas que la CI).

Con cambios (piden confirmación humana):
  bootstrap [--verify-only]  Crea/verifica el bucket del Terraform State.
  deploy <stack|all>         Ejecuta el deploy.sh del stack (apply interactivo).
  backup-state <stack|all>   Copia cifrada (GPG) del state fuera de AWS.
  start | stop               Enciende/apaga el cómputo (EC2 vector y ECS).

Stacks: ingesta, vector, rag ("all" = ingesta → vector → rag).
Variables: RAG_EXPECTED_AWS_ACCOUNT_ID (cuenta esperada, 12 dígitos),
  PLAYAS_BACKUP_GPG_RECIPIENT (clave GPG pública del destinatario; sin ella
  se cifra con frase de paso), PLAYAS_STATE_BACKUP_DIR (default
  ~/playas-state-backups), ALLOW_EMPTY_STATE=1 (stack nuevo, ver README).
EOF
}

# ── Stacks ────────────────────────────────────────────────────────────────────
stack_dir() {
  case "$1" in
    ingesta) echo "${REPO_ROOT}/ingesta/infrastructure" ;;
    vector)  echo "${REPO_ROOT}/vector-infraestructura" ;;
    rag)     echo "${REPO_ROOT}/rag/infrastructure" ;;
    *) die "Stack desconocido: $1 (ingesta, vector, rag o all)." ;;
  esac
}

# Nombre del stack en la key del state y en los respaldos.
stack_name() {
  case "$1" in
    ingesta) echo "ingesta" ;;
    vector)  echo "vector-infraestructura" ;;
    rag)     echo "rag-infrastructure" ;;
    *) die "Stack desconocido: $1 (ingesta, vector, rag o all)." ;;
  esac
}

# load_stacks <stack|all>: deja en STACKS los stacks en orden de despliegue.
# Se llama sin subshell para que un stack inválido detenga el script.
STACKS=()
load_stacks() {
  [[ $# -eq 1 ]] || die "Indica un stack: ingesta, vector, rag o all."
  if [[ "$1" == "all" ]]; then
    STACKS=("${STACK_ORDER[@]}")
  else
    case "$1" in
      ingesta|vector|rag) STACKS=("$1") ;;
      *) die "Stack desconocido: $1 (ingesta, vector, rag o all)." ;;
    esac
  fi
}

# Estas variables pueden inyectar -auto-approve, -upgrade, -migrate-state o
# desactivar los prompts en cualquier comando de Terraform.
reject_tf_cli_overrides() {
  local var
  for var in TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan TF_CLI_ARGS_apply TF_INPUT; do
    [[ -z "${!var:-}" ]] || die "${var} está definida y alteraría Terraform. Ejecuta: unset ${var}"
  done
}

require_tty() {
  [[ -t 0 ]] || die "$1 requiere una terminal interactiva."
}

aws_account() {
  aws sts get-caller-identity --query Account --output text --region "${REGION}"
}

# ── doctor ────────────────────────────────────────────────────────────────────
DOCTOR_FAIL=0
DOCTOR_WARN=0
d_ok()   { ok "$*"; }
d_warn() { warn "$*"; DOCTOR_WARN=$((DOCTOR_WARN + 1)); }
d_fail() { err "$*"; DOCTOR_FAIL=$((DOCTOR_FAIL + 1)); }

# tfvars_value <archivo> <clave>: valor sin comillas (nunca se imprime si es secreto).
tfvars_value() {
  grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null \
    | sed -E 's/^[^=]+=[[:space:]]*"?([^"#]*)"?.*/\1/' | tr -d ' ' | head -1
}

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

cmd_doctor() {
  [[ $# -eq 0 ]] || die "doctor no admite argumentos."
  local tool version account arn bucket key s rag_tfvars f

  log "Herramientas"
  for tool in terraform aws curl openssl git; do
    if command -v "${tool}" >/dev/null 2>&1; then d_ok "${tool}"; else d_fail "Falta ${tool}"; fi
  done
  for tool in docker session-manager-plugin gpg uv; do
    command -v "${tool}" >/dev/null 2>&1 && d_ok "${tool}" \
      || d_warn "Falta ${tool} (necesario para: $(case "${tool}" in
           docker) echo "push de imágenes en deploy rag" ;;
           session-manager-plugin) echo "ECS Exec: usuarios semilla y export de Postgres" ;;
           gpg) echo "backup-state" ;;
           uv) echo "importación a ChromaDB y test" ;;
         esac))"
  done
  if command -v terraform >/dev/null 2>&1; then
    version=$(terraform version 2>/dev/null | head -1 | sed -E 's/^Terraform v//')
    # required_version = "~> 1.17.1": cualquier 1.17.x >= 1.17.1.
    [[ "${version}" =~ ^1\.17\.([0-9]+)$ && "${BASH_REMATCH[1]}" -ge 1 ]] && d_ok "Terraform ${version}" \
      || d_fail "Terraform ${version:-?}: los stacks exigen ~> ${TERRAFORM_VERSION} (required_version)."
  fi

  log "Entorno"
  for v in TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan TF_CLI_ARGS_apply TF_INPUT TF_WORKSPACE AWS_S3_ENDPOINT AWS_STS_ENDPOINT; do
    [[ -z "${!v:-}" ]] || d_fail "${v} está definida (los deploy.sh se detendrán)."
  done
  while IFS= read -r v; do
    [[ -n "${v}" && -n "${!v:-}" ]] && d_fail "${v} está definida (desviaría el backend S3)."
  done < <(compgen -v AWS_ENDPOINT_URL || true)
  local cfg_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  if [[ -z "${cfg_region}" ]] && command -v aws >/dev/null 2>&1; then
    cfg_region=$(aws configure get region 2>/dev/null || true)
  fi
  if [[ -z "${cfg_region}" || "${cfg_region}" == "${REGION}" ]]; then
    d_ok "Región: ${REGION} (fija en backends y providers)"
  else
    d_fail "Región configurada ${cfg_region}: los stacks y scripts usan ${REGION}. Exporta AWS_REGION=${REGION}."
  fi

  log "Credenciales y cuenta"
  if ! command -v aws >/dev/null 2>&1 || ! read -r account arn < <(aws sts get-caller-identity \
      --query '[Account,Arn]' --output text --region "${REGION}" 2>/dev/null); then
    d_fail "No hay credenciales AWS válidas (ausentes o caducadas: reinicia el lab y exporta AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY y AWS_SESSION_TOKEN)."
    account=""
  else
    d_ok "Cuenta ${account}"
    [[ "${arn}" == *assumed-role/voclabs* ]] || d_warn "La identidad no parece una sesión de Learner Lab (rol voclabs). Comprueba el perfil."
    if [[ -n "${RAG_EXPECTED_AWS_ACCOUNT_ID:-}" && "${RAG_EXPECTED_AWS_ACCOUNT_ID}" != "${account}" ]]; then
      d_fail "Las credenciales son de ${account}, no de RAG_EXPECTED_AWS_ACCOUNT_ID=${RAG_EXPECTED_AWS_ACCOUNT_ID}."
    fi
  fi

  log "Backend del Terraform State"
  bucket=""
  if [[ ! -f "${REPO_ROOT}/backend.hcl" ]]; then
    d_fail "Falta backend.hcl: ejecuta 'scripts/playas.sh bootstrap' y copia backend.hcl.example."
  elif [[ -n "${account}" ]]; then
    # La validación de la biblioteca termina el proceso al fallar: se aísla en una subshell.
    if (tf_reject_backend_overrides && tf_verify_backend_config "${REPO_ROOT}/backend.hcl") >/dev/null; then
      bucket="${TF_DEFAULT_PROJECT}-tfstate-${account}"
      d_ok "backend.hcl válido; bucket ${bucket} de la cuenta ${account}"
    else
      d_fail "backend.hcl o el bucket del state no son válidos (ver el mensaje anterior)."
    fi
  fi
  if [[ -n "${bucket}" ]]; then
    if RAG_EXPECTED_AWS_ACCOUNT_ID="${account}" "${REPO_ROOT}/scripts/bootstrap_tfstate_bucket.sh" --verify-only >/dev/null 2>&1; then
      d_ok "Bucket: versionado, SSE-S3, Block Public Access, BucketOwnerEnforced y TLS obligatorio"
    else
      d_fail "La configuración del bucket no es la esperada: ejecuta 'scripts/playas.sh bootstrap --verify-only'."
    fi
    key="vector-infraestructura/terraform.tfstate"
    if aws s3api list-object-versions --bucket "${bucket}" --expected-bucket-owner "${account}" \
        --prefix "${key}" --max-items 1 --region "${REGION}" >/dev/null 2>&1; then
      d_ok "Permiso s3:ListBucketVersions (protección de state vacío)"
    else
      d_warn "Sin s3:ListBucketVersions: ALLOW_EMPTY_STATE=1 se detendrá."
    fi
  fi

  log "Stacks locales"
  for s in "${STACK_ORDER[@]}"; do
    f="$(stack_dir "${s}")"
    if [[ -s "${f}/terraform.tfstate" ]]; then
      d_fail "${s}: hay terraform.tfstate local (migración manual pendiente, ver README 'Terraform State remoto')."
    fi
    if [[ -f "${f}/terraform.tfvars" ]]; then
      [[ "$(file_mode "${f}/terraform.tfvars")" == "600" ]] \
        && d_ok "${s}: terraform.tfvars (600)" \
        || d_warn "${s}: terraform.tfvars no tiene permisos 600 (contiene secretos): chmod 600."
    fi
  done
  [[ -f "$(stack_dir vector)/terraform.tfvars" || -n "${TF_VAR_legacy_public_service_access:-}" ]] \
    || d_warn "vector: falta terraform.tfvars con legacy_public_service_access (copiar terraform.tfvars.example)."

  log "Contratos entre stacks (rag/infrastructure/terraform.tfvars)"
  rag_tfvars="$(stack_dir rag)/terraform.tfvars"
  if [[ ! -f "${rag_tfvars}" ]]; then
    d_warn "No existe todavía: lo crea 'deploy vector' desde el ejemplo."
  else
    for key in chroma_host ollama_base_url vector_clients_security_group_id; do
      [[ -n "$(tfvars_value "${rag_tfvars}" "${key}")" ]] \
        && d_ok "${key} definido (output de vector)" \
        || d_warn "${key} vacío: ejecuta 'deploy vector' antes de 'deploy rag'."
    done
    local data_bucket
    data_bucket=$(tfvars_value "${rag_tfvars}" s3_bucket_name)
    if [[ -z "${data_bucket}" ]]; then
      d_warn "s3_bucket_name vacío (output de ingesta)."
    elif [[ -n "${account}" && "${data_bucket}" != "${TF_DEFAULT_PROJECT}-data-${account}" ]]; then
      d_fail "s3_bucket_name=${data_bucket} no es el bucket de ingesta de esta cuenta (${TF_DEFAULT_PROJECT}-data-${account})."
    else
      d_ok "s3_bucket_name coincide con el bucket de ingesta"
    fi
  fi

  echo ""
  if [[ "${DOCTOR_FAIL}" -gt 0 ]]; then
    die "doctor: ${DOCTOR_FAIL} errores, ${DOCTOR_WARN} avisos."
  fi
  ok "doctor: sin errores (${DOCTOR_WARN} avisos)."
}

# ── bootstrap ─────────────────────────────────────────────────────────────────
cmd_bootstrap() {
  local account answer
  if [[ -z "${RAG_EXPECTED_AWS_ACCOUNT_ID:-}" ]]; then
    account=$(aws_account) || die "No hay credenciales AWS válidas."
    warn "RAG_EXPECTED_AWS_ACCOUNT_ID no está definida: se usará la cuenta de las credenciales (${account})."
    [[ "${1:-}" == "--verify-only" ]] || {
      require_tty "bootstrap"
      read -r -p "¿Es ${account} la cuenta de tu Learner Lab? Escribe la cuenta para continuar: " answer
      [[ "${answer}" == "${account}" ]] || die "Cancelado: no se ha hecho ningún cambio."
    }
    export RAG_EXPECTED_AWS_ACCOUNT_ID="${account}"
  fi
  exec "${REPO_ROOT}/scripts/bootstrap_tfstate_bucket.sh" "$@"
}

# ── plan ──────────────────────────────────────────────────────────────────────
cmd_plan() {
  local s dir
  reject_tf_cli_overrides
  load_stacks "$@"
  for s in "${STACKS[@]}"; do
    dir="$(stack_dir "${s}")"
    log "plan ${s} ($(stack_name "${s}"))"
    tf_init "${dir}" "${REPO_ROOT}"
    # Sin -out: los planes no se guardan (pueden contener secretos).
    terraform -chdir="${dir}" plan -input=false -lock-timeout=60s
  done
}

# ── deploy ────────────────────────────────────────────────────────────────────
cmd_deploy() {
  local s script
  require_tty "deploy (terraform apply pide confirmación)"
  reject_tf_cli_overrides
  load_stacks "$@"
  for s in "${STACKS[@]}"; do
    script="$(stack_dir "${s}")/scripts/deploy.sh"
    log "deploy ${s}: ${script#"${REPO_ROOT}/"}"
    "${script}" || die "deploy ${s} terminó con error; no se continúa con los stacks siguientes."
  done
}

# ── backup-state ──────────────────────────────────────────────────────────────
# Copia cifrada del state remoto fuera de AWS. El state en claro nunca toca el
# disco: `state pull` va a memoria y de ahí a gpg.
cmd_backup_state() {
  local s dir name state dest file
  command -v gpg >/dev/null 2>&1 || die "Se necesita gpg para cifrar el respaldo."
  reject_tf_cli_overrides
  load_stacks "$@"
  dest="${PLAYAS_STATE_BACKUP_DIR:-${HOME}/playas-state-backups}"
  if grep -qsi amazon /sys/class/dmi/id/sys_vendor; then
    warn "Parece una EC2: el respaldo quedaría dentro de AWS. Ejecútalo desde tu equipo o copia el .gpg fuera."
  fi
  if [[ -n "${PLAYAS_BACKUP_GPG_RECIPIENT:-}" ]]; then
    gpg --list-keys "${PLAYAS_BACKUP_GPG_RECIPIENT}" >/dev/null 2>&1 \
      || die "La clave pública ${PLAYAS_BACKUP_GPG_RECIPIENT} no está en el llavero de gpg."
  else
    require_tty "backup-state sin PLAYAS_BACKUP_GPG_RECIPIENT (gpg pide la frase de paso)"
    # stdin de gpg es la pipe con el state: pinentry necesita la terminal.
    GPG_TTY="$(tty)"
    export GPG_TTY
  fi
  # Con symlinks resueltos; un destino dentro del clon detiene el respaldo.
  tf_require_outside_repo "${dest}"
  mkdir -p "${dest}"
  chmod 700 "${dest}"

  for s in "${STACKS[@]}"; do
    dir="$(stack_dir "${s}")"
    name="$(stack_name "${s}")"
    tf_backend_is_s3 "${dir}" || die "${s} no usa backend S3."
    log "backup-state ${s}"
    tf_init "${dir}" "${REPO_ROOT}" >/dev/null
    state=$(terraform -chdir="${dir}" state pull) \
      || die "terraform state pull falló en ${s} (backend o credenciales)."
    # Comprobación en bash puro: un here-string grande se escribiría en /tmp.
    if [[ -z "${state}" || "${state}" =~ \"lineage\"[[:space:]]*:[[:space:]]*\"\" ]]; then
      warn "${s}: el state remoto está vacío; no hay nada que respaldar."
      continue
    fi
    file="${dest}/${name}-$(date -u +%Y%m%dT%H%M%SZ).tfstate.gpg"
    if [[ -n "${PLAYAS_BACKUP_GPG_RECIPIENT:-}" ]]; then
      printf '%s' "${state}" | gpg --batch --yes --quiet --encrypt \
        --recipient "${PLAYAS_BACKUP_GPG_RECIPIENT}" --output "${file}.partial" \
        || { rm -f "${file}.partial"; die "gpg no pudo cifrar el state de ${s}."; }
    else
      printf '%s' "${state}" | gpg --yes --quiet --symmetric --cipher-algo AES256 \
        --output "${file}.partial" \
        || { rm -f "${file}.partial"; die "gpg no pudo cifrar el state de ${s}."; }
    fi
    unset state
    mv "${file}.partial" "${file}"
    chmod 600 "${file}"
    (cd "${dest}" && sha256sum "$(basename "${file}")" > "$(basename "${file}").sha256")
    ok "${s}: ${file}"
  done
  echo "  Descifrar: gpg --decrypt <archivo>.tfstate.gpg > /ruta/segura/terraform.tfstate"
  echo "  Restaurar un state es manual (README: 'Recuperar una versión anterior del state')."
}

# ── status ────────────────────────────────────────────────────────────────────
# q <comando aws...>: imprime la salida o "?" si la consulta falla.
q() { "$@" 2>/dev/null || echo "?"; }

cmd_status() {
  [[ $# -eq 0 ]] || die "status no admite argumentos."
  local account bucket s key out
  account=$(aws_account) || die "No hay credenciales AWS válidas."
  bucket="${TF_DEFAULT_PROJECT}-tfstate-${account}"
  log "Cuenta ${account} · ${REGION}"

  log "Terraform State (s3://${bucket})"
  for s in "${STACK_ORDER[@]}"; do
    key="$(stack_name "${s}")/terraform.tfstate"
    if out=$(aws s3api head-object --bucket "${bucket}" --key "${key}" \
        --expected-bucket-owner "${account}" --region "${REGION}" \
        --query 'LastModified' --output text 2>/dev/null); then
      echo "  ${s}: ${key} (modificado ${out})"
    else
      echo "  ${s}: ${key} sin state (o sin acceso)"
    fi
    if aws s3api head-object --bucket "${bucket}" --key "${key}.tflock" \
        --expected-bucket-owner "${account}" --region "${REGION}" >/dev/null 2>&1; then
      warn "  ${s}: existe ${key}.tflock (operación en curso o lock huérfano; ver README antes de tocarlo)"
    fi
  done

  log "ingesta"
  if aws s3api head-bucket --bucket "${TF_DEFAULT_PROJECT}-data-${account}" \
      --expected-bucket-owner "${account}" --region "${REGION}" >/dev/null 2>&1; then
    echo "  bucket ${TF_DEFAULT_PROJECT}-data-${account}: existe"
  else
    echo "  bucket ${TF_DEFAULT_PROJECT}-data-${account}: no existe (o sin acceso)"
  fi

  log "vector (EC2)"
  q aws ec2 describe-instances --region "${REGION}" \
    --filters "Name=tag:Name,Values=${TF_DEFAULT_PROJECT}-chromadb,${TF_DEFAULT_PROJECT}-ollama,${TF_DEFAULT_PROJECT}-bastion" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceType,State.Name,PublicIpAddress]' \
    --output text | sed 's/^/  /'
  echo "  EIP asignadas: $(q aws ec2 describe-addresses --region "${REGION}" \
    --filters "Name=tag:Name,Values=${TF_DEFAULT_PROJECT}-chromadb-eip,${TF_DEFAULT_PROJECT}-ollama-eip" \
    --query 'length(Addresses)' --output text)"

  log "rag (ECS, ALB, EFS)"
  q aws ecs describe-services --region "${REGION}" --cluster "${NAME_PREFIX}" \
    --services "${NAME_PREFIX}-app" "${NAME_PREFIX}-frontend" \
    --query 'services[].[serviceName,status,join(`/`,[to_string(runningCount),to_string(desiredCount)])]' \
    --output text | sed 's/^/  /'
  echo "  ALB: $(q aws elbv2 describe-load-balancers --region "${REGION}" --names "${NAME_PREFIX}-alb" \
    --query 'LoadBalancers[0].[State.Code,DNSName]' --output text)"
  echo "  EFS (bytes): $(q aws efs describe-file-systems --region "${REGION}" \
    --creation-token "${NAME_PREFIX}-postgres" --query 'FileSystems[0].SizeInBytes.Value' --output text)"

  echo ""
  warn "Cobran aunque el lab esté detenido: EIP, volúmenes EBS, ALB, EFS, ECR y S3."
  echo "  'scripts/playas.sh stop' apaga EC2 y ECS, pero no elimina esos costes fijos."
}

# ── health ────────────────────────────────────────────────────────────────────
HEALTH_FAIL=0
probe_http() {
  local label="$1" url="$2"
  if curl -sf --max-time 10 -o /dev/null "${url}"; then
    ok "${label}: ${url}"
  else
    err "${label}: sin respuesta correcta en ${url}"
    HEALTH_FAIL=$((HEALTH_FAIL + 1))
  fi
}

instance_ip() {
  aws ec2 describe-instances --region "${REGION}" \
    --filters "Name=tag:Name,Values=$1" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null || true
}

cmd_health() {
  [[ $# -eq 0 ]] || die "health no admite argumentos."
  local ip dns lb_arn tg tgs states w healthy
  local -a words
  aws_account >/dev/null || die "No hay credenciales AWS válidas."

  log "vector (por IP pública; requiere tu IP en operator_cidr_blocks)"
  ip=$(instance_ip "${TF_DEFAULT_PROJECT}-chromadb")
  if [[ -n "${ip}" && "${ip}" != "None" ]]; then
    probe_http "ChromaDB" "http://${ip}:8000/api/v2/heartbeat"
  else
    err "ChromaDB: no hay EC2 en ejecución"; HEALTH_FAIL=$((HEALTH_FAIL + 1))
  fi
  ip=$(instance_ip "${TF_DEFAULT_PROJECT}-ollama")
  if [[ -n "${ip}" && "${ip}" != "None" ]]; then
    probe_http "Ollama" "http://${ip}:11434/api/tags"
  else
    err "Ollama: no hay EC2 en ejecución"; HEALTH_FAIL=$((HEALTH_FAIL + 1))
  fi

  log "rag (ALB)"
  if read -r lb_arn dns < <(aws elbv2 describe-load-balancers --region "${REGION}" \
      --names "${NAME_PREFIX}-alb" --query 'LoadBalancers[0].[LoadBalancerArn,DNSName]' \
      --output text 2>/dev/null) && [[ -n "${dns:-}" ]]; then
    probe_http "API liveness" "http://${dns}/api/health"
    probe_http "API readiness" "http://${dns}/api/ready"
    probe_http "Frontend" "http://${dns}/"
    tgs=$(aws elbv2 describe-target-groups --region "${REGION}" --load-balancer-arn "${lb_arn}" \
      --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null || true)
    for tg in ${tgs}; do
      states=$(q aws elbv2 describe-target-health --region "${REGION}" \
        --target-group-arn "${tg}" --query 'TargetHealthDescriptions[].TargetHealth.State' --output text)
      read -r -a words <<< "${states}"
      healthy=1
      [[ "${#words[@]}" -gt 0 ]] || healthy=0
      for w in "${words[@]}"; do [[ "${w}" == "healthy" ]] || healthy=0; done
      if [[ "${healthy}" -eq 1 ]]; then
        ok "Targets ${tg##*/targetgroup/}: ${states}"
      else
        err "Targets ${tg##*/targetgroup/}: ${states:-sin targets}"
        HEALTH_FAIL=$((HEALTH_FAIL + 1))
      fi
    done
  else
    err "ALB ${NAME_PREFIX}-alb no encontrado"; HEALTH_FAIL=$((HEALTH_FAIL + 1))
  fi

  [[ "${HEALTH_FAIL}" -eq 0 ]] || die "health: ${HEALTH_FAIL} comprobaciones fallidas."
  ok "health: todo responde."
}

# ── test ──────────────────────────────────────────────────────────────────────
test_static() {
  local s dir data_dir
  local -a scripts
  command -v terraform >/dev/null 2>&1 || die "Falta terraform ${TERRAFORM_VERSION}."
  for s in "${STACK_ORDER[@]}"; do
    dir="$(stack_dir "${s}")"
    log "Terraform fmt/validate/test: ${s}"
    terraform -chdir="${dir}" fmt -check -recursive -diff
    # TF_DATA_DIR temporal: no toca el .terraform (ni el backend) del clon.
    data_dir="$(mktemp -d)"
    # shellcheck disable=SC2064  # se expande ahora a propósito
    trap "rm -rf '${data_dir}'" EXIT
    TF_DATA_DIR="${data_dir}" terraform -chdir="${dir}" init -backend=false -lockfile=readonly -input=false >/dev/null
    TF_DATA_DIR="${data_dir}" terraform -chdir="${dir}" validate
    if compgen -G "${dir}/tests/*.tftest.hcl" >/dev/null; then
      TF_DATA_DIR="${data_dir}" terraform -chdir="${dir}" test
    fi
    rm -rf "${data_dir}"
  done

  log "Shellcheck"
  # Scripts versionados (o nuevos sin ignorar), salvo las utilidades de SageMaker.
  mapfile -t scripts < <(cd "${REPO_ROOT}" && git ls-files --cached --others --exclude-standard \
    '*.sh' ':!rag/scripts/sagemaker-*.sh' | sort -u)
  [[ "${#scripts[@]}" -gt 0 ]] || die "No se encontraron scripts para shellcheck."
  printf '  %s\n' "${scripts[@]}"
  # Versión fijada (la de la CI); la del sistema solo si no hay uvx.
  if command -v uvx >/dev/null 2>&1; then
    (cd "${REPO_ROOT}" && uvx --from "shellcheck-py==${SHELLCHECK_PY_VERSION}" shellcheck -S warning "${scripts[@]}")
  else
    command -v shellcheck >/dev/null 2>&1 || die "Falta shellcheck (o uvx para instalar la versión fijada)."
    warn "Sin uvx: se usa $(shellcheck --version | sed -n 2p); la CI usa shellcheck-py ${SHELLCHECK_PY_VERSION}."
    (cd "${REPO_ROOT}" && shellcheck -S warning "${scripts[@]}")
  fi
  ok "Validaciones estáticas correctas"
}

test_unit() {
  command -v uv >/dev/null 2>&1 || die "Falta uv."
  log "Tests de scripts de infraestructura (sin AWS)"
  (cd "${REPO_ROOT}/rag" && uv run --frozen pytest -q -ra "${INFRA_TESTS[@]}")
}

cmd_test() {
  case "${1:-all}" in
    static) test_static ;;
    unit)   test_unit ;;
    all)    test_static; test_unit ;;
    *) die "test admite static, unit o all." ;;
  esac
}

# ── start / stop ──────────────────────────────────────────────────────────────
cmd_compute() {
  local action="$1"
  shift
  exec "${REPO_ROOT}/rag/infrastructure/scripts/${action}.sh" "$@"
}

main() {
  local cmd="${1:-help}"
  [[ $# -gt 0 ]] && shift
  case "${cmd}" in
    help|-h|--help) usage ;;
    doctor)       cmd_doctor "$@" ;;
    bootstrap)    cmd_bootstrap "$@" ;;
    plan)         cmd_plan "$@" ;;
    deploy)       cmd_deploy "$@" ;;
    backup-state) cmd_backup_state "$@" ;;
    status)       cmd_status "$@" ;;
    health)       cmd_health "$@" ;;
    test)         cmd_test "$@" ;;
    start|stop)   cmd_compute "${cmd}" "$@" ;;
    *) usage >&2; die "Comando desconocido: ${cmd}" ;;
  esac
}

main "$@"
