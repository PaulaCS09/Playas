#!/usr/bin/env bash
# Crea una EC2 "bastion" y despliega ATLAS completo desde ella siguiendo las
# instrucciones de despliegue automático descritas en el README del proyecto:
#
#   1. Infraestructura vectorial (ChromaDB + Ollama)  → vector-infraestructura/scripts/deploy.sh
#   2. Aplicación RAG en ECS Fargate                  → rag/infrastructure/scripts/deploy.sh
#
# La EC2 se usa solo como máquina de build/despliegue (corre Terraform, AWS
# CLI y Docker); la app final queda en ECS Fargate detrás del ALB, no en esta
# instancia.
#
# Requisitos locales:
#   - AWS CLI configurado (usa las credenciales de sesión del Learner Lab)
#   - Key pair "vockey" ya creado en EC2 y el archivo vockey.pem disponible localmente
#   - rag/.env con OPENAI_API_KEY configurada (se usa para el paso 2 del README)
#
# Uso (desde una terminal interactiva):
#   BASTION_SSH_CIDR=<ip-publica>/32 ./scripts/deploy_bastion_ec2.sh
#
# Variables obligatorias:
#   BASTION_SSH_CIDR  IP pública autorizada para SSH, con máscara /32.
#                     No se admite 0.0.0.0/0.
#
# Variables opcionales:
#   KEY_FILE   Ruta al .pem (default: ~/vockey.pem)
#   AWS_REGION Región AWS (default: us-east-1)
#
# Cada terraform apply se ejecuta por `ssh -t` y pide confirmación humana.
# Si el bastion ya tiene ~/playas, no se borra: se respaldan los states y se
# actualiza el clon con `git pull --ff-only`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT="rag-playas"
INSTANCE_NAME="${PROJECT}-bastion"
INSTANCE_TYPE="m4.large"
VOLUME_SIZE_GB=50
KEY_NAME="vockey"
KEY_FILE="${KEY_FILE:-${HOME}/vockey.pem}"
# Versión de Terraform común a stacks (~> 1.17.1), CI y bastion. El hash es el
# de terraform_1.17.1_linux_amd64.zip en releases.hashicorp.com (SHA256SUMS);
# cambiarlo solo junto con la versión, en un PR revisado.
TERRAFORM_VERSION="1.17.1"
TERRAFORM_SHA256="4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061"
REGION="${AWS_REGION:-us-east-1}"
# LabRole es el rol preexistente en AWS Academy (ver rag/infrastructure/iam.tf);
# LabInstanceProfile es el instance profile estándar que lo expone a las EC2.
IAM_INSTANCE_PROFILE="LabInstanceProfile"
REPO_URL="https://github.com/jpospinalo/playas.git"
SSH_USER="ubuntu"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10
  -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -i "${KEY_FILE}")

# ── Colores / helpers ──────────────────────────────────────────────────────
bold='\033[1m'; green='\033[0;32m'; yellow='\033[1;33m'; red='\033[0;31m'; reset='\033[0m'

LOCAL_STEP=0
LOCAL_TOTAL=11
step()  { LOCAL_STEP=$((LOCAL_STEP+1)); echo ""; echo -e "${bold}▶ [LOCAL ${LOCAL_STEP}/${LOCAL_TOTAL}] $*${reset}"; }
ok()    { echo -e "${green}✓ $*${reset}"; }
warn()  { echo -e "${yellow}⚠ $*${reset}"; }
fail()  { echo -e "${red}✗ $*${reset}"; exit 1; }

# announce_ssh_rule <sg_id>: describe la escritura en AWS antes de hacerla.
announce_ssh_rule() {
  echo -e "${bold}Se va a crear esta regla de entrada en AWS:${reset}"
  echo "  Security group: $1"
  echo "  IP autorizada:  ${BASTION_SSH_CIDR%/32}"
  echo "  Regla:          tcp/22 desde ${BASTION_SSH_CIDR}"
}

