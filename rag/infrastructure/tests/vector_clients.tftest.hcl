# Prueba de plan sin AWS: `terraform test` usa un provider simulado.
# Ejecutar desde rag/infrastructure/: terraform init -backend=false && terraform test

mock_provider "aws" {
  override_during = plan
  mock_resource "aws_security_group" { defaults = { id = "sg-000000000000app" } }
  mock_resource "aws_ecr_repository" { defaults = { repository_url = "000000000000.dkr.ecr.us-east-1.amazonaws.com/repo" } }
  mock_data "aws_subnets" { defaults = { ids = ["subnet-a"] } }
  mock_data "aws_vpc" { defaults = { id = "vpc-1", cidr_block = "172.31.0.0/16" } }
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::000000000000:role/LabRole" } }
  mock_data "aws_security_group" { defaults = { id = "sg-0123456789abcdef0" } }
}

variables {
  postgres_password = "x"
  jwt_secret_key    = "y"
  chroma_host       = "172.31.1.1"
  ollama_base_url   = "http://172.31.1.2:11434"
}

run "sin_sg_de_clientes" {
  command = plan
  assert {
    condition     = length(aws_ecs_service.app.network_configuration[0].security_groups) == 1
    error_message = "Sin vector_clients_security_group_id solo se adjunta el SG de la app"
  }
}

run "con_sg_de_clientes" {
  command = plan
  variables { vector_clients_security_group_id = "sg-0123456789abcdef0" }
  assert {
    condition     = contains(aws_ecs_service.app.network_configuration[0].security_groups, "sg-0123456789abcdef0")
    error_message = "El backend debe llevar el SG vector_clients"
  }
  assert {
    condition     = !contains(aws_ecs_service.frontend.network_configuration[0].security_groups, "sg-0123456789abcdef0")
    error_message = "El frontend no necesita el SG vector_clients"
  }
}

run "sg_invalido_rechazado" {
  command = plan
  variables { vector_clients_security_group_id = "0123" }
  expect_failures = [var.vector_clients_security_group_id]
}
