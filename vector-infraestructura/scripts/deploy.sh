#!/usr/bin/env bash
# Despliega la infraestructura de Ollama y ChromaDB en EC2 y actualiza las
# variables de entorno del proyecto RAG con las IPs estáticas resultantes.
# Una vez que ChromaDB esté disponible, importa el archivo .jsonl.gz de data/.
#
# Uso:
#   ./scripts/deploy.sh
#
# terraform apply siempre pide confirmación humana: el script exige una
# terminal interactiva y rechaza --auto-approve. Antes del apply respalda el
# state (local o S3, ver scripts/lib/terraform_state.sh) en TFSTATE_BACKUP_DIR
# (default: ~/tfstate-backups), fuera del clon.
#
# Archivos que actualiza:
#   rag/.env                             — CHROMA_HOST y OLLAMA_BASE_URL (IP pública,
#                                          para el operador)
#   rag/infrastructure/terraform.tfvars  — chroma_host y ollama_base_url (IP privada,
#                                          para ECS) y vector_clients_security_group_id
#
# Ambos archivos contienen secretos: quedan con permisos 600 y los valores
# nunca se imprimen.
set -euo pipefail
# Todo archivo que cree este script (tfvars, state) nace legible solo por el dueño.
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VECTOR_INFRA_DIR="${SCRIPT_DIR}/.."
# shellcheck source=../../scripts/lib/terraform_state.sh
source "${REPO_ROOT}/scripts/lib/terraform_state.sh"
RAG_DIR="${SCRIPT_DIR}/../../rag"
DATA_DIR="${SCRIPT_DIR}/../../data"
RAG_ENV="${RAG_DIR}/.env"
ECS_TFVARS="${RAG_DIR}/infrastructure/terraform.tfvars"

# Tiempo máximo de espera para que ChromaDB levante tras el boot de la EC2
CHROMA_WAIT_SECONDS=900   # 15 min — el user-data instala Docker y descarga la imagen

# ── Colores ───────────────────────────────────────────────────────────────────
bold='\033[1m'
green='\033[0;32m'
yellow='\033[1;33m'
reset='\033[0m'

log()  { echo -e "${bold}▶ $*${reset}"; }
ok()   { echo -e "${green}✓ $*${reset}"; }
warn() { echo -e "${yellow}⚠ $*${reset}"; }

# ── 0. Salvaguardas: confirmación humana y copia del state ────────────────────
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

# Sin valor por defecto: el acceso público transitorio a ChromaDB/Ollama debe
# decidirse de forma explícita, para que un clon sin terraform.tfvars no reabra
# 8000/11434 tras la fase 3 de la migración de red.
VECTOR_TFVARS="${VECTOR_INFRA_DIR}/terraform.tfvars"
if [[ -z "${TF_VAR_legacy_public_service_access:-}" ]] \
  && ! grep -qE '^[[:space:]]*legacy_public_service_access[[:space:]]*=' "${VECTOR_TFVARS}" 2>/dev/null; then
  echo "✗ Falta legacy_public_service_access en ${VECTOR_TFVARS}." >&2
  echo "  false si la migración de red ya terminó (fase 3); true solo durante las fases 1-2." >&2
  echo "  Ver terraform.tfvars.example y 'Migración sin cortes' en vector-infraestructura/README.md." >&2
  exit 1
fi

# ── 1. Terraform init + apply ─────────────────────────────────────────────────
log "Inicializando Terraform en vector-infraestructura..."
tf_init "${VECTOR_INFRA_DIR}" "${REPO_ROOT}"

backup_state "${VECTOR_INFRA_DIR}" "vector-infraestructura"

# Con el state remoto vacío (ALLOW_EMPTY_STATE=1) se crearían de nuevo todas
# las EC2. Si ya existen en la cuenta, el bucket o la key no son los correctos:
# se detiene en lugar de duplicarlas (consulta de solo lectura).
if [[ "${TF_REMOTE_STATE_EMPTY}" -eq 1 ]]; then
  if ! existing=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=rag-playas-chromadb,rag-playas-ollama" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text --region us-east-1); then
    echo "✗ No se pudo comprobar si ya existen las EC2 de ChromaDB/Ollama." >&2
    exit 1
  fi
  if [[ -n "${existing//[[:space:]]/}" && "${existing}" != "None" ]]; then
    echo "✗ El state remoto está vacío pero ya existen EC2 de ChromaDB/Ollama (${existing})." >&2
    echo "  Revisar backend.hcl y la key antes de desplegar: no se crean duplicados." >&2
    exit 1
  fi
fi

log "Ejecutando terraform apply en vector-infraestructura (requiere confirmación)..."
terraform -chdir="${VECTOR_INFRA_DIR}" apply -input=true
restrict_state "${VECTOR_INFRA_DIR}"

# ── 2. Leer IPs estáticas del output ─────────────────────────────────────────
log "Leyendo IPs estáticas desde Terraform outputs..."

