# ADR-005 — ChromaDB y Ollama por IP privada, y protección de datos persistentes

- **Estado:** Aceptado (2026-11-02 / 2026-11-03)
- **Ámbito:** `vector-infraestructura/`, `rag/infrastructure/ecs.tf`, `efs.tf`, `ingesta/infrastructure/s3.tf`

## Contexto

En `main`, los Security Groups de ChromaDB y Ollama abrían los puertos 8000,
11434 y 22 a `0.0.0.0/0`, y ECS llegaba a ellos por su IP pública. Ningún
recurso con datos (EFS de Postgres, discos de las EC2 vectoriales, bucket de
datos) estaba protegido frente a un `destroy` o un reemplazo accidental.

## Decisión

- Nuevo SG **`vector_clients`** sin reglas propias, que se adjunta a las tareas
  ECS del backend. ChromaDB y Ollama solo admiten tráfico desde ese SG, por IP
  privada.
- El acceso público a 8000/11434 y a SSH queda limitado a
  `operator_cidr_blocks` / `ssh_cidr_blocks`, y las variables **solo aceptan
  IPv4 /32** (se rechazan `0.0.0.0/0` e IPv6).
- La **migración se hace sin cortes, en tres fases**, con
  `legacy_public_service_access` (obligatoria, sin valor por defecto). El
  despliegue avisa mientras siga en `true`.
- `prevent_destroy` en el EFS, en las dos EC2 vectoriales y en el bucket de
  datos. `ignore_changes = [ami, user_data]` evita reemplazos por una AMI nueva.

## Alternativas descartadas

- **Mover vector a subredes privadas con NAT Gateway:** coste fijo por hora no
  justificado en el lab y cambio de arquitectura mayor.
- **Cerrar los puertos públicos de golpe:** cortaría el servicio hasta que ECS
  pasara a IP privadas.

## Consecuencias

- Reemplazar o destruir esos recursos exige quitar `prevent_destroy` con una
  decisión explícita (procedimiento en el README de vector).
- El ALB sigue público en :80 (ADR-001).

## Verificación

- `vector-infraestructura/tests/network.tftest.hcl` (5 runs: fase 1, fase 3 sin
  acceso público, rechazo de CIDR abierto, de IPv6 y de SSH distinto de /32).
- `vector-infraestructura/tests/no_replace.tftest.hcl`: la fase 3 no reemplaza
  las instancias.
- `rag/tests/unit/test_vector_network_security.py` (12 pruebas).
