# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
 agente_rrhh_looker_sap.py — Agente de RRHH NATIVO en la interfaz de Looker
 (views hrp1000/hrp1001 de SAP, unidas en un explore de su modelo LookML)
═══════════════════════════════════════════════════════════════════════════════

Este script crea el agente DENTRO de Looker, como contenido nativo de la
instancia (igual que un dashboard o un Look), usando exclusivamente la API
de Looker — endpoints de ConversationalAnalytics (POST /agents, /chat…).

  ✔ El agente aparece en la interfaz de Looker:
        Conversational Analytics → pestaña Agents
  ✔ Solo requiere URL + API Client ID + Secret de Looker.
  ✔ NO usa la Conversational Analytics API de GCP (geminidataanalytics),
    NO requiere proyecto de GCP, roles IAM ni consola.

Qué hace, paso a paso:

  1. Solicita MANUALMENTE la URL de Looker, el API Client ID y el Secret
     (con getpass: no quedan guardados en el notebook ni en el archivo).
  2. Preflight: versión de la instancia, endpoints de agentes disponibles
     y permisos del usuario API (gemini_in_looker, save_agents).
  3. Lee los campos reales del explore y los cruza con el diccionario
     SAP→español para construir las instrucciones del agente.
  4. Genera además un refinement LookML (hrp_etiquetas_es.layer.lkml) con
     label/description en español, listo para pegar en su proyecto.
     ⚠️ La API pública de Looker no escribe archivos LookML (viven en git),
     por eso este paso final es un copy/paste único en el IDE de Looker.
  5. Crea o actualiza el agente (idempotente, buscado por nombre).
  6. Abre un chat de prueba en español dentro del propio Colab, usando el
     mismo endpoint de chat que usa la interfaz de Looker.

────────────────────────────────────────────────────────────────────────────────
 USO EN COLAB
────────────────────────────────────────────────────────────────────────────────
  # Celda 1:
  !pip install -q --upgrade looker-sdk pandas
  # Celda 2: suba este archivo (icono de carpeta → upload) y edite la
  #          CONFIGURACIÓN (modelo, explore, nombre del agente).
  # Celda 3:
  %run agente_rrhh_looker_sap.py

  Flags opcionales:
    %run agente_rrhh_looker_sap.py --preflight   # solo verificaciones
    %run agente_rrhh_looker_sap.py --solo-lkml   # solo genera el refinement
    %run agente_rrhh_looker_sap.py --no-chat     # crea/actualiza sin chatear
    %run agente_rrhh_looker_sap.py --list | --show | --delete

 PRERREQUISITOS
────────────────────────────────────────────────────────────────────────────────
  • Instancia de Looker (Google Cloud core) en versión 25.18+ (los agentes
    como contenido de Looker y sus endpoints de API llegaron en 25.18).
  • Gemini in Looker habilitado en la instancia (acción única del
    administrador con roles/looker.admin).
  • API keys de Looker (Admin → Users → su usuario → API Keys) cuyo usuario
    tenga, sobre el modelo SAP:
        - permisos de consulta:  access_data, explore
        - permiso Gemini:        gemini_in_looker
        - permiso de agentes:    save_agents  (o el rol predeterminado
          "Conversational Analytics Agent Manager")
  • El explore con hrp1000/hrp1001 ya unido en un modelo LookML.
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import os
import sys
from getpass import getpass

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║                            CONFIGURACIÓN                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝

LOOKML_MODEL    = "YOUR_MODEL"            # Modelo LookML, p. ej. "sap_hr"
LOOKER_EXPLORE  = "YOUR_EXPLORE"          # Explore que une hrp1000 + hrp1001

AGENT_NAME        = "Agente de RRHH (SAP OM)"   # Nombre visible en Looker
AGENT_DESCRIPTION = ("Asistente de Recursos Humanos en español sobre la "
                     "estructura organizativa de SAP (HRP1000/HRP1001).")
