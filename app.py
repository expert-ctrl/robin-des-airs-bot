from flask import Flask, request, jsonify
import requests
import os
import json
import base64
import re
import hashlib
import time
import random
import threading
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL  = os.environ.get("WATI_BASE_URL", "")

# Session Wati qui maintient POST après redirect 301 (app.wati.io → auth.wati.io)
class _PostRedirectSession(requests.Session):
    def rebuild_method(self, prepared_request, response):
        """Forcer POST même après 301/302."""
        prepared_request.method = "POST"
        return prepared_request

_wati_session = _PostRedirectSession()
RDA_SITE       = os.environ.get("RDA_SITE", "https://robindesairs.eu")
MANDAT_BASE_URL = os.environ.get("MANDAT_BASE_URL", f"{RDA_SITE}/mandat.html")

# ============================================
# AIRTABLE — CONFIG (repris du v11)
# ============================================
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appv72lKbQtjt7EIP")
AT_TABLE_ID      = os.environ.get("AIRTABLE_TABLE_ID", "tblfg688AGxaywi7O")

# IDs des champs Airtable
F_NOM_PASSAGER      = "fldCtJysGhTYF2LNf"  # Nom Passager (primary)
F_REF_DOSSIER       = "flduSWqrqxeNoQkKW"  # Référence Dossier
F_DATE_DOSSIER      = "fldU8r9ME43CeOZ1T"  # Date Dossier
F_MONTANT_CLIENT    = "fldloBwQlvX9I3dyu"  # Montant Client
F_COMMISSION_RDA    = "fld576OhR8Bi0AV1s"  # Commission RDA
F_STATUT_DOSSIER    = "fldw5cnmqhMjO2cSc"  # Statut Dossier (singleSelect)
F_REMARQUES         = "fldqks5asIPXar8BD"  # Remarques
F_WHATSAPP          = "fldsFH0PoWe3AV0sI"  # Numéro WhatsApp
F_COMPAGNIE         = "fld8Ku1jGMOPWnrQc"  # Compagnie Aérienne
F_NUMERO_VOL        = "fldcVnS4B86eZntjr"  # Numéro de vol
F_DATE_VOL          = "flduDNEC3osPnTMAv"  # Date du vol (date)
F_PNR               = "fld7scWE20q3DRPUa"  # PNR
F_TYPE_INCIDENT     = "fldci5VnHb0HpOoKL"  # Type d'incident (singleSelect)
F_MONTANT_INDEMNITE = "fldlzkJOqqC8AYbIM"  # Montant de l'indemnité
F_STATUT_SUIVI      = "fldUnBUQFKeoKf8LL"  # Statut du Dossier Suivi
F_ITINERAIRE        = os.environ.get("AIRTABLE_F_ITINERAIRE", "fldtCISegQZ58Yvrl").strip()

# Champs attachments (optionnels — ID fld… via env)
F_CARTE_EMBARQUEMENT = os.environ.get("AIRTABLE_F_CARTE_EMB", "").strip()
F_PIECE_IDENTITE     = os.environ.get("AIRTABLE_F_IDENTITE", "").strip()
F_MANDAT_SIGNE       = os.environ.get("AIRTABLE_F_MANDAT_SIGNE", "").strip()

# Cases relances Airtable
F_STOP_RELANCE    = os.environ.get("AIRTABLE_F_STOP_RELANCE", "").strip()
F_SEQUENCE_ACTIVE = os.environ.get("AIRTABLE_F_SEQUENCE_ACTIVE", "").strip()

# Webhook mandat signé
MANDAT_SIGNED_WEBHOOK_SECRET = os.environ.get("MANDAT_SIGNED_WEBHOOK_SECRET", "").strip()

# Template Wati post-soumission (hors fenêtre 24 h)
WATI_POST_SUBMIT_TEMPLATE_NAME      = os.environ.get("WATI_POST_SUBMIT_TEMPLATE_NAME", "").strip()
WATI_POST_SUBMIT_TEMPLATE_BROADCAST = os.environ.get("WATI_POST_SUBMIT_TEMPLATE_BROADCAST", "rda_post_submit_reminder").strip()
WATI_TEMPLATE_CHANNEL_NUMBER        = os.environ.get("WATI_TEMPLATE_CHANNEL_NUMBER", "").strip()

# Options singleSelect EXACTES dans Airtable
INCIDENT_AT = {
    "delay":  "Retard +3h",
    "cancel": "Annulation",
    "denied": "Surbooking",
}
STATUT_DOSSIER_DEFAUT = "Ouvert"
STATUT_SUIVI_DEFAUT   = "Nouveau"

COMMISSION_PCT = 0.25
NET_PCT        = 0.75

# ===== MEMOIRE =====
conversations         = {}
recent_event_ids      = {}
recent_payload_keys   = {}
recent_outbound_sends = {}
MEMORY_HOURS              = 24
DEDUP_WINDOW_SECONDS      = 25
EVENT_ID_TTL_SECONDS      = 900
OUTBOUND_DEDUP_SECONDS    = int(os.environ.get("OUTBOUND_DEDUP_SECONDS", "45"))
OUTBOUND_CACHE_TTL_SECONDS = int(os.environ.get("OUTBOUND_CACHE_TTL_SECONDS", "900"))

# Relances — timing fenêtre 24h WhatsApp
RELANCE_THRESHOLDS_HOURS = [2, 8, 22]

# ============================================
# NOUVEAU FLUX v8 — ORDRE OPTIMISÉ
# ============================================
# MSG 1  — Accroche commerciale + stat choc + bouton "Vérifier mon indemnité"
# MSG 2  — Langue (9 options avec drapeaux)
# MSG 3  — Route (4 boutons reformulés)
# MSG 4  — Incident + durée retard
# MSG 5  — Nb passagers
# MSG 6  — Type de vol (direct/escale) — AVANT le scan
# MSG 7  — Motivation (montant calculé avec nb pax connu)
# MSG 8  — Scan document(s) — on sait combien scanner
# MSG 9  — Noms passagers (si pas lus par scan)
# MSG 10 — Vol & Date (si pas lus par scan)
# MSG 11 — Mineurs
# MSG 12 — Récap modifiable
# MSG 13 — Documents justificatifs (passeport, carte, e-billet, certificat)
# MSG 14 — RGPD + Mandat + Annonce expert humain

STEP_PROGRESS = {
    None:                    0,
    "welcome":               0,
    "language":              0,
    "route_qualify":         1,
    "incident_type":         2,
    "delay_duration":        2,
    "passengers":            3,
    "flight_type":           4,
    "document":              5,
    "doc_confirm":           5,
    "doc_correction":        5,
    "trip_select":           5,
    "passenger_collect":     5,
    "passenger_confirm":     5,
    "flight_number":         6,
    "flight_number_confirm": 6,
    "flight_date":           6,
    "flight_date_confirm":   6,
    "minor_check":           6,
    "minor_select":          6,
    "recap":                 7,
    "recap_modify":          7,
    "route_input":           7,
    "passport_collect":      7,
    "boarding_collect":      7,
    "ebillet_collect":       7,
    "certificat_collect":    7,
    "rgpd":                  7,
    "summary":               7,
    "completed":             8,
}


def progress_bar(step_done):
    """step_done = nombre d'étapes validées (0 à 8) → barre 8 pastilles."""
    step_done = max(0, min(8, int(step_done)))
    return "🟢" * step_done + "⚪" * (8 - step_done)


def bar_for_step(step):
    """Barre de progression pour une étape (clé current_step)."""
    return progress_bar(STEP_PROGRESS.get(step, 0))


def with_bar(step, message):
    """Préfixe un message avec la barre de progression de l'étape."""
    return f"{bar_for_step(step)}\n\n{message}"


# ===== STAT CHOC — ROTATION A/B =====
STAT_VARIANTS = [
    "Seulement 5% des passagers réclament leur indemnité — soyez dans les 5%.",
    "Les compagnies gardent 95% des indemnités dues... faute de réclamation.",
    "Un vol sur 4 au départ d'Afrique arrive en retard en Europe.",
    "Des centaines de dossiers gagnés. Des familles indemnisées. Votre dossier est le suivant.",
    "600€ par passager. C'est la loi. La compagnie le sait. Vous aussi maintenant.",
    "Chaque année, des millions d'euros d'indemnités ne sont jamais réclamés faute de démarche.",
]

STAT_VARIANTS_EN = [
    "Only 5% of passengers claim their compensation — be in the 5%.",
    "Airlines keep 95% of the compensation owed... because nobody claims it.",
    "One in four flights leaving Africa arrives late in Europe.",
    "Hundreds of cases won. Families compensated. Your file is next.",
    "€600 per passenger. It's the law. The airline knows it. Now you do too.",
    "Every year, millions of euros in compensation go unclaimed for lack of action.",
]


# ===== COMPAGNIES PAR PRÉFIXE IATA =====
AIRLINE_PREFIXES = {
    "AF": "Air France", "KL": "KLM", "SN": "Brussels Airlines",
    "LH": "Lufthansa", "TP": "TAP Portugal", "IB": "Iberia",
    "BA": "British Airways", "AZ": "ITA Airways", "FR": "Ryanair",
    "U2": "EasyJet", "VY": "Vueling", "W6": "Wizz Air",
    "TO": "Transavia", "BJ": "Corsair", "SS": "Corsair",
    "HC": "Air Sénégal", "SC": "Air Sénégal",
    "AT": "Royal Air Maroc", "TU": "Tunisair",
    "ET": "Ethiopian Airlines", "KQ": "Kenya Airways",
    "WB": "RwandAir", "QR": "Qatar Airways",
    "EK": "Emirates", "MS": "EgyptAir",
    "CM": "COPA Airlines", "DL": "Delta", "AA": "American Airlines",
}

# Table IATA enrichie (repris du v11) pour résolution codeshare
FLIGHT_PREFIX_TO_AIRLINE = {
    "EJU": "easyJet", "BZZ": "Buzz", "TUI": "TUI fly",
    "AF": "Air France", "KL": "KLM", "SN": "Brussels Airlines",
    "LH": "Lufthansa", "TP": "TAP Portugal", "SS": "Corsair",
    "HC": "Air Senegal", "HF": "Air Côte d'Ivoire", "AT": "Royal Air Maroc",
    "UX": "Air Europa", "IB": "Iberia", "BA": "British Airways",
    "U2": "easyJet", "FR": "Ryanair", "W6": "Wizz Air", "VY": "Vueling",
    "EI": "Aer Lingus", "LX": "Swiss", "OS": "Austrian", "EW": "Eurowings",
    "DE": "Condor", "TO": "Transavia France", "HV": "Transavia",
    "PC": "Pegasus Airlines", "TK": "Turkish Airlines", "MS": "EgyptAir",
    "ET": "Ethiopian Airlines", "SK": "SAS", "DY": "Norwegian",
    "XQ": "SunExpress", "TS": "Air Transat", "UA": "United Airlines",
    "DL": "Delta Air Lines", "AA": "American Airlines", "AC": "Air Canada",
    "QR": "Qatar Airways", "EK": "Emirates", "GF": "Gulf Air", "WY": "Oman Air",
    "SV": "Saudia", "RJ": "Royal Jordanian", "TU": "Tunisair", "AH": "Air Algérie",
    "MD": "Air Madagascar", "KP": "ASKY", "WB": "RwandAir", "KQ": "Kenya Airways",
    "SA": "South African Airways", "BT": "airBaltic", "LO": "LOT Polish Airlines",
    "OK": "Czech Airlines", "RO": "TAROM", "FB": "Bulgaria Air", "A3": "Aegean Airlines",
    "CY": "Cyprus Airways", "KM": "Air Malta", "LG": "Luxair", "DT": "TAAG Angola Airlines",
    "W3": "Arik Air", "P4": "Air Peace", "TM": "LAM Mozambique", "UR": "Uganda Airlines",
    "4Y": "Eurowings Discover", "BF": "French Bee", "J9": "Jazeera Airways",
    "SM": "Air Cairo", "NP": "Nile Air", "NE": "Nesma Airlines", "ZN": "Zambia Airways",
    "G9": "Air Arabia", "3O": "Air Arabia Maroc", "TX": "Air Caraïbes",
    "CM": "COPA Airlines", "AZ": "ITA Airways", "BJ": "Corsair",
}

def airline_from_iata(code):
    """Code compagnie seul (AF, KL, EJU…) → nom. Base locale uniquement."""
    c = re.sub(r"[^A-Z]", "", (code or "").upper())
    if not c:
        return None
    for plen in (3, 2):
        if len(c) >= plen:
            pref = c[:plen]
            if pref in FLIGHT_PREFIX_TO_AIRLINE:
                return FLIGHT_PREFIX_TO_AIRLINE[pref]
    return None

def guess_airline(flight_number):
    """Devine la compagnie à partir du préfixe IATA du numéro de vol."""
    fn = (flight_number or "").strip().upper()
    m = re.match(r"^([A-Z]{2,3})\d", fn)
    if m:
        name = airline_from_iata(m.group(1))
        if name:
            return name
    for prefix, name in AIRLINE_PREFIXES.items():
        if fn.startswith(prefix):
            return name
    return None


# ============================================
# HELPERS CONVERSATION
# ============================================

def get_or_create_conversation(phone):
    if phone not in conversations:
        conversations[phone] = {
            "messages":     [],
            "current_step": None,
            "data": {
                "route_zone":        None,  # "africa_europe" / "europe" / "depart_europe" / "other"
                "incident_type":     None,  # "delay" / "cancel" / "denied"
                "delay_ok":          None,  # True / False
                "passengers":        None,
                "passenger_names":   [],
                "current_pax_index": 0,
                "flight_number":     None,
                "airline":           None,
                "operating_airline": None,  # exploitant réel si ≠ marketing (codeshare)
                "codeshare_note":    None,
                "marketing_carrier_iata": None,
                "operating_carrier_iata": None,
                "pnr":               None,
                "flight_date":       None,
                "flight_type":       None,  # "direct" / "connection"
                "route":             None,
                "has_minors":        None,
                "minors_count":      0,
                "minor_names":       [],
                "passports_collected":  [],
                "boarding_collected":   False,
                "ebillet_collected":    False,
                "certificat_collected": False,
                "docs_intro_sent":      False,
                "relance_count":     0,
                "boarding_pass_confirmed": False,
                "scanned_docs":      [],
                "scan_pax_index":    0,
                "language":          "fr",
                "preferred_language": None,
                "stat_variant":      None,
                "temp_year":         None,
                "temp_month":        None,
                "dossier_status":    "en_cours",
                # en_cours / non_eligible / escalade_expert / docs_en_attente / mandat_envoye / complet
                "non_eligibility_reason": None,
                "escalade_reason":   None,
            },
            "created": datetime.now(),
            "last_activity": datetime.now(),
        }
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        del conversations[phone]
        return get_or_create_conversation(phone)
    return conversations[phone]


def touch_activity(conv):
    """Met à jour le timestamp d'activité (pour relance d'abandon)."""
    conv["last_activity"] = datetime.now()


def generate_ref_dossier(phone):
    today  = datetime.now().strftime("%Y%m%d")
    suffix = hashlib.md5(f"{phone}{today}".encode()).hexdigest()[:4].upper()
    return f"RDA-{today}-{suffix}"


def _mandat_wa_display(phone):
    p = str(phone or "").strip().replace(" ", "")
    if not p: return ""
    if p.startswith("+"): return p
    if p.startswith("00"): return "+" + p[2:]
    if p.isdigit(): return "+" + p
    return p


def _mandat_fn_ln_from_names(names):
    if not names: return "", ""
    first = (names[0] or "").strip()
    parts = first.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:]).upper()
    return "", ""


def detect_language(text):
    tl = text.lower()
    en = sum(1 for w in ["hello","hi","the","my","flight","delay","delayed","cancel","yes","no","thanks"] if w in tl.split())
    fr = sum(1 for w in ["bonjour","salut","le","mon","vol","retard","annul","oui","non","merci"] if w in tl.split())
    return "en" if en > fr else "fr"


# ============================================
# DEDUPLICATION
# ============================================

def _cleanup_dedup_caches(now):
    for cache in [recent_event_ids, recent_payload_keys]:
        to_del = [k for k, ts in cache.items() if (now - ts).total_seconds() > EVENT_ID_TTL_SECONDS]
        for k in to_del: cache.pop(k, None)

def _extract_event_id(data):
    if not isinstance(data, dict): return ""
    candidates = [data.get("messageId"), data.get("id"), data.get("whatsappMessageId")]
    for c in candidates:
        if c: return str(c).strip()
    return ""

def is_duplicate_event(phone, data, payload_signature):
    now = datetime.now()
    _cleanup_dedup_caches(now)
    event_id = _extract_event_id(data)
    if event_id:
        if event_id in recent_event_ids: return True
        recent_event_ids[event_id] = now
    sig = (payload_signature or "").strip()
    if sig:
        key = hashlib.sha256(f"{phone}|{sig.lower()}".encode()).hexdigest()
        if key in recent_payload_keys:
            if (now - recent_payload_keys[key]).total_seconds() < DEDUP_WINDOW_SECONDS: return True
        recent_payload_keys[key] = now
    return False

def _conversation_step_for_phone(phone):
    conv = conversations.get(phone)
    return str(conv.get("current_step") or "none") if conv else "none"

def _cleanup_outbound_cache(now):
    to_del = [k for k, ts in recent_outbound_sends.items() if (now - ts).total_seconds() > OUTBOUND_CACHE_TTL_SECONDS]
    for k in to_del: recent_outbound_sends.pop(k, None)

def _outbound_should_block(phone, step, kind, fp):
    now = datetime.now()
    _cleanup_outbound_cache(now)
    key = hashlib.sha256(f"{phone}|{step}|{kind}|{fp}".encode()).hexdigest()
    if key in recent_outbound_sends:
        if (now - recent_outbound_sends[key]).total_seconds() < OUTBOUND_DEDUP_SECONDS:
            print(f"[OUTBOUND_SKIP] {phone} {step} {kind}")
            return True
    return False

def _register_outbound_success(phone, step, kind, fp):
    now = datetime.now()
    _cleanup_outbound_cache(now)
    key = hashlib.sha256(f"{phone}|{step}|{kind}|{fp}".encode()).hexdigest()
    recent_outbound_sends[key] = now

def _fp_text(msg): return hashlib.sha256(msg.strip().encode()).hexdigest()
def _fp_buttons(body, buttons, hdr, ftr):
    return hashlib.sha256(json.dumps({"body":body,"btns":[b.get("title","") for b in (buttons or [])]}, sort_keys=True).encode()).hexdigest()
def _fp_list(body, label, sections, hdr, ftr):
    return hashlib.sha256(json.dumps({"body":body,"label":label,"s":str(sections)}, sort_keys=True).encode()).hexdigest()


# ============================================
# WATI — ENVOI
# ============================================

def send_whatsapp_text(phone, message, *, skip_outbound_dedup=False):
    message = message.strip()
    if not message: return 0
    step = _conversation_step_for_phone(phone)
    fp   = _fp_text(message)
    if not skip_outbound_dedup and _outbound_should_block(phone, step, "text", fp): return 429
    url  = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    hdrs = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    r = _wati_session.post(url, headers=hdrs, params={"messageText": message}, timeout=30)
    print(f"Wati TEXT: {r.status_code}")
    if r.status_code == 200 and not skip_outbound_dedup:
        _register_outbound_success(phone, step, "text", fp)
    return r.status_code


