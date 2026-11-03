variable "project" {
  description = "Nombre del proyecto, usado en nombres y tags de recursos"
  type        = string
  default     = "rag-playas"
}

variable "environment" {
  description = "Entorno de despliegue"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "El entorno debe ser dev, staging o prod."
  }
}

variable "key_pair_name" {
  description = "Nombre del key pair de AWS existente para acceso SSH"
  type        = string
  default     = "vockey"
}

# ── Red: orígenes autorizados ─────────────────────────────────────────────────

variable "operator_cidr_blocks" {
  description = "IPs públicas (/32) del operador o del bastion que pueden llegar a ChromaDB (8000) y Ollama (11434) por la IP pública: heartbeat, importación y start.sh."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for c in var.operator_cidr_blocks :
      can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$", c)) && can(cidrhost(c, 0)) && !startswith(c, "0.0.0.0/")
    ])
    error_message = "operator_cidr_blocks solo admite IPv4 con máscara /32 (no 0.0.0.0/0)."
  }
}

variable "ssh_cidr_blocks" {
  description = "IPs públicas (/32) autorizadas para SSH a las EC2 de ChromaDB y Ollama. Vacío: SSH cerrado."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for c in var.ssh_cidr_blocks :
      can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$", c)) && can(cidrhost(c, 0)) && !startswith(c, "0.0.0.0/")
    ])
    error_message = "ssh_cidr_blocks solo admite IPv4 con máscara /32 (no 0.0.0.0/0)."
  }
}

variable "legacy_public_service_access" {
  description = "Obligatoria, sin valor por defecto. true SOLO durante las fases 1-2 de la migración de red (8000/11434 abiertos a 0.0.0.0/0 mientras ECS pasa a IP privadas); false desde la fase 3 y para siempre. Ver README, 'Migración sin cortes'."
  type        = bool
  nullable    = false
}
