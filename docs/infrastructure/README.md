# Infraestructura de PLAYAS

Documentación de la plataforma AWS (AWS Academy Learner Lab). Lo que hacen
los scripts lo definen los propios scripts y sus tests; estos documentos
explican cómo se combinan.

| Documento | Contenido |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diagrama, stacks, dependencias, contratos entre stacks y Terraform State |
| [adr/](adr/README.md) | Decisiones de arquitectura: contexto, alternativas y consecuencias |
| [CONTROLS.md](CONTROLS.md) | Matriz control → implementación → prueba que lo verifica |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Uso de `scripts/playas.sh`: primer despliegue, actualización, respaldo cifrado |
| [AWS_ACADEMY.md](AWS_ACADEMY.md) | Permisos, límites de Learner Lab y costes |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Errores frecuentes y cómo resolverlos |

Las operaciones de bajo nivel (migración manual del state, rollback,
recuperación de versiones, bastion) siguen en el [README raíz](../../README.md).