def send_whatsapp_buttons(phone, body_text, buttons, header_text=None, footer_text=None):
    url  = f"{WATI_BASE_URL}/api/v1/sendInteractiveButtonsMessage"
    hdrs = {"Authorization": f"Bearer {WATI_API_TOKEN}", "Content-Type": "application/json", "accept": "*/*"}
    payload = {"body": body_text, "buttons": [{"text": b["title"]} for b in buttons[:3]]}
    if header_text: payload["header"] = header_text
    if footer_text: payload["footer"] = footer_text
    step = _conversation_step_for_phone(phone)
    fp   = _fp_buttons(body_text, buttons, header_text, footer_text)
    if _outbound_should_block(phone, step, "buttons", fp): return 429
    r = _wati_session.post(url, headers=hdrs, params={"whatsappNumber": phone}, json=payload, timeout=30)
    print(f"Wati BUTTONS: {r.status_code} - {r.text[:150]}")
    if r.status_code != 200:
        fallback = body_text + "\n\n" + "\n".join(f"{i+1}. {b['title']}" for i, b in enumerate(buttons))
        fallback += "\n\nRépondez avec le numéro de votre choix."
        send_whatsapp_text(phone, fallback, skip_outbound_dedup=True)
    else:
        _register_outbound_success(phone, step, "buttons", fp)
    return r.status_code


def send_whatsapp_list(phone, body_text, button_label, sections, header_text=None, footer_text=None):
    url  = f"{WATI_BASE_URL}/api/v1/sendInteractiveListMessage"
    hdrs = {"Authorization": f"Bearer {WATI_API_TOKEN}", "Content-Type": "application/json", "accept": "*/*"}
    normalized = []
    for sec in sections:
        rows = []
        for row in sec.get("rows", []):
            rid = row.get("id") or row.get("rowId") or ""
            rows.append({"id": rid, "rowId": rid, "title": row.get("title",""), "description": row.get("description","")})
        normalized.append({"title": sec.get("title",""), "rows": rows})
    payload = {"body": body_text, "buttonText": button_label, "sections": normalized}
    if header_text: payload["header"] = header_text
    if footer_text: payload["footer"] = footer_text
    step = _conversation_step_for_phone(phone)
    fp   = _fp_list(body_text, button_label, sections, header_text, footer_text)
    if _outbound_should_block(phone, step, "list", fp): return 429
    r = _wati_session.post(url, headers=hdrs, params={"whatsappNumber": phone}, json=payload, timeout=30)
    print(f"Wati LIST: {r.status_code} - {r.text[:150]}")
    if r.status_code != 200:
        fallback = body_text + "\n\n"
        idx = 1
        for sec in sections:
            for row in sec["rows"]:
                fallback += f"{idx}. {row['title']}\n"
                idx += 1
        fallback += "\nRépondez avec le numéro de votre choix."
        send_whatsapp_text(phone, fallback, skip_outbound_dedup=True)
    else:
        _register_outbound_success(phone, step, "list", fp)
    return r.status_code


# ============================================
# OPENAI
# ============================================

def call_openai(phone, user_message, image_data=None):
    try:
        conv = get_or_create_conversation(phone)
        if image_data:
            user_content = [
                {"type": "text", "text": user_message or "Voici mon document de voyage"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ]
            conv["messages"].append({"role": "user", "content": user_content})
        else:
            conv["messages"].append({"role": "user", "content": user_message})
        if len(conv["messages"]) > 20:
            conv["messages"] = conv["messages"][-20:]
        system = (
            "Tu es l'assistant IA de ROBIN DES AIRS, spécialiste indemnisation vols Afrique-Europe.\n\n"
            "RÈGLES ABSOLUES :\n"
            "- Réponses COURTES : 2-3 phrases maximum, jamais plus\n"
            "- Toujours 2-3 emojis par message, ton friendly et chaleureux\n"
            "- Jamais de listes à puces longues, jamais de paragraphes\n"
            "- Toujours finir par rediriger vers le flux : 'Tapez *menu* pour démarrer 👇' ou 'Tapez *menu* pour reprendre 👇'\n"
            "- Si question complexe ou juridique : 'Je laisse ça à notre expert 🙏 Tapez *menu* et on s'en occupe 👇'\n\n"
            "INFOS CLÉS (à utiliser si pertinent) :\n"
            "- 600€/passager, vols Afrique-Europe ou départ Europe\n"
            "- 25% commission uniquement si succès\n"
            "- 5 ans de rétroactivité\n"
            "- Retard minimum : 3h à l'arrivée\n"
        )
        messages = [{"role": "system", "content": system}] + conv["messages"]
        model = "gpt-4o" if image_data else "gpt-4o-mini"
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 150, "temperature": 0.7},
            timeout=45
        )
        data = response.json()
        if "choices" in data:
            text = data["choices"][0]["message"]["content"].strip()
            conv["messages"].append({"role": "assistant", "content": text})
            return text
        return None
    except Exception as e:
        print(f"OpenAI error: {e}")
        return None


# ============================================
# LANGUES — LISTE + MESSAGES NATIFS
# ============================================

LANGUAGE_SECTIONS = [
    {
        "title": "🌍 Langues européennes",
        "rows": [
            {"id": "lang_fr", "title": "🇫🇷 Français"},
            {"id": "lang_en", "title": "🇬🇧 English"},
        ]
    },
    {
        # Langues actives — classées par nb de locuteurs
        "title": "🌍 Langues africaines",
        "rows": [
            {"id": "lang_yo",       "title": "🇳🇬 Yoruba"},        # ~50M locuteurs
            {"id": "lang_lingala",  "title": "🇨🇩 Lingala"},       # ~40M
            {"id": "lang_wo",       "title": "🇸🇳 Wolof"},         # ~12M
            {"id": "lang_twi",      "title": "🇬🇭 Twi"},           # ~9M
            {"id": "lang_mandinka", "title": "🇬🇲 Mandinka"},      # ~5M
        ]
    },
    {
        # Langues à venir — classées par nb de locuteurs
        "title": "⏳ Bientôt disponibles",
        "rows": [
            {"id": "lang_soon_swahili",  "title": "🇰🇪 Swahili (bientôt)"},     # ~200M
            {"id": "lang_soon_bambara",  "title": "🇲🇱 Bambara (bientôt)"},     # ~14M
            {"id": "lang_soon_dioula",   "title": "🇨🇮 Dioula (bientôt)"},      # ~12M
            {"id": "lang_soon_sonike",   "title": "🇸🇳 Soninké (bientôt)"},     # ~2M
        ]
    },
]

EXPERT_MSG = {
    # Langues actives
    "wo":       "🇸🇳 Dëkk sa Wolof, bëgg na la wax — expert bi dafa di xam Wolof, dafa di waxleen ci kanam. 🤝\n📱 +33 7 56 86 36 30",
    "mandinka": "🇬🇲 An b'i Mandinka kalan — expert be i sɔrɔ ka kuma Mandinka la. 🤝\n📱 +33 7 56 86 36 30",
    "twi":      "🇬🇭 Yɛte wo Twi kasa — obi nyansafo bɛfrɛ wo Twi so. 🤝\n📱 +33 7 56 86 36 30",
    "yo":       "🇳🇬 A gbọ Yorùbá rẹ — akọwe wa yóò pe yín ní Yorùbá. 🤝\n📱 +33 7 56 86 36 30",
    "lingala":  "🇨🇩 Toyebi lingala — moto ya biso akobeta yo telefone na lingala. 🤝\n📱 +33 7 56 86 36 30",
}

# Langues "bientôt" — message d'attente
COMING_SOON_MSG = {
    "fr": (
        "⏳ *Cette langue arrive bientôt chez Robin des Airs !*\n\n"
        "En attendant, un expert francophone prend en charge votre dossier. 🤝\n"
        "Je continue à vous guider en français 👇"
    ),
    "en": (
        "⏳ *This language is coming soon to Robin des Airs!*\n\n"
        "In the meantime, a French-speaking expert handles your file. 🤝\n"
        "Let me continue guiding you in French 👇"
    ),
}


# ============================================
# FLUX — FONCTIONS D'ENVOI
# ============================================

def send_welcome_hook(phone, conv):
    """MSG 1 — Accroche commerciale + stat choc + bouton."""
    lang = conv["data"].get("language", "fr")
    if conv["data"].get("stat_variant") is None:
        conv["data"]["stat_variant"] = random.randrange(len(STAT_VARIANTS))
    idx = conv["data"]["stat_variant"]

    if lang == "en":
        stat = STAT_VARIANTS_EN[idx % len(STAT_VARIANTS_EN)]
        body = with_bar("welcome", (
            "👋 *Welcome to Robin des Airs* 🏹\n"
            "*Specialists in delayed or cancelled African flights.*\n\n"
            f"*\"{stat}\"*\n\n"
            "✈️ EU law CE 261/2004 entitles you to *€600 per person*:\n"
            "• All flights *departing from Europe* (any airline)\n"
            "• Flights *arriving in Europe* with a *European airline*\n\n"
            "*€0 if we don't win. No risk for you.*"
        ))
        buttons = [{"id": "start_check", "title": "✈️ Check my rights"}]
    else:
        stat = STAT_VARIANTS[idx % len(STAT_VARIANTS)]
        body = with_bar("welcome", (
            "👋 *Bienvenue chez Robin des Airs* 🏹\n"
            "*Spécialiste des vols africains retardés ou annulés.*\n\n"
            f"*\"{stat}\"*\n\n"
            "✈️ La loi européenne CE 261/2004 vous donne droit à *600 € par personne* "
            "pour les vols :\n"
            "• *Au départ de l'Europe* — toutes compagnies confondues\n"
            "• *Vers l'Europe* — si la compagnie est européenne\n\n"
            "*0€ si on ne gagne pas. Aucun risque pour vous.*"
        ))
        buttons = [{"id": "start_check", "title": "✅ Vérifier droits"}]
    send_whatsapp_buttons(phone, body, buttons)


def ask_language(phone):
    """MSG 2 — Choix de la langue."""
    body = with_bar("language", (
        "🌍 *Dans quelle langue souhaitez-vous être accompagné(e) ?*\n\n"
        "Chez Robin des Airs, nous parlons votre langue — il est toujours plus facile de s'expliquer dans sa langue maternelle. 🤝\n\n"
        "*In which language would you like to be assisted?*"
    ))
    sections = LANGUAGE_SECTIONS
    send_whatsapp_list(phone, body, "Choisir 🌍", sections)


def ask_route_qualify(phone, lang="fr"):
    """MSG 3 — Qualification route (4 boutons reformulés)."""
    if lang == "en":
        body = with_bar("route_qualify", "🗺️ Where was your flight?\n\nThis determines if EU regulation CE 261/2004 applies.")
        buttons = [
            {"id": "zone_africa_europe", "title": "🌍 Africa ↔ Europe"},
            {"id": "zone_europe",        "title": "🇪🇺 Europe ↔ Europe"},
            {"id": "zone_depart_europe", "title": "🛫 Via Europe"},
            {"id": "zone_other",         "title": "🌐 Other route"},
        ]
    else:
        body = with_bar("route_qualify", "🗺️ Votre vol était sur quelle route ?\n\nCela détermine si le règlement européen CE 261/2004 s'applique.")
        buttons = [
            {"id": "zone_africa_europe", "title": "🌍 Afrique ↔ Europe"},
            {"id": "zone_europe",        "title": "🇪🇺 Europe ↔ Europe"},
            {"id": "zone_depart_europe", "title": "🛫 Via Europe"},
            {"id": "zone_other",         "title": "🌐 Autre route"},
        ]
    # WhatsApp = max 3 boutons → on bascule en liste si 4
    if len(buttons) > 3:
        rows = [{"id": b["id"], "title": b["title"][:24], "description": ""} for b in buttons]
        sections = [{"title": "Route", "rows": rows}]
        send_whatsapp_list(phone, body, "Choisir 🗺️" if lang == "fr" else "Choose 🗺️", sections)
    else:
        send_whatsapp_buttons(phone, body, buttons)


def ask_incident_type(phone, lang="fr"):
    """MSG 4 — Type d'incident."""
    if lang == "en":
        body = with_bar("incident_type", "✈️ What happened with your flight?")
        buttons = [
            {"id": "inc_delay",  "title": "⏱️ Delay at arrival"},
            {"id": "inc_cancel", "title": "❌ Cancellation"},
            {"id": "inc_denied", "title": "🚫 Denied boarding"},
        ]
    else:
        body = with_bar("incident_type", "✈️ Que s'est-il passé avec votre vol ?")
        buttons = [
            {"id": "inc_delay",  "title": "⏱️ Retard"},
            {"id": "inc_cancel", "title": "❌ Annulation"},
            {"id": "inc_denied", "title": "🚫 Refus embarquement"},
        ]
    send_whatsapp_buttons(phone, body, buttons)


def ask_delay_duration(phone, lang="fr"):
    """MSG 4 — Durée retard (uniquement si retard)."""
    if lang == "en":
        body = with_bar("delay_duration", "⏱️ How many hours late were you at *arrival* ?")
        buttons = [
            {"id": "delay_3plus",   "title": "✅ More than 3 hours"},
            {"id": "delay_lt3",     "title": "❌ Less than 3 hours"},
            {"id": "delay_unknown", "title": "🤔 I'm not sure"},
        ]
    else:
        body = with_bar("delay_duration", "⏱️ De combien d'heures était le retard à l'*arrivée* ?")
        buttons = [
            {"id": "delay_3plus",   "title": "✅ Plus de 3 heures"},
            {"id": "delay_lt3",     "title": "❌ Moins de 3 heures"},
            {"id": "delay_unknown", "title": "🤔 Je ne sais plus"},
        ]
    send_whatsapp_buttons(phone, body, buttons)


def send_estimation(phone, conv):
    """MSG 4 — Estimation générique (route inconnue à ce stade)."""
    lang = conv["data"]["language"]
    d    = conv["data"]
    incident_labels = {
        "delay":  "retard +3h"           if lang=="fr" else "delay +3h",
        "cancel": "annulation"           if lang=="fr" else "cancellation",
        "denied": "refus d'embarquement" if lang=="fr" else "denied boarding",
    }
    incident = incident_labels.get(d.get("incident_type",""), "incident")
    if lang == "en":
        msg = f"💡 Your flight with {incident} = potentially *€600 per passenger*. Let's continue!"
    else:
        msg = f"💡 Votre vol avec {incident} = potentiellement *600€ par passager*. Continuons !"
    send_whatsapp_text(phone, with_bar("passengers", msg))
    time.sleep(1)


def ask_passengers(phone, lang="fr"):
    """MSG 5 — Nombre de passagers (AVANT scan)."""
    if lang == "en":
        body  = with_bar("passengers", "👥 How many passengers are claiming on this flight?")
        label = "Select 👥"
    else:
        body  = with_bar("passengers", "👥 Combien de passagers réclament sur ce vol ?")
        label = "Choisir 👥"
    sections = [{"title": "Passagers", "rows": [
        {"id": "pax_1", "title": "1 passager" if lang=="fr" else "1 passenger", "description": "= 600 €"},
        {"id": "pax_2", "title": "2 passagers" if lang=="fr" else "2 passengers","description": "= 1 200 €"},
        {"id": "pax_3", "title": "3 passagers" if lang=="fr" else "3 passengers","description": "= 1 800 €"},
        {"id": "pax_4", "title": "4 passagers" if lang=="fr" else "4 passengers","description": "= 2 400 €"},
        {"id": "pax_5", "title": "5 passagers" if lang=="fr" else "5 passengers","description": "= 3 000 €"},
        {"id": "pax_more","title":"6 ou plus" if lang=="fr" else "6 or more",   "description": "Dossier prioritaire" if lang=="fr" else "Priority file"},
    ]}]
    send_whatsapp_list(phone, body, label, sections)


def ask_flight_type(phone, conv):
    """MSG 6 — Direct ou escale (AVANT scan)."""
    lang = conv["data"]["language"]
    if lang == "en":
        body    = with_bar("flight_type", "✈️ Was it a direct flight or with connection(s)?")
        buttons = [
            {"id": "type_direct",     "title": "✈️ Direct flight"},
            {"id": "type_connection", "title": "🔄 With connection"},
        ]
    else:
        body    = with_bar("flight_type", "✈️ C'était un vol direct ou avec escale(s) ?")
        buttons = [
            {"id": "type_direct",     "title": "✈️ Vol direct"},
            {"id": "type_connection", "title": "🔄 Avec escale"},
        ]
    send_whatsapp_buttons(phone, body, buttons)


def send_motivation(phone, conv):
    """MSG 7 — Motivation + 25% (montant calculé avec nb pax connu)."""
    lang  = conv["data"]["language"]
    pax   = conv["data"]["passengers"]
    total = 600 * pax
    net   = int(total * 0.75)
    if lang == "en":
        msg = (
            f"🎉 *{pax} passenger(s) = up to {total} € in compensation!*\n\n"
            f"💶 You receive *{net} € net* (75%).\n"
            f"Robin des Airs takes *25% success fee — only if we win.*\n"
            f"If you receive nothing → we charge nothing.\n\n"
            f"*Our interest is yours.* 🤝"
        )
    else:
        msg = (
            f"🎉 *{pax} passager(s) = jusqu'à {total} € d'indemnité !*\n\n"
            f"💶 Vous percevez *{net} € nets* (75%).\n"
            f"Robin des Airs prélève *25% de frais de succès — uniquement si nous obtenons le paiement.*\n"
            f"Si vous ne touchez rien → nous ne touchons rien.\n\n"
            f"*Notre intérêt est donc le vôtre.* 🤝"
        )
    send_whatsapp_text(phone, with_bar("flight_type", msg))
    time.sleep(1)


def ask_document(phone, conv):
    """MSG 8 — Scan document (on sait combien de passagers)."""
    lang = conv["data"]["language"]
    pax  = conv["data"].get("passengers", 1)
    if pax > 1:
        scan_hint_fr = f"\n\n👥 Vous êtes *{pax} passagers* — on vous demandera la carte de chacun, un par un."
        scan_hint_en = f"\n\n👥 You are *{pax} passengers* — we'll ask for each boarding pass, one by one."
    else:
        scan_hint_fr = scan_hint_en = ""
    if lang == "en":
        msg = with_bar("document", (
            "⚡ *Let's save you time!*\n\n"
            "Send a photo of your *boarding pass* or your *booking confirmation (e-ticket)* "
            "— our system reads the information automatically so you don't have to type everything."
            f"{scan_hint_en}\n\n"
            "📎 Send your document\n"
            "✏️ Or type *manual* to enter the info yourself"
        ))
    else:
        msg = with_bar("document", (
            "⚡ *On va vous faire gagner du temps !*\n\n"
            "Envoyez une photo de votre *carte d'embarquement* ou de votre *confirmation de réservation (e-billet)* "
            "— notre système lit les informations automatiquement pour vous éviter de tout retaper."
            f"{scan_hint_fr}\n\n"
            "📎 Envoyez votre document\n"
            "✏️ Ou tapez *manuel* pour saisir les infos vous-même"
        ))
    send_whatsapp_text(phone, msg)


