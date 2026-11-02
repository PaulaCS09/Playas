# ------------------------------------------------------------------------------
# Ollama Embeddings — Security Group
# ------------------------------------------------------------------------------
resource "aws_security_group" "ollama" {
  name        = "${var.project}-ollama"
  description = "Permite SSH y trafico Ollama (puerto 11434)"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Ollama API"
    from_port   = 11434
    to_port     = 11434
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
    Name = "${var.project}-ollama-sg"
  }
}

# ------------------------------------------------------------------------------
# Ollama Embeddings — EC2 Instance
# ------------------------------------------------------------------------------
resource "aws_instance" "ollama" {
  ami                    = data.aws_ami.ubuntu_24.id
  instance_type          = "t3.large"
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.ollama.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = file("${path.module}/scripts/ec2_ollama_embeddings.sh")

  tags = {
    Name = "${var.project}-ollama"
  }

  # Protección de datos:
  # - prevent_destroy bloquea cualquier plan que destruya o reemplace la
  #   instancia y su disco raíz, donde están los modelos
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
# Ollama Embeddings — Elastic IP
# ------------------------------------------------------------------------------
resource "aws_eip" "ollama" {
  domain = "vpc"

  tags = {
    Name = "${var.project}-ollama-eip"
  }
}

resource "aws_eip_association" "ollama" {
  instance_id   = aws_instance.ollama.id
  allocation_id = aws_eip.ollama.id
}
