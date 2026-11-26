# Infraestructura de ingesta (bucket S3)

Stack Terraform del bucket de datos (`s3.tf`). Las EC2 de ChromaDB/Ollama
pertenecen a `../../vector-infraestructura/`. Despliegue y orden de stacks:
[DEPLOYMENT.md](../../docs/infrastructure/DEPLOYMENT.md); state remoto:
[ADR-002](../../docs/infrastructure/adr/002-state-remoto-s3.md); controles:
[CONTROLS.md](../../docs/infrastructure/CONTROLS.md).

## Protección del bucket

`aws_s3_bucket.data` tiene `prevent_destroy = true` y versionado activado:
`terraform destroy` del stack falla, y sin `force_destroy` AWS tampoco borra un
bucket con objetos. Para retirarlo de forma consciente: respaldar el contenido
(`make bucket-backup`, desde `ingesta/`), quitar `prevent_destroy` en un cambio
revisado, vaciar el bucket (incluidas las versiones) y ejecutar
`terraform plan -destroy` antes de `terraform destroy`.

`prevent_destroy` solo actúa sobre planes de Terraform: no impide borrar objetos
o el bucket desde la consola o la CLI de AWS, ni `terraform state rm`. El state
de este stack usa el backend S3 (`terraform -chdir=ingesta/infrastructure init
-backend-config=../../backend.hcl`; ver "Terraform State remoto" en el
[README raíz](../../README.md#terraform-state-remoto)). Si se pierde, el bucket
queda fuera de Terraform y un `apply` posterior fallaría al intentar crearlo de
nuevo (el nombre ya existe). Resguardar el state y no modificar el bucket fuera
de Terraform.