def ask_scan_next_passenger(phone, conv):
    """MSG 8 — Scan multi-passagers : demande le document du passager suivant."""
    lang  = conv["data"]["language"]
    pax   = conv["data"].get("passengers", 1)
    idx   = conv["data"].get("scan_pax_index", 0)
    num   = idx + 1
    if lang == "en":
        msg = with_bar("document", (
            f"📸 *Passenger {num} of {pax}* — Send this passenger's boarding pass.\n"
            f"✏️ Type *skip* if you already sent it or if the info is identical."
        ))
    else:
        msg = with_bar("document", (
            f"📸 *Passager {num} sur {pax}* — Envoyez la carte d'embarquement de ce passager.\n"
            f"✏️ Tapez *passer* si vous l'avez déjà envoyée ou si les infos sont identiques."
        ))
    send_whatsapp_text(phone, msg)


def _sort_scanned_docs(conv):
    """Trie les docs scannés par heure de départ (ordre chronologique vol 1 → vol 2)."""
    docs = conv["data"].get("scanned_docs", [])
    if len(docs) <= 1:
        return
    def sort_key(d):
        # Essayer date + heure de départ
        dep = (d.get("departure_time") or d.get("date") or "").strip()
        try:
            # Formats possibles : "15/03/2023 06:30" ou "15/03/2023" ou "06:30"
            for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%H:%M"):
                try: return datetime.strptime(dep, fmt)
                except ValueError: pass
        except Exception:
            pass
        return datetime.min
    docs.sort(key=sort_key)
    conv["data"]["scanned_docs"] = docs
    # Mettre à jour route complète (vol 1 origine → ... → vol N destination)
    origins = [d.get("origin","") for d in docs if d.get("origin")]
    dests   = [d.get("destination","") for d in docs if d.get("destination")]
    if origins and dests:
        if len(docs) > 1:
            # Route multi-segments : CDG → CMN → DSS
            waypoints = [origins[0]] + [d.get("destination","") for d in docs]
            conv["data"]["route"] = " → ".join(w for w in waypoints if w)
        else:
            conv["data"]["route"] = f"{origins[0]} → {dests[0]}"


def _advance_after_scan(phone, conv):
    """Après scan(s), passe à la collecte des noms/vol/date manquants."""
    pax   = conv["data"].get("passengers", 1)
    names = conv["data"].get("passenger_names", []) or []
    if len(names) < pax:
        # Reprendre la collecte des noms manquants
        conv["data"]["current_pax_index"] = len(names)
        conv["current_step"] = "passenger_collect"
        ask_next_passenger(phone, conv)
    else:
        _after_names_collected(phone, conv)


def _after_names_collected(phone, conv):
    """Une fois tous les noms connus → vol & date (si manquants) puis mineurs."""
    if conv["data"].get("flight_number"):
        if conv["data"].get("flight_date"):
            conv["current_step"] = "minor_check"
            ask_minors(phone, conv)
        else:
            conv["current_step"] = "flight_date"
            ask_flight_date(phone, conv)
    else:
        conv["current_step"] = "flight_number"
        ask_flight_number(phone, conv)


def ask_next_passenger(phone, conv):
    """MSG 9 — Collecte passagers un par un (noms)."""
    lang  = conv["data"]["language"]
    pax   = conv["data"]["passengers"]
    idx   = conv["data"].get("current_pax_index", 0)

    if idx >= pax:
        _after_names_collected(phone, conv)
        return

    num = idx + 1
    if lang == "en":
        msg = f"👤 *Passenger {num} of {pax}* — First name and last name?\n_(eg: John Doe)_"
    else:
        msg = f"👤 *Passager {num} sur {pax}* — Prénom et nom ?\n_(ex : Jean Dupont)_"
    send_whatsapp_text(phone, with_bar("passenger_collect", msg))


def confirm_passenger(phone, conv, name):
    """Confirme le nom saisi pour un passager."""
    lang = conv["data"]["language"]
    idx  = conv["data"].get("current_pax_index", 0)
    num  = idx + 1
    if lang == "en":
        body = with_bar("passenger_confirm", f"✅ Passenger {num}: *{name}*\nIs this correct?")
        buttons = [
            {"id": "pax_confirm_yes", "title": "✅ Yes, correct"},
            {"id": "pax_confirm_no",  "title": "✏️ Correct it"},
        ]
    else:
        body = with_bar("passenger_confirm", f"✅ Passager {num} : *{name}*\nC'est correct ?")
        buttons = [
            {"id": "pax_confirm_yes", "title": "✅ Oui, correct"},
            {"id": "pax_confirm_no",  "title": "✏️ Corriger"},
        ]
    send_whatsapp_buttons(phone, body, buttons)


def ask_flight_number(phone, conv):
    """MSG 10 — Numéro de vol."""
    lang = conv["data"]["language"]
    if lang == "en":
        msg = with_bar("flight_number", (
            "📝 What is your *flight number* as shown on your ticket?\n\n"
            "_(eg: AF718, KL563, SN271)_\n\n"
            "ℹ️ This is the commercial number — use it even for codeshare flights.\n\n"
            "📸 You can also send a photo of your boarding pass."
        ))
    else:
        msg = with_bar("flight_number", (
            "📝 Quel est le *numéro de vol* tel qu'il apparaît sur votre billet ?\n\n"
            "_(ex : AF718, KL563, SN271)_\n\n"
            "ℹ️ C'est le numéro commercial — utilisez-le même en cas de code share.\n\n"
            "📸 Vous pouvez aussi envoyer une photo de votre carte d'embarquement."
        ))
    send_whatsapp_text(phone, msg)


def confirm_flight_number(phone, conv, flight_number, airline):
    """Confirme le numéro de vol et la compagnie déduite."""
    lang = conv["data"]["language"]
    if airline:
        if lang == "en":
            body = f"✅ Flight *{flight_number}* — *{airline}*\nIs this correct?"
        else:
            body = f"✅ Vol *{flight_number}* — *{airline}*\nC'est correct ?"
    else:
        if lang == "en":
            body = f"✅ Flight *{flight_number}*\nIs this correct?"
        else:
            body = f"✅ Vol *{flight_number}*\nC'est correct ?"
    buttons = [
        {"id": "fn_confirm_yes", "title": "✅ Oui" if lang=="fr" else "✅ Yes"},
        {"id": "fn_confirm_no",  "title": "✏️ Corriger" if lang=="fr" else "✏️ Correct it"},
    ]
    send_whatsapp_buttons(phone, with_bar("flight_number_confirm", body), buttons)


def ask_flight_date(phone, conv):
    """MSG 10 — Date du vol (saisie libre)."""
    lang = conv["data"]["language"]
    if lang == "en":
        msg = "📅 What was the *date of the flight*?\n\n_(eg: 15/03/2023 or March 15 2023)_"
    else:
        msg = "📅 Quelle était la *date du vol* ?\n\n_(ex : 15/03/2023 ou 15 mars 2023)_"
    send_whatsapp_text(phone, with_bar("flight_date", msg))


def confirm_flight_date(phone, conv, date_str):
    """Confirme la date saisie."""
    lang = conv["data"]["language"]
    if lang == "en":
        body = f"✅ Date: *{date_str}*\nIs this correct?"
    else:
        body = f"✅ Date : *{date_str}*\nC'est correct ?"
    buttons = [
        {"id": "date_confirm_yes", "title": "✅ Oui" if lang=="fr" else "✅ Yes"},
        {"id": "date_confirm_no",  "title": "✏️ Corriger" if lang=="fr" else "✏️ Correct it"},
    ]
    send_whatsapp_buttons(phone, with_bar("flight_date_confirm", body), buttons)


def ask_minors(phone, conv):
    """MSG 11 — Mineurs."""
    lang = conv["data"]["language"]
    pax  = conv["data"]["passengers"]
    if pax == 1:
        if lang == "en":
            body    = with_bar("minor_check", "👤 Are you over 18 years old?")
            buttons = [
                {"id": "minor_no",   "title": "✅ Yes, adult"},
                {"id": "minor_self", "title": "👶 No, I'm a minor"},
            ]
        else:
            body    = with_bar("minor_check", "👤 Êtes-vous majeur(e) (18+ ans) ?")
            buttons = [
                {"id": "minor_no",   "title": "✅ Oui, majeur(e)"},
                {"id": "minor_self", "title": "👶 Je suis mineur(e)"},
            ]
    else:
        if lang == "en":
            body    = with_bar("minor_check", f"👶 Among the {pax} passengers, are there any minors (under 18)?")
            buttons = [
                {"id": "minor_no",  "title": "✅ All adults"},
                {"id": "minor_yes", "title": "👶 Yes, some minors"},
            ]
        else:
            body    = with_bar("minor_check", f"👶 Parmi les {pax} passagers, y a-t-il des mineurs (moins de 18 ans) ?")
            buttons = [
                {"id": "minor_no",  "title": "✅ Tous majeurs"},
                {"id": "minor_yes", "title": "👶 Oui, des mineurs"},
            ]
    send_whatsapp_buttons(phone, body, buttons)


def ask_minor_select(phone, conv):
    """Demande lesquels des passagers sont mineurs — liste de noms."""
    lang  = conv["data"]["language"]
    names = conv["data"].get("passenger_names", [])
    if lang == "en":
        body  = with_bar("minor_check", "👶 Which passengers are minors (under 18)?\n\nSelect each minor one by one and type *done* when finished.")
        label = "Select minor"
    else:
        body  = with_bar("minor_check", "👶 Lesquels des passagers sont mineurs (moins de 18 ans) ?\n\nSélectionnez-les un par un et tapez *ok* quand c'est fini.")
        label = "Choisir mineur"
    rows = [{"id": f"minor_pax_{i}", "title": names[i], "description": "👶 Mineur" if lang=="fr" else "👶 Minor"} for i in range(len(names))]
    sections = [{"title": "Passagers", "rows": rows}]
    send_whatsapp_list(phone, body, label, sections)


def _estimate_docs_time(conv):
    """Estime le temps total de collecte des documents en minutes."""
    pax = conv["data"].get("passengers", 1)
    has_boarding = conv["data"].get("boarding_pass_confirmed", False)
    minutes = pax * 0.5 + 0.5
    if not has_boarding:
        minutes += 0.5
    return max(1, round(minutes))


def ask_passport(phone, conv):
    """MSG 13 — Séquence docs : intro puis passeports un par un."""
    lang = conv["data"]["language"]

    if not conv["data"].get("docs_intro_sent"):
        conv["data"]["docs_intro_sent"] = True
        mins = _estimate_docs_time(conv)
        if lang == "en":
            intro = with_bar("passport_collect", (
                f"📁 *Documents — last step before your file is complete!*\n\n"
                f"We need a few photos to build your official claim.\n"
                f"*Estimated time : {mins} minute(s).*\n\n"
                f"Everything is stored securely. 🔒"
            ))
        else:
            intro = with_bar("passport_collect", (
                f"📁 *Documents — dernière étape avant que votre dossier soit complet !*\n\n"
                f"Nous avons besoin de quelques photos pour constituer votre dossier officiel.\n"
                f"*Temps estimé : {mins} minute(s).*\n\n"
                f"Tout est conservé en sécurité. 🔒"
            ))
        send_whatsapp_text(phone, intro)
        time.sleep(1)

    _ask_passport_next(phone, conv)


def _ask_passport_next(phone, conv):
    """Demande le prochain passeport."""
    lang      = conv["data"]["language"]
    names     = conv["data"].get("passenger_names", [])
    passports = conv["data"].get("passports_collected", [])
    idx       = len(passports)
    total     = conv["data"].get("passengers", 1)

    if idx >= total:
        conv["current_step"] = "boarding_collect"
        ask_boarding_collect(phone, conv)
        return

    name = names[idx] if idx < len(names) else f"Passager {idx+1}"
    if lang == "en":
        msg = with_bar("passport_collect", (
            f"🛂 *Passport {idx+1}/{total} — {name}*\n\n"
            f"Send a photo of the *photo page* of {name}'s passport.\n\n"
            f"✏️ Type *skip* to send later by email."
        ))
    else:
        msg = with_bar("passport_collect", (
            f"🛂 *Passeport {idx+1}/{total} — {name}*\n\n"
            f"Envoyez une photo de la *page photo* du passeport de {name}.\n\n"
            f"✏️ Tapez *passer* pour l'envoyer plus tard par email."
        ))
    send_whatsapp_text(phone, msg)


def ask_boarding_collect(phone, conv):
    """Demande carte d'embarquement comme justificatif (si pas déjà scannée)."""
    lang = conv["data"]["language"]
    if conv["data"].get("boarding_pass_confirmed"):
        conv["data"]["boarding_collected"] = True
        conv["current_step"] = "ebillet_collect"
        ask_ebillet(phone, conv)
        return
    if lang == "en":
        msg = with_bar("boarding_collect", (
            "🎫 *Boarding pass — proof of travel*\n\n"
            "Send a photo of your *boarding pass* for the delayed/cancelled flight.\n\n"
            "📧 No boarding pass? Your *booking confirmation email* works too.\n"
            "✏️ Type *skip* to send later by email.\n"
            "📞 Type *appel* if you've lost everything — an expert helps you."
        ))
    else:
        msg = with_bar("boarding_collect", (
            "🎫 *Carte d'embarquement — justificatif de voyage*\n\n"
            "Envoyez une photo de votre *carte d'embarquement* pour le vol retardé/annulé.\n\n"
            "📧 Pas de carte ? Votre *email de confirmation de réservation* fonctionne aussi.\n"
            "✏️ Tapez *passer* pour l'envoyer plus tard par email.\n"
            "📞 Tapez *appel* si vous avez tout perdu — un expert vous aide."
        ))
    send_whatsapp_text(phone, msg)


def ask_ebillet(phone, conv):
    """Demande confirmation de réservation (e-billet)."""
    lang = conv["data"]["language"]
    if lang == "en":
        msg = with_bar("ebillet_collect", (
            "📧 *Booking confirmation (e-ticket) — proof of travel*\n\n"
            "Send a screenshot of your *booking confirmation email*.\n"
            "_(Check your inbox, spam folder, or travel app like Booking/Expedia)_\n\n"
            "✏️ Type *skip* to send later.\n"
            "📞 Type *call* if you can't find it — an expert helps you retrieve it."
        ))
    else:
        msg = with_bar("ebillet_collect", (
            "📧 *Confirmation de réservation (e-billet) — justificatif de voyage*\n\n"
            "Envoyez une capture d'écran de votre *email de confirmation de réservation*.\n"
            "_(Vérifiez votre boîte mail, spams, ou votre appli voyage Booking/Expedia)_\n\n"
            "✏️ Tapez *passer* pour l'envoyer plus tard.\n"
            "📞 Tapez *appel* si vous ne trouvez pas — un expert vous aide à le récupérer."
        ))
    send_whatsapp_text(phone, msg)


def ask_certificat_retard(phone, conv):
    """Demande optionnelle du certificat de retard."""
    lang = conv["data"]["language"]
    if lang == "en":
        msg = with_bar("certificat_collect", (
            "📄 *Delay/cancellation certificate* _(optional)_\n\n"
            "If the airline gave you a certificate, send it — it speeds up your case.\n\n"
            "✏️ Type *skip* if you don't have one _(most common)_."
        ))
    else:
        msg = with_bar("certificat_collect", (
            "📄 *Certificat de retard/annulation* _(optionnel)_\n\n"
            "Si la compagnie vous a remis un certificat, envoyez-le — il accélère votre dossier.\n\n"
            "✏️ Tapez *passer* si vous n'en avez pas _(cas le plus fréquent)_."
        ))
    send_whatsapp_text(phone, msg)


def show_recap(phone, conv):
    """MSG 12 — Récapitulatif complet modifiable."""
    lang = conv["data"]["language"]
    d    = conv["data"]
    pax  = d["passengers"]
    net  = int(600 * pax * 0.75)

    incident_labels = {
        "delay":  "Retard +3h"          if lang=="fr" else "Delay +3h",
        "cancel": "Annulation"           if lang=="fr" else "Cancellation",
        "denied": "Refus d'embarquement" if lang=="fr" else "Denied boarding",
    }
    incident = incident_labels.get(d.get("incident_type",""), d.get("incident_type","?"))
    names_str = ", ".join(d.get("passenger_names") or ["—"])
    route_str = f"\n🗺️ {d.get('route')}" if d.get("route") else ""
    op_str    = ""
    if d.get("operating_airline") and d["operating_airline"].strip().lower() != (d.get("airline") or "").strip().lower():
        op_label = "Operated by" if lang == "en" else "Opéré par"
        op_str = f"\n🔧 {op_label} : {d['operating_airline']} (codeshare)"
    ft_str    = ("Direct" if d.get("flight_type")=="direct" else "Avec escale") if lang=="fr" else ("Direct" if d.get("flight_type")=="direct" else "With connection")

    if lang == "en":
        recap = with_bar("recap", (
            f"📋 *Summary — please confirm*\n\n"
            f"👥 {pax} passenger(s): {names_str}\n"
            f"✈️ {d.get('flight_number','?')} — {d.get('airline','?')}{op_str}{route_str}\n"
            f"📅 {d.get('flight_date','?')} — {incident}\n"
            f"🛤️ {ft_str}\n"
            f"💵 *Target: {net} € net (75%)*"
        ))
        buttons = [
            {"id": "recap_ok",     "title": "✅ All correct"},
            {"id": "recap_modify", "title": "✏️ Modify"},
        ]
    else:
        recap = with_bar("recap", (
            f"📋 *Récapitulatif — confirmez svp*\n\n"
            f"👥 {pax} passager(s) : {names_str}\n"
            f"✈️ {d.get('flight_number','?')} — {d.get('airline','?')}{op_str}{route_str}\n"
            f"📅 {d.get('flight_date','?')} — {incident}\n"
            f"🛤️ {ft_str}\n"
            f"💵 *Objectif : {net} € nets (75%)*"
        ))
        buttons = [
            {"id": "recap_ok",     "title": "✅ Tout est correct"},
            {"id": "recap_modify", "title": "✏️ Modifier"},
        ]
    send_whatsapp_buttons(phone, recap, buttons)


def ask_what_to_modify(phone, lang="fr"):
    """Menu de modification."""
    if lang == "en":
        body  = with_bar("recap_modify", "✏️ What would you like to modify?")
        label = "Choose"
    else:
        body  = with_bar("recap_modify", "✏️ Que souhaitez-vous modifier ?")
        label = "Choisir"
    sections = [{"title": "Modifier", "rows": [
        {"id": "mod_names",    "title": "👤 Noms passagers"   if lang=="fr" else "👤 Passenger names"},
        {"id": "mod_flight",   "title": "✈️ Numéro de vol"    if lang=="fr" else "✈️ Flight number"},
        {"id": "mod_date",     "title": "📅 Date du vol"      if lang=="fr" else "📅 Flight date"},
        {"id": "mod_incident", "title": "⚡ Type d'incident"  if lang=="fr" else "⚡ Incident type"},
        {"id": "mod_route",    "title": "🗺️ Trajet"           if lang=="fr" else "🗺️ Route"},
    ]}]
    send_whatsapp_list(phone, body, label, sections)


