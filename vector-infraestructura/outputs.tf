output "chromadb_public_ip" {
  description = "IP publica estatica de la instancia ChromaDB"
  value       = aws_eip.chromadb.public_ip
}

output "chromadb_public_dns" {
  description = "DNS publico de la instancia ChromaDB"
  value       = aws_eip.chromadb.public_dns
}

output "chromadb_instance_id" {
  description = "ID de la instancia EC2 ChromaDB"
  value       = aws_instance.chromadb.id
}

output "ollama_public_ip" {
  description = "IP publica estatica de la instancia Ollama"
  value       = aws_eip.ollama.public_ip
}

output "ollama_public_dns" {
  description = "DNS publico de la instancia Ollama"
  value       = aws_eip.ollama.public_dns
}

output "ollama_instance_id" {
  description = "ID de la instancia EC2 Ollama"
  value       = aws_instance.ollama.id
}

output "chromadb_private_ip" {
  description = "IP privada de ChromaDB: la que usa el backend en ECS (chroma_host)"
  value       = aws_instance.chromadb.private_ip
}

output "ollama_private_ip" {
  description = "IP privada de Ollama: la que usa el backend en ECS (ollama_base_url)"
  value       = aws_instance.ollama.private_ip
}

output "vector_clients_security_group_id" {
  description = "SG que deben llevar las tareas ECS del backend (vector_clients_security_group_id en rag/infrastructure)"
  value       = aws_security_group.vector_clients.id
}

output "legacy_public_service_access" {
  description = "true mientras 8000/11434 sigan abiertos a 0.0.0.0/0 (fase transitoria); deploy.sh avisa"
  value       = var.legacy_public_service_access
}

output "operator_cidr_blocks_count" {
  description = "Número de CIDR de operador autorizados; 0 implica que el heartbeat por IP pública fallará sin acceso público"
  value       = length(var.operator_cidr_blocks)
}