CODE_INTERPRETER  = True   # Advanced Analytics (requiere Trusted Tester habilitado);
                           # si la instancia no lo permite, cámbielo a False.

# La URL y las credenciales de Looker SE PIDEN EN TIEMPO DE EJECUCIÓN
# (puede pre-llenarse la URL aquí para omitir ese prompt):
LOOKER_BASE_URL = ""                      # p. ej. "https://suempresa.cloud.looker.com"


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║        DICCIONARIO SAP → ESPAÑOL (etiquetas para campos de las views)   ║
# ║  Clave = nombre del campo en la view (sin prefijo). Edite/añada         ║
# ║  libremente.                                                            ║
# ╚═════════════════════════════════════════════════════════════════════════╝

ETIQUETAS_SAP = {
    # campo: (label en español, descripción en español)
    "mandt": ("Mandante", "Mandante SAP (cliente del sistema)."),
    "plvar": ("Variante de plan", "Variante de plan; '01' es la activa/productiva."),
    "otype": ("Tipo de objeto", "O=unidad organizativa, S=posición, C=puesto, P=persona, K=centro de costo."),
    "objid": ("ID de objeto", "Identificador del objeto (clave junto con el tipo de objeto)."),
    "istat": ("Estado", "Estado de planificación; '1' = activo."),
    "begda": ("Inicio de validez", "Fecha de inicio de validez del registro."),
    "endda": ("Fin de validez", "Fecha de fin de validez; 9999-12-31 = vigente hoy."),
    "langu": ("Idioma", "Idioma del texto (E=inglés, S=español…)."),
    "short": ("Abreviatura", "Nombre corto / abreviatura del objeto."),
    "stext": ("Denominación", "Nombre legible del objeto (texto largo)."),
    "rsign": ("Sentido de relación", "A = de abajo hacia arriba; B = de arriba hacia abajo. Usar un solo sentido para no duplicar conteos."),
    "relat": ("Tipo de relación", "002=reporta a/es jefe de, 003=pertenece a/incorpora, 007=es descrita por (posición↔puesto), 008=titular (posición↔persona), 011=centro de costo, 012=dirige unidad."),
    "sclas": ("Tipo de objeto destino", "Tipo del objeto destino de la relación (O/S/C/P/K)."),
    "sobid": ("ID de objeto destino", "Identificador del objeto destino (texto; puede traer ceros a la izquierda)."),
    "prozt": ("Porcentaje", "Porcentaje de la asignación (p. ej. titularidad parcial)."),
}


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║       INSTRUCCIONES BASE DEL AGENTE (campo Instructions en Looker)      ║
# ╚═════════════════════════════════════════════════════════════════════════╝

