terraform {
  required_version = "~> 1.17.1"

  # State remoto en S3, un bucket por cuenta de Learner Lab
  # (rag-playas-tfstate-<account_id>, creado con scripts/bootstrap_tfstate_bucket.sh).
  # El bucket no es fijo: se pasa con -backend-config=<raíz>/backend.hcl
  # (ver backend.hcl.example). use_lockfile bloquea con <key>.tflock en S3.
  # La migración desde el state local es manual: ver "Terraform State remoto"
  # en README.md. La CI valida con init -backend=false.
  backend "s3" {
    key          = "rag-infrastructure/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
