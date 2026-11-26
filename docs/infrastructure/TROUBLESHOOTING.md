# Resolución de problemas

Empezar siempre por `scripts/playas.sh doctor`. Los scripts fallan de forma
cerrada: un error de credenciales o del backend detiene el despliegue, nunca
se interpreta como "no hay state".

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `No hay credenciales AWS válidas` / `ExpiredToken` | Sesión del lab caducada | Reiniciar el lab, volver a exportar las tres variables |
| `Falta backend.hcl` | Bootstrap no hecho en este clon | `scripts/playas.sh bootstrap`, copiar `backend.hcl.example` |
| `backend.hcl, línea N (key)` | Atributos distintos de `bucket` | Dejar solo `bucket = "rag-playas-tfstate-<cuenta>"` |
| `El bucket del state … no es de la cuenta actual` | Credenciales de otra cuenta o `backend.hcl` copiado de otra persona | Corregir credenciales o el ID de cuenta en `backend.hcl` |
| `AWS_ENDPOINT_URL_… está definida` / `TF_WORKSPACE` | Variable que desvía el backend o la key | `unset` de la variable indicada |
| `El workspace … no es 'default'` | `terraform workspace select` previo | `terraform -chdir=<stack> workspace select default` |
| `El state remoto de <stack> está vacío` | Stack nuevo, o bucket/key equivocados | Si es nuevo: `ALLOW_EMPTY_STATE=1` solo en esa ejecución. Si no, revisar `backend.hcl` |
| `… tiene versiones anteriores en el bucket` | El state existió y se borró o sobrescribió | No recrear: recuperar la versión (README raíz) o restaurar desde `backup-state` |
| `ya existen en AWS recursos del stack` | State local sin migrar, otra key o state borrado | Revisar; nunca importar en un state vacío sin entender el origen |
| `hay terraform.tfstate local` | State anterior a la migración a S3 | Migración manual (README raíz, paso 2); no borrarlo |
| `existe <key>.tflock` en `status` | Apply en curso o interrumpido | Confirmar que nadie más opera; luego `force-unlock` manual |
| `plan rag` pide `chroma_host` u otras variables | Falta `deploy vector` | Desplegar en orden: ingesta → vector → rag |
| `s3_bucket_name … no es el bucket de ingesta` | tfvars de rag con el bucket de otra cuenta | Usar el valor que muestra `deploy ingesta` |
| `health`: ChromaDB/Ollama sin respuesta | EC2 parada, user-data aún instalando, o tu IP fuera de `operator_cidr_blocks` | `status`; esperar hasta 15 min; añadir tu IP /32 |
| `health`: `/api/ready` falla y `/api/health` responde | Grafo sin compilar o índice vacío | Revisar logs: `aws logs tail /ecs/rag-playas-prod-app --follow` |
| Siembra de usuarios falla por ECS Exec | Agente SSM aún no listo o falta session-manager-plugin | Reintentar; instalar el plugin |
| `Terraform X: los stacks exigen ~> 1.17.1` | Versión local distinta | Instalar Terraform 1.17.1 |
| `Sin s3:ListBucketVersions` | Permiso no concedido en el lab | La protección de state vacío no puede usarse; desplegar un stack nuevo exige revisión manual |
