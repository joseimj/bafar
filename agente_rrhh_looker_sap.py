# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
 agente_rrhh_looker_sap.py — Agente de RRHH desplegado sobre LOOKER
 (views hrp1000/hrp1001 de SAP, unidas en un explore de su modelo LookML)
═══════════════════════════════════════════════════════════════════════════════

Qué hace este script (pensado para correr en Google Colab):

  1. Solicita MANUALMENTE la URL de Looker, el API Client ID y el Secret
     (con getpass: no quedan guardados en el notebook ni en el archivo).
  2. Verifica vía Looker API que el modelo/explore existe y lee sus campos.
  3. "Etiqueta" los campos SAP en español por dos vías:
       a) Inyecta un glosario de campos (generado desde su explore real)
          en el contexto del agente.  ← esto es lo que el agente usa.
       b) Genera un archivo de refinement LookML (hrp_etiquetas_es.layer.lkml)
          con label/description en español, listo para pegar en su proyecto.
          ⚠️ La API pública de Looker NO permite escribir archivos LookML
          (el código vive en git), así que ese paso final es un copy/paste
          de una sola vez en el IDE de Looker — el script le deja todo preparado.
  4. Crea (o actualiza — idempotente) un Data Agent de la Conversational
     Analytics API cuyo ÚNICO origen de datos es su explore de Looker:
     toda consulta pasa por la capa semántica y permisos de Looker.
  5. Abre un chat en español dentro del propio Colab para probarlo.

  El agente queda como recurso gestionado consumible desde Looker: si su
  instancia tiene Gemini in Looker / Conversational Analytics habilitado y
  está vinculada al MISMO proyecto de GCP (BILLING_PROJECT), los agentes
  guardados aparecen en esa experiencia. Verifíquelo con su administrador de Looker.

────────────────────────────────────────────────────────────────────────────────
 USO EN COLAB
────────────────────────────────────────────────────────────────────────────────
  # Celda 1:
  !pip install -q google-cloud-geminidataanalytics looker-sdk pandas
  # Celda 2: suba este archivo (icono de carpeta → upload) y edite la CONFIGURACIÓN
  # Celda 3:
  %run agente_rrhh_looker_sap.py

  Flags opcionales:
    %run agente_rrhh_looker_sap.py --preflight   # solo verifica APIs/IAM/Looker
    %run agente_rrhh_looker_sap.py --solo-lkml   # solo genera el refinement
    %run agente_rrhh_looker_sap.py --no-chat     # crea/actualiza sin chatear
    %run agente_rrhh_looker_sap.py --list | --show | --delete

 PRERREQUISITOS
────────────────────────────────────────────────────────────────────────────────
  • Proyecto GCP con facturación; su cuenta con el rol
    roles/geminidataanalytics.dataAgentCreator (o Editor/Owner).
  • API keys de Looker (Admin → Users → su usuario → API Keys) cuyo usuario
    tenga permisos de consulta (access_data, explore) sobre el modelo SAP.
  • El explore con hrp1000/hrp1001 ya unido en un modelo LookML.
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import os
import subprocess
import sys
import uuid
from getpass import getpass

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║                            CONFIGURACIÓN                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝

BILLING_PROJECT = "YOUR_GCP_PROJECT_ID"   # Proyecto GCP que aloja/factura el agente
LOCATION        = "global"

LOOKML_MODEL    = "YOUR_MODEL"            # Modelo LookML, p. ej. "sap_hr"
LOOKER_EXPLORE  = "YOUR_EXPLORE"          # Explore que une hrp1000 + hrp1001

DATA_AGENT_ID      = "agente-rrhh-sap"    # minúsculas, números y guiones
AGENT_DISPLAY_NAME = "Agente de RRHH (SAP OM)"

# La URL y las credenciales de Looker SE PIDEN EN TIEMPO DE EJECUCIÓN
# (puede pre-llenarse la URL aquí para omitir ese prompt):
LOOKER_BASE_URL = ""                      # p. ej. "https://tuempresa.cloud.looker.com"


