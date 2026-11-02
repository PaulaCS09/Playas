resource "aws_efs_file_system" "postgres" {
  creation_token   = "${local.name_prefix}-postgres"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name_prefix}-postgres-efs" }

  # Contiene los datos de Postgres: bloquea destroy y reemplazos accidentales.
  # Para destruirlo de forma consciente, ver "Destruir la infraestructura" en
  # rag/infrastructure/README.md.
  lifecycle {
    prevent_destroy = true
  }
}

# Copias automáticas de AWS Backup. Desactivado por defecto: no está comprobado
# que AWS Academy Learner Lab permita AWS Backup con LabRole, y genera coste de
# almacenamiento. El respaldo soportado es scripts/export_postgres.sh.
resource "aws_efs_backup_policy" "postgres" {
  count          = var.enable_efs_backup ? 1 : 0
  file_system_id = aws_efs_file_system.postgres.id

  backup_policy {
    status = "ENABLED"
  }
}

# Access point with UID/GID of the postgres user in alpine (999)
resource "aws_efs_access_point" "postgres" {
  file_system_id = aws_efs_file_system.postgres.id

  posix_user {
    uid = 999
    gid = 999
  }

  root_directory {
    path = "/postgres"
    creation_info {
      owner_uid   = 999
      owner_gid   = 999
      permissions = "755"
    }
  }

  tags = { Name = "${local.name_prefix}-postgres-ap" }
}

resource "aws_efs_mount_target" "postgres" {
  for_each = toset(data.aws_subnets.default.ids)

  file_system_id  = aws_efs_file_system.postgres.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}
