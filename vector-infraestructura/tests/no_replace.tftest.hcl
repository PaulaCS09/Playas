# Pasar de la fase 1 a la fase 3 de la migración de red no debe reemplazar
# las EC2 (perderían el índice de ChromaDB). Prueba sin AWS con provider simulado.
# Ejecutar desde vector-infraestructura/: terraform init -backend=false && terraform test
#
# Limitación: con mock_provider "aws" esta prueba solo detecta reemplazos que
# derivan de la configuración (p. ej. replace_triggered_by, nombres,
# count/for_each). No detecta los que decide el provider real (atributos
# ForceNew), porque el mock no los conoce. La protección real son
# prevent_destroy, ignore_changes = [ami, user_data] y la revisión humana del
# plan (ningún "must be replaced").

mock_provider "aws" {}

# Fase 1 de la migración; cada run puede sobrescribirlo.
variables {
  legacy_public_service_access = true
}

run "crear_estado_fase_1" {
  command = apply
}

# Si el cambio de reglas forzara reemplazar una EC2, prevent_destroy haría
# fallar este plan; además los IDs de las instancias no cambian.
run "fase_3_no_reemplaza_instancias" {
  command = plan
  variables {
    legacy_public_service_access = false
    operator_cidr_blocks         = ["203.0.113.10/32"]
    ssh_cidr_blocks              = ["203.0.113.10/32"]
  }

  assert {
    condition = (
      aws_instance.chromadb.id == run.crear_estado_fase_1.chromadb_instance_id &&
      aws_instance.ollama.id == run.crear_estado_fase_1.ollama_instance_id
    )
    error_message = "Las instancias no deben reemplazarse al cerrar el acceso público"
  }
}