def send_rgpd(phone, lang="fr"):
    """MSG 14 — RGPD / CGV (juste avant le mandat)."""
    if lang == "en":
        rgpd = with_bar("rgpd", (
            "🔒 *Your privacy first — before signing.*\n\n"
            "Your documents are used *solely* to claim your compensation from the airline. "
            "They are never sold or shared with commercial third parties. "
            "You can request their deletion at any time.\n\n"
            "By continuing you accept our:\n"
            "• *Privacy policy* : robindesairs.eu/politique-confidentialite.html\n"
            "• *Terms & Conditions* : robindesairs.eu/cgv.html"
        ))
    else:
        rgpd = with_bar("rgpd", (
            "🔒 *Avant la signature, votre vie privée d'abord.*\n\n"
            "Vos documents servent *uniquement* à réclamer votre indemnité auprès de la compagnie. "
            "Ils ne sont jamais vendus ni partagés à des tiers commerciaux. "
            "Vous pouvez demander leur suppression à tout moment.\n\n"
            "En continuant, vous acceptez notre :\n"
            "• *Politique de confidentialité* : robindesairs.eu/politique-confidentialite.html\n"
            "• *Conditions Générales de Vente* : robindesairs.eu/cgv.html"
        ))
    send_whatsapp_text(phone, rgpd)
    time.sleep(1)


def send_ia_fallback(phone, lang="fr"):
    """Fallback IA — client écrit hors flux."""
    if lang == "en":
        body = (
            "🤖 I'm the AI assistant of *Robin des Airs*.\n\n"
            "I can answer your questions about your rights ✈️\n\n"
            "To open your file, tap *menu* 👇\n"
            "Or would you prefer an *expert to call you back*?"
        )
        buttons = [
            {"id": "start_check",  "title": "📋 Start my file"},  # 15 chars OK
            {"id": "rappel_expert","title": "📞 Be called back"},
        ]
    else:
        body = (
            "🤖 Je suis l'assistant IA de *Robin des Airs*.\n\n"
            "Je peux répondre à vos questions sur vos droits ✈️\n\n"
            "Pour ouvrir votre dossier, tapez *menu* 👇\n"
            "Ou préférez-vous qu'un *expert vous rappelle* ?"
        )
        buttons = [
            {"id": "start_check",  "title": "📋 Mon dossier"},
            {"id": "rappel_expert","title": "📞 Être rappelé"},
        ]
    send_whatsapp_buttons(phone, body, buttons)


def _resume_step(phone, conv):
    """Renvoie la question correspondant à l'étape en cours (reprise après abandon)."""
    step = conv.get("current_step")
    lang = conv["data"].get("language", "fr")

    if step == "welcome":
        send_welcome_hook(phone, conv)
    elif step == "language":
        ask_language(phone)
    elif step == "route_qualify":
        ask_route_qualify(phone, lang)
    elif step == "incident_type":
        ask_incident_type(phone, lang)
    elif step == "delay_duration":
        ask_delay_duration(phone, lang)
    elif step == "passengers":
        ask_passengers(phone, lang)
    elif step == "flight_type":
        ask_flight_type(phone, conv)
    elif step in ("document", "doc_confirm", "doc_correction", "trip_select"):
        ask_document(phone, conv)
    elif step in ("passenger_collect", "passenger_confirm"):
        ask_next_passenger(phone, conv)
    elif step in ("flight_number", "flight_number_confirm"):
        ask_flight_number(phone, conv)
    elif step in ("flight_date", "flight_date_confirm"):
        ask_flight_date(phone, conv)
    elif step == "minor_check":
        ask_minors(phone, conv)
    elif step == "minor_select":
        ask_minor_select(phone, conv)
    elif step in ("passport_collect",):
        ask_passport(phone, conv)
    elif step == "boarding_collect":
        ask_boarding_collect(phone, conv)
    elif step == "ebillet_collect":
        ask_ebillet(phone, conv)
    elif step == "certificat_collect":
        ask_certificat_retard(phone, conv)
    elif step in ("recap", "recap_modify", "route_input"):
        show_recap(phone, conv)
    elif step in ("rgpd", "summary"):
        show_summary_and_mandat(phone, conv)
    else:
        conv["current_step"] = "welcome"
        send_welcome_hook(phone, conv)


def show_summary_and_mandat(phone, conv):
    """MSG 14 — RGPD puis Mandat pré-rempli + annonce expert humain (TOUT À LA FIN)."""
    lang = conv["data"]["language"]
    d    = conv["data"]
    pax  = d["passengers"]
    net  = int(600 * pax * 0.75)

    send_rgpd(phone, lang)

    ref  = conv.get("ref_dossier") or generate_ref_dossier(phone)
    conv["ref_dossier"] = ref

    fn0, ln0   = _mandat_fn_ln_from_names(d.get("passenger_names") or [])
    name       = f"{fn0} {ln0}".strip() or (d.get("passenger_names") or ["—"])[0]
    wa_disp    = _mandat_wa_display(phone)
    names_joined = ",".join(d.get("passenger_names") or [])
    motif_map  = {"delay": "Retard de vol", "cancel": "Annulation de vol", "denied": "Refus d'embarquement"}

    params = {
        "ref":       ref,
        "phone":     wa_disp,
        "name":      name,
        "vol":       (d.get("flight_number") or "").strip(),
        "date":      (d.get("flight_date")   or "").strip(),
        "compagnie": (d.get("airline")        or "").strip(),
        "motif":     motif_map.get(d.get("incident_type",""), "Retard de vol"),
        "nbpax":     str(pax),
        "source":    "whatsapp",
    }
    if names_joined and pax > 1:
        params["paxlist"] = names_joined
    mandat_url = f"{MANDAT_BASE_URL.split('?')[0]}?{urlencode({k:v for k,v in params.items() if v})}"
    conv["mandat_url"] = mandat_url

    incident_labels = {
        "delay":  "Retard +3h"          if lang=="fr" else "Delay +3h",
        "cancel": "Annulation"           if lang=="fr" else "Cancellation",
        "denied": "Refus d'embarquement" if lang=="fr" else "Denied boarding",
    }
    incident  = incident_labels.get(d.get("incident_type",""), "?")
    route_str = f"\n🗺️ {d.get('route')}" if d.get("route") else ""
    op_str    = ""
    if d.get("operating_airline") and d["operating_airline"].strip().lower() != (d.get("airline") or "").strip().lower():
        op_label = "Operated by" if lang == "en" else "Opéré par"
        op_str = f"\n🔧 {op_label} : {d['operating_airline']} (codeshare)"

    if lang == "en":
        msg_a = with_bar("summary", (
            f"🎉 *File registered!* Ref. *{ref}*\n\n"
            f"👤 {name}\n"
            f"✈️ {d.get('flight_number','?')} — {d.get('airline','?')}{op_str}{route_str}\n"
            f"📅 {d.get('flight_date','?')} — {incident}\n"
            f"💵 *Target: {net} € net*\n\n"
            f"Last step: sign your mandate in *2 minutes*."
        ))
        msg_b = with_bar("completed", (
            f"✅ *File {ref}*\n\n"
            f"Sign your *representation mandate* (readable before signing).\n"
            f"*No bank details* asked at this step.\n\n"
            f"👉 {mandat_url}\n\n"
            f"Without signature we cannot act on your behalf.\n\n"
            f"_Robin des Airs team_ 🏹"
        ))
    else:
        msg_a = with_bar("summary", (
            f"🎉 *Dossier enregistré !* Réf. *{ref}*\n\n"
            f"👤 {name}\n"
            f"✈️ {d.get('flight_number','?')} — {d.get('airline','?')}{op_str}{route_str}\n"
            f"📅 {d.get('flight_date','?')} — {incident}\n"
            f"💵 *Objectif : {net} € nets*\n\n"
            f"Dernière étape : signez le mandat en *2 minutes*."
        ))
        msg_b = with_bar("completed", (
            f"✅ *Dossier {ref}*\n\n"
            f"Signez votre *mandat de représentation* (lisible avant signature).\n"
            f"*Aucune information bancaire* demandée à cette étape.\n\n"
            f"👉 {mandat_url}\n\n"
            f"Sans signature nous ne pouvons pas agir en votre nom.\n\n"
            f"_L'équipe Robin des Airs_ 🏹"
        ))

    # Airtable — récap validé (avant documents déjà fait), ici avant mandat
    upsert_airtable(phone, conv)

    send_whatsapp_text(phone, msg_a)
    time.sleep(3)
    send_whatsapp_text(phone, msg_b)
    conv["data"]["dossier_status"] = "mandat_envoye"
    # Airtable — statut mandat envoyé + amorce relances post-soumission
    upsert_airtable(phone, conv)
    _arm_post_submit(phone, conv)

    time.sleep(5)
    ref = conv.get("ref_dossier", "")
    if lang == "en":
        msg_c = (
            f"🎉 *File {ref} — received!*\n\n"
            f"We have everything we need. Our team is taking over your claim against the airline.\n\n"
            f"⏱️ You'll receive an update within *48 working hours*.\n\n"
            f"Thank you for your trust. *Robin des Airs team* 🏹"
        )
    else:
        msg_c = (
            f"🎉 *Dossier {ref} — bien reçu !*\n\n"
            f"Nous avons tout ce qu'il nous faut. Notre équipe prend en charge votre réclamation contre la compagnie aérienne.\n\n"
            f"⏱️ Vous recevrez une mise à jour sous *48h ouvrées*.\n\n"
            f"Merci de votre confiance. *L'équipe Robin des Airs* 🏹"
        )
    send_whatsapp_text(phone, msg_c)

    # Annonce expert humain — fin étape 14
    time.sleep(2)
    if lang == "en":
        msg_d = (
            "🤝 *At Robin des Airs, support is human.*\n\n"
            "Your file is now in the hands of an expert. They will contact you to follow every step until payment.\n\n"
            "*The AI opens the file. The human wins it.* 🏹"
        )
    else:
        msg_d = (
            "🤝 *Chez Robin des Airs, l'accompagnement est humain.*\n\n"
            "Votre dossier est maintenant entre les mains d'un expert. Il vous contactera pour suivre chaque étape jusqu'au paiement.\n\n"
            "*L'IA ouvre le dossier. L'humain le gagne.* 🏹"
        )
    send_whatsapp_text(phone, msg_d)

    conv["current_step"] = "completed"
    conv["data"]["dossier_status"] = "complet"

    passports = conv["data"].get("passports_collected", [])
    any_pp_skipped = any(p.get("skipped") for p in passports)
    boarding_missing = (not conv["data"].get("boarding_collected")
                        and not conv["data"].get("boarding_pass_confirmed"))
    if any_pp_skipped or boarding_missing:
        conv["data"]["dossier_status"] = "docs_en_attente"

    # Airtable — statut final
    upsert_airtable(phone, conv)


# ============================================
# AIRTABLE + CODESHARE + POST-SOUMISSION (repris/adapté du v11)
# ============================================

def at_headers():
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def at_url():
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AT_TABLE_ID}"

def fmt_date_for_airtable(date_str):
    """JJ/MM/AAAA → AAAA-MM-JJ"""
    parts = (date_str or "").split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return None

def _calc_amounts(pax):
    pax = max(1, int(pax or 1))
    brut = 600 * pax
    net  = round(brut * NET_PCT)
    com  = round(brut * COMMISSION_PCT)
    return brut, net, com

def at_find(ref):
    """Trouve les records par Référence Dossier."""
    if not AIRTABLE_API_KEY or not ref:
        return []
    try:
        esc     = ref.replace("'", "''")
        formula = f"{{{F_REF_DOSSIER}}}='{esc}'"
        url     = f"{at_url()}?filterByFormula={requests.utils.quote(formula)}"
        r       = requests.get(url, headers=at_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("records", [])
        print(f"at_find {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"at_find error: {e}")
    return []

def upsert_airtable(phone, conv):
    """Sauvegarde progressive — crée ou met à jour les records Airtable (1 ligne / passager)."""
    if not AIRTABLE_API_KEY:
        print(f"❌ at: AIRTABLE_API_KEY manquant (tel={phone}, ref={conv.get('ref_dossier')})")
        return
    try:
        d   = conv["data"]
        ref = conv.get("ref_dossier") or generate_ref_dossier(phone)
        conv["ref_dossier"] = ref

        pax   = d.get("passengers") or 1
        names = d.get("passenger_names") or []
        brut, net, com = _calc_amounts(pax)

        date_vol_at = fmt_date_for_airtable(d.get("flight_date") or "")
        incident_at = INCIDENT_AT.get(d.get("incident_type") or "", "")

        common = {
            F_REF_DOSSIER:    ref,
            F_DATE_DOSSIER:   datetime.now().strftime("%Y-%m-%d"),
            F_WHATSAPP:       str(phone),
            F_STATUT_DOSSIER: STATUT_DOSSIER_DEFAUT,
            F_STATUT_SUIVI:   STATUT_SUIVI_DEFAUT,
        }
        if d.get("airline"):       common[F_COMPAGNIE]  = d["airline"]
        if d.get("flight_number"): common[F_NUMERO_VOL] = d["flight_number"]
        if date_vol_at:            common[F_DATE_VOL]    = date_vol_at
        if d.get("pnr"):           common[F_PNR]         = str(d["pnr"]).strip().upper()
        if incident_at:            common[F_TYPE_INCIDENT] = incident_at
        if F_ITINERAIRE and d.get("route"):
            common[F_ITINERAIRE] = str(d["route"])[:8000]

        extra_bits = []
        if d.get("route"):             extra_bits.append(f"Itinéraire: {d['route']}")
        if d.get("codeshare_note"):    extra_bits.append(d["codeshare_note"])
        if d.get("operating_airline"): extra_bits.append(f"Opéré par: {d['operating_airline']}")
        if d.get("has_minors"):        extra_bits.append("Mineur(s): oui")
        if d.get("dossier_status"):    extra_bits.append(f"Statut bot: {d['dossier_status']}")
        rem_extra = (" | " + " ; ".join(extra_bits)) if extra_bits else ""

        existing = at_find(ref)

        if not existing:
            records = []
            for i in range(pax):
                f = dict(common)
                f[F_NOM_PASSAGER] = names[i] if i < len(names) else f"Passager {i+1}"
                f[F_REMARQUES]    = f"Ref: {ref} | Passager {i+1}/{pax} | Bot WhatsApp" + (rem_extra if i == 0 else "")
                if i == 0:
                    f[F_MONTANT_CLIENT]    = float(net)
                    f[F_COMMISSION_RDA]    = float(com)
                    f[F_MONTANT_INDEMNITE] = float(brut)
                else:
                    f[F_MONTANT_CLIENT]    = 0.0
                    f[F_COMMISSION_RDA]    = 0.0
                    f[F_MONTANT_INDEMNITE] = 0.0
                records.append({"fields": f})
            r = requests.post(at_url(), headers=at_headers(), json={"records": records}, timeout=15)
            if r.status_code in (200, 201):
                print(f"✅ Airtable CREATE {pax} (ref={ref})")
            else:
                print(f"❌ Airtable CREATE {r.status_code}: {r.text[:400]}")
        else:
            updates = []
            for i, rec in enumerate(existing[:pax]):
                f = dict(common)
                f[F_NOM_PASSAGER] = names[i] if i < len(names) else f"Passager {i+1}"
                f[F_REMARQUES]    = f"Ref: {ref} | Passager {i+1}/{pax} | Bot WhatsApp" + (rem_extra if i == 0 else "")
                if i == 0:
                    f[F_MONTANT_CLIENT]    = float(net)
                    f[F_COMMISSION_RDA]    = float(com)
                    f[F_MONTANT_INDEMNITE] = float(brut)
                updates.append({"id": rec["id"], "fields": f})
            r = requests.patch(at_url(), headers=at_headers(), json={"records": updates}, timeout=15)
            if r.status_code == 200:
                print(f"✅ Airtable UPDATE {len(updates)} (ref={ref})")
            else:
                print(f"❌ Airtable PATCH {r.status_code}: {r.text[:400]}")
    except Exception as e:
        print(f"❌ Airtable exception: {e}")
        import traceback
        traceback.print_exc()

# ===== UPLOAD PIÈCES JOINTES =====

def _img_bytes(image_b64):
    if not image_b64:
        return None
    try:
        return base64.b64decode(image_b64, validate=False)
    except Exception:
        return None

def _img_suffix(raw):
    if not raw or len(raw) < 8:
        return ".jpg", "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n": return ".png", "image/png"
    if raw[:2] == b"\xff\xd8":          return ".jpg", "image/jpeg"
    if raw[:4] == b"GIF8":              return ".gif", "image/gif"
    if raw[:4] == b"RIFF" and len(raw) >= 12 and raw[8:12] == b"WEBP": return ".webp", "image/webp"
    return ".jpg", "image/jpeg"

def at_upload_attachment(record_id, field_id, raw_bytes, fname):
    """POST uploadAttachment (API Airtable) sur un champ attachment."""
    if not AIRTABLE_API_KEY or not field_id or not record_id or not raw_bytes:
        return False
    if len(raw_bytes) > 5 * 1024 * 1024:
        print("at_upload_attachment: fichier > 5 Mo")
        return False
    ext, mime = _img_suffix(raw_bytes)
    if not fname.endswith(ext):
        fname += ext
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    payload = {"contentType": mime, "file": b64, "filename": fname}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}
    urls = (
        f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/{field_id}/uploadAttachment",
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/{field_id}/uploadAttachment",
    )
    last_err = None
    for url in urls:
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code in (200, 201):
                return True
            last_err = f"{url.split('/')[2]} {r.status_code}: {r.text[:300]}"
        except Exception as e:
            last_err = f"{url.split('/')[2]} exc: {e}"
    print(f"Airtable uploadAttachment failed: {last_err}")
    return False

def _at_record_id_for_index(recs, names, idx0):
    if not recs or idx0 < 0:
        return None
    target = str(names[idx0]).strip() if (isinstance(names, list) and idx0 < len(names)) else ""
    if target:
        for r in recs:
            fn = (r.get("fields") or {}).get(F_NOM_PASSAGER)
            if fn and str(fn).strip() == target:
                return r.get("id")
    if idx0 < len(recs):
        return recs[idx0].get("id")
    return None

def at_attach_image(phone, conv, image_b64, field_id, idx0=0, tag="doc"):
    """Upload une image sur le champ attachment de la ligne passager idx0. Crée le dossier si besoin."""
    if not AIRTABLE_API_KEY or not field_id or not image_b64:
        return False
    raw = _img_bytes(image_b64)
    if not raw:
        return False
    ref = conv.get("ref_dossier") or generate_ref_dossier(phone)
    conv["ref_dossier"] = ref
    recs = at_find(ref)
    if not recs:
        upsert_airtable(phone, conv)
        recs = at_find(ref)
    names = conv["data"].get("passenger_names") or []
    rid = _at_record_id_for_index(recs, names, idx0)
    if not rid:
        return False
    fname = f"{tag}_{ref}_p{idx0+1}"
    return at_upload_attachment(rid, field_id, raw, fname)

# ===== CODESHARE — RECHERCHE OPÉRATEUR RÉEL =====

def lookup_operating_airline(conv):
    """
    Recherche en arrière-plan de l'opérateur réel (codeshare).
    Base locale IATA : si le préfixe du n° de vol commercial diffère d'un opérateur connu,
    on tente de détecter via les codes déjà extraits (scan billet) ou via le préfixe.
    Stocke operating_airline / codeshare_note si l'exploitant diffère de la compagnie marketing.
    """
    d = conv["data"]
    fn = (d.get("flight_number") or "").strip().upper()
    marketing = (d.get("airline") or "").strip()

    mark_iata = re.sub(r"[^A-Z]", "", (d.get("marketing_carrier_iata") or "").upper())
    op_iata   = re.sub(r"[^A-Z]", "", (d.get("operating_carrier_iata") or "").upper())

    # Cas 1 : codes marketing/opérateur déjà lus sur le billet (scan vision)
    if mark_iata and op_iata and mark_iata[:2] != op_iata[:2]:
        mn = airline_from_iata(mark_iata) or marketing or mark_iata[:2]
        on = airline_from_iata(op_iata) or op_iata[:2]
        if on.strip().lower() != mn.strip().lower():
            d["operating_airline"] = on
            d["codeshare_note"] = f"Code-share : commercial {mn} ({mark_iata[:2]}) / opéré par {on} ({op_iata[:2]})"
            if not d.get("airline"):
                d["airline"] = mn
            return d["operating_airline"]

    # Cas 2 : la compagnie marketing saisie diffère du préfixe du n° de vol
    if fn and marketing:
        prefix_air = guess_airline(fn)
        if prefix_air and prefix_air.strip().lower() != marketing.strip().lower():
            # Le préfixe du n° de vol = transporteur commercial réel ; marketing saisi = exploitant probable
            d["operating_airline"] = marketing
            d["airline"] = prefix_air
            d["codeshare_note"] = f"Code-share possible : n° de vol {fn} ({prefix_air}) / saisi {marketing}"
            return d["operating_airline"]
    return None

def lookup_operating_airline_async(phone):
    """Lance la recherche codeshare en arrière-plan sans bloquer le flux."""
    def _run():
        try:
            conv = conversations.get(phone)
            if not conv:
                return
            res = lookup_operating_airline(conv)
            if res:
                print(f"[CODESHARE] {phone} → opéré par {res}")
                if conv["data"].get("dossier_status") in ("en_cours", "mandat_envoye"):
                    upsert_airtable(phone, conv)
        except Exception as e:
            print(f"lookup_operating_airline_async: {e}")
    threading.Thread(target=_run, daemon=True, name=f"codeshare_{phone}").start()

# ===== TEMPLATE WATI POST-SOUMISSION =====

def _first_name(conv):
    names = conv["data"].get("passenger_names") or []
    if names:
        return (names[0] or "").strip().split(" ")[0]
    return ""

def send_wati_template_v2(phone, conv):
    """Envoie le template Meta/Wati (hors fenêtre 24 h)."""
    if not WATI_API_TOKEN or not WATI_BASE_URL:
        return False
    if not WATI_POST_SUBMIT_TEMPLATE_NAME or not WATI_TEMPLATE_CHANNEL_NUMBER:
        print("send_wati_template_v2: template_name ou channel_number manquant")
        return False
    d = conv["data"]
    lang = d.get("language", "fr")
    pax = d.get("passengers") or 1
    ref = conv.get("ref_dossier") or ""
    _, net, _ = _calc_amounts(pax)
    prenom = _first_name(conv) or "Client"
    link = conv.get("mandat_url") or MANDAT_BASE_URL
    names_csv = os.environ.get("WATI_POST_SUBMIT_TEMPLATE_PARAM_NAMES", "").strip()
    keys = [k.strip() for k in names_csv.split(",") if k.strip()] or ["first_name", "amount", "reference", "link"]
    values = [prenom, f"{net} €", ref, link]
    parameters = []
    for i, k in enumerate(keys):
        v = values[i] if i < len(values) else ""
        if v:
            parameters.append({"name": k, "value": str(v)[:1024]})
    url = f"{WATI_BASE_URL}/api/v2/sendTemplateMessage"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "Content-Type": "application/json", "accept": "*/*"}
    body = {
        "template_name":  WATI_POST_SUBMIT_TEMPLATE_NAME,
        "broadcast_name": WATI_POST_SUBMIT_TEMPLATE_BROADCAST,
        "channel_number": WATI_TEMPLATE_CHANNEL_NUMBER,
        "parameters":     parameters,
    }
    try:
        r = requests.post(url, headers=headers, params={"whatsappNumber": phone}, json=body, timeout=45)
        print(f"Wati template v2 {r.status_code} {r.text[:200]}")
        ok_http = 200 <= r.status_code < 300
        try:
            js = r.json()
        except Exception:
            js = {}
        return ok_http and js.get("result") is not False
    except Exception as e:
        print(f"Wati template error: {e}")
        return False

