data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# SG vector_clients de vector-infraestructura. La lectura hace fallar el plan
# si el ID no existe (p. ej. un valor de otro lab tras un reset) o si está en
# otra VPC, en lugar de fallar a mitad del apply del servicio ECS.
data "aws_security_group" "vector_clients" {
  count  = var.vector_clients_security_group_id == "" ? 0 : 1
  id     = var.vector_clients_security_group_id
  vpc_id = data.aws_vpc.default.id
}
