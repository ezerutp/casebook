---
name: demo-investigation
description: Investigar un caso de la demo con las tools de casebook (OpenSearch local).
---

# Investigar un caso de la demo

1. Abre una sesión con `session_start` (por ejemplo `name="DEMO-1"`) y pásale al usuario el link del visor.
2. Busca primero en el conocimiento (`knowledge_search`) antes de adivinar índices o campos.
3. Usa `os_indices` con `demo-logs-*`, `os_mapping` si falta el esquema, y `os_count` u `os_search` con `size`
   chico y `source` limitado.
4. Deja lo que concluyes con `session_note`, citando los `query_id`.