# ===== POST-SOUMISSION : flags Airtable + relances =====

def _airtable_field_has_attachment(records, field_id):
    if not field_id or not records:
        return False
    for rec in records:
        val = (rec.get("fields") or {}).get(field_id)
        if isinstance(val, list) and len(val) > 0:
            return True
    return False

def _airtable_relance_halt(records):
    if not records:
        return False
    if F_STOP_RELANCE:
        for rec in records:
            if (rec.get("fields") or {}).get(F_STOP_RELANCE) is True:
                return True
        return False
    if F_SEQUENCE_ACTIVE:
        flds = (records[0].get("fields") or {})
        if F_SEQUENCE_ACTIVE not in flds:
            return False
        return flds.get(F_SEQUENCE_ACTIVE) is not True
    return False

def refresh_post_submit_airtable_flags(conv):
    """Met à jour _post_submit depuis Airtable (pièces jointes / mandat signé). Throttlé ~90 s."""
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict) or not ps.get("active"):
        return
    now = time.time()
    if now - float(ps.get("_at_sync_at") or 0) < 90:
        return
    ps["_at_sync_at"] = now
    ref = (conv.get("ref_dossier") or "").strip()
    if not ref or not AIRTABLE_API_KEY:
        return
    try:
        recs = at_find(ref)
    except Exception as e:
        print(f"refresh_post_submit_airtable_flags: {e}")
        return
    if F_CARTE_EMBARQUEMENT:
        ps["air_boarding_attachment"] = bool(_airtable_field_has_attachment(recs, F_CARTE_EMBARQUEMENT))
    if F_PIECE_IDENTITE:
        ps["air_id_attachment"] = bool(_airtable_field_has_attachment(recs, F_PIECE_IDENTITE))
    if F_MANDAT_SIGNE:
        ps["air_mandat_signed"] = bool(_airtable_field_has_attachment(recs, F_MANDAT_SIGNE))
    if _airtable_relance_halt(recs):
        ps["active"] = False

def post_submit_fully_done(conv):
    ps = (conv.get("data") or {}).get("_post_submit") or {}
    if ps.get("mandate_signed_server") or ps.get("air_mandat_signed"):
        return True
    return False

def _arm_post_submit(phone, conv):
    """Amorce la séquence de relances post-soumission (relances template hors fenêtre 24 h)."""
    ps = conv["data"].setdefault("_post_submit", {})
    ps["active"]        = True
    ps["summary_at"]    = time.time()
    ps["last_template_at"] = 0
    ps["template_mode"] = False
    ps.setdefault("relances_sent", [])
    ps.setdefault("mandate_signed_server", False)
    ps.setdefault("air_mandat_signed", False)

def process_post_submit_reminders():
    """Tick par minute : relance template post-soumission si mandat non signé."""
    now = time.time()
    repeat_h = max(6, int(os.environ.get("POST_SUBMIT_TEMPLATE_REPEAT_HOURS", "48") or "48"))
    for phone, conv in list(conversations.items()):
        d = conv.get("data") or {}
        ps = d.get("_post_submit")
        if not isinstance(ps, dict) or not ps.get("active"):
            continue
        if conv.get("current_step") != "completed":
            continue
        refresh_post_submit_airtable_flags(conv)
        if post_submit_fully_done(conv):
            ps["active"] = False
            continue
        if not ps.get("active"):
            continue
        if WATI_POST_SUBMIT_TEMPLATE_NAME and WATI_TEMPLATE_CHANNEL_NUMBER:
            last_tpl = float(ps.get("last_template_at") or 0)
            interval = repeat_h * 3600
            # 1re relance après ~24h, puis selon interval
            since_summary = now - float(ps.get("summary_at") or now)
            due = (last_tpl <= 0 and since_summary >= 24 * 3600) or (last_tpl > 0 and (now - last_tpl) >= interval)
            if due:
                if send_wati_template_v2(phone, conv):
                    ps["last_template_at"] = now

def _post_submit_reminder_loop():
    while True:
        time.sleep(60)
        try:
            process_post_submit_reminders()
        except Exception as e:
            print(f"_post_submit_reminder_loop: {e}")

_reminder_started = False
_reminder_lock = threading.Lock()

def start_post_submit_reminder_thread():
    global _reminder_started
    with _reminder_lock:
        if _reminder_started:
            return
        _reminder_started = True
    threading.Thread(target=_post_submit_reminder_loop, daemon=True, name="post_submit_relances").start()


# ============================================
# TRAITEMENT BOUTONS / LISTES
# ============================================

