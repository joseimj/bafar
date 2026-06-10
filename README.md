# Agente de RRHH nativo en Looker 🧑‍💼🔭

**Un agente conversacional de Recursos Humanos, en español, creado como contenido nativo dentro de la interfaz de Looker (Google Cloud core) — desplegado íntegramente desde Google Colab usando solo la API de Looker.**

El agente consulta el explore que une las views de SAP Organizational Management (`hrp1000` y `hrp1001`, cargadas en BigQuery) y aparece directamente en **Conversational Analytics → pestaña Agents** dentro de Looker, listo para que el equipo de RRHH converse con él:

```
"¿Cuál es el headcount por unidad organizativa?"
"¿Cuántas posiciones vacantes hay y en qué áreas?"
"¿Quién dirige cada unidad organizativa?"
```

Inspirado en el patrón de despliegue de [joseimj/mirador](https://github.com/joseimj/mirador): un solo script, configuración al inicio, ejecución idempotente y verificación *preflight* antes de desplegar.

---

## Arquitectura

```
 Google Colab ────────────► Looker (Google Cloud core)
 (este script)              API de Looker · endpoints ConversationalAnalytics
      │                     POST /agents · /conversations · /chat
      │                              │
      │  • crea/actualiza el agente  │  El agente es CONTENIDO de Looker
      │    como contenido nativo     │  (como un dashboard o un Look):
      │  • inyecta el glosario SAP   │  visible en Conversational Analytics,
      │  • abre un chat de prueba    │  compartible, gobernado por permisos
      │                              ▼  de Looker.
      └── genera                 Explore LookML (capa semántica)
          hrp_etiquetas_es.layer.lkml      │
                                           ▼
                                  BigQuery (hrp1000, hrp1001 de SAP)
```

Decisiones de diseño:

1. **Solo API de Looker.** No se usa la Conversational Analytics API de GCP (`geminidataanalytics`), no se requiere proyecto de GCP, roles IAM ni consola. La única autenticación es URL + API Client ID + Secret de Looker, ingresados manualmente en tiempo de ejecución.
2. **El agente es contenido de Looker.** Se administra como cualquier otro contenido: el creador lo comparte desde la interfaz, los usuarios con acceso View chatean con él, y la gobernanza es la de Looker (permisos por modelo, acceso a contenido).
3. **Toda consulta pasa por la capa semántica.** El agente traduce lenguaje natural a consultas del explore; nunca toca BigQuery directamente.
4. **El conocimiento SAP va en las instrucciones.** Los campos de HRP1000/HRP1001 son crípticos (`RELAT='008'`, `OTYPE='S'`); el script lee el explore real y genera un glosario en español que inyecta en el campo Instructions del agente.

## Qué hace el script

| Paso | Acción |
|---|---|
| 1 | Solicita manualmente la URL de Looker, el API Client ID y el Secret (no se almacenan) |
| 2 | **Preflight**: versión de la instancia (≥ 25.18), endpoints de agentes operativos y permisos del usuario API (`gemini_in_looker`, `save_agents`) |
| 3 | Lee los campos reales del explore y los cruza con el diccionario SAP→español |
| 4 | Genera `hrp_etiquetas_es.layer.lkml` (refinement con etiquetas en español para las views) |
| 5 | Crea o actualiza el agente nativo — idempotente: lo busca por nombre antes de crear |
| 6 | Abre un chat de prueba en Colab usando el mismo endpoint que la interfaz de Looker |

## Prerrequisitos

| Necesita | Dónde se obtiene |
|---|---|
| Instancia de Looker (Google Cloud core) en versión **25.18 o superior** | Verificable con `--preflight` |
| **Gemini in Looker** habilitado en la instancia | Administrador con `roles/looker.admin` (acción única en la configuración de la instancia) |
| API keys de Looker (Client ID + Secret) | Looker → Admin → Users → su usuario → API Keys |
| Nombre del modelo LookML y del explore que une `hrp1000` y `hrp1001` | Visible en la URL del explore: `/explore/<modelo>/<explore>` |

### Permisos de Looker

Todo se gobierna del lado de Looker (no se requieren roles IAM de GCP):

| Quién | Permisos requeridos |
|---|---|
| Usuario API que ejecuta el script | `access_data` y `explore` sobre el modelo SAP; `gemini_in_looker`; `save_agents` (o el rol predeterminado **Conversational Analytics Agent Manager**) |
| Usuarios finales de RRHH | `access_data` sobre el modelo; `gemini_in_looker` (o el rol predeterminado **Conversational Analytics User**); acceso **View** al agente (lo otorga el creador al compartirlo) |

## Uso en Colab

```python
# Celda 1 — dependencias
!pip install -q --upgrade looker-sdk pandas

# Celda 2 — suba agente_rrhh_looker_sap.py (panel de archivos → upload)
#           y edite el bloque CONFIGURACIÓN: LOOKML_MODEL, LOOKER_EXPLORE,
#           AGENT_NAME. El script se niega a correr con valores YOUR_.

# Celda 3 — ejecución completa (preflight → etiquetas → agente → chat)
%run agente_rrhh_looker_sap.py
```

Al terminar, el agente queda visible en **Looker → Conversational Analytics → Agents**. Compártalo desde ahí con el equipo de RRHH (acceso View para chatear).

### Flags

| Flag | Efecto |
|---|---|
| `--preflight` | Solo ejecuta las verificaciones, sin desplegar |
| `--solo-lkml` | Solo genera el refinement de etiquetas, sin tocar el agente |
| `--no-chat` | Crea/actualiza el agente sin abrir el chat de prueba |
| `--list` / `--show` / `--delete` | Administración del agente, todo por API |

Recomendación: ejecute primero `--preflight` y comparta el reporte con su administrador si aparece algo en ⚠️ o ⛔.

## Etiquetas en español para las views SAP

La API pública de Looker **no permite escribir archivos LookML** (el código vive en git y se edita en el IDE en modo desarrollo), así que el etiquetado se resuelve en dos vías complementarias:

- **Vía A — automática (la que usa el agente):** el glosario SAP→español se genera desde los campos reales del explore y se inyecta en las instrucciones del agente. No requiere tocar LookML.
- **Vía B — semiautomática (para que las etiquetas se vean en todo Looker):** el script genera `hrp_etiquetas_es.layer.lkml`, un *refinement* con `label` y `description` en español para cada dimensión SAP encontrada. Se pega una sola vez en el IDE de Looker, se añade el `include` al modelo, se valida y se hace deploy. Si el explore usa alias en los joins (`from:`), ajuste el nombre en `view: +...` al nombre real de la view.

El diccionario de etiquetas (`ETIQUETAS_SAP`) y las instrucciones del agente (`INSTRUCCIONES_BASE`) son editables al inicio del script.

## Qué sabe el agente de SAP OM

El agente recibe el modelo semántico completo de HRP1000/HRP1001 en sus instrucciones: tipos de objeto (O/S/C/P/K), tipos de relación (002 reporta a, 003 pertenece a, 007 es descrita por, 008 titular, 011 centro de costo, 012 dirige), sentidos de relación (A/B, usando uno solo para no duplicar conteos), reglas de vigencia (estado activo, plan '01', fecha actual dentro del periodo de validez) y definiciones de negocio (vacante = posición activa sin titular vigente, headcount, span of control).

Incluye además reglas de privacidad: no revela información sensible de personas identificables y solo entrega agregados con un mínimo de 5 personas por grupo. Responde siempre en español y trata al usuario de usted.

## Solución de problemas

| Síntoma | Causa más probable |
|---|---|
| `404` en los endpoints de agentes | Instancia anterior a 25.18, o Gemini in Looker deshabilitado |
| `403` al crear el agente | El usuario API no tiene `save_agents` / rol Agent Manager |
| El chat falla con error de acceso a datos | Falta `access_data`/`explore` o `gemini_in_looker` sobre el modelo |
| El explore no devuelve campos | Nombre de modelo/explore incorrecto, o sin visibilidad para el usuario API |
| El SDK no tiene `create_agent` | Versión antigua de `looker-sdk`: actualice e reinicie la sesión de Colab |

## Seguridad

- Las credenciales de Looker se solicitan con `getpass`: no quedan en el archivo, en el historial de Colab ni en ningún recurso.
- Para producción, utilice un usuario API de Looker dedicado, con un rol limitado al modelo de RRHH (consulta + Gemini + agentes).
- Los endpoints de ConversationalAnalytics de la API de Looker son recientes: fije la versión de `looker-sdk` en la celda de instalación cuando tenga una combinación funcionando.

## Estructura del repositorio

```
.
├── agente_rrhh_looker_sap.py   # Script único: preflight + etiquetas + agente + chat
├── hrp_etiquetas_es.layer.lkml # (generado) refinement de etiquetas para Looker
└── README.md
```
