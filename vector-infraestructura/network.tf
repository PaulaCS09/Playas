# ------------------------------------------------------------------------------
# Clientes autorizados de ChromaDB y Ollama
# ------------------------------------------------------------------------------
# SG sin reglas propias: las tareas ECS del backend lo llevan adjunto y los SG
# de ChromaDB y Ollama admiten tráfico desde él. Solo coincide con tráfico por
# IP privada, por eso el backend usa chromadb_private_ip / ollama_private_ip.
# rag/infrastructure lo recibe como vector_clients_security_group_id.
resource "aws_security_group" "vector_clients" {
  name        = "${var.project}-vector-clients"
  description = "Clientes autorizados de ChromaDB y Ollama (tareas ECS del backend)"

  tags = {
    Name = "${var.project}-vector-clients-sg"
  }
}