def process_button_reply(phone, button_id, button_title, conv):
    print(f"[BTN] {button_id} = {button_title}")
    button_id    = (button_id    or "").strip()
    button_title = (button_title or "").strip().lower()
    lang = conv["data"].get("language", "fr")
    touch_activity(conv)

    # ── NORMALISATION POSITION → ID ─────────────────────────────
    # Wati renvoie parfois la position du bouton ("1","2","3","4")
    # au lieu de l'ID configuré. On convertit selon l'étape courante.
    current_step = conv.get("current_step")
    step_position_map = {
        "route_qualify":         {"1": "zone_africa_europe", "2": "zone_europe", "3": "zone_depart_europe", "4": "zone_other"},
        "incident_type":         {"1": "inc_delay",     "2": "inc_cancel",     "3": "inc_denied"},
        "delay_duration":        {"1": "delay_3plus",   "2": "delay_lt3",      "3": "delay_unknown"},
        "flight_type":           {"1": "type_direct",   "2": "type_connection"},
        "doc_confirm":           {"1": "doc_confirm_yes",  "2": "doc_confirm_no"},
        "passenger_confirm":     {"1": "pax_confirm_yes",  "2": "pax_confirm_no"},
        "flight_number_confirm": {"1": "fn_confirm_yes",   "2": "fn_confirm_no"},
        "flight_date_confirm":   {"1": "date_confirm_yes", "2": "date_confirm_no"},
        "minor_check":           {"1": "minor_no",      "2": "minor_yes"},
        "recap":                 {"1": "recap_ok",      "2": "recap_modify"},
        "trip_select":           {"1": "trip_outbound", "2": "trip_return",    "3": "trip_both"},
    }
    if button_id in ("1", "2", "3", "4") and current_step in step_position_map:
        button_id = step_position_map[current_step].get(button_id, button_id)
    # Passagers (liste 1-6) : position → pax_N (6 = pax_more)
    if current_step == "passengers" and button_id in ("1", "2", "3", "4", "5", "6"):
        button_id = "pax_more" if button_id == "6" else f"pax_{button_id}"
    # Cas particulier mineurs : avec 1 seul passager, le 2e bouton = "minor_self"
    if current_step == "minor_check" and button_id == "minor_yes" and conv["data"].get("passengers") == 1:
        button_id = "minor_self"

    # ── FALLBACK MOTS-CLÉS pour les confirmations OUI/NON ───────
    _yes_kw = ("oui", "yes", "correct", "bon", "ok")
    _no_kw  = ("corriger", "correct it", "non", "no", "modif", "modify")
    _confirm_steps = {
        "passenger_confirm":     ("pax_confirm_yes", "pax_confirm_no"),
        "flight_number_confirm": ("fn_confirm_yes", "fn_confirm_no"),
        "flight_date_confirm":   ("date_confirm_yes", "date_confirm_no"),
        "doc_confirm":           ("doc_confirm_yes", "doc_confirm_no"),
    }
    if current_step in _confirm_steps and button_id not in _confirm_steps[current_step]:
        yes_id, no_id = _confirm_steps[current_step]
        if any(k in button_title for k in _no_kw):
            button_id = no_id
        elif any(k in button_title for k in _yes_kw):
            button_id = yes_id

    # ── FALLBACK MOTS-CLÉS pour le récap ────────────────────────
    if current_step == "recap" and button_id not in ("recap_ok", "recap_modify"):
        if any(k in button_title for k in ("modif", "modify", "corriger")):
            button_id = "recap_modify"
        elif any(k in button_title for k in ("correct", "tout", "all")):
            button_id = "recap_ok"

    # ── FALLBACK MOTS-CLÉS pour les mineurs ─────────────────────
    if current_step == "minor_check" and button_id not in ("minor_no", "minor_yes", "minor_self"):
        if any(k in button_title for k in ("majeur", "adult", "tous", "over 18")):
            button_id = "minor_no"
        elif any(k in button_title for k in ("mineur", "minor")):
            button_id = "minor_self" if conv["data"].get("passengers") == 1 else "minor_yes"

    # ── MSG 1 — ACCROCHE → LANGUE ───────────────────────────────
    # Wati peut envoyer l'ID "start_check" OU le titre du bouton OU "1"
    check_keywords = ("vérifier", "verifier", "check", "start", "commencer", "droits", "rights", "indemnité", "indemnite")
    if button_id in ("start_check", "1") or button_title in ("start_check",) or any(k in button_title for k in check_keywords):
        conv["current_step"] = "language"
        ask_language(phone)
        return

    # ── FALLBACK IA — DEMANDE RAPPEL EXPERT ─────────────────────
    if button_id == "rappel_expert" or any(k in button_title for k in ("rappel", "call", "expert", "rappelé", "called back")):
        conv["data"]["dossier_status"] = "escalade_expert"
        conv["data"]["escalade_reason"] = "demande_rappel"
        if lang == "en":
            send_whatsapp_text(phone, (
                "📱 *A Robin des Airs expert is contacting you.*\n"
                "Keep this conversation open — they'll write to you directly here.\n"
                "_The Robin des Airs team_"
            ))
        else:
            send_whatsapp_text(phone, (
                "📱 *Un expert Robin des Airs vous contacte.*\n"
                "Laissez cette conversation ouverte — il vous écrit directement ici.\n"
                "_L'équipe Robin des Airs_"
            ))
        return

    # ── MSG 2 — LANGUE ──────────────────────────────────────────
    if button_id.startswith("lang_soon_"):
        # Langue "bientôt disponible" → message d'attente + continue en FR
        soon_lang = button_id.replace("lang_soon_", "")
        conv["data"]["preferred_language"] = soon_lang
        lang = conv["data"].get("language", "fr")
        msg = COMING_SOON_MSG.get(lang, COMING_SOON_MSG["fr"])
        send_whatsapp_text(phone, msg)
        time.sleep(1)
        conv["data"]["language"] = "fr"
        conv["current_step"] = "route_qualify"
        ask_route_qualify(phone, "fr")
        return

    if button_id.startswith("lang_"):
        chosen = button_id.replace("lang_", "")
        conv["data"]["preferred_language"] = chosen
        if chosen in ("fr", "en"):
            conv["data"]["language"] = chosen
            conv["current_step"]     = "route_qualify"
            ask_route_qualify(phone, chosen)
        else:
            conv["data"]["dossier_status"] = "escalade_expert"
            conv["data"]["escalade_reason"] = "langue_africaine"
            expert_msg = EXPERT_MSG.get(chosen, "Un expert vous rappelle directement 🤝\n📱 +33 7 56 86 36 30")
            send_whatsapp_text(phone, expert_msg)
            time.sleep(1)
            send_whatsapp_text(phone, (
                "🤝 *Votre dossier est entre de bonnes mains.*\n\n"
                "Un expert parlant votre langue vous contactera en cours de dossier.\n\n"
                "En attendant, je continue à vous guider en français. 👇"
            ))
            time.sleep(1)
            conv["data"]["language"] = "fr"
            conv["current_step"]     = "route_qualify"
            ask_route_qualify(phone, "fr")
        return

    # ── MSG 3 — ZONE (qualification route) ──────────────────────
    if button_id == "zone_africa_europe" or (current_step == "route_qualify" and any(k in button_title for k in ("afrique", "africa", "europe", "spécialité", "specialite"))):
        conv["data"]["route_zone"]  = "africa_europe"
        conv["current_step"]        = "incident_type"
        ask_incident_type(phone, lang)
        return

    if button_id == "zone_europe" or (current_step == "route_qualify" and any(k in button_title for k in ("intra", "within"))):
        conv["data"]["route_zone"]  = "europe"
        conv["current_step"]        = "incident_type"
        if lang == "en":
            send_whatsapp_text(phone, "🇪🇺 Intra-European flights are covered by CE 261 ✅\nOur specialty is Africa ↔ Europe routes, but let's continue.")
        else:
            send_whatsapp_text(phone, "🇪🇺 Les vols intra-européens sont couverts par le CE 261 ✅\nNotre spécialité c'est les routes Afrique ↔ Europe, mais on continue.")
        time.sleep(1)
        ask_incident_type(phone, lang)
        return

    if button_id == "zone_depart_europe" or (current_step == "route_qualify" and any(k in button_title for k in ("via", "départ", "depart", "arrivée", "arrivee", "departure"))):
        conv["data"]["route_zone"]  = "depart_europe"
        conv["current_step"]        = "incident_type"
        if lang == "en":
            send_whatsapp_text(phone, "🛫 A departure or arrival in Europe can be eligible — especially with a European airline or airport. Let's check it together. ✅")
        else:
            send_whatsapp_text(phone, "🛫 Un départ ou une arrivée en Europe peut être éligible — surtout avec une compagnie ou un aéroport européen. Vérifions ensemble. ✅")
        time.sleep(1)
        ask_incident_type(phone, lang)
        return

    if button_id == "zone_other" or (current_step == "route_qualify" and any(k in button_title for k in ("autre", "other"))):
        conv["data"]["dossier_status"] = "non_eligible"
        conv["data"]["non_eligibility_reason"] = "route_hors_europe"
        if lang == "en":
            send_whatsapp_text(phone, (
                "😔 *Your flight does not appear to be covered by European law.*\n\n"
                "EU Regulation CE 261/2004 applies only to flights:\n"
                "• Departing from or arriving at a European airport, or\n"
                "• Operated by a European airline.\n\n"
                "From what you've indicated, your flight doesn't meet these conditions.\n\n"
                "❓ *If you think there's an error*, type *menu* to restart and choose a different route — it only takes a few seconds.\n\n"
                "_The Robin des Airs team_"
            ))
        else:
            send_whatsapp_text(phone, (
                "😔 *Votre vol ne semble pas couvert par la loi européenne.*\n\n"
                "Le règlement CE 261/2004 s'applique uniquement aux vols :\n"
                "• Au départ ou à l'arrivée d'un aéroport européen, ou\n"
                "• Opérés par une compagnie européenne.\n\n"
                "D'après ce que vous avez indiqué, votre vol ne remplit aucune de ces conditions.\n\n"
                "❓ *Si vous pensez qu'il y a une erreur*, tapez *menu* pour recommencer et choisir une autre route — cela ne prend que quelques secondes.\n\n"
                "_L'équipe Robin des Airs_"
            ))
        conv["current_step"] = None
        return

    # ── MSG 4 — INCIDENT ────────────────────────────────────────
    if current_step == "incident_type" and button_id not in ("inc_delay", "inc_cancel", "inc_denied"):
        if any(k in button_title for k in ("retard", "delay")):
            button_id = "inc_delay"
        elif any(k in button_title for k in ("annul", "cancel")):
            button_id = "inc_cancel"
        elif any(k in button_title for k in ("refus", "denied", "surbooking", "boarding")):
            button_id = "inc_denied"
    if button_id in ("inc_delay", "inc_cancel", "inc_denied"):
        mapping = {"inc_delay": "delay", "inc_cancel": "cancel", "inc_denied": "denied"}
        conv["data"]["incident_type"] = mapping[button_id]
        if button_id == "inc_delay":
            conv["current_step"] = "delay_duration"
            ask_delay_duration(phone, lang)
        else:
            conv["data"]["delay_ok"] = True
            send_estimation(phone, conv)
            conv["current_step"] = "passengers"
            ask_passengers(phone, lang)
        return

    # ── MSG 4 — DURÉE RETARD ────────────────────────────────────
    if current_step == "delay_duration" and button_id not in ("delay_3plus", "delay_lt3", "delay_unknown"):
        if any(k in button_title for k in ("plus", "more", "3h", "3 h")):
            button_id = "delay_3plus"
        elif any(k in button_title for k in ("moins", "less", "1h", "2h")):
            button_id = "delay_lt3"
        elif any(k in button_title for k in ("sais", "sure", "remember")):
            button_id = "delay_unknown"
    if button_id == "delay_lt3":
        conv["data"]["dossier_status"] = "non_eligible"
        conv["data"]["non_eligibility_reason"] = "retard_trop_court"
        if lang == "en":
            send_whatsapp_text(phone, (
                "😔 *Delay under 3 hours — no compensation available.*\n\n"
                "EU law CE 261/2004 requires a minimum delay of *3 hours at arrival* to trigger flat-rate compensation.\n\n"
                "Below this threshold, no compensation is provided by law, regardless of the circumstances.\n\n"
                "💡 *Not sure of the exact delay?* Type *menu* and choose 'I'm not sure' — an expert checks for free.\n\n"
                "_Robin des Airs team_"
            ))
        else:
            send_whatsapp_text(phone, (
                "😔 *Retard inférieur à 3 heures — pas d'indemnisation possible.*\n\n"
                "La loi européenne CE 261/2004 fixe un seuil minimum de *3 heures de retard à l'arrivée* pour ouvrir droit à une indemnité forfaitaire.\n\n"
                "En dessous de ce seuil, aucune compensation n'est prévue par la loi, quelles que soient les circonstances.\n\n"
                "💡 *Vous n'êtes pas sûr de la durée exacte ?* Tapez *menu* et choisissez 'Je ne sais plus' — un expert vérifie pour vous gratuitement.\n\n"
                "_L'équipe Robin des Airs_"
            ))
        conv["current_step"] = None
        return

    if button_id == "delay_unknown":
        conv["data"]["delay_ok"] = True
        conv["data"]["dossier_status"] = "escalade_expert"
        conv["data"]["escalade_reason"] = "duree_inconnue"
        if lang == "en":
            send_whatsapp_text(phone, (
                "🤔 *No problem — we'll check for you.*\n\n"
                "The exact delay is recorded in aviation databases. Our team can retrieve it from your flight number and date.\n\n"
                "Let's keep filling in your file. If the delay turns out to be under 3h, we'll let you know at no cost.\n\n"
                "📱 *An expert will contact you* to confirm the duration before starting the procedure."
            ))
        else:
            send_whatsapp_text(phone, (
                "🤔 *Pas de problème — on vérifie pour vous.*\n\n"
                "La durée exacte du retard figure dans les bases de données aériennes. Notre équipe peut la retrouver à partir de votre numéro de vol et de votre date.\n\n"
                "Continuons à remplir votre dossier. Si le retard s'avère inférieur à 3h, nous vous en informerons sans frais.\n\n"
                "📱 *Un expert vous contactera* pour confirmer la durée avant d'engager la procédure."
            ))
        time.sleep(1)
        send_estimation(phone, conv)
        conv["current_step"] = "passengers"
        ask_passengers(phone, lang)
        return

    if button_id == "delay_3plus":
        conv["data"]["delay_ok"] = True
        send_estimation(phone, conv)
        conv["current_step"] = "passengers"
        ask_passengers(phone, lang)
        return

    # ── MSG 5 — PASSAGERS ───────────────────────────────────────
    if button_id.startswith("pax_") and button_id not in ("pax_confirm_yes", "pax_confirm_no"):
        if button_id == "pax_more":
            conv["data"]["dossier_status"] = "escalade_expert"
            conv["data"]["escalade_reason"] = "groupe_6plus"
            if lang == "en":
                send_whatsapp_text(phone, (
                    "👥 *Group of 6+ passengers — priority file!*\n\n"
                    "Potentially *more than €3,600*.\n\n"
                    "For groups we handle files manually.\n\n"
                    "👉 robindesairs.eu/depot-express\n\n"
                    "*The Robin des Airs team*"
                ))
            else:
                send_whatsapp_text(phone, (
                    "👥 *Groupe de 6+ passagers — dossier prioritaire !*\n\n"
                    "Potentiellement *plus de 3 600€*.\n\n"
                    "Pour les groupes nous traitons les dossiers manuellement.\n\n"
                    "👉 robindesairs.eu/depot-express\n\n"
                    "*L'équipe Robin des Airs*"
                ))
            conv["current_step"] = None
            return
        conv["data"]["passengers"] = int(button_id.split("_")[1])
        upsert_airtable(phone, conv)  # Airtable — après confirmation nb passagers
        conv["current_step"]       = "flight_type"
        ask_flight_type(phone, conv)
        return

    # ── CONFIRMATION PASSAGER ────────────────────────────────────
    if button_id == "pax_confirm_yes":
        name = conv["data"].pop("_temp_pax_name", "")
        if name:
            conv["data"]["passenger_names"].append(name)
        idx = conv["data"].get("current_pax_index", 0) + 1
        conv["data"]["current_pax_index"] = idx
        conv["current_step"] = "passenger_collect"
        ask_next_passenger(phone, conv)
        return

    if button_id == "pax_confirm_no":
        conv["current_step"] = "passenger_collect"
        idx  = conv["data"].get("current_pax_index", 0)
        num  = idx + 1
        if lang == "en":
            send_whatsapp_text(phone, f"✍️ Passenger {num} — Please type the correct first name and last name:")
        else:
            send_whatsapp_text(phone, f"✍️ Passager {num} — Tapez le prénom et nom corrects :")
        return

    # ── MSG 10 — CONFIRMATION NUMÉRO DE VOL ─────────────────────
    if button_id == "fn_confirm_yes":
        # Recherche codeshare en arrière-plan (opérateur réel) — flux continue normalement
        lookup_operating_airline_async(phone)
        if conv["data"].get("flight_date"):
            conv["current_step"] = "flight_date_confirm"
            confirm_flight_date(phone, conv, conv["data"]["flight_date"])
        else:
            conv["current_step"] = "flight_date"
            ask_flight_date(phone, conv)
        return

    if button_id == "fn_confirm_no":
        conv["current_step"] = "flight_number"
        if lang == "en":
            send_whatsapp_text(phone, "✍️ Please type the correct flight number:")
        else:
            send_whatsapp_text(phone, "✍️ Tapez le numéro de vol correct :")
        return

    # ── MSG 10 — CONFIRMATION DATE ──────────────────────────────
    if button_id == "date_confirm_yes":
        date_str = conv["data"].get("flight_date", "")
        flight_date_obj = None
        for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
            try:
                flight_date_obj = datetime.strptime(date_str.strip(), fmt)
                break
            except ValueError:
                pass
        if flight_date_obj is None:
            year_match = re.search(r'\b(20\d{2})\b', date_str)
            if year_match:
                flight_date_obj = datetime(int(year_match.group(1)), 6, 1)

        if flight_date_obj and (datetime.now() - flight_date_obj).days > 5 * 365:
                conv["data"]["dossier_status"] = "non_eligible"
                conv["data"]["non_eligibility_reason"] = "vol_trop_ancien"
                if lang == "en":
                    send_whatsapp_text(phone, (
                        "😔 *Flight too old — legal time limit exceeded.*\n\n"
                        "The law provides a *5-year limitation period* from the date of the flight.\n"
                        "Beyond that, no legal action or claim is possible, even if the delay was real.\n\n"
                        f"📅 Your flight is dated *{date_str}*, more than 5 years ago. We cannot process this file.\n\n"
                        "❓ *If the date is incorrect*, type *menu* to correct it.\n\n"
                        "_Robin des Airs team_"
                    ))
                else:
                    send_whatsapp_text(phone, (
                        "😔 *Vol trop ancien — délai légal dépassé.*\n\n"
                        "La loi prévoit une *prescription de 5 ans* à compter de la date du vol.\n"
                        "Au-delà, aucune action en justice ou réclamation n'est possible, même si le retard était réel.\n\n"
                        f"📅 Votre vol date de *{date_str}*, soit plus de 5 ans. Nous ne pouvons pas traiter ce dossier.\n\n"
                        "❓ *Si la date est incorrecte*, tapez *menu* pour corriger.\n\n"
                        "_L'équipe Robin des Airs_"
                    ))
                conv["current_step"] = None
                return
        upsert_airtable(phone, conv)  # Airtable — après confirmation vol + date
        conv["current_step"] = "minor_check"
        ask_minors(phone, conv)
        return

    if button_id == "date_confirm_no":
        conv["current_step"] = "flight_date"
        if lang == "en":
            send_whatsapp_text(phone, "✍️ Please type the correct date (eg: 15/03/2023):")
        else:
            send_whatsapp_text(phone, "✍️ Tapez la date correcte (ex : 15/03/2023) :")
        return

    # ── MSG 6 — TYPE VOL → SCAN ─────────────────────────────────
    if current_step == "flight_type" and button_id not in ("type_direct", "type_connection"):
        if "direct" in button_title:
            button_id = "type_direct"
        elif any(k in button_title for k in ("escale", "connection", "correspondance")):
            button_id = "type_connection"
    if button_id in ("type_direct", "type_connection"):
        conv["data"]["flight_type"] = "direct" if button_id == "type_direct" else "connection"
        send_motivation(phone, conv)
        conv["current_step"] = "document"
        ask_document(phone, conv)
        return

    # ── MSG 11 — MINEURS ────────────────────────────────────────
    if button_id == "minor_no":
        conv["data"]["has_minors"]   = False
        conv["data"]["minors_count"] = 0
        conv["current_step"]         = "recap"
        show_recap(phone, conv)
        return

    if button_id == "minor_self":
        conv["data"]["dossier_status"] = "escalade_expert"
        conv["data"]["escalade_reason"] = "mineur_seul"
        if lang == "en":
            send_whatsapp_text(phone, (
                "👶 *Minor passenger travelling alone — parental signature required.*\n\n"
                "The law requires a parent or legal guardian to sign the representation mandate for a minor.\n\n"
                "We'll handle this file with you directly.\n\n"
                "📱 *A Robin des Airs expert will call you back within 24h* to guide you through the procedure.\n\n"
                "_Robin des Airs team_"
            ))
        else:
            send_whatsapp_text(phone, (
                "👶 *Passager mineur voyageant seul — signature parentale requise.*\n\n"
                "La loi exige qu'un parent ou tuteur légal signe le mandat de représentation pour un mineur.\n\n"
                "Nous allons traiter ce dossier avec vous directement.\n\n"
                "📱 *Un expert Robin des Airs vous rappelle dans les 24h* pour accompagner la procédure.\n\n"
                "_L'équipe Robin des Airs_"
            ))
        return

    if button_id == "minor_yes":
        conv["data"]["has_minors"] = True
        conv["current_step"] = "minor_select"
        ask_minor_select(phone, conv)
        return

    if button_id.startswith("minor_pax_"):
        idx = int(button_id.split("_")[2])
        names = conv["data"].get("passenger_names", [])
        minors = conv["data"].get("minor_names", [])
        if idx < len(names) and names[idx] not in minors:
            minors.append(names[idx])
        conv["data"]["minor_names"] = minors
        if lang == "en":
            send_whatsapp_text(phone, f"✅ *{names[idx]}* marked as minor. Select another or type *done*.")
        else:
            send_whatsapp_text(phone, f"✅ *{names[idx]}* marqué comme mineur. Sélectionnez un autre ou tapez *ok*.")
        return

    # ── MSG 12 — RÉCAP ──────────────────────────────────────────
    if button_id == "recap_ok":
        upsert_airtable(phone, conv)  # Airtable — après récap validé (avant documents)
        conv["current_step"] = "passport_collect"
        ask_passport(phone, conv)
        return

    if button_id == "recap_modify":
        conv["current_step"] = "recap_modify"
        ask_what_to_modify(phone, lang)
        return

    # ── MENU MODIFICATION ────────────────────────────────────────
    if button_id == "mod_names":
        conv["data"]["passenger_names"]   = []
        conv["data"]["current_pax_index"] = 0
        conv["current_step"]              = "passenger_collect"
        ask_next_passenger(phone, conv)
        return

    if button_id == "mod_flight":
        conv["current_step"] = "flight_number"
        ask_flight_number(phone, conv)
        return

    if button_id == "mod_date":
        conv["current_step"] = "flight_date"
        ask_flight_date(phone, conv)
        return

    if button_id == "mod_incident":
        conv["current_step"] = "incident_type"
        ask_incident_type(phone, lang)
        return

    if button_id == "mod_route":
        conv["current_step"] = "route_input"
        if lang == "en":
            send_whatsapp_text(phone, "🗺️ Type your route:\n_(eg: Paris CDG → Dakar DSS)_")
        else:
            send_whatsapp_text(phone, "🗺️ Tapez votre trajet :\n_(ex : Paris CDG → Dakar DSS)_")
        return