INSTRUCCIONES_BASE = """\
ROL Y TONO
Usted es "Talento", el asistente analítico del equipo de Recursos Humanos.
Responda SIEMPRE en español y trate al usuario de usted, con tono profesional,
cercano y conciso. Presente listas y comparaciones como tabla. Su especialidad
es la estructura organizativa de SAP (infotipos HRP1000 = objetos y
HRP1001 = relaciones, ya unidos en este explore): unidades organizativas,
posiciones, puestos, titulares, vacantes y líneas de reporte.

GLOSARIO SAP
- Tipo de objeto 'O' = unidad organizativa (departamento/área); 'S' = posición
  (plaza concreta); 'C' = puesto/job (rol genérico); 'P' = persona (empleado);
  'K' = centro de costo.
- Tipo de relación '002' = reporta a / es jefe de (línea de mando);
  '003' = pertenece a / incorpora; '007' = es descrita por (posición↔puesto);
  '008' = titular (posición↔persona); '011' = imputa al centro de costo;
  '012' = dirige / responsable de la unidad.
- Una posición ('S') activa SIN relación 008 vigente es una VACANTE.
- Sentido de relación: A = de abajo hacia arriba; B = de arriba hacia abajo.
  Cada relación suele existir en ambos sentidos: use un solo sentido por
  análisis y menciónelo en la respuesta.
- headcount = personas con relación 008 vigente hacia una posición.
- span of control = número de subordinados directos de una jefatura.

REGLAS DE COMPORTAMIENTO
- Salvo petición histórica explícita, considere solo registros vigentes:
  estado activo ('1'), variante de plan '01' y la fecha actual dentro del
  periodo de validez (inicio/fin de validez).
- Si la pregunta es ambigua (sin periodo o unidad), asuma la foto vigente a
  hoy y aclárelo en la respuesta.
- Privacidad primero, los datos de RRHH son sensibles. No revele información
  sensible de personas identificables; ofrezca agregados por unidad
  organizativa o puesto con un mínimo de 5 personas por grupo.
- Nunca invente cifras; responda solo con resultados de las consultas.
- No dé asesoría legal ni laboral; sugiera consultar al área legal.
- No responda temas ajenos a RRHH o a este explore; redirija con cortesía.

PREGUNTAS DE EJEMPLO
- ¿Cuál es el headcount por unidad organizativa?
- ¿Cuántas posiciones vacantes hay y en qué áreas?
- ¿Quién dirige cada unidad organizativa?
- ¿Cuál es el span of control promedio de las jefaturas?
- ¿Qué posiciones se crearon en los últimos 12 meses?
"""


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║                A partir de aquí no hace falta editar                     ║
# ╚═════════════════════════════════════════════════════════════════════════╝

LKML_REFINEMENT_PATH = "hrp_etiquetas_es.layer.lkml"
OK, WARN, FAIL = "✅", "⚠️ ", "⛔"
VERSION_MINIMA_AGENTES = (25, 18)   # agentes como contenido de Looker + API


def _validar_config() -> None:
    pendientes = [n for n, v in {
        "LOOKML_MODEL": LOOKML_MODEL,
        "LOOKER_EXPLORE": LOOKER_EXPLORE,
    }.items() if not v or v.startswith("YOUR_")]
    if pendientes:
        sys.exit(f"⛔ Complete la CONFIGURACIÓN antes de ejecutar: {', '.join(pendientes)}")


