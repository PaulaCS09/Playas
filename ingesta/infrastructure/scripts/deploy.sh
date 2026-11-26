#!/usr/bin/env bash
# Despliega el bucket S3 de datos de la ingesta (ingesta/infrastructure) con
# las mismas salvaguardas que vector-infraestructura y rag/infrastructure.
#
# Uso:
#   ./ingesta/infrastructure/scripts/deploy.sh
#   (o scripts/playas.sh deploy ingesta)
#
# terraform apply siempre pide confirmación humana: el script exige una
# terminal interactiva y rechaza --auto-approve. Antes del apply respalda el
# state (scripts/lib/terraform_state.sh) en TFSTATE_BACKUP_DIR (default:
# ~/tfstate-backups), fuera del clon. Con el state remoto vacío solo continúa
# con ALLOW_EMPTY_STATE=1, si la key nunca tuvo versiones, si el bucket de
# datos no existe ya y tras escribir el nombre del stack.
#
# No modifica otros stacks: al terminar indica el valor de s3_bucket_name que
# necesita rag/infrastructure/terraform.tfvars.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."
# shellcheck source=../../../scripts/lib/terraform_state.sh
source "${REPO_ROOT}/scripts/lib/terraform_state.sh"

bold='\033[1m'
green='\033[0;32m'
reset='\033[0m'
log() { echo -e "${bold}▶ $*${reset}"; }
ok()  { echo -e "${green}✓ $*${reset}"; }

# ── 0. Salvaguardas: confirmación humana ──────────────────────────────────────
if [[ $# -gt 0 ]]; then
  echo "✗ Argumento no admitido: $1. terraform apply requiere confirmación humana; --auto-approve no está permitido." >&2
  exit 1
fi
if [[ ! -t 0 ]]; then
  echo "✗ Se requiere una terminal interactiva para confirmar terraform apply (en SSH, usar ssh -t)." >&2
  exit 1
fi
for tf_var in TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan TF_CLI_ARGS_apply TF_INPUT; do
  if [[ -n "${!tf_var:-}" ]]; then
    echo "✗ ${tf_var} está definida; puede omitir la confirmación de terraform apply. Ejecuta: unset ${tf_var}" >&2
    exit 1
  fi
done

# ── 1. Terraform init + respaldo + protección de state vacío ──────────────────
log "Inicializando Terraform en ingesta/infrastructure..."
tf_init "${INFRA_DIR}" "${REPO_ROOT}"
backup_state "${INFRA_DIR}" "ingesta"
tf_guard_empty_remote_state "${INFRA_DIR}" "ingesta"

# ── 2. Terraform apply ────────────────────────────────────────────────────────
log "Ejecutando terraform apply en ingesta/infrastructure (requiere confirmación)..."
terraform -chdir="${INFRA_DIR}" apply -input=true
restrict_state "${INFRA_DIR}"

# ── 3. Contrato con rag/infrastructure ────────────────────────────────────────
BUCKET=$(terraform -chdir="${INFRA_DIR}" output -raw s3_bucket_name)
[[ -n "${BUCKET}" ]] || tf_fail "El output s3_bucket_name está vacío; revisar el apply de ingesta."
ok "Bucket de datos: ${BUCKET}"
echo ""
echo "  rag/infrastructure/terraform.tfvars debe contener:"
echo "    s3_bucket_name = \"${BUCKET}\""
echo "  (scripts/playas.sh doctor comprueba que coincide)."
