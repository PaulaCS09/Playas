# ------------------------------------------------------------------------------
# AMI: Ubuntu Server 24.04 LTS (última versión disponible)
# ------------------------------------------------------------------------------
data "aws_ami" "ubuntu_24" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ------------------------------------------------------------------------------
# ChromaDB — Security Group
# ------------------------------------------------------------------------------
resource "aws_security_group" "chromadb" {
  name        = "${var.project}-chromadb"
  description = "Permite SSH y trafico ChromaDB (puerto 8000)"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "ChromaDB API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Salida sin restricciones"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project}-chromadb-sg"
  }
}

# ------------------------------------------------------------------------------
# ChromaDB — EC2 Instance
# ------------------------------------------------------------------------------
resource "aws_instance" "chromadb" {
  ami                    = data.aws_ami.ubuntu_24.id
  instance_type          = "t3.large"
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.chromadb.id]

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
  }

  user_data = file("${path.module}/scripts/ec2_chroma_db.sh")

  tags = {
    Name = "${var.project}-chromadb"
  }

  # Protección de datos:
  # - prevent_destroy bloquea cualquier plan que destruya o reemplace la
  #   instancia y su disco raíz, donde vive /opt/chroma-data
  #   (destroy, -replace o un cambio que fuerce reemplazo, como key_name).
  # - ignore_changes evita que una AMI nueva de Canonical (most_recent) o un
  #   cambio en el script de arranque fuercen el reemplazo. user_data solo se
  #   ejecuta en el primer arranque; aplicarlo exige recrear la instancia.
  # Para reemplazarla de forma consciente, ver "Reemplazar o destruir las EC2"
  # en vector-infraestructura/README.md.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [ami, user_data]
  }
}

# ------------------------------------------------------------------------------
# ChromaDB — Elastic IP
# ------------------------------------------------------------------------------
resource "aws_eip" "chromadb" {
  domain = "vpc"

  tags = {
    Name = "${var.project}-chromadb-eip"
  }
}

resource "aws_eip_association" "chromadb" {
  instance_id   = aws_instance.chromadb.id
  allocation_id = aws_eip.chromadb.id
}