CHROMA_IP=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw chromadb_public_ip)
OLLAMA_IP=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw ollama_public_ip)
CHROMA_PRIVATE_IP=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw chromadb_private_ip)
OLLAMA_PRIVATE_IP=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw ollama_private_ip)
VECTOR_CLIENTS_SG=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw vector_clients_security_group_id)

for required in CHROMA_IP OLLAMA_IP CHROMA_PRIVATE_IP OLLAMA_PRIVATE_IP VECTOR_CLIENTS_SG; do
  if [[ -z "${!required}" ]]; then
    echo "✗ El output de Terraform para ${required} está vacío; revisar el apply de vector-infraestructura." >&2
    exit 1
  fi
done

ok "ChromaDB IP: ${CHROMA_IP} (privada ${CHROMA_PRIVATE_IP})"
ok "Ollama IP:   ${OLLAMA_IP} (privada ${OLLAMA_PRIVATE_IP})"
ok "SG de clientes: ${VECTOR_CLIENTS_SG}"

LEGACY_PUBLIC=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw legacy_public_service_access)
OPERATOR_CIDRS=$(terraform -chdir="${VECTOR_INFRA_DIR}" output -raw operator_cidr_blocks_count)
if [[ "${LEGACY_PUBLIC}" == "true" ]]; then
  warn "ChromaDB (8000) y Ollama (11434) siguen abiertos a 0.0.0.0/0 (legacy_public_service_access = true)."
  warn "Tras desplegar RAG y comprobar la app, poner legacy_public_service_access = false (fase 3, ver README)."
fi
if [[ "${OPERATOR_CIDRS}" == "0" && "${LEGACY_PUBLIC}" != "true" ]]; then
  warn "operator_cidr_blocks está vacío: el heartbeat y la importación por IP pública fallarán desde aquí."
fi

# Operador (rag/.env, heartbeat, importación): IP pública, accesible desde
# operator_cidr_blocks. ECS: IP privada, accesible desde el SG vector_clients.
CHROMA_HOST_VAL="${CHROMA_IP}"
OLLAMA_URL_VAL="http://${OLLAMA_IP}:11434"
ECS_CHROMA_HOST="${CHROMA_PRIVATE_IP}"
ECS_OLLAMA_URL="http://${OLLAMA_PRIVATE_IP}:11434"

# ── 3. Helper: actualiza o agrega una variable ────────────────────────────────
# update_env <archivo> <clave> <valor>
update_env() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^${key}=" "${file}" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${file}"
  else
    echo "${key}=${value}" >> "${file}"
  fi
}

# update_tfvars <archivo> <clave> <valor>  (formato HCL: key = "value")
update_tfvars() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "${file}" 2>/dev/null; then
    sed -i "s|^[[:space:]]*${key}[[:space:]]*=.*|${key} = \"${value}\"|" "${file}"
  else
    echo "${key} = \"${value}\"" >> "${file}"
  fi
}

# get_tfvars_value <archivo> <clave>  → valor sin comillas, vacío si no existe
get_tfvars_value() {
  local file="$1" key="$2"
  grep -E "^[[:space:]]*${key}[[:space:]]*=" "${file}" 2>/dev/null \
    | sed -E 's/^[^=]+=[[:space:]]*"?([^"#]*)"?.*/\1/' \
    | tr -d ' ' \
    | head -1
}

# auto_generate_secret <archivo> <clave> <valor_generado>
# Escribe el secreto solo si el valor actual está vacío o es un placeholder
# (empieza con "cambia-"). Nunca imprime el valor: solo informa si lo generó.
auto_generate_secret() {
  local file="$1" key="$2" generated="$3"
  local current
  current=$(get_tfvars_value "${file}" "${key}")
  if [[ -z "${current}" || "${current}" == cambia-* ]]; then
    update_tfvars "${file}" "${key}" "${generated}"
    ok "${key}: generado"
  else
    ok "${key}: ya configurado, se conserva"
  fi
}

# ── 4. Actualizar rag/.env ────────────────────────────────────────────────────
if [[ -f "${RAG_ENV}" ]]; then
  log "Actualizando ${RAG_ENV}..."
  chmod 600 "${RAG_ENV}"
  update_env "${RAG_ENV}" "CHROMA_HOST"     "${CHROMA_HOST_VAL}"
  update_env "${RAG_ENV}" "OLLAMA_BASE_URL" "${OLLAMA_URL_VAL}"
  ok ".env actualizado"
else
  warn "${RAG_ENV} no existe — omitiendo"
fi

# ── 5. Actualizar rag/infrastructure/terraform.tfvars ────────────────────────
ECS_TFVARS_EXAMPLE="${RAG_DIR}/infrastructure/terraform.tfvars.example"

if [[ ! -f "${ECS_TFVARS}" ]]; then
  if [[ -f "${ECS_TFVARS_EXAMPLE}" ]]; then
    log "Creando ${ECS_TFVARS} desde el ejemplo..."
    cp "${ECS_TFVARS_EXAMPLE}" "${ECS_TFVARS}"
    ok "terraform.tfvars creado"
  else
    warn "No se encontró terraform.tfvars.example — creando archivo vacío"
    touch "${ECS_TFVARS}"
  fi
