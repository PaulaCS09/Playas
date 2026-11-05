#!/usr/bin/env bash
# Despliega la aplicación RAG en ECS Fargate.
# Inicializa Terraform, aplica la infraestructura, construye las imágenes
# y fuerza el redespliegue de los servicios ECS.
#
# Uso:
#   ./scripts/deploy.sh
#
# terraform apply siempre pide confirmación humana: el script exige una
# terminal interactiva y rechaza --auto-approve. Antes del apply respalda el
# state (local o S3, ver scripts/lib/terraform_state.sh) en TFSTATE_BACKUP_DIR
# (default: ~/tfstate-backups), fuera del clon.
#
# Requisitos:
#   - rag/infrastructure/terraform.tfvars con todas las variables configuradas
#   - Credenciales AWS exportadas en el entorno
set -euo pipefail
# El state local y sus respaldos contienen secretos: legibles solo por el dueño.
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."
# shellcheck source=../../../scripts/lib/terraform_state.sh
source "${REPO_ROOT}/scripts/lib/terraform_state.sh"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Coincide con local.name_prefix = var.project + "-" + var.environment (defaults)
NAME_PREFIX="rag-playas-prod"

# ── Colores ───────────────────────────────────────────────────────────────────
bold='\033[1m'
green='\033[0;32m'
yellow='\033[1;33m'
reset='\033[0m'

log()  { echo -e "${bold}▶ $*${reset}"; }
ok()   { echo -e "${green}✓ $*${reset}"; }
warn() { echo -e "${yellow}⚠ $*${reset}"; }

