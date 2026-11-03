# Prueba de plan sin AWS: `terraform test` usa un provider simulado.
# Ejecutar desde vector-infraestructura/: terraform init -backend=false && terraform test

mock_provider "aws" {
  override_during = plan
  mock_resource "aws_security_group" { defaults = { id = "sg-0000000000000mock" } }
  mock_data "aws_ami" { defaults = { id = "ami-0000000000000mock" } }
}

# Fase 1 de la migración; cada run puede sobrescribirlo.
variables {
  legacy_public_service_access = true
}

run "fase_1_por_defecto" {
  command = plan

  assert {
    condition = anytrue([
      for r in aws_security_group.chromadb.ingress : contains(r.cidr_blocks == null ? [] : r.cidr_blocks, "0.0.0.0/0") && r.from_port == 8000
    ])
    error_message = "En la fase 1 el puerto 8000 sigue abierto de forma transitoria"
  }
  assert {
    condition = anytrue([
      for r in aws_security_group.chromadb.ingress : contains(r.security_groups == null ? [] : r.security_groups, aws_security_group.vector_clients.id) && r.from_port == 8000
    ])
    error_message = "ChromaDB debe admitir el SG vector_clients en el 8000"
  }
  assert {
    condition = alltrue([
      for sg in [aws_security_group.chromadb, aws_security_group.ollama] : alltrue([for r in sg.ingress : r.from_port != 22])
    ])
    error_message = "Sin ssh_cidr_blocks no debe haber regla SSH"
  }
}

run "fase_3_sin_acceso_publico" {
  command = plan
  variables {
    legacy_public_service_access = false
    operator_cidr_blocks         = ["203.0.113.10/32"]
    ssh_cidr_blocks              = ["203.0.113.10/32"]
  }

  assert {
    condition = alltrue([
      for sg in [aws_security_group.chromadb, aws_security_group.ollama] : alltrue([
        for r in sg.ingress : !contains(r.cidr_blocks == null ? [] : r.cidr_blocks, "0.0.0.0/0")
      ])
    ])
    error_message = "En la fase 3 ningún ingress puede admitir 0.0.0.0/0"
  }
  assert {
    condition = alltrue([
      for sg in [aws_security_group.chromadb, aws_security_group.ollama] : alltrue([
        for r in sg.ingress : r.from_port != 22 || r.cidr_blocks == tolist(["203.0.113.10/32"])
      ])
    ])
    error_message = "SSH solo desde ssh_cidr_blocks"
  }
  assert {
    condition = anytrue([
      for r in aws_security_group.ollama.ingress : r.from_port == 11434 && r.cidr_blocks == tolist(["203.0.113.10/32"])
    ])
    error_message = "El operador debe conservar acceso a Ollama"
  }
}

run "cidr_abierto_rechazado" {
  command = plan
  variables { operator_cidr_blocks = ["0.0.0.0/0"] }
  expect_failures = [var.operator_cidr_blocks]
}

run "ipv6_rechazado" {
  command = plan
  variables { operator_cidr_blocks = ["::/32"] }
  expect_failures = [var.operator_cidr_blocks]
}

run "ssh_no_32_rechazado" {
  command = plan
  variables { ssh_cidr_blocks = ["10.0.0.0/24"] }
  expect_failures = [var.ssh_cidr_blocks]
}
