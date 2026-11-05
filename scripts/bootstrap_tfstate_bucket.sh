#!/usr/bin/env bash
# Crea y verifica el bucket S3 del Terraform State de PLAYAS en la cuenta del
# Learner Lab actual: rag-playas-tfstate-<account_id>, us-east-1.
#
# Configuración que aplica (idempotente; nunca borra nada):
#   - Object Ownership BucketOwnerEnforced (ACL desactivadas)
#   - Block Public Access completo
#   - Versionado
#   - Cifrado por defecto SSE-S3 (AES256)
#   - Política que deniega cualquier petición sin TLS (aws:SecureTransport)
# No configura ciclo de vida ni expiración de versiones.
#
# Uso (desde una terminal interactiva, con credenciales del lab):
#   RAG_EXPECTED_AWS_ACCOUNT_ID=<12 dígitos> ./scripts/bootstrap_tfstate_bucket.sh
#   RAG_EXPECTED_AWS_ACCOUNT_ID=<12 dígitos> ./scripts/bootstrap_tfstate_bucket.sh --verify-only
#
# --verify-only no escribe nada: solo comprueba la configuración del bucket.
# Al terminar indica el contenido de backend.hcl (ver backend.hcl.example).
set -euo pipefail

REGION="us-east-1"
PREFIX="rag-playas-tfstate"

bold='\033[1m'; green='\033[0;32m'; red='\033[0;31m'; reset='\033[0m'
log()  { echo -e "${bold}▶ $*${reset}"; }
ok()   { echo -e "${green}✓ $*${reset}"; }
fail() { echo -e "${red}✗ $*${reset}" >&2; exit 1; }

VERIFY_ONLY=0
case "${1:-}" in
  "") ;;
  --verify-only) VERIFY_ONLY=1 ;;
  *) fail "Argumento no admitido: $1 (solo --verify-only)." ;;
esac

command -v aws >/dev/null 2>&1 || fail "aws CLI no está instalado."

EXPECTED_ACCOUNT="${RAG_EXPECTED_AWS_ACCOUNT_ID:-}"
[[ "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]] \
  || fail "Define RAG_EXPECTED_AWS_ACCOUNT_ID con el ID de 12 dígitos de tu cuenta del lab."

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
[[ "${ACCOUNT}" == "${EXPECTED_ACCOUNT}" ]] \
  || fail "Las credenciales son de la cuenta ${ACCOUNT}, no de ${EXPECTED_ACCOUNT}. No se hace nada."
BUCKET="${PREFIX}-${ACCOUNT}"
ok "Cuenta ${ACCOUNT}; bucket ${BUCKET} (${REGION})"

# Todas las llamadas al bucket exigen que su dueño sea esta cuenta.
s3api() { aws s3api "$@" --bucket "${BUCKET}" --expected-bucket-owner "${ACCOUNT}" --region "${REGION}"; }

TLS_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${BUCKET}",
        "arn:aws:s3:::${BUCKET}/*"
      ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
EOF
)

if [[ "${VERIFY_ONLY}" -eq 0 ]]; then
  [[ -t 0 ]] || fail "Ejecuta el bootstrap desde una terminal interactiva: pide confirmación."
  echo ""
  echo "Se va a crear o completar en AWS (cuenta ${ACCOUNT}, ${REGION}):"
  echo "  bucket ${BUCKET}: BucketOwnerEnforced, Block Public Access completo,"
  echo "  versionado, SSE-S3 (AES256) y política que deniega peticiones sin TLS."
  echo "  No se borra ni se modifica ningún otro recurso."
  read -r -p "Escribe 'si' para continuar: " answer
  [[ "${answer}" == "si" ]] || fail "Cancelado: no se ha hecho ningún cambio."

  # Orden: dueño y bloqueo público primero; la política (solo Deny, no pública)
  # al final, para no aplicarla antes de que el bucket esté cerrado.
  if s3api head-bucket >/dev/null 2>&1; then
    ok "El bucket ya existe en esta cuenta; se completa su configuración"
  else
    log "Creando ${BUCKET}..."
    # us-east-1 no admite LocationConstraint.
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --object-ownership BucketOwnerEnforced >/dev/null
    ok "Bucket creado"
  fi

  log "Aplicando configuración..."
  s3api put-bucket-ownership-controls \
    --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
  s3api put-public-access-block --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
  s3api put-bucket-versioning --versioning-configuration Status=Enabled
  s3api put-bucket-encryption --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  s3api put-bucket-policy --policy "${TLS_POLICY}"
  ok "Configuración aplicada"
fi

# ── Verificación (solo lectura) ──────────────────────────────────────────────
log "Verificando ${BUCKET}..."
errors=0
check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    ok "${label}: ${actual}"
  else
    echo -e "${red}✗ ${label}: se esperaba '${expected}' y hay '${actual}'${reset}" >&2
    errors=$((errors + 1))
  fi
}

s3api head-bucket >/dev/null 2>&1 || fail "El bucket ${BUCKET} no existe o no pertenece a ${ACCOUNT}."
check "Versionado" "Enabled" \
  "$(s3api get-bucket-versioning --query Status --output text)"
check "Cifrado" "AES256" \
  "$(s3api get-bucket-encryption \
      --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
      --output text)"
check "Block Public Access" "True True True True" \
  "$(s3api get-public-access-block \
      --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
      --output text | tr '\t' ' ')"
check "Object Ownership" "BucketOwnerEnforced" \
  "$(s3api get-bucket-ownership-controls \
      --query 'OwnershipControls.Rules[0].ObjectOwnership' --output text)"
POLICY=$(s3api get-bucket-policy --query Policy --output text 2>/dev/null || true)
if grep -q '"aws:SecureTransport":"false"' <<< "$(tr -d ' \n' <<< "${POLICY}")" \
  && grep -q '"Effect":"Deny"' <<< "$(tr -d ' \n' <<< "${POLICY}")"; then
  ok "Política: deniega peticiones sin TLS"
else
  echo -e "${red}✗ Política: falta la denegación de peticiones sin TLS${reset}" >&2
  errors=$((errors + 1))
fi

[[ "${errors}" -eq 0 ]] || fail "La configuración del bucket no es la esperada (${errors} diferencias)."

echo ""
ok "Bucket listo. backend.hcl (raíz del repositorio, no versionado) debe contener:"
echo "    bucket = \"${BUCKET}\""