# ╔═════════════════════════════════════════════════════════════════════════╗
# ║        DICCIONARIO SAP → ESPAÑOL (etiquetas para campos de las views)   ║
# ║  Clave = nombre del campo en la view (sin prefijo). Edite/añada libremente.  ║
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
# ║              INSTRUCCIONES BASE DEL AGENTE DE RRHH (editable)           ║
# ╚═════════════════════════════════════════════════════════════════════════╝

SYSTEM_INSTRUCTION_BASE = """
- system_instruction: >-
    Usted es "Talento", el asistente analítico del equipo de Recursos
    Humanos. Responda SIEMPRE en español y trate al usuario de usted, con
    tono profesional, cercano y conciso. Consulte exclusivamente el explore
    de Looker conectado, que modela la estructura organizativa de SAP
    (infotipos HRP1000 = objetos y HRP1001 = relaciones, ya unidos en el
    explore). Su especialidad: unidades organizativas, posiciones, puestos,
    titulares, vacantes y líneas de reporte. Presente listas y
    comparaciones como tabla.

- glossary:
    - "tipo de objeto 'O'": unidad organizativa (departamento / área)
    - "tipo de objeto 'S'": posición (plaza concreta)
    - "tipo de objeto 'C'": puesto / job (rol genérico)
    - "tipo de objeto 'P'": persona (empleado)
    - "tipo de objeto 'K'": centro de costo
    - "relación '002'": reporta a / es jefe de (línea de mando)
    - "relación '003'": pertenece a / incorpora (posición→unidad, unidad→unidad)
    - "relación '007'": es descrita por (posición→puesto)
    - "relación '008'": titular (posición→persona); una posición activa SIN
        relación 008 vigente es una VACANTE
    - "relación '011'": imputa al centro de costo
    - "relación '012'": dirige / responsable de la unidad
    - headcount: personas con relación 008 vigente hacia una posición
    - span of control: número de subordinados directos de una jefatura

- behavior_rules:
    - Responda siempre en español, aunque le pregunten en otro idioma,
      y trate al usuario de usted.
    - Salvo petición histórica explícita, considere solo registros vigentes,
      estado activo ('1'), variante de plan '01' y fecha actual dentro del
      periodo de validez.
    - Las relaciones existen en ambos sentidos (A y B); use un solo sentido
      por análisis y menciónelo.
    - Si la pregunta es ambigua (sin periodo o unidad), asuma la foto vigente
      a hoy y aclárelo.
    - Privacidad primero, los datos de RRHH son sensibles. No revele
      información sensible de personas identificables; ofrezca agregados por
      unidad o puesto con mínimo 5 personas por grupo.
    - Nunca invente cifras; responda solo con resultados de las consultas.
    - No dé asesoría legal ni laboral; sugiera consultar al área legal.
    - No responda temas ajenos a RRHH o a este explore; redirija con cortesía.

- example_questions:
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


def _validar_config() -> None:
    pendientes = [n for n, v in {
        "BILLING_PROJECT": BILLING_PROJECT,
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


def _autenticar_gcp() -> None:
    try:
        from google.colab import auth as colab_auth  # type: ignore
        colab_auth.authenticate_user()
        print("✅ Autenticado en Google Cloud (Colab).")
    except ImportError:
        print("ℹ️  Entorno local: usando Application Default Credentials.")


def _habilitar_apis() -> None:
    cmd = ["gcloud", "services", "enable",
           "geminidataanalytics.googleapis.com", "cloudaicompanion.googleapis.com",
           f"--project={BILLING_PROJECT}", "--quiet"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        print(f"✅ APIs habilitadas en {BILLING_PROJECT}.")
    except (FileNotFoundError, subprocess.SubprocessError):
        print("⚠️  No pude habilitar las APIs con gcloud (quizá ya lo están). "
              "Si falla más adelante, solicite a su administrador ejecutar:\n   " + " ".join(cmd))


# ───────────────────────── PREFLIGHT (estilo Mirador) ──────────────────────
# Verifica ANTES de desplegar: APIs habilitadas, permisos IAM del usuario en
# el proyecto, versión de la instancia Looker Core y permiso gemini_in_looker
# del usuario API. Falla rápido en bloqueantes; avisa en lo demás.

OK, WARN, FAIL = "✅", "⚠️ ", "⛔"

VERSION_MINIMA_AGENTES = (25, 18, 9)   # Looker Core: agentes guardados de CA

PERMISOS_IAM_REQUERIDOS = {
    # permiso IAM → (etiqueta, bloqueante)
    "geminidataanalytics.dataAgents.create": ("Crear agentes (dataAgentCreator)", True),
    "geminidataanalytics.dataAgents.update": ("Actualizar agentes", True),
    "geminidataanalytics.dataAgents.get":    ("Leer agentes", True),
    "looker.instances.get":                  ("Acceso a la instancia Looker Core (looker.instanceUser)", False),
    "serviceusage.services.enable":          ("Habilitar APIs vía gcloud (opcional si ya están)", False),
}

APIS_REQUERIDAS = ["geminidataanalytics.googleapis.com", "cloudaicompanion.googleapis.com"]


def _sesion_gcp():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _check_apis(sesion, reporte: list) -> bool:
    """Verifica que las APIs estén habilitadas (Service Usage API)."""
    todo_ok = True
    for api in APIS_REQUERIDAS:
        url = (f"https://serviceusage.googleapis.com/v1/projects/"
               f"{BILLING_PROJECT}/services/{api}")
        try:
            r = sesion.get(url, timeout=30)
            if r.status_code == 200 and r.json().get("state") == "ENABLED":
                reporte.append((OK, f"API habilitada: {api}"))
            else:
                reporte.append((FAIL, f"API NO habilitada: {api} "
                                "(el script intentará habilitarla con gcloud)"))
                todo_ok = False
        except Exception as exc:
            reporte.append((WARN, f"No pude verificar la API {api}: {exc}"))
    return todo_ok


def _check_iam(sesion, reporte: list) -> bool:
    """testIamPermissions sobre el proyecto: respeta grupos y herencia."""
    url = (f"https://cloudresourcemanager.googleapis.com/v1/projects/"
           f"{BILLING_PROJECT}:testIamPermissions")
    sin_bloqueantes = True
    for permiso, (etiqueta, bloqueante) in PERMISOS_IAM_REQUERIDOS.items():
        try:
            r = sesion.post(url, json={"permissions": [permiso]}, timeout=30)
            if r.status_code != 200:
                reporte.append((WARN, f"No pude verificar '{permiso}' "
                                f"(HTTP {r.status_code})."))
                continue
            concedido = permiso in (r.json().get("permissions") or [])
            if concedido:
                reporte.append((OK, f"{etiqueta} [{permiso}]"))
            else:
                icono = FAIL if bloqueante else WARN
                reporte.append((icono, f"FALTA: {etiqueta} [{permiso}]"))
                if bloqueante:
                    sin_bloqueantes = False
        except Exception as exc:
            reporte.append((WARN, f"No pude verificar '{permiso}': {exc}"))
    return sin_bloqueantes


def _check_looker(sdk, reporte: list) -> None:
    """Checks del lado Looker: versión de la instancia y permiso gemini_in_looker."""
    # Versión mínima para agentes guardados de Conversational Analytics
    try:
        version_txt = (sdk.versions().looker_release_version or "").strip()
        partes = tuple(int(p) for p in version_txt.split(".")[:3] if p.isdigit())
        if partes and partes >= VERSION_MINIMA_AGENTES:
            reporte.append((OK, f"Versión de Looker {version_txt} "
                            f"(≥ {'.'.join(map(str, VERSION_MINIMA_AGENTES))})"))
        elif partes:
            reporte.append((WARN, f"Looker {version_txt}: los AGENTES GUARDADOS de "
                            "Conversational Analytics requieren "
                            f"{'.'.join(map(str, VERSION_MINIMA_AGENTES))}+. El agente "
                            "funcionará vía API, pero podría no verse dentro de Looker."))
        else:
            reporte.append((WARN, f"No pude interpretar la versión de Looker: '{version_txt}'"))
    except Exception as exc:
        reporte.append((WARN, f"No pude leer la versión de Looker: {exc}"))

    # Permiso gemini_in_looker del usuario API (vía sus roles)
    try:
        yo = sdk.me()
        permisos = set()
        for role_id in (yo.role_ids or []):
            try:
                rol = sdk.role(role_id=str(role_id))
                permisos.update((rol.permission_set.permissions or []))
            except Exception:
                pass
        if "gemini_in_looker" in permisos:
            reporte.append((OK, "El usuario API tiene el permiso 'gemini_in_looker'."))
        elif permisos:
            reporte.append((WARN, "El usuario API NO tiene 'gemini_in_looker'. Solicite al "
                            "administrador de Looker asignarle el rol Gemini (con alcance "
                            f"sobre el modelo '{LOOKML_MODEL}')."))
        else:
            reporte.append((WARN, "No pude leer los roles del usuario API "
                            "(necesita permiso para ver roles); verifica "
                            "'gemini_in_looker' manualmente con el administrador."))
    except Exception as exc:
        reporte.append((WARN, f"No pude verificar permisos de Looker: {exc}"))


def preflight(sdk) -> None:
    """Imprime el reporte y aborta solo ante bloqueantes IAM."""
    print("\n" + "═" * 70)
    print("🛫 PREFLIGHT — verificación previa al despliegue")
    print("═" * 70)
    reporte: list = []
    try:
        sesion = _sesion_gcp()
        apis_ok = _check_apis(sesion, reporte)
        iam_ok = _check_iam(sesion, reporte)
    except Exception as exc:
        reporte.append((WARN, f"No pude crear sesión GCP para verificar: {exc}"))
        apis_ok, iam_ok = True, True   # no bloquear por fallo del verificador
    _check_looker(sdk, reporte)

    for icono, msg in reporte:
        print(f"  {icono} {msg}")
    print("═" * 70)

    if not iam_ok:
        sys.exit("\n⛔ Faltan permisos IAM bloqueantes. Solicite a su administrador el rol "
                 "'roles/geminidataanalytics.dataAgentCreator' en el proyecto "
                 f"{BILLING_PROJECT} y vuelva a ejecutar el script.")
    if not apis_ok:
        print("\nℹ️  Hay APIs sin habilitar: el script intentará habilitarlas "
              "con gcloud en el siguiente paso.")
    print("✅ Preflight superado (los ⚠️ son avisos, no bloquean).\n")


# ───────────────────── Looker: introspección del explore ───────────────────

def _init_looker_sdk(url: str, client_id: str, client_secret: str):
    os.environ["LOOKERSDK_BASE_URL"] = url
    os.environ["LOOKERSDK_CLIENT_ID"] = client_id
    os.environ["LOOKERSDK_CLIENT_SECRET"] = client_secret
    os.environ["LOOKERSDK_VERIFY_SSL"] = "true"
    import looker_sdk
    sdk = looker_sdk.init40()
    me = sdk.me()
    print(f"✅ Conectado a Looker como: {me.display_name or me.email}")
    return sdk


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


# ───────── Vía (a): glosario de campos inyectado al contexto del agente ─────

def _construir_system_instruction(campos: list[dict], etiquetados: list[dict]) -> str:
    lineas = ["", "- field_glossary:  # generado automáticamente desde el explore real"]
    for e in etiquetados:
        lineas.append(f"    - \"{e['name']}\": {e['label_es']}. {e['desc_es']}")
    # Campos no-SAP (medidas u otros) con su etiqueta LookML existente:
    otros = [c for c in campos if c["name"] not in {e["name"] for e in etiquetados}]
    if otros:
        lineas.append("- other_available_fields:")
        for c in otros[:60]:
            etiqueta = c["label"] or c["name"]
            lineas.append(f"    - \"{c['name']}\": {etiqueta}")
    return SYSTEM_INSTRUCTION_BASE + "\n".join(lineas) + "\n"


# ───────── Vía (b): refinement LookML para etiquetar las views en Looker ────

def _generar_refinement_lkml(etiquetados: list[dict]) -> str:
    """Genera un refinement (view: +nombre) con label/description en español.
    La API pública de Looker no escribe LookML (vive en git), así que esto se
    pega UNA vez en el IDE de Looker o se commitea al repo del proyecto."""
    por_view: dict[str, list[dict]] = {}
    for e in etiquetados:
        if e["categoria"] != "dimension":   # las columnas base SAP son dimensiones
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
    print("   (En Colab: panel de archivos → descárguelo, o copie el contenido de abajo)")
    print("─" * 65)
    print(contenido)
    print("─" * 65)


# ─────────────── Conversational Analytics: agente sobre Looker ─────────────

def _crear_o_actualizar_agente(gda, agent_client, parent: str, agent_name: str,
                               looker_url: str, system_instruction: str) -> None:
    from google.api_core.exceptions import NotFound

    ref = gda.LookerExploreReference()
    ref.looker_instance_uri = looker_url
    ref.lookml_model = LOOKML_MODEL
    ref.explore = LOOKER_EXPLORE

    refs = gda.DatasourceReferences()
    refs.looker.explore_references = [ref]
    # OJO: SIN credenciales aquí — la API exige pasarlas en el chat, no en
    # el agente. Así el secreto de Looker nunca queda persistido.

    contexto = gda.Context()
    contexto.system_instruction = system_instruction
    contexto.datasource_references = refs
    contexto.options.analysis.python.enabled = True  # análisis avanzado

    agente = gda.DataAgent()
    agente.name = agent_name
    agente.display_name = AGENT_DISPLAY_NAME
    agente.description = ("Agente de RRHH en español sobre el explore de Looker "
                          f"{LOOKML_MODEL}::{LOOKER_EXPLORE} (SAP HRP1000/HRP1001).")
    agente.data_analytics_agent.published_context = contexto

    try:
        agent_client.get_data_agent(request=gda.GetDataAgentRequest(name=agent_name))
        agent_client.update_data_agent(request=gda.UpdateDataAgentRequest(
            data_agent=agente,
            update_mask="display_name,description,data_analytics_agent",
        )).result()
        print(f"🔄 Agente existente actualizado: {agent_name}")
    except NotFound:
        agent_client.create_data_agent(request=gda.CreateDataAgentRequest(
            parent=parent, data_agent_id=DATA_AGENT_ID, data_agent=agente,
        )).result()
        print(f"✨ Agente creado: {agent_name}")


# ─────────────────────────────── Chat ──────────────────────────────────────

def _mostrar_datos(data_msg) -> None:
    if "generated_looker_query" in data_msg:
        q = data_msg.generated_looker_query
        print(f"\n   🔎 Consulta Looker → modelo: {q.model} | explore: {q.explore}")
        if q.fields:
            print(f"      campos:  {list(q.fields)}")
        if q.filters:
            print(f"      filtros: {[f'{f.field} {f.value}' for f in q.filters]}")
    if "result" in data_msg:
        try:
            import pandas as pd
            campos = [f.name for f in data_msg.result.schema.fields]
            df = pd.DataFrame([dict(r) for r in data_msg.result.data])
            if not df.empty and campos:
                df = df[[c for c in campos if c in df.columns]]
            print(df.to_string(index=False, max_rows=30))
        except Exception:
            print(data_msg.result)


def _mostrar_respuesta(stream) -> None:
    for response in stream:
        m = response.system_message
        if "text" in m:
            print("".join(m.text.parts), end="", flush=True)
        elif "schema" in m:
            print("\n   📐 Explorando la capa semántica…")
        elif "data" in m:
            print()
            _mostrar_datos(m.data)
        elif "chart" in m:
            print("\n   📊 (Visualización generada — renderizable vía Vega-Lite.)")
        elif "error" in m:
            print(f"\n   ⚠️ Error del agente: {m.error}")
    print()


def _chatear(gda, chat_client, parent: str, agent_name: str,
             client_id: str, client_secret: str) -> None:
    # Credenciales de Looker: van en el contexto de la conversación (requerido)
    credentials = gda.Credentials()
    credentials.oauth.secret.client_id = client_id
    credentials.oauth.secret.client_secret = client_secret

    conversation_id = f"conv-rrhh-{uuid.uuid4().hex[:8]}"
    conv = gda.Conversation()
    conv.agents = [agent_name]
    conv.name = f"{parent}/conversations/{conversation_id}"
    conv = chat_client.create_conversation(request=gda.CreateConversationRequest(
        parent=parent, conversation_id=conversation_id, conversation=conv,
    ))
    conv_ref = gda.ConversationReference()
    conv_ref.conversation = conv.name
    conv_ref.data_agent_context.data_agent = agent_name
    conv_ref.data_agent_context.credentials = credentials

    print("\n" + "═" * 70)
    print("🧑‍💼 'Talento' listo (responde en español, vía la capa semántica de Looker).")
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
            print("👋 Conversación terminada.")
            break
        msg = gda.Message()
        msg.user_message.text = pregunta
        request = gda.ChatRequest(parent=parent, messages=[msg],
                                  conversation_reference=conv_ref)
        print("Talento ▸ ", end="")
        try:
            _mostrar_respuesta(chat_client.chat(request=request))
        except Exception as exc:
            print(f"\n⚠️ Falló la consulta: {exc}\n   (Causa típica: el usuario API de "
                  "Looker no tiene access_data/explore sobre el modelo.)")


# ─────────────────────────────── main ──────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de RRHH sobre Looker (SAP OM)")
    parser.add_argument("--list", action="store_true", help="Lista agentes del proyecto")
    parser.add_argument("--show", action="store_true", help="Muestra la definición del agente")
    parser.add_argument("--delete", action="store_true", help="Elimina el agente")
    parser.add_argument("--no-chat", action="store_true", help="Crea/actualiza sin abrir chat")
    parser.add_argument("--solo-lkml", action="store_true",
                        help="Solo genera el refinement de etiquetas, sin tocar el agente")
    parser.add_argument("--preflight", action="store_true",
                        help="Solo corre las verificaciones, sin desplegar nada")
    args, _ = parser.parse_known_args()  # tolera argv extra de Colab/%run

    _validar_config()
    _autenticar_gcp()

    from google.cloud import geminidataanalytics as gda
    agent_client = gda.DataAgentServiceClient()
    chat_client = gda.DataChatServiceClient()
    parent = f"projects/{BILLING_PROJECT}/locations/{LOCATION}"
    agent_name = f"{parent}/dataAgents/{DATA_AGENT_ID}"

    if args.list:
        for a in agent_client.list_data_agents(request=gda.ListDataAgentsRequest(parent=parent)):
            print(f"• {a.display_name:<35} {a.name}")
        return
    if args.show:
        print(agent_client.get_data_agent(request=gda.GetDataAgentRequest(name=agent_name)))
        return
    if args.delete:
        agent_client.delete_data_agent(request=gda.DeleteDataAgentRequest(name=agent_name)).result()
        print(f"🗑️ Agente {DATA_AGENT_ID} eliminado.")
        return

    # 1) Credenciales manuales + conexión a Looker
    looker_url, client_id, client_secret = _pedir_credenciales_looker()
    sdk = _init_looker_sdk(looker_url, client_id, client_secret)

    # 2) PREFLIGHT: APIs, IAM, versión de Looker Core y permiso Gemini
    preflight(sdk)
    if args.preflight:
        return

    # 3) Introspección del explore real
    campos = _leer_campos_explore(sdk)
    etiquetados = _emparejar_etiquetas(campos)

    # 4) Etiquetas: refinement LookML (vía b)
    _escribir_refinement(etiquetados)
    if args.solo_lkml:
        return

    # 5) Etiquetas: glosario inyectado al agente (vía a) + crear/actualizar
    _habilitar_apis()
    system_instruction = _construir_system_instruction(campos, etiquetados)
    _crear_o_actualizar_agente(gda, agent_client, parent, agent_name,
                               looker_url, system_instruction)

    print("\nℹ️  Para que el agente aparezca dentro de Looker (Conversational "
          "Analytics), su administrador debe tener Gemini in Looker habilitado y la "
          f"instancia vinculada al proyecto {BILLING_PROJECT}.")

    # 6) Chat de prueba en Colab
    if not args.no_chat:
        _chatear(gda, chat_client, parent, agent_name, client_id, client_secret)


if __name__ == "__main__":
    main()