# ── 1. Validar prerrequisitos locales ──────────────────────────────────────
step "Validando prerrequisitos locales"

command -v aws >/dev/null 2>&1 || fail "aws CLI no está instalado."

# normalize_ssh_cidr <cidr>: imprime la IPv4 /32 normalizada (sin ceros a la
# izquierda) o falla si no es una IPv4 /32 válida distinta de 0.0.0.0.
normalize_ssh_cidr() {
  local cidr="$1" octet
  local -a octets normalized=()
  [[ "${cidr}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || return 1
  IFS='.' read -r -a octets <<< "${cidr%/32}"
  for octet in "${octets[@]}"; do
    octet=$((10#${octet}))
    (( octet <= 255 )) || return 1
    normalized+=("${octet}")
  done
  [[ "${normalized[*]}" != "0 0 0 0" ]] || return 1
  local IFS='.'
  echo "${normalized[*]}/32"
}

BASTION_SSH_CIDR="${BASTION_SSH_CIDR:-}"
[[ -n "${BASTION_SSH_CIDR}" ]] || fail "Define BASTION_SSH_CIDR con tu IP pública autorizada (formato a.b.c.d/32)."
BASTION_SSH_CIDR="$(normalize_ssh_cidr "${BASTION_SSH_CIDR}")" \
  || fail "BASTION_SSH_CIDR debe ser una IPv4 con máscara /32 (no se admite 0.0.0.0/0)."
[[ -t 0 ]] || fail "Ejecuta este script desde una terminal interactiva: cada terraform apply pide confirmación."

[[ -f "${KEY_FILE}" ]] || fail "No se encontró el key pair local en ${KEY_FILE} (usa KEY_FILE=/ruta/vockey.pem)."
chmod 400 "${KEY_FILE}"

RAG_ENV="${REPO_DIR}/rag/.env"
[[ -f "${RAG_ENV}" ]] || fail "No se encontró ${RAG_ENV}."

OPENAI_API_KEY="$(grep -E '^OPENAI_API_KEY=' "${RAG_ENV}" | head -1 | cut -d'=' -f2-)"
[[ -n "${OPENAI_API_KEY}" ]] || fail "OPENAI_API_KEY no está configurada en ${RAG_ENV}."

# Con backend S3 en los stacks, el bastion necesita backend.hcl (bucket del
# state de esta cuenta). Se comprueba aquí, antes de crear nada en AWS.
BACKEND_HCL="${REPO_DIR}/backend.hcl"
if grep -qsE '^[[:space:]]*backend[[:space:]]+"s3"' \
  "${REPO_DIR}"/vector-infraestructura/*.tf "${REPO_DIR}"/rag/infrastructure/*.tf; then
  [[ -f "${BACKEND_HCL}" ]] \
    || fail "Falta ${BACKEND_HCL}: crear el bucket con scripts/bootstrap_tfstate_bucket.sh y copiar backend.hcl.example."
  grep -qE '^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"rag-playas-tfstate-[0-9]{12}"' "${BACKEND_HCL}" \
    || fail "${BACKEND_HCL} debe contener bucket = \"rag-playas-tfstate-<account_id>\" (ver backend.hcl.example)."
fi

ok "Prerrequisitos OK (key pair, OPENAI_API_KEY presentes)"

# ── 2. Security group SSH ──────────────────────────────────────────────────
step "Buscando/creando security group para SSH"

VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' --output text --region "${REGION}")
[[ "${VPC_ID}" != "None" ]] || fail "No se encontró una VPC por defecto en ${REGION}."

SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${INSTANCE_NAME}-sg" \
  --query 'SecurityGroups[0].GroupId' --output text --region "${REGION}" 2>/dev/null || true)

if [[ -z "${SG_ID}" || "${SG_ID}" == "None" ]]; then
  SG_ID=$(aws ec2 create-security-group \
    --group-name "${INSTANCE_NAME}-sg" \
    --description "SSH para bastion de despliegue de ${PROJECT}" \
    --vpc-id "${VPC_ID}" \
    --query 'GroupId' --output text --region "${REGION}")
  announce_ssh_rule "${SG_ID}"
  aws ec2 authorize-security-group-ingress \
    --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${BASTION_SSH_CIDR}" \
    --region "${REGION}" >/dev/null
  ok "Security group creado: ${SG_ID} (SSH solo desde ${BASTION_SSH_CIDR})"
else
  # Lista de permitidos: toda regla que alcance el puerto 22/tcp (protocolo -1 o
  # un rango tcp que incluya el 22), IPv4 o IPv6, debe ser exactamente BASTION_SSH_CIDR.
  SSH_RULES="SecurityGroups[0].IpPermissions[?IpProtocol=='-1' || (IpProtocol=='tcp' && FromPort<=\`22\` && ToPort>=\`22\`)]"
  # Asignación directa: si aws falla, set -e detiene el script.
  SSH_V4_RAW=$(aws ec2 describe-security-groups --group-ids "${SG_ID}" \
    --query "${SSH_RULES}.IpRanges[].CidrIp" --output text --region "${REGION}")
  SSH_V6_RAW=$(aws ec2 describe-security-groups --group-ids "${SG_ID}" \
    --query "${SSH_RULES}.Ipv6Ranges[].CidrIpv6" --output text --region "${REGION}")
  # La salida text puede ocupar varias líneas o columnas: se aplana a una lista.
  read -r -a SSH_V4 <<< "$(tr '\n\t' '  ' <<< "${SSH_V4_RAW}")"
  read -r -a SSH_V6 <<< "$(tr '\n\t' '  ' <<< "${SSH_V6_RAW}")"
  UNEXPECTED=()
  HAS_OPERATOR_CIDR=0
  for cidr in "${SSH_V4[@]}" "${SSH_V6[@]}"; do
    [[ -z "${cidr}" || "${cidr}" == "None" ]] && continue
    if [[ "${cidr}" == "${BASTION_SSH_CIDR}" ]]; then
      HAS_OPERATOR_CIDR=1
    else
      UNEXPECTED+=("${cidr}")
    fi
  done
  if [[ ${#UNEXPECTED[@]} -gt 0 ]]; then
    fail "El security group ${SG_ID} permite SSH desde orígenes no autorizados: ${UNEXPECTED[*]}.
  Una persona debe revisar y revocar esas reglas antes de continuar, por ejemplo:
    aws ec2 revoke-security-group-ingress --group-id ${SG_ID} --protocol tcp --port 22 --cidr <cidr> --region ${REGION}"
  fi
  if [[ "${HAS_OPERATOR_CIDR}" -eq 0 ]]; then
    announce_ssh_rule "${SG_ID}"
    aws ec2 authorize-security-group-ingress \
      --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${BASTION_SSH_CIDR}" \
      --region "${REGION}" >/dev/null
    ok "Regla SSH añadida al security group ${SG_ID}: ${BASTION_SSH_CIDR}"
  fi
  ok "Security group existente reutilizado: ${SG_ID} (SSH solo desde ${BASTION_SSH_CIDR})"
fi

# ── 3. AMI Ubuntu 24.04 ─────────────────────────────────────────────────────
step "Buscando AMI Ubuntu 24.04 LTS"

AMI_ID=$(aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
            "Name=virtualization-type,Values=hvm" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text --region "${REGION}")
[[ "${AMI_ID}" != "None" ]] || fail "No se encontró una AMI de Ubuntu 24.04."
ok "AMI: ${AMI_ID}"

# ── 4. Lanzar (o reutilizar) la instancia EC2 ──────────────────────────────
step "Lanzando instancia EC2 (${INSTANCE_TYPE}, ${VOLUME_SIZE_GB}GB, key=${KEY_NAME})"

INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${INSTANCE_NAME}" "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text --region "${REGION}" 2>/dev/null || true)

if [[ -n "${INSTANCE_ID}" && "${INSTANCE_ID}" != "None" ]]; then
  ok "Reutilizando instancia existente: ${INSTANCE_ID}"
else
  INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" \
    --security-group-ids "${SG_ID}" \
    --iam-instance-profile "Name=${IAM_INSTANCE_PROFILE}" \
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":${VOLUME_SIZE_GB},\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}}]" \
    --query 'Instances[0].InstanceId' --output text --region "${REGION}")
  ok "Instancia creada: ${INSTANCE_ID}"
fi

log_wait() { echo -e "${bold}… $*${reset}"; }

log_wait "Esperando a que la instancia esté 'running'..."
aws ec2 wait instance-running --instance-ids "${INSTANCE_ID}" --region "${REGION}"

PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text --region "${REGION}")
ok "Instancia running — IP pública: ${PUBLIC_IP}"

# ── 5. Esperar SSH disponible ──────────────────────────────────────────────
step "Esperando a que SSH esté disponible en ${PUBLIC_IP}"

for i in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "${SSH_USER}@${PUBLIC_IP}" "true" 2>/dev/null; then
    ok "SSH disponible"
    break
  fi
  [[ "${i}" == "30" ]] && fail "SSH no respondió tras 5 minutos."
  echo -n "."
  sleep 10
done

# ── 6. Copiar API key de OpenAI y datos semilla al bastion ────────────────
step "Copiando OPENAI_API_KEY y archivos de datos semilla al bastion (sin exponerlos en logs)"

printf '%s' "${OPENAI_API_KEY}" | ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
  'umask 077; cat > ~/.openai_api_key'
ok "OPENAI_API_KEY transferida"

# Si el script termina antes de volcar la clave en terraform.tfvars (p. ej., la
# persona rechaza el primer apply), se borra la copia temporal del bastion.
cleanup_remote_key() {
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
    'shred -u ~/.openai_api_key 2>/dev/null || rm -f ~/.openai_api_key' >/dev/null 2>&1 \
    || warn "No se pudo borrar ~/.openai_api_key del bastion; borrarla manualmente."
}
trap cleanup_remote_key EXIT

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" 'mkdir -p ~/seed_data'

LATEST_EXPORT="$(ls -t "${REPO_DIR}"/data/*.jsonl.gz 2>/dev/null | head -1 || true)"
if [[ -n "${LATEST_EXPORT}" ]]; then
  scp "${SSH_OPTS[@]}" "${LATEST_EXPORT}" "${SSH_USER}@${PUBLIC_IP}:~/seed_data/" >/dev/null
  ok "Export de ChromaDB copiado: $(basename "${LATEST_EXPORT}")"
else
  warn "No se encontró ningún data/*.jsonl.gz local; la importación a ChromaDB se omitirá."
fi

if [[ -f "${REPO_DIR}/data/seed_users.sql" ]]; then
  scp "${SSH_OPTS[@]}" "${REPO_DIR}/data/seed_users.sql" "${SSH_USER}@${PUBLIC_IP}:~/seed_data/" >/dev/null
  ok "seed_users.sql copiado"
fi

# ── 7. Bootstrap remoto: instalar dependencias y preparar el clon ─────────
# Este bloque se envía por stdin y no ejecuta Terraform: un terraform apply
# dentro del heredoc leería el resto del script como respuesta al prompt.
BOOTSTRAP_ONLY="${BOOTSTRAP_ONLY:-0}"
step "Ejecutando bootstrap remoto en la EC2 (esto tarda varios minutos)"

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
  "REPO_URL='${REPO_URL}' TERRAFORM_VERSION='${TERRAFORM_VERSION}' TERRAFORM_SHA256='${TERRAFORM_SHA256}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

bold='\033[1m'; green='\033[0;32m'; reset='\033[0m'
STEP=0; TOTAL=8
step() {
  STEP=$((STEP+1))
  echo ""
  echo -e "${bold}════════════════════════════════════════════════════${reset}"
  echo -e "${bold}▶ [EC2 ${STEP}/${TOTAL}] $*${reset}"
  echo -e "${bold}════════════════════════════════════════════════════${reset}"
}
ok() { echo -e "${green}✓ $*${reset}"; }

APT_ENV=(sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a)

step "Actualizando paquetes del sistema"
"${APT_ENV[@]}" apt-get update -y
"${APT_ENV[@]}" apt-get upgrade -y

step "Instalando git"
"${APT_ENV[@]}" apt-get install -y git
ok "git $(git --version)"

step "Instalando Docker y agregando ubuntu al grupo docker"
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
sudo systemctl enable --now docker
ok "docker $(docker --version)"

step "Instalando AWS CLI v2"
if ! command -v aws >/dev/null 2>&1; then
  "${APT_ENV[@]}" apt-get install -y unzip
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  (cd /tmp && unzip -q awscliv2.zip && sudo ./aws/install)
fi
ok "$(aws --version)"

step "Instalando Terraform ${TERRAFORM_VERSION} (zip oficial verificado con SHA256)"
# Versión fija y verificada: apt-get upgrade no la cambia (no se instala desde apt).
"${APT_ENV[@]}" apt-get install -y unzip
if [[ -x /usr/local/bin/terraform ]] \
  && /usr/local/bin/terraform version | head -1 | grep -qx "Terraform v${TERRAFORM_VERSION}"; then
  ok "Terraform ${TERRAFORM_VERSION} ya instalado"
else
  tf_tmp="$(mktemp -d)"
  curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    -o "${tf_tmp}/terraform.zip"
  echo "${TERRAFORM_SHA256}  ${tf_tmp}/terraform.zip" | sha256sum -c -
  unzip -q -o "${tf_tmp}/terraform.zip" terraform -d "${tf_tmp}"
  sudo install -m 0755 "${tf_tmp}/terraform" /usr/local/bin/terraform
  rm -rf "${tf_tmp}"
fi
hash -r
[[ "$(command -v terraform)" == "/usr/local/bin/terraform" ]] \
  || { echo "✗ El terraform del PATH no es /usr/local/bin/terraform (revisar instalaciones previas)." >&2; exit 1; }
ok "$(terraform version | head -1)"

step "Instalando uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="${HOME}/.local/bin:${PATH}"
ok "$(uv --version)"

step "Instalando Session Manager Plugin"
curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o /tmp/session-manager-plugin.deb
sudo dpkg -i /tmp/session-manager-plugin.deb
ok "session-manager-plugin instalado"

step "Preparando el repositorio en ~/playas (sin borrar states ni tfvars)"
if [[ -e ~/playas ]]; then
  [[ -d ~/playas/.git ]] || { echo "✗ ~/playas existe pero no es un repositorio Git; revisarlo manualmente." >&2; exit 1; }
  # Los *.tfstate y *.tfvars están ignorados por Git: git pull no los toca.
  # Aun así se respaldan fuera del clon antes de actualizar.
  BACKUP_DIR="${HOME}/tfstate-backups/pre-pull-$(date +%Y%m%d-%H%M%S)"
  (
    umask 077
    mkdir -p "${BACKUP_DIR}"
    cd ~/playas
    find . -path ./.git -prune -o -name 'terraform.tfstate*' \( -type f -o -type l \) -print \
      | while IFS= read -r f; do
          mkdir -p "${BACKUP_DIR}/$(dirname "${f}")"
          cp -pL "${f}" "${BACKUP_DIR}/${f}"
          chmod 600 "${BACKUP_DIR}/${f}"
        done
  )
  ok "States existentes respaldados en ${BACKUP_DIR}"
  BRANCH="$(git -C ~/playas rev-parse --abbrev-ref HEAD)"
  if [[ "${BRANCH}" != "main" ]]; then
    echo "✗ ~/playas está en '${BRANCH}', no en main. Revisarlo manualmente (git -C ~/playas switch main)." >&2
    exit 1
  fi
  if [[ -n "$(git -C ~/playas status --porcelain --untracked-files=no)" ]]; then
    echo "✗ ~/playas tiene cambios locales en archivos versionados:" >&2
    git -C ~/playas status --short --untracked-files=no >&2
    echo "  Revisarlos (p. ej., git -C ~/playas diff) y descartarlos o guardarlos manualmente antes de repetir." >&2
    exit 1
  fi
  git -C ~/playas pull --ff-only
  ok "Repositorio actualizado en ~/playas ($(git -C ~/playas rev-parse --short HEAD))"
else
  git clone "${REPO_URL}" ~/playas
  ok "Repositorio clonado en ~/playas ($(git -C ~/playas rev-parse --short HEAD))"
fi

mkdir -p ~/playas/data
[[ -d ~/seed_data ]] && cp -v ~/seed_data/* ~/playas/data/ 2>/dev/null || true
ok "Bootstrap completado"
REMOTE_SCRIPT

# backend.hcl (solo el nombre del bucket, sin credenciales) al clon del bastion.
# Por stdin, sin heredoc: los deploy.sh lo leen de ~/playas/backend.hcl.
if [[ -f "${BACKEND_HCL}" ]]; then
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" 'umask 077; cat > ~/playas/backend.hcl' < "${BACKEND_HCL}"
  ok "backend.hcl copiado al bastion"
fi

if [[ "${BOOTSTRAP_ONLY}" == "1" ]]; then
  echo ""
  echo -e "${bold}BOOTSTRAP_ONLY=1: deteniendo antes de los despliegues de Terraform.${reset}"
  exit 0
fi

# ── 8. Despliegues con confirmación humana (ssh -t asigna una terminal) ────
REMOTE_PATH='export PATH="${HOME}/.local/bin:${PATH}"'

step "Desplegando infraestructura vectorial - README paso 1 (ChromaDB + Ollama)"
ssh -t "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
  "${REMOTE_PATH}; cd ~/playas && ./vector-infraestructura/scripts/deploy.sh"

step "Configurando OPENAI_API_KEY en el bastion - README paso 2"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" "bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
green='\033[0;32m'; reset='\033[0m'
ok() { echo -e "${green}✓ $*${reset}"; }
TFVARS="${HOME}/playas/rag/infrastructure/terraform.tfvars"
KEY_VALUE="$(cat "${HOME}/.openai_api_key")"
if grep -q '^openai_api_key' "${TFVARS}" 2>/dev/null; then
  sed -i "s|^openai_api_key.*|openai_api_key = \"${KEY_VALUE}\"|" "${TFVARS}"
else
  echo "openai_api_key = \"${KEY_VALUE}\"" >> "${TFVARS}"
fi
shred -u "${HOME}/.openai_api_key" 2>/dev/null || rm -f "${HOME}/.openai_api_key"
ok "openai_api_key configurada en terraform.tfvars"
REMOTE_SCRIPT
trap - EXIT

step "Desplegando aplicación RAG en ECS Fargate - README paso 2"
ssh -t "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
  "${REMOTE_PATH}; cd ~/playas && sg docker -c ./rag/infrastructure/scripts/deploy.sh"

ALB_URL=$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
  "terraform -chdir=\${HOME}/playas/rag/infrastructure output -raw alb_url" 2>/dev/null \
  || echo "(ejecuta 'terraform output alb_url' manualmente)")
ok "URL de la aplicación: ${ALB_URL}"

echo ""
step "Resumen"
ok "Bastion EC2: ${INSTANCE_ID} (${PUBLIC_IP})"
ok "Conéctate con: ssh -i ${KEY_FILE} ${SSH_USER}@${PUBLIC_IP}"