# ── 0. Salvaguardas: confirmación humana ──────────────────────────────────────
if [[ $# -gt 0 ]]; then
  echo "✗ Argumento no admitido: $1. terraform apply requiere confirmación humana; --auto-approve no está permitido." >&2
  exit 1
fi
if [[ ! -t 0 ]]; then
  echo "✗ Se requiere una terminal interactiva para confirmar terraform apply (en SSH, usar ssh -t)." >&2
  exit 1
fi
# Estas variables pueden inyectar -auto-approve, -upgrade o desactivar el prompt.
for tf_var in TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan TF_CLI_ARGS_apply TF_INPUT; do
  if [[ -n "${!tf_var:-}" ]]; then
    echo "✗ ${tf_var} está definida; puede omitir la confirmación de terraform apply. Ejecuta: unset ${tf_var}" >&2
    exit 1
  fi
done

# ── Verificación de security groups ───────────────────────────────────────────
# Detecta un SG que existe en AWS con un ID distinto al del state (p. ej., tras
# un destroy parcial). No modifica el state: se detiene e indica los comandos
# para que una persona los revise y ejecute manualmente.
#
# Distingue "el recurso no está en el state" de un error del backend o de
# credenciales: la lista de direcciones se lee una sola vez y debe poder
# leerse (si falla, se detiene sin sugerir import). Si el recurso está en la
# lista, `state show` también debe funcionar.
SG_DRIFT=0
STATE_ADDRS_LOADED=0
STATE_ADDRS=""
check_sg_drift() {
  local resource="$1" sg_name="$2" vpc_id="$3"
  local aws_id state_id="" shown

  if ! aws_id=$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${vpc_id}" "Name=group-name,Values=${sg_name}" \
    --query 'SecurityGroups[0].GroupId' --output text --region "${REGION}"); then
    echo "✗ No se pudieron consultar los security groups en AWS (credenciales o permisos)." >&2
    exit 1
  fi

  [[ -z "${aws_id}" || "${aws_id}" == "None" ]] && return 0

  # State remoto vacío (ALLOW_EMPTY_STATE=1) con el SG ya en AWS: el bucket o
  # la key no son los correctos. No se sugiere state rm/import (se importaría
  # en un state equivocado) ni se crean duplicados.
  if [[ "${TF_REMOTE_STATE_EMPTY}" -eq 1 ]]; then
    echo "✗ El state remoto está vacío pero ya existe el SG '${sg_name}' (${aws_id})." >&2
    echo "  Revisar backend.hcl y la key antes de desplegar: no importar ni crear duplicados." >&2
    SG_DRIFT=1
    return 0
  fi

  if [[ "${STATE_ADDRS_LOADED}" -eq 0 ]]; then
    STATE_ADDRS=$(tf_state_addresses "${INFRA_DIR}")
    STATE_ADDRS_LOADED=1
  fi

  # -F: el punto de la dirección no es comodín; -x: sin coincidencias parciales.
  if grep -Fxq -- "${resource}" <<< "${STATE_ADDRS}"; then
    if ! shown=$(terraform -chdir="${INFRA_DIR}" state show -no-color "${resource}"); then
      echo "✗ terraform state show ${resource} falló aunque el recurso está en el state." >&2
      exit 1
    fi
    state_id=$(sed -nE 's/^[[:space:]]+id[[:space:]]+=[[:space:]]+"([^"]*)".*/\1/p' <<< "${shown}" | head -1)
  fi

  [[ "${state_id}" == "${aws_id}" ]] && return 0

  warn "SG '${sg_name}' existe en AWS (${aws_id}) pero el state tiene '${state_id:-<vacío>}'."
  echo "  Revisar antes de continuar. El state ya se respaldó. Si procede, una persona debe ejecutar manualmente:"
  [[ -n "${state_id}" ]] && echo "    terraform -chdir=\"${INFRA_DIR}\" state rm ${resource}"
  echo "    terraform -chdir=\"${INFRA_DIR}\" import ${resource} ${aws_id}"
  SG_DRIFT=1
}

# ── 1. Terraform init ─────────────────────────────────────────────────────────
log "Inicializando Terraform en rag/infrastructure..."
tf_init "${INFRA_DIR}" "${REPO_ROOT}"

# Respaldo antes de verificar los SG: si hay que corregir el state a mano,
# ya existe una copia.
backup_state "${INFRA_DIR}" "rag-infrastructure"

# ── 1b. Verificar security groups contra AWS (sin modificar el state) ─────────
log "Verificando security groups existentes en AWS..."
VPC_ID=$(aws ec2 describe-vpcs \
  --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' --output text --region "${REGION}")

check_sg_drift "aws_security_group.alb"      "${NAME_PREFIX}-alb"      "${VPC_ID}"
check_sg_drift "aws_security_group.app"      "${NAME_PREFIX}-app"      "${VPC_ID}"
check_sg_drift "aws_security_group.efs"      "${NAME_PREFIX}-efs"      "${VPC_ID}"
check_sg_drift "aws_security_group.frontend" "${NAME_PREFIX}-frontend" "${VPC_ID}"

if [[ "${SG_DRIFT}" -ne 0 ]]; then
  echo "✗ El state no coincide con los security groups de AWS. Despliegue detenido sin cambios." >&2
  exit 1
fi

# ── 2. Terraform apply ────────────────────────────────────────────────────────
log "Aplicando infraestructura ECS (requiere confirmación)..."
terraform -chdir="${INFRA_DIR}" apply -input=true
restrict_state "${INFRA_DIR}"

# ── 3. Construir y subir imágenes a ECR ───────────────────────────────────────
log "Construyendo y subiendo imágenes a ECR..."
"${SCRIPT_DIR}/push_images.sh"

# ── 4. Forzar redespliegue en ECS ─────────────────────────────────────────────
log "Forzando redespliegue de servicios ECS..."
CLUSTER=$(terraform -chdir="${INFRA_DIR}" output -raw ecs_cluster_name)

aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${CLUSTER}-app" \
  --force-new-deployment \
  --region "${REGION}" \
  --query "service.serviceName" --output text

aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "${CLUSTER}-frontend" \
  --force-new-deployment \
  --region "${REGION}" \
  --query "service.serviceName" --output text

ok "Servicios ECS actualizados"

# ── 5. Importar usuarios semilla a postgres ───────────────────────────────────
# El backend crea la tabla `users` al arrancar (init_db), así que hay que esperar
# a que el servicio esté estable antes de importar. El SQL usa ON CONFLICT DO
# NOTHING, por lo que re-ejecutarlo en cada deploy es inocuo.
SEED_SQL="${SCRIPT_DIR}/../../../data/seed_users.sql"

if [[ -f "${SEED_SQL}" ]]; then
  log "Esperando a que el servicio app esté estable para importar usuarios..."
  aws ecs wait services-stable \
    --cluster "${CLUSTER}" \
    --services "${CLUSTER}-app" \
    --region "${REGION}"

  TASK_ARN=$(aws ecs list-tasks \
    --cluster "${CLUSTER}" \
    --service-name "${CLUSTER}-app" \
    --desired-status RUNNING \
    --query 'taskArns[0]' --output text --region "${REGION}")

  if [[ -z "${TASK_ARN}" || "${TASK_ARN}" == "None" ]]; then
    warn "No se encontró una tarea RUNNING del servicio app; se omite la importación de usuarios."
  else
    log "Importando usuarios semilla vía ECS Exec..."
    # base64 evita problemas de escapado al pasar SQL multilínea por --command
    SEED_B64=$(base64 -w0 "${SEED_SQL}")

    # El agente de ECS Exec puede tardar unos segundos en estar listo tras el deploy
    for intento in 1 2 3 4 5; do
      if aws ecs execute-command \
        --cluster "${CLUSTER}" \
        --task "${TASK_ARN}" \
        --container postgres \
        --interactive \
        --region "${REGION}" \
        --command "sh -c 'echo ${SEED_B64} | base64 -d | psql -U atlas -d atlas -v ON_ERROR_STOP=1'"; then
        ok "Usuarios semilla importados"
        break
      fi
      if [[ "${intento}" == "5" ]]; then
        warn "No se pudo importar usuarios tras ${intento} intentos; importar manualmente con ECS Exec."
      else
        warn "ECS Exec aún no disponible (intento ${intento}); reintentando en 15s..."
        sleep 15
      fi
    done
  fi
else
  warn "No existe ${SEED_SQL}; se omite la importación de usuarios."
fi

# ── 6. Resumen ────────────────────────────────────────────────────────────────
ALB_URL=$(terraform -chdir="${INFRA_DIR}" output -raw alb_url)

echo ""
echo -e "${bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${reset}"
echo -e "${bold}Despliegue completado${reset}"
echo ""
echo -e "  URL: ${ALB_URL}"
echo ""
echo -e "  Logs en tiempo real:"
echo -e "    aws logs tail /ecs/${CLUSTER}-app      --region ${REGION} --follow"
echo -e "    aws logs tail /ecs/${CLUSTER}-frontend --region ${REGION} --follow"
echo -e "${bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${reset}"