fi
# Un tfvars creado antes de este script (o por otra herramienta) puede ser
# legible por otros usuarios: se fuerza 600 antes de escribir secretos.
chmod 600 "${ECS_TFVARS}"

log "Generando secretos automáticamente (solo si son placeholders)..."
auto_generate_secret "${ECS_TFVARS}" "postgres_password" "$(openssl rand -hex 16)"
auto_generate_secret "${ECS_TFVARS}" "jwt_secret_key"    "$(openssl rand -hex 32)"
ok "Secretos configurados en ${ECS_TFVARS} (permisos 600; los valores no se muestran)"

log "Actualizando ${ECS_TFVARS}..."
update_tfvars "${ECS_TFVARS}" "chroma_host"     "${ECS_CHROMA_HOST}"
update_tfvars "${ECS_TFVARS}" "ollama_base_url" "${ECS_OLLAMA_URL}"
update_tfvars "${ECS_TFVARS}" "vector_clients_security_group_id" "${VECTOR_CLIENTS_SG}"
ok "terraform.tfvars actualizado"

# ── 6. Esperar a que ChromaDB esté disponible ─────────────────────────────────
log "Esperando a que ChromaDB esté disponible en ${CHROMA_HOST_VAL}:8000..."
echo "   (la EC2 necesita instalar Docker y arrancar el contenedor — puede tardar varios minutos)"

deadline=$(( $(date +%s) + CHROMA_WAIT_SECONDS ))
chroma_ready=0

while [[ $(date +%s) -lt ${deadline} ]]; do
  if curl -sf --max-time 5 "http://${CHROMA_HOST_VAL}:8000/api/v2/heartbeat" > /dev/null 2>&1; then
    chroma_ready=1
    break
  fi
  echo -n "."
  sleep 10
done
echo ""

if [[ ${chroma_ready} -eq 0 ]]; then
  warn "ChromaDB no respondió en ${CHROMA_WAIT_SECONDS}s. La importación no se ejecutará."
  echo "  Puede ejecutarla manualmente cuando ChromaDB esté listo:"
  echo "    cd rag && uv run python3 ../data/import_to_chromadb.py <archivo.jsonl.gz> --host ${CHROMA_HOST_VAL}"
else
  ok "ChromaDB disponible"

  # ── 7. Localizar el archivo de exportación ──────────────────────────────────
  EXPORT_FILE=""
  if [[ ! -d "${DATA_DIR}" ]]; then
    warn "El directorio ${DATA_DIR} no existe. Asegúrate de clonar el repositorio completo."
  else
    # ls -t ordena por fecha de modificación (compatible con Linux y macOS)
    EXPORT_FILE=$(ls -t "${DATA_DIR}"/*.jsonl.gz 2>/dev/null | head -1)
  fi

  if [[ -z "${EXPORT_FILE}" ]]; then
    warn "No se encontró ningún archivo .jsonl.gz en ${DATA_DIR}. La importación no se ejecutará."
    echo "  Coloca el archivo de exportación en data/ y ejecuta:"
    echo "    cd rag && uv run python3 ../data/import_to_chromadb.py <archivo.jsonl.gz> --host ${CHROMA_HOST_VAL}"
  else
    log "Importando $(basename "${EXPORT_FILE}") → ChromaDB ${CHROMA_HOST_VAL}:8000..."
    (
      cd "${RAG_DIR}"
      uv run python3 "${DATA_DIR}/import_to_chromadb.py" \
        "${EXPORT_FILE}" \
        --host "${CHROMA_HOST_VAL}" \
        --port 8000 \
        --collection rag_playas
    )
    ok "Importación completada"
  fi
fi

# ── 8. Resumen ────────────────────────────────────────────────────────────────
echo ""
echo -e "${bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${reset}"
echo -e "${bold}Despliegue completado${reset}"
echo ""
echo -e "  CHROMA_HOST       = ${CHROMA_HOST_VAL} (rag/.env, operador)"
echo -e "  OLLAMA_BASE_URL   = ${OLLAMA_URL_VAL} (rag/.env, operador)"
echo -e "  ECS: chroma_host = ${ECS_CHROMA_HOST}, ollama_base_url = ${ECS_OLLAMA_URL} (IP privada)"
echo -e "  postgres_password y jwt_secret_key: configurados en terraform.tfvars (no se muestran)"
echo ""
echo -e "  Archivos actualizados:"
[[ -f "${RAG_ENV}"     ]] && echo -e "    • rag/.env"
[[ -f "${ECS_TFVARS}" ]] && echo -e "    • rag/infrastructure/terraform.tfvars"
echo ""
echo -e "  Antes de desplegar ECS, agrega al menos una API key de LLM en:"
echo -e "    rag/infrastructure/terraform.tfvars"
echo -e "      openai_api_key     = \"...\"   # prioridad 1"
echo -e "      openrouter_api_key = \"...\"   # prioridad 2"
echo -e "      google_api_key     = \"...\"   # prioridad 3"
echo ""
echo -e "  Luego ejecuta:"
echo -e "    ./rag/infrastructure/scripts/deploy.sh"
echo -e "${bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${reset}"