# ============================================
# WEBHOOK PRINCIPAL
# ============================================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        try:
            print("[WEBHOOK] IN POST preview=", json.dumps(data, ensure_ascii=False)[:300])
        except Exception:
            pass

        phone = data.get("waId") or data.get("from") or data.get("phone")
        if not phone:
            return jsonify({"status": "no phone"}), 200
        if data.get("owner") is True:
            return jsonify({"status": "ignored own"}), 200

        conv = get_or_create_conversation(phone)
        lang = conv["data"].get("language", "fr")

        # ── BOUTONS / LISTES ────────────────────────────────────
        button_reply = (
            data.get("buttonReply")
            or data.get("interactiveButtonReply")
            or (data.get("interactive") or {}).get("button_reply")
            or data.get("button_reply")
        )
        list_reply = (
            data.get("listReply")
            or data.get("interactiveListReply")
            or (data.get("interactive") or {}).get("list_reply")
            or data.get("list_reply")
        )

        if button_reply:
            btn_id    = button_reply.get("id") or button_reply.get("buttonId") or button_reply.get("payload") or ""
            btn_title = button_reply.get("title") or button_reply.get("text") or ""
            if is_duplicate_event(phone, data, f"button|{btn_id}|{btn_title}"):
                return jsonify({"status": "duplicate"}), 200
            # Normalisation position → ID pour les boutons interceptés tôt
            _step = conv.get("current_step")
            if btn_id in ("1", "2", "3"):
                if _step == "trip_select":
                    btn_id = {"1": "trip_outbound", "2": "trip_return", "3": "trip_both"}.get(btn_id, btn_id)
                elif _step == "doc_confirm":
                    btn_id = {"1": "doc_confirm_yes", "2": "doc_confirm_no"}.get(btn_id, btn_id)
            _btn_title_l = (btn_title or "").strip().lower()
            if btn_id in ("trip_outbound", "trip_return", "trip_both") or (_step == "trip_select" and any(k in _btn_title_l for k in ("deux", "both", "aller", "outbound", "retour", "return"))):
                if btn_id not in ("trip_outbound", "trip_return", "trip_both"):
                    if any(k in _btn_title_l for k in ("deux", "both")):
                        btn_id = "trip_both"
                    elif any(k in _btn_title_l for k in ("retour", "return")):
                        btn_id = "trip_return"
                    else:
                        btn_id = "trip_outbound"
                _handle_trip_select(phone, btn_id, conv)
                return jsonify({"status": "ok"}), 200
            _doc_no  = any(k in _btn_title_l for k in ("corriger", "correct it", "non", "no"))
            _doc_yes = any(k in _btn_title_l for k in ("oui", "yes", "bon")) and not _doc_no
            if btn_id in ("doc_confirm_yes", "doc_confirm_no") or (_step == "doc_confirm" and (_doc_yes or _doc_no)):
                if btn_id == "doc_confirm_yes":
                    _yes = True
                elif btn_id == "doc_confirm_no":
                    _yes = False
                else:
                    _yes = _doc_yes
                _handle_doc_confirm(phone, _yes, conv)
                return jsonify({"status": "ok"}), 200
            process_button_reply(phone, btn_id, btn_title, conv)
            return jsonify({"status": "ok"}), 200

        if list_reply:
            row_id    = list_reply.get("id") or list_reply.get("rowId") or list_reply.get("payload") or ""
            row_title = list_reply.get("title") or list_reply.get("text") or ""
            if is_duplicate_event(phone, data, f"list|{row_id}|{row_title}"):
                return jsonify({"status": "duplicate"}), 200
            process_button_reply(phone, row_id, row_title, conv)
            return jsonify({"status": "ok"}), 200

        # ── MESSAGE TEXTE OU IMAGE ───────────────────────────────
        message_type = data.get("type", "text")
        image_data   = None
        message_text = ""

        if message_type == "image" or "image" in data:
            print(f"[IMAGE] reçue de {phone}")
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    r = requests.get(media_url, headers={"Authorization": f"Bearer {WATI_API_TOKEN}"}, timeout=30)
                    if r.status_code == 200:
                        image_data = base64.b64encode(r.content).decode("utf-8")
                except Exception as e:
                    print(f"[IMAGE] erreur download: {e}")
            message_text = data.get("caption", "") or ""
        else:
            if "text" in data:
                message_text = data["text"].get("body","") if isinstance(data["text"], dict) else str(data["text"])
            elif "body" in data:
                message_text = str(data["body"])

        if not message_text and not image_data:
            return jsonify({"status": "ignored empty"}), 200

        payload_sig = f"text|{message_text.strip().lower()}|img:{bool(image_data)}"
        if is_duplicate_event(phone, data, payload_sig):
            return jsonify({"status": "duplicate"}), 200

        touch_activity(conv)
        print(f"[MSG] from={phone} step={conv.get('current_step')} text={message_text[:50]!r} img={bool(image_data)}")

        current_step = conv.get("current_step")
        txt_lower    = message_text.strip().lower()

        # ── RESET / MENU ─────────────────────────────────────────
        if txt_lower in ("nouveau", "new", "reset", "/reset", "recommencer"):
            if phone in conversations:
                del conversations[phone]
            conv = get_or_create_conversation(phone)
            conv["ref_dossier"]  = generate_ref_dossier(phone)
            conv["current_step"] = "welcome"
            send_welcome_hook(phone, conv)
            return jsonify({"status": "restarted"}), 200

        if txt_lower in ("menu", "restart", "start", "reprendre", "continuer", "suite"):
            step = conv.get("current_step")
            if step and step not in (None, "completed"):
                lang = conv["data"].get("language", "fr")
                if lang == "en":
                    send_whatsapp_text(phone, "👋 Welcome back! Let's pick up where you left off.")
                else:
                    send_whatsapp_text(phone, "👋 Re-bonjour ! On reprend votre dossier là où vous vous étiez arrêté.")
                time.sleep(1)
                _resume_step(phone, conv)
            else:
                conv["ref_dossier"]  = generate_ref_dossier(phone)
                conv["current_step"] = "welcome"
                send_welcome_hook(phone, conv)
            return jsonify({"status": "resumed"}), 200

        # ── DÉMARRAGE — MSG 1 : ACCROCHE ─────────────────────────
        if current_step in (None, "completed"):
            conv["ref_dossier"]  = generate_ref_dossier(phone)
            conv["current_step"] = "welcome"
            send_welcome_hook(phone, conv)
            return jsonify({"status": "flow started"}), 200

        if message_text and not conv["data"].get("preferred_language"):
            conv["data"]["language"] = detect_language(message_text)
            lang = conv["data"]["language"]

        # ── SCAN DOCUMENT (image envoyée) — MSG 8 ────────────────
        if image_data and current_step in ("document", "flight_number", "doc_confirm"):
            print(f"[SCAN] document pour {phone}")
            extracted_json = call_openai(
                phone,
                'Extract from this travel document and reply ONLY with valid JSON: '
                '{"flight_number":"...","date":"DD/MM/YYYY","departure_time":"HH:MM","passenger_name":"...","airline":"...","marketing_carrier_iata":"...","operating_carrier_iata":"...","pnr":"...","origin":"IATA_CODE","destination":"IATA_CODE","return_flight_number":"...","return_date":"..."}. '
                'For date: boarding passes often show only DD/MM or DD MON (e.g. "30 MAI" or "30/05") without the year — in that case infer the most likely year: if the date has not yet passed this year use current year, else use previous year. Always output full DD/MM/YYYY. '
                'departure_time = scheduled departure time on the card (HH:MM). origin/destination = 3-letter IATA airport codes. '
                'marketing_carrier_iata = 2-letter code of the marketing/ticketing carrier. '
                'operating_carrier_iata = 2-letter code of the operating carrier ONLY if "Operated by / Opéré par" is printed (codeshare), else "". '
                'pnr = 6-char record locator if visible.',
                image_data
            )
            if extracted_json:
                try:
                    match = re.search(r'\{.*\}', extracted_json, re.DOTALL)
                    if match:
                        info = json.loads(match.group())
                        fn   = (info.get("flight_number") or "").strip().upper()
                        if fn:
                            conv["data"]["flight_number"] = fn
                            guessed = guess_airline(fn)
                            if guessed: conv["data"]["airline"] = guessed
                        if info.get("date"):
                            raw_date = info["date"].strip()
                            # Si pas d'année (ex: "30/05" ou "30 MAI"), déduire l'année
                            if raw_date and not re.search(r'\b(20\d{2})\b', raw_date):
                                try:
                                    now = datetime.now()
                                    # Essayer DD/MM
                                    dt = datetime.strptime(raw_date, "%d/%m")
                                    dt = dt.replace(year=now.year)
                                    if dt < now:
                                        dt = dt.replace(year=now.year - 1)
                                    raw_date = dt.strftime("%d/%m/%Y")
                                except ValueError:
                                    pass  # Garder tel quel si format inconnu
                            conv["data"]["flight_date"] = raw_date
                        if info.get("airline"): conv["data"]["airline"]     = info["airline"]
                        if info.get("marketing_carrier_iata"):
                            conv["data"]["marketing_carrier_iata"] = re.sub(r"[^A-Za-z]", "", info["marketing_carrier_iata"]).upper()
                        if info.get("operating_carrier_iata"):
                            conv["data"]["operating_carrier_iata"] = re.sub(r"[^A-Za-z]", "", info["operating_carrier_iata"]).upper()
                        if info.get("pnr"):
                            conv["data"]["pnr"] = re.sub(r"[^A-Za-z0-9]", "", str(info["pnr"]).upper())[:8]
                        # Résoudre l'opérateur réel (codeshare) à partir des codes lus
                        lookup_operating_airline(conv)
                        if info.get("origin") and info.get("destination"):
                            conv["data"]["route"] = f"{info['origin']} → {info['destination']}"
                        if info.get("passenger_name"):
                            names = conv["data"].get("passenger_names") or []
                            if info["passenger_name"] not in names:
                                names.append(info["passenger_name"])
                            conv["data"]["passenger_names"] = names
                        # Stocker le doc scanné avec heure de départ pour tri chronologique
                        scanned = conv["data"].setdefault("scanned_docs", [])
                        scanned.append({
                            "flight_number":  fn,
                            "passenger_name": info.get("passenger_name"),
                            "date":           info.get("date"),
                            "departure_time": info.get("departure_time", ""),
                            "origin":         info.get("origin", ""),
                            "destination":    info.get("destination", ""),
                        })

                        pax_total   = conv["data"].get("passengers", 1)
                        scanned_cnt = len(scanned)

                        # Si vol avec escale ET plusieurs cartes à scanner
                        if conv["data"].get("flight_type") == "connection" and scanned_cnt < pax_total:
                            # Proposer de scanner la carte suivante
                            if lang == "en":
                                more_msg = with_bar("document", (
                                    f"✅ Card {scanned_cnt}/{pax_total} scanned!\n\n"
                                    f"📸 Send the *next boarding pass* (passenger {scanned_cnt+1}).\n"
                                    f"✏️ Or type *done* if you've sent all cards."
                                ))
                            else:
                                more_msg = with_bar("document", (
                                    f"✅ Carte {scanned_cnt}/{pax_total} scannée !\n\n"
                                    f"📸 Envoyez la *carte suivante* (passager {scanned_cnt+1}).\n"
                                    f"✏️ Ou tapez *fini* si vous avez tout envoyé."
                                ))
                            send_whatsapp_text(phone, more_msg)
                            conv["current_step"] = "document"
                            return jsonify({"status": "ok"}), 200

                        # Si vol avec escale et 1 seul passager → peut avoir 2 cartes (2 segments)
                        if conv["data"].get("flight_type") == "connection" and pax_total == 1 and scanned_cnt == 1:
                            if lang == "en":
                                seg_msg = with_bar("document", (
                                    f"✅ Segment 1 scanned!\n\n"
                                    f"📸 Do you have a *second boarding pass* (connecting flight)?\n"
                                    f"✏️ Type *fini* if this was your only card."
                                ))
                            else:
                                seg_msg = with_bar("document", (
                                    f"✅ Segment 1 scanné !\n\n"
                                    f"📸 Avez-vous une *deuxième carte* (vol de correspondance) ?\n"
                                    f"✏️ Tapez *fini* si c'est votre seule carte."
                                ))
                            send_whatsapp_text(phone, seg_msg)
                            conv["current_step"] = "document"
                            return jsonify({"status": "ok"}), 200

                        # Trier les docs scannés par heure de départ (ordre chronologique)
                        _sort_scanned_docs(conv)

                        # Aller-retour ? (2 trajets détectés)
                        if info.get("return_flight_number") or info.get("return_date"):
                            if lang == "en":
                                body = with_bar("trip_select", (
                                    f"🔄 Your booking has *2 trips*:\n\n"
                                    f"1️⃣ *{info.get('origin','?')} → {info.get('destination','?')}* "
                                    f"({info.get('date','?')}) — {fn}\n"
                                    f"2️⃣ *{info.get('destination','?')} → {info.get('origin','?')}* "
                                    f"({info.get('return_date','?')})\n\n"
                                    f"Which trip are you claiming for?"
                                ))
                                orig = info.get('origin','?')[:3]
                                dest = info.get('destination','?')[:3]
                                buttons = [
                                    {"id": "trip_outbound", "title": f"1️⃣ {orig} → {dest}"},
                                    {"id": "trip_return",   "title": f"2️⃣ {dest} → {orig}"},
                                    {"id": "trip_both",     "title": "🔄 Both trips"},
                                ]
                            else:
                                body = with_bar("trip_select", (
                                    f"🔄 Votre réservation contient *2 trajets* :\n\n"
                                    f"1️⃣ *{info.get('origin','?')} → {info.get('destination','?')}* "
                                    f"({info.get('date','?')}) — {fn}\n"
                                    f"2️⃣ *{info.get('destination','?')} → {info.get('origin','?')}* "
                                    f"({info.get('return_date','?')})\n\n"
                                    f"Pour quel trajet réclamez-vous ?"
                                ))
                                orig = info.get('origin','?')[:3]
                                dest = info.get('destination','?')[:3]
                                buttons = [
                                    {"id": "trip_outbound", "title": f"1️⃣ {orig} → {dest}"},
                                    {"id": "trip_return",   "title": f"2️⃣ {dest} → {orig}"},
                                    {"id": "trip_both",     "title": "🔄 Les deux trajets"},
                                ]
                            send_whatsapp_buttons(phone, body, buttons)
                            conv["current_step"] = "trip_select"
                            return jsonify({"status": "ok"}), 200

                        # Confirmer les infos lues
                        airline_display = conv["data"].get("airline","?")
                        route_display   = conv["data"].get("route","")
                        pnr_display = conv["data"].get("pnr", "")
                        if lang == "en":
                            confirm = with_bar("doc_confirm", (
                                f"✅ *Document read!*\n\n"
                                + (f"🔑 PNR : *{pnr_display}*\n" if pnr_display else "") +
                                f"✈️ Flight: *{fn or '?'}* — {airline_display}\n"
                                f"📅 Date: *{conv['data'].get('flight_date','?')}*\n"
                                f"👤 Passenger: *{info.get('passenger_name','?')}*\n"
                                + (f"🗺️ Route: *{route_display}*\n" if route_display else "") +
                                f"\nIs this correct?"
                            ))
                        else:
                            confirm = with_bar("doc_confirm", (
                                f"✅ *Document lu !*\n\n"
                                + (f"🔑 PNR : *{pnr_display}*\n" if pnr_display else "") +
                                f"✈️ Vol : *{fn or '?'}* — {airline_display}\n"
                                f"📅 Date : *{conv['data'].get('flight_date','?')}*\n"
                                f"👤 Passager : *{info.get('passenger_name','?')}*\n"
                                + (f"🗺️ Trajet : *{route_display}*\n" if route_display else "") +
                                f"\nC'est correct ?"
                            ))
                        buttons = [
                            {"id": "doc_confirm_yes", "title": "✅ Oui" if lang=="fr" else "✅ Yes"},
                            {"id": "doc_confirm_no",  "title": "✏️ Corriger" if lang=="fr" else "✏️ Correct it"},
                        ]
                        send_whatsapp_buttons(phone, confirm, buttons)
                        conv["current_step"] = "doc_confirm"
                        return jsonify({"status": "ok"}), 200
                except Exception as e:
                    print(f"[SCAN] erreur parsing: {e}")

            # Scan échoué → message d'excuse puis collecte manuelle
            if lang == "en":
                send_whatsapp_text(phone, with_bar("document", (
                    "😕 *Sorry, the image quality didn't allow automatic reading.*\n"
                    "Try again with more light, or answer the questions below — it only takes 2 minutes. 👇"
                )))
            else:
                send_whatsapp_text(phone, with_bar("document", (
                    "😕 *Désolé, la qualité de l'image n'a pas permis une lecture automatique.*\n"
                    "Essayez avec plus de lumière, ou répondez aux questions ci-dessous — ça prend 2 minutes. 👇"
                )))
            time.sleep(1)
            _advance_after_scan(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── RÉCEPTION PASSEPORT — MSG 13 ─────────────────────────
        if current_step == "passport_collect" and image_data:
            passports = conv["data"].get("passports_collected", [])
            names = conv["data"].get("passenger_names", [])
            idx   = len(passports)
            name  = names[idx] if idx < len(names) else f"Passager {idx+1}"
            passports.append({"name": name, "received": True})
            conv["data"]["passports_collected"] = passports
            # Airtable — upload pièce d'identité + sauvegarde après document reçu
            if F_PIECE_IDENTITE:
                at_attach_image(phone, conv, image_data, F_PIECE_IDENTITE, idx0=idx, tag="passeport")
            upsert_airtable(phone, conv)
            if lang == "en":
                send_whatsapp_text(phone, with_bar("passport_collect", f"✅ Passport *{name}* received! ({idx+1}/{conv['data'].get('passengers',1)})"))
            else:
                send_whatsapp_text(phone, with_bar("passport_collect", f"✅ Passeport de *{name}* reçu ! ({idx+1}/{conv['data'].get('passengers',1)})"))
            time.sleep(1)
            _ask_passport_next(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── IMAGE : carte d'embarquement justificatif ────────────
        if current_step == "boarding_collect" and image_data:
            conv["data"]["boarding_collected"] = True
            # Airtable — upload carte d'embarquement + sauvegarde après document reçu
            if F_CARTE_EMBARQUEMENT:
                at_attach_image(phone, conv, image_data, F_CARTE_EMBARQUEMENT, idx0=0, tag="carte")
            upsert_airtable(phone, conv)
            if lang == "en":
                send_whatsapp_text(phone, with_bar("boarding_collect", "✅ Boarding pass received!"))
            else:
                send_whatsapp_text(phone, with_bar("boarding_collect", "✅ Carte d'embarquement reçue !"))
            time.sleep(1)
            conv["current_step"] = "ebillet_collect"
            ask_ebillet(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── IMAGE : e-billet ─────────────────────────────────────
        if current_step == "ebillet_collect" and image_data:
            conv["data"]["ebillet_collected"] = True
            if lang == "en":
                send_whatsapp_text(phone, with_bar("ebillet_collect", "✅ Booking confirmation received!"))
            else:
                send_whatsapp_text(phone, with_bar("ebillet_collect", "✅ Confirmation de réservation reçue !"))
            time.sleep(1)
            conv["current_step"] = "certificat_collect"
            ask_certificat_retard(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── IMAGE : certificat de retard ─────────────────────────
        if current_step == "certificat_collect" and image_data:
            conv["data"]["certificat_collected"] = True
            if lang == "en":
                send_whatsapp_text(phone, with_bar("certificat_collect", "✅ Certificate received! Excellent — this will speed up your case."))
            else:
                send_whatsapp_text(phone, with_bar("certificat_collect", "✅ Certificat reçu ! Excellent — il va accélérer votre dossier."))
            time.sleep(1)
            conv["current_step"] = "summary"
            show_summary_and_mandat(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── MANUEL (option sans photo) — passe aux noms ──────────
        if current_step == "document" and txt_lower in ("manuel","manual","manuellement","manually","✏️"):
            _advance_after_scan(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── SCAN MULTI-PAX : fini / passer ───────────────────────
        if current_step == "document" and txt_lower in ("fini","done","terminé","termine","passer","skip","c'est tout","that's all"):
            scanned = conv["data"].get("scanned_docs", [])
            if scanned:
                _sort_scanned_docs(conv)
                # Afficher récap des cartes scannées
                if lang == "en":
                    lines = "\n".join(f"✅ {d.get('flight_number','?')} — {d.get('passenger_name','?')} ({d.get('date','?')})" for d in scanned)
                    send_whatsapp_text(phone, f"📋 *All scanned cards:*\n{lines}\n\n{'🗺️ Route: ' + conv['data'].get('route','') if conv['data'].get('route') else ''}")
                else:
                    lines = "\n".join(f"✅ {d.get('flight_number','?')} — {d.get('passenger_name','?')} ({d.get('date','?')})" for d in scanned)
                    send_whatsapp_text(phone, f"📋 *Cartes scannées :*\n{lines}\n\n{'🗺️ Trajet : ' + conv['data'].get('route','') if conv['data'].get('route') else ''}")
                time.sleep(1)
            _advance_after_scan(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── CONFIRMATION DOCUMENT (texte) ────────────────────────
        if current_step == "doc_confirm":
            if txt_lower in ("oui","yes","ok","correct","c'est bon","👍","yep"):
                _handle_doc_confirm(phone, True, conv)
            else:
                _handle_doc_confirm(phone, False, conv)
            return jsonify({"status": "ok"}), 200

        # ── CORRECTION DOCUMENT ──────────────────────────────────
        if current_step == "doc_correction":
            fn_match = re.search(r'\b([A-Z]{2}\d{2,4})\b', message_text.upper())
            if fn_match:
                fn = fn_match.group(1)
                conv["data"]["flight_number"] = fn
                guessed = guess_airline(fn)
                if guessed: conv["data"]["airline"] = guessed
            date_match = re.search(r'\b(\d{1,2})[/\-\. ](\d{1,2})[/\-\. ](\d{4})\b', message_text)
            if date_match:
                conv["data"]["flight_date"] = f"{date_match.group(1)}/{date_match.group(2)}/{date_match.group(3)}"
            if lang == "en":
                send_whatsapp_text(phone, "✅ Updated! Let's continue.")
            else:
                send_whatsapp_text(phone, "✅ Mis à jour ! On continue.")
            _advance_after_scan(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── SÉLECTION TRAJET (aller-retour) ──────────────────────
        if current_step == "trip_select":
            _handle_trip_select(phone, "trip_outbound", conv)
            return jsonify({"status": "ok"}), 200

        # ── COLLECTE PASSAGER UN PAR UN — MSG 9 ──────────────────
        if current_step == "passenger_collect":
            name = message_text.strip()
            if len(name) >= 2:
                conv["data"]["_temp_pax_name"] = name
                conv["current_step"]           = "passenger_confirm"
                confirm_passenger(phone, conv, name)
            else:
                if lang == "en":
                    send_whatsapp_text(phone, "✍️ Please type the first name and last name (eg: Jean Dupont):")
                else:
                    send_whatsapp_text(phone, "✍️ Tapez le prénom et le nom (ex : Jean Dupont) :")
            return jsonify({"status": "ok"}), 200

        if current_step == "passenger_confirm":
            if txt_lower in ("oui","yes","ok","correct","c'est bon","👍"):
                name = conv["data"].pop("_temp_pax_name", "")
                if name:
                    conv["data"]["passenger_names"].append(name)
                idx = conv["data"].get("current_pax_index", 0) + 1
                conv["data"]["current_pax_index"] = idx
                conv["current_step"] = "passenger_collect"
                ask_next_passenger(phone, conv)
            else:
                conv["current_step"] = "passenger_collect"
                idx = conv["data"].get("current_pax_index", 0)
                num = idx + 1
                if lang == "en":
                    send_whatsapp_text(phone, f"✍️ Passenger {num} — Type the correct first name and last name:")
                else:
                    send_whatsapp_text(phone, f"✍️ Passager {num} — Tapez le prénom et nom corrects :")
            return jsonify({"status": "ok"}), 200

        # ── SAISIE NUMÉRO DE VOL — MSG 10 ────────────────────────
        if current_step == "flight_number":
            fn_match = re.search(r'\b([A-Z]{2,3}\d{1,4})\b', message_text.upper())
            fn = fn_match.group(1) if fn_match else message_text.strip().upper()
            conv["data"]["flight_number"] = fn
            guessed = guess_airline(fn)
            if guessed and not conv["data"].get("airline"):
                conv["data"]["airline"] = guessed
            conv["current_step"] = "flight_number_confirm"
            confirm_flight_number(phone, conv, fn, conv["data"].get("airline"))
            return jsonify({"status": "ok"}), 200

        # ── SAISIE DATE — MSG 10 ─────────────────────────────────
        if current_step == "flight_date":
            date_match = (
                re.search(r'\b(\d{1,2})[/\-\. ](\d{1,2})[/\-\. ](\d{4})\b', message_text) or
                re.search(r'\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b', message_text)
            )
            if date_match:
                grps = date_match.groups()
                if len(grps[0]) == 4:
                    date_str = f"{grps[2]}/{grps[1]}/{grps[0]}"
                else:
                    date_str = f"{grps[0]}/{grps[1]}/{grps[2]}"
            else:
                date_str = message_text.strip()
            conv["data"]["flight_date"] = date_str
            conv["current_step"]        = "flight_date_confirm"
            confirm_flight_date(phone, conv, date_str)
            return jsonify({"status": "ok"}), 200

        # ── SÉLECTION MINEURS (validation) — MSG 11 ──────────────
        if current_step == "minor_select":
            if txt_lower in ("ok", "done", "fini", "c'est bon", "terminé"):
                minors = conv["data"].get("minor_names", [])
                conv["data"]["minors_count"] = len(minors)
                if len(minors) == conv["data"].get("passengers") and conv["data"].get("passengers") == 1:
                    conv["data"]["dossier_status"] = "escalade_expert"
                    conv["data"]["escalade_reason"] = "mineur_seul"
                    if lang == "en":
                        send_whatsapp_text(phone, (
                            "👶 *Minor passenger travelling alone — parental signature required.*\n\n"
                            "The law requires a parent or legal guardian to sign the representation mandate for a minor.\n\n"
                            "We'll handle this file with you directly.\n\n"
                            "📱 *A Robin des Airs expert will call you back within 24h* to guide you through the procedure.\n\n"
                            "_Robin des Airs team_"
                        ))
                    else:
                        send_whatsapp_text(phone, (
                            "👶 *Passager mineur voyageant seul — signature parentale requise.*\n\n"
                            "La loi exige qu'un parent ou tuteur légal signe le mandat de représentation pour un mineur.\n\n"
                            "Nous allons traiter ce dossier avec vous directement.\n\n"
                            "📱 *Un expert Robin des Airs vous rappelle dans les 24h* pour accompagner la procédure.\n\n"
                            "_L'équipe Robin des Airs_"
                        ))
                    return jsonify({"status": "ok"}), 200
                conv["current_step"] = "recap"
                show_recap(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── PASSER PASSEPORT — MSG 13 ────────────────────────────
        if current_step == "passport_collect":
            if txt_lower in ("passer", "skip", "plus tard", "later"):
                passports = conv["data"].get("passports_collected", [])
                names     = conv["data"].get("passenger_names", [])
                idx       = len(passports)
                name      = names[idx] if idx < len(names) else f"Passager {idx+1}"
                passports.append({"name": name, "received": False, "skipped": True})
                conv["data"]["passports_collected"] = passports
                if lang == "en":
                    send_whatsapp_text(phone, f"⏭️ Passport *{name}* — to send later by email. Noted.")
                else:
                    send_whatsapp_text(phone, f"⏭️ Passeport *{name}* — à envoyer plus tard par email. Noté.")
                time.sleep(1)
                _ask_passport_next(phone, conv)
            return jsonify({"status": "ok"}), 200

        if current_step == "boarding_collect":
            if txt_lower in ("appel", "call", "perdu", "lost", "je n'ai plus", "i lost"):
                conv["data"]["dossier_status"]  = "escalade_expert"
                conv["data"]["escalade_reason"] = "document_perdu"
                if lang == "en":
                    send_whatsapp_text(phone, "📞 *No worries — our expert can help you retrieve your documents.*\n\nLeave this conversation open. An expert will contact you directly to help.\n\n_Robin des Airs team_")
                else:
                    send_whatsapp_text(phone, "📞 *Pas de panique — notre expert peut vous aider à retrouver vos documents.*\n\nLaissez cette conversation ouverte. Un expert vous contacte directement pour vous aider.\n\n_L'équipe Robin des Airs_")
                return jsonify({"status": "ok"}), 200
            if txt_lower in ("passer", "skip", "plus tard", "later"):
                conv["data"]["boarding_collected"] = False
                if lang == "en": send_whatsapp_text(phone, "⏭️ Boarding pass — to send later by email. Noted.")
                else: send_whatsapp_text(phone, "⏭️ Carte d'embarquement — à envoyer plus tard par email. Noté.")
                time.sleep(1)
                conv["current_step"] = "ebillet_collect"
                ask_ebillet(phone, conv)
            return jsonify({"status": "ok"}), 200

        if current_step == "ebillet_collect":
            if txt_lower in ("appel", "call", "perdu", "lost", "je ne trouve pas", "can't find"):
                conv["data"]["dossier_status"]  = "escalade_expert"
                conv["data"]["escalade_reason"] = "document_perdu"
                if lang == "en":
                    send_whatsapp_text(phone, "📞 *Our expert will help you find your booking confirmation.*\n\nCheck your spam folder or your travel app (Booking, Expedia, airline website).\nIf you still can't find it — leave this conversation open, an expert contacts you.\n\n_Robin des Airs team_")
                else:
                    send_whatsapp_text(phone, "📞 *Notre expert peut vous aider à retrouver votre confirmation.*\n\nVérifiez vos spams ou votre appli voyage (Booking, Expedia, site de la compagnie).\nSi vous ne trouvez toujours pas — laissez cette conversation ouverte, un expert vous contacte.\n\n_L'équipe Robin des Airs_")
                return jsonify({"status": "ok"}), 200
            if txt_lower in ("passer", "skip", "plus tard", "later"):
                conv["data"]["ebillet_collected"] = False
                if lang == "en": send_whatsapp_text(phone, "⏭️ Booking confirmation — to send later by email. Noted.")
                else: send_whatsapp_text(phone, "⏭️ Confirmation de réservation — à envoyer plus tard par email. Noté.")
                time.sleep(1)
                conv["current_step"] = "certificat_collect"
                ask_certificat_retard(phone, conv)
            return jsonify({"status": "ok"}), 200

        if current_step == "certificat_collect":
            if txt_lower in ("passer", "skip", "plus tard", "later", "non", "no", "j'en ai pas", "i don't have one"):
                conv["data"]["certificat_collected"] = False
                if lang == "en": send_whatsapp_text(phone, "⏭️ No certificate — noted. Most common case, no worries!")
                else: send_whatsapp_text(phone, "⏭️ Pas de certificat — noté. C'est le cas le plus fréquent, pas de souci !")
                time.sleep(1)
                conv["current_step"] = "summary"
                show_summary_and_mandat(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── SAISIE ROUTE (modification) ──────────────────────────
        if current_step == "route_input":
            conv["data"]["route"]    = message_text.strip()
            conv["current_step"]     = "recap"
            show_recap(phone, conv)
            return jsonify({"status": "ok"}), 200

        # ── FALLBACK IA — CLIENT HORS FLUX ───────────────────────
        send_ia_fallback(phone, lang)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error"}), 500


def _handle_trip_select(phone, btn_id, conv):
    """Sélection trajet aller-retour → confirme et continue vers la suite du scan/noms."""
    touch_activity(conv)
    if btn_id == "trip_return":
        rt = conv["data"].get("route","")
        if "→" in rt:
            parts = rt.split("→")
            conv["data"]["route"] = f"{parts[1].strip()} → {parts[0].strip()}"
    conv["data"]["boarding_pass_confirmed"] = True
    _advance_after_scan(phone, conv)


def _handle_doc_confirm(phone, yes, conv):
    """Confirmation des données lues sur le document → suite (noms ou vol/date)."""
    touch_activity(conv)
    lang = conv["data"].get("language", "fr")
    if yes:
        conv["data"]["boarding_pass_confirmed"] = True
        _advance_after_scan(phone, conv)
    else:
        if lang == "en":
            send_whatsapp_text(phone, "✍️ What would you like to correct?\nType the correct flight number and/or date.")
        else:
            send_whatsapp_text(phone, "✍️ Qu'est-ce que vous voulez corriger ?\nTapez le numéro de vol et/ou la date corrects.")
        conv["current_step"] = "doc_correction"


# ============================================
# ENDPOINTS UTILITAIRES
# ============================================

@app.route("/test_flow/<phone>", methods=["GET"])
def test_flow(phone):
    conv = get_or_create_conversation(phone)
    conv["ref_dossier"]  = generate_ref_dossier(phone)
    conv["current_step"] = "welcome"
    send_welcome_hook(phone, conv)
    return jsonify({"status": "started", "phone": phone}), 200

@app.route("/dossier_status/<phone>", methods=["GET"])
def dossier_status(phone):
    conv = conversations.get(phone)
    if not conv:
        return jsonify({"status": "not_found"}), 404
    d = conv["data"]
    return jsonify({
        "phone": phone,
        "dossier_status": d.get("dossier_status", "en_cours"),
        "non_eligibility_reason": d.get("non_eligibility_reason"),
        "escalade_reason": d.get("escalade_reason"),
        "current_step": conv.get("current_step"),
        "ref_dossier": conv.get("ref_dossier"),
        "stat_variant": d.get("stat_variant"),
        "passengers": d.get("passengers"),
        "flight_number": d.get("flight_number"),
        "flight_date": d.get("flight_date"),
        "airline": d.get("airline"),
        "route": d.get("route"),
        "incident_type": d.get("incident_type"),
        "passenger_names": d.get("passenger_names", []),
        "preferred_language": d.get("preferred_language"),
        "docs": {
            "passports": len([p for p in d.get("passports_collected", []) if p.get("received")]),
            "passports_skipped": len([p for p in d.get("passports_collected", []) if p.get("skipped")]),
            "boarding_collected": d.get("boarding_collected"),
            "ebillet_collected": d.get("ebillet_collected"),
            "certificat_collected": d.get("certificat_collected"),
            "scanned_docs": len(d.get("scanned_docs", [])),
        },
        "last_activity": conv.get("last_activity", conv["created"]).isoformat(),
    }), 200


@app.route("/conversations", methods=["GET"])
def list_conversations():
    result = {}
    for phone, conv in conversations.items():
        result[phone] = {
            "step": conv.get("current_step"),
            "data": conv["data"],
            "messages": len(conv["messages"]),
            "created": conv["created"].isoformat(),
            "last_activity": conv.get("last_activity", conv["created"]).isoformat(),
        }
    return jsonify(result), 200

def _relance_message(conv, lang, montant):
    """Choisit le message de relance selon le nombre de relances déjà envoyées."""
    count = conv.get("relance_count", 0)
    ref   = conv.get("ref_dossier", "")
    ref_str = f" (réf. {ref})" if ref else ""

    # Variantes de relance 2 (preuve sociale) — rotation par stat_variant ou aléatoire
    import random as _random
    r2_idx = conv["data"].get("stat_variant", _random.randrange(3)) % 3

    if lang == "en":
        relance2_variants = [
            f"💬 *Passengers like you are already getting paid.*\n\nThis week, a Paris-Dakar traveller received *1 350€* thanks to Robin des Airs.\n\nYour file{ref_str} is still open. Type *menu* 👇",
            f"👨‍👩‍👧 *A family of 3 just recovered 1 800€ this week.*\n\nTheir file took less than 5 minutes to open.\n\nYours{ref_str} is ready. Type *menu* 👇",
            f"✍️ *A passenger from Abidjan-Paris just signed their mandate.*\n\nYours{ref_str} is waiting — don't leave money on the table.\n\nType *menu* 👇",
        ]
        messages = [
            f"✈️ *Your file is waiting!*\n\nYou were 2 minutes away from claiming *{montant}€*.\n\nType *menu* to pick up where you left off 👇",
            relance2_variants[r2_idx],
            f"⏳ *Last chance today.*\n\nWe can only keep this conversation open a little longer. Don't let the airline keep your money.\n\nType *menu* now 👇\n\n_Robin des Airs team_",
        ]
    else:
        relance2_variants = [
            f"💬 *Des passagers comme vous ont déjà récupéré leur argent.*\n\nCette semaine, un voyageur Paris-Dakar a reçu *1 350€* grâce à Robin des Airs.\n\nVotre dossier{ref_str} est toujours ouvert. Tapez *menu* 👇",
            f"👨‍👩‍👧 *Une famille de 3 vient de récupérer 1 800€ cette semaine.*\n\nLeur dossier a été ouvert en moins de 5 minutes.\n\nLe vôtre{ref_str} est prêt. Tapez *menu* 👇",
            f"✍️ *Un passager Abidjan-Paris vient de signer son mandat.*\n\nLe vôtre{ref_str} vous attend — ne laissez pas cet argent à la compagnie.\n\nTapez *menu* 👇",
        ]
        messages = [
            f"✈️ *Votre dossier vous attend !*\n\nVous étiez à 2 minutes de réclamer *{montant}€*.\n\nTapez *menu* pour reprendre là où vous vous êtes arrêté 👇",
            relance2_variants[r2_idx],
            f"⏳ *Dernière chance aujourd'hui.*\n\nNous ne pouvons garder cette conversation ouverte que peu de temps encore. Ne laissez pas la compagnie garder votre argent.\n\nTapez *menu* maintenant 👇\n\n_L'équipe Robin des Airs_",
        ]
    return messages[min(count, len(messages)-1)]


@app.route("/check_abandoned", methods=["GET"])
def check_abandoned():
    """Liste les conversations abandonnées (step != completed) selon les paliers RELANCE_THRESHOLDS_HOURS.
    Pour un cron externe. Avec ?send=1, envoie la relance correspondant au prochain palier dû."""
    now = datetime.now()
    do_send = request.args.get("send") in ("1", "true", "yes")
    abandoned = []
    for phone, conv in conversations.items():
        step = conv.get("current_step")
        if step in (None, "completed"):
            continue
        last = conv.get("last_activity", conv["created"])
        idle_hours = (now - last).total_seconds() / 3600.0
        count = conv.get("relance_count", 0)

        # Plus de relances disponibles
        if count >= len(RELANCE_THRESHOLDS_HOURS):
            continue
        # Le palier de la prochaine relance n'est pas encore atteint
        threshold = RELANCE_THRESHOLDS_HOURS[count]
        if idle_hours < threshold:
            continue

        d   = conv["data"]
        lang = d.get("language", "fr")
        pax = d.get("passengers") or 1
        montant = 600 * pax
        relance = _relance_message(conv, lang, montant)
        entry = {
            "phone": phone,
            "step": step,
            "idle_hours": round(idle_hours, 1),
            "threshold": threshold,
            "montant": montant,
            "relance_count": count,
            "relance": relance,
        }
        if do_send:
            try:
                code = send_whatsapp_text(phone, relance, skip_outbound_dedup=True)
                entry["sent_status"] = code
                conv["relance_count"] = count + 1
            except Exception as e:
                entry["sent_status"] = f"error: {e}"
        abandoned.append(entry)
    return jsonify({
        "count": len(abandoned),
        "thresholds_hours": RELANCE_THRESHOLDS_HOURS,
        "abandoned": abandoned,
    }), 200

@app.route("/reset/<phone>", methods=["GET"])
def reset(phone):
    conversations.pop(phone, None)
    return jsonify({"status": "reset", "phone": phone}), 200

@app.route("/test", methods=["GET"])
def test():
    return jsonify({
        "status": "running",
        "version": "v9 — flux v8 optimisé + Airtable + codeshare lookup",
        "active_conversations": len(conversations),
        "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
        "openai":   "OK" if OPENAI_API_KEY else "MISSING",
        "wati":     "OK" if WATI_API_TOKEN else "MISSING",
        "wati_post_submit_template": WATI_POST_SUBMIT_TEMPLATE_NAME or None,
        "wati_template_channel_configured": bool(WATI_TEMPLATE_CHANNEL_NUMBER),
        "airtable_f_carte_embarquement": F_CARTE_EMBARQUEMENT or None,
        "airtable_f_identite": F_PIECE_IDENTITE or None,
        "airtable_f_mandat_signe": F_MANDAT_SIGNE or None,
        "airtable_f_stop_relance": F_STOP_RELANCE or None,
        "airtable_f_sequence_active": F_SEQUENCE_ACTIVE or None,
        "mandat_signed_webhook": "on" if MANDAT_SIGNED_WEBHOOK_SECRET else "off",
    }), 200

@app.route("/mandat_signed", methods=["POST"])
def mandat_signed_webhook():
    """
    Notification signature mandat (site / Make / Zapier).
    Body JSON : {"ref":"RDA-…","secret":"…"} — secret = MANDAT_SIGNED_WEBHOOK_SECRET.
    Optionnel : "waId"/"phone" pour désambigüiser.
    """
    if not MANDAT_SIGNED_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "MANDAT_SIGNED_WEBHOOK_SECRET not configured"}), 503
    data = request.get_json(silent=True) or {}
    got = (data.get("secret") or request.headers.get("X-Mandat-Secret") or "").strip()
    try:
        if not secrets.compare_digest(got, MANDAT_SIGNED_WEBHOOK_SECRET):
            return jsonify({"ok": False, "error": "forbidden"}), 403
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    ref = (data.get("ref") or "").strip()
    phone_hint = (data.get("waId") or data.get("phone") or "").strip()
    if not ref:
        return jsonify({"ok": False, "error": "ref required"}), 400

    def _digits(s):
        return re.sub(r"\D", "", s or "")

    h_norm = _digits(phone_hint)
    updated = 0
    for p, c in list(conversations.items()):
        if c.get("ref_dossier") != ref:
            continue
        if h_norm:
            p_norm = _digits(str(p))
            if p_norm != h_norm and not (p_norm.endswith(h_norm) or h_norm.endswith(p_norm)):
                continue
        ps = (c.get("data") or {}).setdefault("_post_submit", {})
        ps["mandate_signed_server"] = True
        ps["active"] = False
        c["data"]["dossier_status"] = "complet"
        upsert_airtable(p, c)
        updated += 1
    return jsonify({"ok": True, "updated": updated}), 200

@app.route("/post_submit/cancel/<phone>", methods=["GET", "POST"])
def post_submit_cancel(phone):
    """Arrête uniquement la séquence de relances post-soumission pour ce numéro WhatsApp."""
    want = (os.environ.get("POST_SUBMIT_CANCEL_SECRET") or "").strip()
    if want:
        got = (request.args.get("secret") or request.headers.get("X-Cancel-Secret") or "").strip()
        if got != want:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    conv = conversations.get(phone)
    if not conv:
        return jsonify({"ok": False, "reason": "no_conversation", "phone": phone}), 404
    ps = (conv.get("data") or {}).get("_post_submit")
    if not isinstance(ps, dict):
        return jsonify({"ok": False, "reason": "no_post_submit", "phone": phone}), 200
    ps["active"] = False
    ps["cancelled_at"] = time.time()
    ps["cancel_reason"] = "operator"
    return jsonify({"ok": True, "phone": phone, "ref": conv.get("ref_dossier")}), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v9 - Running!", 200

start_post_submit_reminder_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