def _pedir_credenciales_looker() -> tuple[str, str, str]:
    """Entrada MANUAL de URL + API key + secret. No se guardan en disco."""
    print("\n🔐 Credenciales de Looker (no se almacenan; solo viven en memoria):")
    url = LOOKER_BASE_URL.strip() or input("   URL de la instancia (https://...): ").strip()
    if not url.startswith("https://"):
        sys.exit("⛔ La URL de Looker debe empezar con https://")
    url = url.rstrip("/")
    client_id = getpass("   API Client ID: ").strip()
    client_secret = getpass("   API Client Secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("⛔ Client ID y Secret son obligatorios.")
    return url, client_id, client_secret


def _init_looker_sdk(url: str, client_id: str, client_secret: str):
    os.environ["LOOKERSDK_BASE_URL"] = url
    os.environ["LOOKERSDK_CLIENT_ID"] = client_id
    os.environ["LOOKERSDK_CLIENT_SECRET"] = client_secret
    os.environ["LOOKERSDK_VERIFY_SSL"] = "true"
    try:
        import looker_sdk
    except ImportError:
        sys.exit("\n⛔ Falta el paquete 'looker-sdk'. Ejecute en una celda:\n"
                 "      !pip install -q --upgrade looker-sdk pandas\n"
                 "   y vuelva a ejecutar este script.")
    sdk = looker_sdk.init40()
    me = sdk.me()
    print(f"✅ Conectado a Looker como: {me.display_name or me.email}")
    return sdk


# ───────────────────────────── PREFLIGHT ───────────────────────────────────

def preflight(sdk) -> None:
    """Verifica versión, disponibilidad de los endpoints de agentes y
    permisos del usuario API. Falla rápido en bloqueantes."""
    print("\n" + "═" * 70)
    print("🛫 PREFLIGHT — verificación previa al despliegue (100% lado Looker)")
    print("═" * 70)
    reporte: list = []
    bloqueante = False

    # 1) Versión de la instancia (agentes nativos: 25.18+)
    try:
        version_txt = (sdk.versions().looker_release_version or "").strip()
        partes = tuple(int(p) for p in version_txt.split(".")[:2] if p.isdigit())
        if partes and partes >= VERSION_MINIMA_AGENTES:
            reporte.append((OK, f"Versión de Looker {version_txt} "
                            f"(≥ {'.'.join(map(str, VERSION_MINIMA_AGENTES))})"))
        elif partes:
            reporte.append((FAIL, f"Looker {version_txt}: los agentes nativos y sus "
                            "endpoints de API requieren 25.18 o superior."))
            bloqueante = True
        else:
            reporte.append((WARN, f"No pude interpretar la versión: '{version_txt}'"))
    except Exception as exc:
        reporte.append((WARN, f"No pude leer la versión de Looker: {exc}"))

    # 2) Endpoints de agentes disponibles en esta instancia/SDK
    if hasattr(sdk, "create_agent") and hasattr(sdk, "search_agents"):
        try:
            sdk.search_agents(limit=1)
            reporte.append((OK, "Endpoints de ConversationalAnalytics (agentes) operativos."))
        except Exception as exc:
            msg = str(exc)
            if "404" in msg or "Not found" in msg:
                reporte.append((FAIL, "La instancia no expone los endpoints de agentes "
                                "(¿versión antigua o Gemini in Looker deshabilitado?)."))
                bloqueante = True
            elif "403" in msg or "permission" in msg.lower():
                reporte.append((FAIL, "El usuario API no puede usar los endpoints de "
                                "agentes: falta el permiso 'save_agents' o el rol "
                                "'Conversational Analytics Agent Manager'."))
                bloqueante = True
            else:
                reporte.append((WARN, f"search_agents respondió con: {msg[:120]}"))
    else:
        reporte.append((FAIL, "Su versión de looker-sdk no incluye create_agent/"
                        "search_agents. Ejecute: !pip install -q --upgrade looker-sdk "
                        "y reinicie la sesión."))
        bloqueante = True

    # 3) Permisos del usuario API (gemini_in_looker, save_agents) vía sus roles
    try:
        yo = sdk.me()
        permisos = set()
        for role_id in (yo.role_ids or []):
            try:
                rol = sdk.role(role_id=str(role_id))
                permisos.update((rol.permission_set.permissions or []))
            except Exception:
                pass
        if permisos:
            for p, etiqueta in [("gemini_in_looker", "Permiso 'gemini_in_looker'"),
                                ("save_agents", "Permiso 'save_agents'")]:
                if p in permisos:
                    reporte.append((OK, f"{etiqueta} presente."))
                else:
                    reporte.append((WARN, f"{etiqueta} NO encontrado en los roles del "
                                    "usuario API. Solicite al administrador de Looker "
                                    "asignarlo (rol Gemini / Agent Manager) sobre el "
                                    f"modelo '{LOOKML_MODEL}'."))
        else:
            reporte.append((WARN, "No pude leer los roles del usuario API; verifique "
                            "'gemini_in_looker' y 'save_agents' con el administrador."))
    except Exception as exc:
        reporte.append((WARN, f"No pude verificar permisos: {exc}"))

    for icono, msg in reporte:
        print(f"  {icono} {msg}")
    print("═" * 70)
    if bloqueante:
        sys.exit("\n⛔ Hay bloqueantes en el preflight. Resuélvalos y vuelva a "
                 "ejecutar el script.")
    print("✅ Preflight superado (los ⚠️ son avisos, no bloquean).\n")


# ───────────────────── Looker: introspección del explore ───────────────────

def _leer_campos_explore(sdk) -> list[dict]:
    """Lee los campos reales del explore (API estable lookml_model_explore)."""
    explore = sdk.lookml_model_explore(
        lookml_model_name=LOOKML_MODEL, explore_name=LOOKER_EXPLORE, fields="fields"
    )
    campos = []
    for categoria in ("dimensions", "measures"):
        for f in getattr(explore.fields, categoria, None) or []:
            if getattr(f, "hidden", False):
                continue
            campos.append({
                "name": f.name,                      # "vista.campo"
                "categoria": categoria[:-1],         # dimension | measure
                "label": getattr(f, "label", "") or "",
                "description": getattr(f, "description", "") or "",
            })
    if not campos:
        sys.exit(f"⛔ El explore {LOOKML_MODEL}::{LOOKER_EXPLORE} no devolvió campos. "
                 "Revise el nombre y los permisos del usuario API.")
    print(f"✅ Explore verificado: {LOOKML_MODEL}::{LOOKER_EXPLORE} ({len(campos)} campos visibles).")
    return campos


def _emparejar_etiquetas(campos: list[dict]) -> list[dict]:
    """Cruza los campos del explore con el diccionario SAP por sufijo de nombre."""
    etiquetados = []
    for c in campos:
        sufijo = c["name"].split(".")[-1].lower()
        if sufijo in ETIQUETAS_SAP:
            label, desc = ETIQUETAS_SAP[sufijo]
            etiquetados.append({**c, "label_es": label, "desc_es": desc})
    print(f"✅ {len(etiquetados)} campos del explore emparejados con etiquetas SAP en español.")
    return etiquetados


def _construir_instrucciones(campos: list[dict], etiquetados: list[dict]) -> str:
    """Instrucciones del agente = base + glosario de campos generado desde el
    explore real (esto es lo que el agente verá en su campo Instructions)."""
    lineas = ["", "GLOSARIO DE CAMPOS DEL EXPLORE (generado automáticamente)"]
    for e in etiquetados:
        lineas.append(f"- {e['name']}: {e['label_es']}. {e['desc_es']}")
    otros = [c for c in campos if c["name"] not in {e["name"] for e in etiquetados}]
    if otros:
        lineas.append("")
        lineas.append("OTROS CAMPOS DISPONIBLES")
        for c in otros[:60]:
            lineas.append(f"- {c['name']}: {c['label'] or c['name']}")
    return INSTRUCCIONES_BASE + "\n".join(lineas) + "\n"


# ───────── Refinement LookML para etiquetar las views en Looker ────────────

def _generar_refinement_lkml(etiquetados: list[dict]) -> str:
    """Genera un refinement (view: +nombre) con label/description en español.
    La API pública de Looker no escribe LookML (vive en git), así que esto se
    pega UNA vez en el IDE de Looker o se commitea al repo del proyecto."""
    por_view: dict[str, list[dict]] = {}
    for e in etiquetados:
        if e["categoria"] != "dimension":
            continue
        view, campo = e["name"].split(".", 1)
        por_view.setdefault(view, []).append({**e, "campo": campo})

    bloques = [
        "# ─────────────────────────────────────────────────────────────",
        "# hrp_etiquetas_es.layer.lkml — generado por agente_rrhh_looker_sap.py",
        "# Refinement: añade etiquetas en español a las views SAP sin tocar",
        "# su definición original. Pasos (una sola vez):",
        "#   1. En Looker: Develop → su proyecto → modo desarrollo.",
        "#   2. Cree un archivo con este nombre y pegue este contenido.",
        "#   3. Añada `include: \"hrp_etiquetas_es.layer.lkml\"` en el modelo.",
        "#   4. Valide el LookML, haga commit y deploy a producción.",
        "# Nota: si el explore usa alias (join ... from:), ajuste el nombre",
        "# de la view en `view: +...` al nombre REAL de la view.",
        "# ─────────────────────────────────────────────────────────────",
        "",
    ]
    for view, items in sorted(por_view.items()):
        bloques.append(f"view: +{view} {{")
        for it in sorted(items, key=lambda x: x["campo"]):
            bloques.append(f"  dimension: {it['campo']} {{")
            bloques.append(f"    label: \"{it['label_es']}\"")
            bloques.append(f"    description: \"{it['desc_es']}\"")
            bloques.append("  }")
        bloques.append("}\n")
    return "\n".join(bloques)


def _escribir_refinement(etiquetados: list[dict]) -> None:
    contenido = _generar_refinement_lkml(etiquetados)
    with open(LKML_REFINEMENT_PATH, "w", encoding="utf-8") as fh:
        fh.write(contenido)
    print(f"\n📄 Refinement LookML generado: ./{LKML_REFINEMENT_PATH}")
    print("   (En Colab: panel de archivos → descárguelo, o copie el contenido)")


# ───────────── Crear / actualizar el agente NATIVO en Looker ───────────────

def _buscar_agente(sdk, models):
    """Busca el agente por nombre exacto (para que la ejecución sea idempotente)."""
    try:
        for a in sdk.search_agents(name=AGENT_NAME):
            if (a.name or "").strip() == AGENT_NAME and not a.deleted:
                return a
    except Exception:
        pass
    return None


def crear_o_actualizar_agente(sdk, instrucciones: str):
    from looker_sdk.sdk.api40 import models

    body = models.WriteAgent(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        sources=[models.Source(model=LOOKML_MODEL, explore=LOOKER_EXPLORE)],
        context=models.Context(instructions=instrucciones),
        code_interpreter=CODE_INTERPRETER,
    )

    existente = _buscar_agente(sdk, models)
    if existente:
        agente = sdk.update_agent(agent_id=str(existente.id), body=body)
        print(f"🔄 Agente existente actualizado en Looker: '{agente.name}' (id {agente.id})")
    else:
        try:
            agente = sdk.create_agent(body=body)
        except Exception as exc:
            if "code_interpreter" in str(exc).lower() or "interpreter" in str(exc).lower():
                print("⚠️  La instancia rechazó code_interpreter; reintentando sin él…")
                body.code_interpreter = False
                agente = sdk.create_agent(body=body)
            else:
                raise
        print(f"✨ Agente creado en Looker: '{agente.name}' (id {agente.id})")

    print("\n📍 Dónde verlo: en Looker → Conversational Analytics → pestaña "
          "Agents. El creador puede compartirlo con otros usuarios desde ahí "
          "(acceso View para chatear).")
    return agente


# ─────────────────────────────── Chat ──────────────────────────────────────

def _render_chat_messages(mensajes) -> None:
    """Renderiza la secuencia de ChatMessage que devuelve el endpoint."""
    for m in mensajes:
        sm = getattr(m, "systemMessage", None) or getattr(m, "system_message", None)
        if not sm:
            continue
        texto = getattr(sm, "text", None)
        if texto is not None:
            partes = getattr(texto, "parts", None)
            print("".join(partes) if partes else str(texto), end="", flush=True)
        if getattr(sm, "data", None) is not None:
            print("\n   🔎 (Consulta de datos ejecutada vía el explore)")
        if getattr(sm, "chart", None) is not None:
            print("\n   📊 (Visualización generada — visible en la interfaz de Looker)")
        if getattr(sm, "error", None) is not None:
            print(f"\n   ⚠️ Error del agente: {sm.error}")
    print()


def chatear(sdk, agent_id: str) -> None:
    from looker_sdk.sdk.api40 import models

    conv = sdk.create_conversation(body=models.WriteConversation(
        name=f"Prueba Colab — {AGENT_NAME}",
        agent_id=str(agent_id),
    ))
    print("\n" + "═" * 70)
    print("🧑‍💼 'Talento' listo — mismo endpoint de chat que usa la interfaz de Looker.")
    print("   Ej.: '¿Cuál es el headcount por unidad organizativa?'")
    print("        '¿Cuántas posiciones vacantes hay?'   |  'salir' para terminar")
    print("═" * 70 + "\n")

    while True:
        try:
            pregunta = input("Usted ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Conversación terminada.")
            break
        if pregunta.lower() in ("salir", "exit", "quit", ""):
            print("👋 Conversación terminada. La conversación también queda "
                  "visible en Conversational Analytics dentro de Looker.")
            break
        print("Talento ▸ ", end="")
        try:
            respuesta = sdk.conversational_analytics_chat(
                body=models.ConversationalAnalyticsChatRequest(
                    conversation_id=str(conv.id),
                    user_message=pregunta,
                ))
            _render_chat_messages(respuesta)
            # Persistir los mensajes para mantener el contexto multi-turno
            # (recomendación oficial para estos endpoints):
            try:
                serializados = [getattr(m, "__dict__", m) for m in respuesta]
                sdk.create_conversation_message(
                    conversation_id=str(conv.id),
                    body=models.WriteConversationMessages(messages=serializados),
                )
            except Exception:
                pass  # si la instancia ya persiste sola, este paso es redundante
        except Exception as exc:
            print(f"\n⚠️ Falló la consulta: {exc}\n   (Causa típica: el usuario API "
                  "no tiene access_data/explore o gemini_in_looker sobre el modelo.)")


# ─────────────────────────────── main ──────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agente de RRHH nativo en Looker (SAP OM)")
    parser.add_argument("--list", action="store_true", help="Lista los agentes de la instancia")
    parser.add_argument("--show", action="store_true", help="Muestra la definición del agente")
    parser.add_argument("--delete", action="store_true", help="Elimina el agente")
    parser.add_argument("--no-chat", action="store_true", help="Crea/actualiza sin abrir chat")
    parser.add_argument("--solo-lkml", action="store_true",
                        help="Solo genera el refinement de etiquetas, sin tocar el agente")
    parser.add_argument("--preflight", action="store_true",
                        help="Solo ejecuta las verificaciones, sin desplegar nada")
    args, _ = parser.parse_known_args()  # tolera argv extra de Colab/%run

    _validar_config()

    # 1) Credenciales manuales + conexión a Looker (única autenticación necesaria)
    looker_url, client_id, client_secret = _pedir_credenciales_looker()
    sdk = _init_looker_sdk(looker_url, client_id, client_secret)
    from looker_sdk.sdk.api40 import models

    if args.list:
        for a in sdk.search_agents():
            estado = " (eliminado)" if a.deleted else ""
            print(f"• [{a.id}] {a.name}{estado} — creado por {a.created_by_name}")
        return
    if args.show:
        agente = _buscar_agente(sdk, models)
        print(agente if agente else f"No existe un agente llamado '{AGENT_NAME}'.")
        return
    if args.delete:
        agente = _buscar_agente(sdk, models)
        if agente:
            sdk.delete_agent(agent_id=str(agente.id))
            print(f"🗑️ Agente '{AGENT_NAME}' (id {agente.id}) eliminado de Looker.")
        else:
            print(f"No existe un agente llamado '{AGENT_NAME}'.")
        return

    # 2) Preflight (versión, endpoints, permisos) — todo del lado de Looker
    preflight(sdk)
    if args.preflight:
        return

    # 3) Introspección del explore real
    campos = _leer_campos_explore(sdk)
    etiquetados = _emparejar_etiquetas(campos)

    # 4) Refinement LookML con etiquetas en español
    _escribir_refinement(etiquetados)
    if args.solo_lkml:
        return

    # 5) Crear/actualizar el agente nativo (idempotente)
    instrucciones = _construir_instrucciones(campos, etiquetados)
    agente = crear_o_actualizar_agente(sdk, instrucciones)

    # 6) Chat de prueba con el mismo endpoint que usa la interfaz
    if not args.no_chat:
        chatear(sdk, agent_id=str(agente.id))


if __name__ == "__main__":
    main()
