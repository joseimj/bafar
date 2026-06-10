# Agente de RRHH sobre Looker

**Un agente conversacional de Recursos Humanos, en español, desplegado sobre la capa semántica de Looker (Google Cloud core) — creado y administrado íntegramente desde Google Colab, sin usar la consola de GCP.**

El agente consulta el explore de Looker que une las views de SAP Organizational Management (`hrp1000` y `hrp1001`, cargadas en BigQuery) y responde preguntas como:

```
"¿Cuál es el headcount por unidad organizativa?"
"¿Cuántas posiciones vacantes hay y en qué áreas?"
"¿Quién dirige cada unidad organizativa?"
```

Inspirado en el patrón de despliegue de [joseimj/mirador](https://github.com/joseimj/mirador): un solo script, configuración al inicio, ejecución idempotente y verificación *preflight* antes de desplegar.

---

## Arquitectura

```
 Google Colab ──────► Conversational Analytics API ──────► Looker (Google Cloud core)
 (este script)        geminidataanalytics.googleapis.com         │
      │                                                          │ capa semántica
      │  • crea/actualiza el Data Agent (idempotente)            │ (modelo LookML,
      │  • inyecta el glosario SAP en español                    │  permisos, joins)
      │  • abre un chat de prueba                                ▼
      │                                                    BigQuery (hrp1000,
      └── genera hrp_etiquetas_es.layer.lkml                hrp1001 de SAP)
```

Decisiones de diseño:

1. **Sin capa intermedia.** El agente es un recurso gestionado de la Conversational Analytics API cuyo único origen de datos es el explore de Looker. No hay Cloud Run, ni Agent Engine, ni servidores que mantener.
2. **Toda consulta pasa por Looker.** El agente traduce lenguaje natural a consultas del explore: hereda la gobernanza, los joins y las definiciones de métricas del modelo LookML. Nunca consulta BigQuery directamente.
3. **Las credenciales nunca se persisten.** La API exige que el secret de Looker viaje en el contexto de cada conversación, no dentro del agente; el script lo solicita con `getpass` y solo vive en memoria.
4. **El conocimiento SAP va en el contexto.** Los campos de HRP1000/HRP1001 son crípticos (`RELAT='008'`, `OTYPE='S'`); el script lee el explore real vía la API de Looker y le inyecta al agente un glosario en español generado automáticamente.

## Qué hace el script

| Paso | Acción |
|---|---|
| 1 | Solicita manualmente la URL de Looker, el API Client ID y el Secret (no se almacenan) |
| 2 | **Preflight**: verifica APIs habilitadas, permisos IAM, versión de Looker Core y permiso `gemini_in_looker` |
| 3 | Lee los campos reales del explore (`lookml_model_explore`) y los cruza con el diccionario SAP→español |
| 4 | Genera `hrp_etiquetas_es.layer.lkml` (refinement con etiquetas en español para las views) |
| 5 | Crea o actualiza el Data Agent — idempotente: puede ejecutarse las veces que sea necesario |
| 6 | Abre un chat interactivo en español dentro del propio Colab para probar el agente |

## Prerrequisitos

| Necesita | Dónde se obtiene |
|---|---|
| Proyecto de GCP con facturación — **el mismo proyecto donde reside la instancia de Looker Core** | Su administrador de GCP |
| Instancia de Looker Core en versión **25.18.9 o superior** (para los agentes guardados de Conversational Analytics) | Verificable con `--preflight` |
| Gemini in Looker habilitado en la instancia | Administrador con `roles/looker.admin` (acción única en la configuración de la instancia) |
| API keys de Looker (Client ID + Secret) | Looker → Admin → Users → su usuario → API Keys |
| Nombre del modelo LookML y del explore que une `hrp1000` y `hrp1001` | Visible en la URL del explore: `/explore/<modelo>/<explore>` |

### Permisos de GCP (IAM)

Para **quien despliega** (ejecuta este script), a nivel del proyecto:

| Rol | Para qué |
|---|---|
| `roles/geminidataanalytics.dataAgentCreator` | Crear agentes (otorga automáticamente Owner sobre los agentes propios) |
| `roles/cloudaicompanion.user` | Conversaciones con estado gestionadas por Google Cloud |
| `roles/looker.instanceUser` | Acceso a los datos de la instancia de Looker Core |
| `roles/serviceusage.serviceUsageAdmin` *(opcional)* | Solo si el script debe habilitar las APIs; alternativamente, el administrador las habilita una vez |

Para **los usuarios finales** (solo conversan): `roles/geminidataanalytics.dataAgentUser` (a nivel proyecto o sobre el agente específico), más `cloudaicompanion.user` y `looker.instanceUser`.

Comando para el administrador:

```bash
PROJECT=su-proyecto-looker-core
USER=su-correo@empresa.com

gcloud services enable geminidataanalytics.googleapis.com cloudaicompanion.googleapis.com --project=$PROJECT

for ROLE in roles/geminidataanalytics.dataAgentCreator roles/cloudaicompanion.user roles/looker.instanceUser; do
  gcloud projects add-iam-policy-binding $PROJECT --member="user:$USER" --role=$ROLE
done
```

### Permisos del lado de Looker

Independientes de IAM. Tanto los usuarios finales como el usuario API cuyas keys se ingresan en el script necesitan:

- Un rol de Looker que contenga el permiso **`gemini_in_looker`** sobre el modelo consultado (incluido en el rol "Gemini" predeterminado).
- Permisos normales de consulta (`access_data`, `explore`) sobre el modelo SAP.

## Uso en Colab

```python
# Celda 1 — dependencias
!pip install -q google-cloud-geminidataanalytics looker-sdk pandas

# Celda 2 — suba agente_rrhh_looker_sap.py (panel de archivos → upload)
#           y edite el bloque CONFIGURACIÓN: BILLING_PROJECT, LOOKML_MODEL,
#           LOOKER_EXPLORE. El script se niega a correr con valores YOUR_.

# Celda 3 — ejecución completa (preflight → etiquetas → agente → chat)
%run agente_rrhh_looker_sap.py
```

### Flags

| Flag | Efecto |
|---|---|
| `--preflight` | Solo ejecuta las verificaciones (APIs, IAM, versión de Looker, permiso Gemini), sin desplegar |
| `--solo-lkml` | Solo genera el refinement de etiquetas, sin tocar el agente |
| `--no-chat` | Crea/actualiza el agente sin abrir el chat de prueba |
| `--list` / `--show` / `--delete` | Administración del agente, todo por API |

Recomendación: ejecute primero `--preflight` y comparta el reporte con su administrador si aparece algo en ⚠️ o ⛔.

## Etiquetas en español para las views SAP

La API pública de Looker **no permite escribir archivos LookML** (el código vive en git y se edita en el IDE en modo desarrollo), así que el script resuelve el etiquetado en dos vías complementarias:

- **Vía A — automática (la que usa el agente):** el glosario SAP→español se genera desde los campos reales del explore y se inyecta en las instrucciones del agente. No requiere tocar LookML.
- **Vía B — semiautomática (para que las etiquetas se vean en Looker):** el script genera `hrp_etiquetas_es.layer.lkml`, un *refinement* con `label` y `description` en español para cada dimensión SAP encontrada. Se pega una sola vez en el IDE de Looker (`Develop → su proyecto → modo desarrollo`), se añade el `include` correspondiente al modelo, se valida y se hace deploy. Si el explore usa alias en los joins (`from:`), ajuste el nombre en `view: +...` al nombre real de la view.

El diccionario de etiquetas (`ETIQUETAS_SAP`) y las instrucciones del agente (`SYSTEM_INSTRUCTION_BASE`) son editables al inicio del script.

## Qué sabe el agente de SAP OM

El agente recibe el modelo semántico completo de HRP1000/HRP1001: tipos de objeto (O/S/C/P/K), tipos de relación (002 reporta a, 003 pertenece a, 007 es descrita por, 008 titular, 011 centro de costo, 012 dirige), sentidos de relación (RSIGN A/B, usando uno solo para no duplicar conteos), reglas de vigencia (ISTAT='1', PLVAR='01', fecha actual dentro de BEGDA/ENDDA) y definiciones de negocio (vacante = posición activa sin relación 008 vigente, headcount, span of control).

Incluye además reglas de privacidad: no revela información sensible de personas identificables y solo entrega agregados con un mínimo de 5 personas por grupo. No da asesoría legal ni responde temas ajenos a RRHH.

## Dónde aparece el agente

El agente queda como recurso gestionado (`dataAgents/agente-rrhh-sap`) en el proyecto de la instancia. Con Gemini in Looker habilitado y la instancia en versión 25.18.9+, los usuarios con el rol Gemini lo encuentran en la experiencia de Conversational Analytics dentro de Looker. También es consumible vía API desde cualquier aplicación (el método `chat` del script funciona desde cualquier backend Python o vía REST).

## Solución de problemas

| Síntoma | Causa más probable |
|---|---|
| `PERMISSION_DENIED` al crear el agente | Falta `roles/geminidataanalytics.dataAgentCreator`; el preflight lo señala como ⛔ |
| El chat falla con error de acceso a datos | El usuario API de Looker no tiene `access_data`/`explore` sobre el modelo, o le falta `gemini_in_looker` |
| El agente no aparece dentro de Looker | Gemini in Looker no está habilitado, la instancia es anterior a 25.18.9, o el agente se creó en un proyecto distinto al de la instancia |
| El explore no devuelve campos | Nombre de modelo/explore incorrecto, o el usuario API no tiene visibilidad sobre ellos |

## Seguridad

- Las credenciales de Looker se solicitan con `getpass`: no quedan en el archivo, en el historial de Colab ni persistidas en el agente.
- Para producción, utilice un usuario API de Looker dedicado, con un rol de solo consulta limitado al modelo de RRHH.
- La Conversational Analytics API se encuentra en *Preview*; las consultas se facturan al proyecto configurado en `BILLING_PROJECT`.

## Estructura del repositorio

```
.
├── agente_rrhh_looker_sap.py   # Script único: preflight + etiquetas + agente + chat
├── hrp_etiquetas_es.layer.lkml # (generado) refinement de etiquetas para Looker
└── README.md
```
