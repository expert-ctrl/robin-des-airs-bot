from flask import Flask, request, jsonify, redirect
import requests
import os
import json
import base64
import zlib
import re
import hashlib
import unicodedata
import time
import threading
import secrets
from html import escape
from datetime import datetime, timedelta, date
from urllib.parse import urlencode, urlparse

app = Flask(__name__)

# ===== CONFIG =====
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN   = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL    = os.environ.get("WATI_BASE_URL", "").rstrip("/")
# Relances hors fenêtre 24 h (Meta) : template WhatsApp approuvé — Wati v2.
WATI_POST_SUBMIT_TEMPLATE_NAME = os.environ.get("WATI_POST_SUBMIT_TEMPLATE_NAME", "").strip()
WATI_POST_SUBMIT_TEMPLATE_BROADCAST = os.environ.get(
    "WATI_POST_SUBMIT_TEMPLATE_BROADCAST", "rda_post_submit_reminder"
).strip()
WATI_TEMPLATE_CHANNEL_NUMBER = os.environ.get("WATI_TEMPLATE_CHANNEL_NUMBER", "").strip()
# Sondage WATI : boutons (≤3) ou liste (≤10) — clic au lieu de taper 1/2/3.
WATI_USE_INTERACTIVE = os.environ.get("WATI_USE_INTERACTIVE", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
POST_SUBMIT_TEMPLATE_REPEAT_H = int(os.environ.get("POST_SUBMIT_TEMPLATE_REPEAT_HOURS", "48") or "48")
META_SESSION_HOURS = int(os.environ.get("META_CUSTOMER_CARE_WINDOW_HOURS", "24") or "24")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appv72lKbQtjt7EIP")

RDA_DOMAIN = os.environ.get("RDA_SITE", os.environ.get("RDA_DOMAIN", "https://robindesairs.eu"))
# Mandat en ligne sur le site (Netlify) — MANDAT_URL par defaut mandat.html
MANDAT_URL = os.environ.get("MANDAT_URL", f"{RDA_DOMAIN.rstrip('/')}/mandat.html")

def _mandat_public_origin():
    """Origine (schéma + hôte) pour le lien court /m — alignée sur MANDAT_URL."""
    pu = urlparse(MANDAT_URL)
    if pu.scheme and pu.netloc:
        return f"{pu.scheme}://{pu.netloc}".rstrip("/")
    return RDA_DOMAIN.rstrip("/")

def mandat_link_compressed(params):
    """
    Lien /m?c=… (JSON + zlib + base64url) → redirection 302 vers mandat.html?…
    """
    filtered = {k: v for k, v in params.items() if v}
    raw = json.dumps(filtered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    blob = zlib.compress(raw, level=9)
    c = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    return f"{_mandat_public_origin()}/m?c={c}", filtered

def mandat_signing_link(params):
    """
    Lien de signature : par défaut toujours la forme compressée /m?c=… (lisible sur WhatsApp).
    Désactiver : MANDAT_PREFER_SHORT_LINK=0 → choix automatique long/court selon la longueur.
    """
    short_url, filtered = mandat_link_compressed(params)
    prefer = os.environ.get("MANDAT_PREFER_SHORT_LINK", "1").strip().lower()
    if prefer not in ("0", "false", "no", "off"):
        return short_url
    long_q = urlencode({k: str(v) for k, v in filtered.items()})
    long_url = f"{MANDAT_URL}?{long_q}" if long_q else MANDAT_URL
    return short_url if len(short_url) <= len(long_url) else long_url
SUIVI_URL   = f"{RDA_DOMAIN.rstrip('/')}/suivi-dossier"
# Politique de confidentialité (URL complète possible via env).
# Optionnel : PRIVACY_HIDE_SHORT_NOTICE=1 (rien sur le 1er écran),
#             PRIVACY_HIDE_DETAILED_BANNER=1 (pas le bloc long avant photo / questions).
#             RDA_TERMS_URL=https://... (sinon URL dérivée de RDA_DOMAIN + /conditions-generales)
PRIVACY_POLICY_URL = os.environ.get("PRIVACY_POLICY_URL", "").strip() or f"{RDA_DOMAIN.rstrip('/')}/politique-confidentialite"
# Conditions générales (CGU) — même logique qu’AirHelp : lien vers le site officiel.
TERMS_URL = os.environ.get("RDA_TERMS_URL", "").strip() or f"{RDA_DOMAIN.rstrip('/')}/conditions-generales"
CLIMBIE_TEL = "+33756863630"

# ===== IDs CHAMPS AIRTABLE (récupérés directement depuis l'API) =====
AT_TABLE_ID         = "tblfg688AGxaywi7O"
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
# Carte d'embarquement (champ Attachment Airtable) — renseigner l'ID fld… dans AIRTABLE_F_CARTE_EMB
F_CARTE_EMBARQUEMENT = os.environ.get("AIRTABLE_F_CARTE_EMB", "").strip()
# Pièces optionnelles pour arrêter les relances quand tout est reçu (Airtable)
F_PIECE_IDENTITE = os.environ.get("AIRTABLE_F_IDENTITE", "").strip()
F_MANDAT_SIGNE = os.environ.get("AIRTABLE_F_MANDAT_SIGNE", "").strip()
# POST JSON {"ref":"RDA-…","secret":"…"} — même secret que côté mandat.html / Make
MANDAT_SIGNED_WEBHOOK_SECRET = os.environ.get("MANDAT_SIGNED_WEBHOOK_SECRET", "").strip()
# Case à cocher Airtable (tblfg688AGxaywi7O) — **une seule** des deux : ID du champ copié depuis Airtable.
# Stop_Relance : coché = arrêter les relances WhatsApp / templates pour ce dossier.
# Sequence_Active : coché = relances autorisées ; **décoché** = arrêt (lire sur la 1ʳᵉ ligne du dossier).
F_STOP_RELANCE = os.environ.get("AIRTABLE_F_STOP_RELANCE", "").strip()
F_SEQUENCE_ACTIVE = os.environ.get("AIRTABLE_F_SEQUENCE_ACTIVE", "").strip()
# Même fld que l’ancien main.py ; surcharge : AIRTABLE_F_ITINERAIRE=fld… ou désactivation : AIRTABLE_F_ITINERAIRE=
F_ITINERAIRE = os.environ.get("AIRTABLE_F_ITINERAIRE", "fldtCISegQZ58Yvrl").strip()

# Options singleSelect EXACTES dans Airtable
INCIDENT_AT = {
    "delay":  "Retard +3h",
    "cancel": "Annulation",
    "denied": "Surbooking",
}
STATUT_DOSSIER_DEFAUT = "Ouvert"
STATUT_SUIVI_DEFAUT   = "Nouveau"

# ===== EU261 =====
EU261_BANDS = {
    "band_250": {"amount_eur": 250, "label": "≤ 1500 km"},
    "band_400": {"amount_eur": 400, "label": "1500–3500 km"},
    "band_600": {"amount_eur": 600, "label": "> 3500 km (Europe-Afrique)"},
}
AMOUNT_DEFAULT     = 600   # long-courrier par défaut
COMMISSION_PCT     = 0.25  # 25% Robin des Airs
NET_PCT            = 0.75  # 75% client

INCIDENT_LABELS = {
    "delay":  "Retard +3h",
    "cancel": "Annulation",
    "denied": "Surbooking",
}
INCIDENT_LABELS_EN = {
    "delay":  "Delay (3h+)",
    "cancel":  "Cancellation",
    "denied":  "Overbooking",
}

AIRLINES_MAP = {
    "1": "Air France",
    "2": "KLM",
    "3": "Brussels Airlines",
    "4": "Lufthansa",
    "5": "TAP Portugal",
    "6": "Corsair",
    "7": "Air Senegal",
    "8": "Royal Air Maroc",
}

# Codes IATA (2–3 lettres) : Europe↔Afrique, hub Proche-Orient, Amériques + code-share fréquents
FLIGHT_PREFIX_TO_AIRLINE = {
    "EJU": "easyJet",
    "BZZ": "Buzz",
    "TUI": "TUI fly",
    "AF": "Air France",
    "KL": "KLM",
    "SN": "Brussels Airlines",
    "LH": "Lufthansa",
    "TP": "TAP Portugal",
    "SS": "Corsair",
    "HC": "Air Senegal",
    "HF": "Air Côte d'Ivoire",
    "AT": "Royal Air Maroc",
    "UX": "Air Europa",
    "IB": "Iberia",
    "BA": "British Airways",
    "U2": "easyJet",
    "FR": "Ryanair",
    "W6": "Wizz Air",
    "VY": "Vueling",
    "EI": "Aer Lingus",
    "LX": "Swiss",
    "OS": "Austrian",
    "EW": "Eurowings",
    "DE": "Condor",
    "TO": "Transavia France",
    "HV": "Transavia",
    "PC": "Pegasus Airlines",
    "TK": "Turkish Airlines",
    "MS": "EgyptAir",
    "ET": "Ethiopian Airlines",
    "SK": "SAS",
    "DY": "Norwegian",
    "XQ": "SunExpress",
    "XQG": "SunExpress",
    "TS": "Air Transat",
    "UA": "United Airlines",
    "DL": "Delta Air Lines",
    "AA": "American Airlines",
    "AC": "Air Canada",
    "QR": "Qatar Airways",
    "EK": "Emirates",
    "GF": "Gulf Air",
    "WY": "Oman Air",
    "SV": "Saudia",
    "RJ": "Royal Jordanian",
    "TU": "Tunisair",
    "AH": "Air Algérie",
    "MD": "Air Madagascar",
    "KP": "ASKY",
    "WB": "RwandAir",
    "KQ": "Kenya Airways",
    "SA": "South African Airways",
    "BT": "airBaltic",
    "LO": "LOT Polish Airlines",
    "OK": "Czech Airlines",
    "RO": "TAROM",
    "FB": "Bulgaria Air",
    "A3": "Aegean Airlines",
    "CY": "Cyprus Airways",
    "KM": "Air Malta",
    "LG": "Luxair",
    # Afrique & vols transcontinentaux vers l’Afrique
    "DT": "TAAG Angola Airlines",
    "W3": "Arik Air",
    "P4": "Air Peace",
    "TM": "LAM Mozambique",
    "UR": "Uganda Airlines",
    "4Y": "Eurowings Discover",
    "BF": "French Bee",
    "J9": "Jazeera Airways",
    "SM": "Air Cairo",
    "NP": "Nile Air",
    "NE": "Nesma Airlines",
    "ZN": "Zambia Airways",
    "G9": "Air Arabia",
    "3O": "Air Arabia Maroc",
    "TX": "Air Caraïbes",
}

def airline_from_iata(code):
    """Code compagnie seul (AF, KL, SS, EJU…) → nom. Base locale uniquement."""
    c = re.sub(r"[^A-Z]", "", (code or "").upper())
    if not c:
        return None
    for plen in (3, 2):
        if len(c) >= plen:
            pref = c[:plen]
            if pref in FLIGHT_PREFIX_TO_AIRLINE:
                return FLIGHT_PREFIX_TO_AIRLINE[pref]
    return None

def airline_guess_from_flight_number(fn):
    """Devine la compagnie à partir du préfixe IATA du n° de vol (ex. SN271 → Brussels Airlines)."""
    fn = (fn or "").strip().upper()
    m = re.match(r"^([A-Z]{2,3})\d", fn)
    if not m:
        return None
    return airline_from_iata(m.group(1))

MONTH_NAMES_FR = (
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
MONTH_NAMES_EN = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

def month_word(month_mm, lang):
    """temp_month '01'..'12' → libellé pour rappels utilisateur."""
    try:
        i = int((month_mm or "0").lstrip("0") or "0")
    except ValueError:
        return month_mm or "?"
    names = MONTH_NAMES_EN if lang == "en" else MONTH_NAMES_FR
    if 1 <= i <= 12:
        return names[i]
    return month_mm or "?"

def _ascii_fold(s):
    """Normalise pour comparer janvier / janv. / accents."""
    if not s:
        return ""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()

_MONTH_ALIASES_FR = {
    "janvier": 1, "janv": 1, "jan": 1,
    "fevrier": 2, "fev": 2, "fevr": 2,
    "mars": 3, "mar": 3, "avril": 4, "avr": 4, "mai": 5,
    "juin": 6, "juillet": 7, "juil": 7,
    "aout": 8, "septembre": 9, "sep": 9, "sept": 9,
    "octobre": 10, "oct": 10, "novembre": 11, "nov": 11,
    "decembre": 12, "dec": 12,
}
_MONTH_ALIASES_EN = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

def month_number_from_word_token(token, lang):
    """1–12 depuis un mot (ex. mai, oct) ou None."""
    t = _ascii_fold((token or "").strip().rstrip("."))
    if not t:
        return None
    aliases = _MONTH_ALIASES_EN if lang == "en" else _MONTH_ALIASES_FR
    if t in aliases:
        return aliases[t]
    for k, v in aliases.items():
        if t.startswith(k) or k.startswith(t):
            return v
    return None

def _expand_two_digit_year(yy):
    """Année sur 2 chiffres → 19xx / 20xx (vols récents privilégiés)."""
    yy = int(yy)
    if yy <= 50:
        return 2000 + yy
    return 1900 + yy

def _disambiguate_slash_parts(d1, d2, lang):
    """d1/d2 sans année : ordre jour/mois vs mois/jour selon plausibilité et langue."""
    if d1 > 12:
        return d1, d2  # jour, mois
    if d2 > 12:
        return d2, d1  # jour, mois (d1 = mois)
    if lang == "en":
        return d2, d1  # MDY → jour=d2, mois=d1
    return d1, d2  # DMY

def _format_flight_date_ddmmyyyy(day, month, year):
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return f"{day:02d}/{month:02d}/{year}"

def looks_like_full_date_input(text):
    """JJ/MM/AAAA (etc.) : ne pas confondre avec un simple choix « 11 »."""
    s = (text or "").strip()
    if len(s) < 6:
        return False
    if re.match(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$", s):
        return True
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}\s*$", s):
        return True
    if re.match(
        r"^\d{1,2}\s+[a-zA-ZéèêëàùâîôûçÉÈÊËÀÙÂÎÔÛÇ]+(?:\s+\d{2,4})?\s*$",
        s,
        re.I,
    ):
        return True
    if re.match(r"^[a-zA-Z]+\s+\d{1,2},?\s+\d{2,4}\s*$", s, re.I):
        return True
    return False

def try_parse_flight_date_message(text, lang):
    """
    Date complète en un message (sans carte) : JJ/MM/AAAA, AAAA-MM-JJ, 12 mai 2024, etc.
    Retourne JJ/MM/AAAA ou None.
    """
    s = (text or "").strip()
    if len(s) < 6:
        return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s*$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _format_flight_date_ddmmyyyy(d, mo, y)

    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*$", s)
    if m:
        d1, d2 = int(m.group(1)), int(m.group(2))
        y_raw = m.group(3)
        y = int(y_raw)
        if len(y_raw) == 2:
            y = _expand_two_digit_year(y)
        day, month = _disambiguate_slash_parts(d1, d2, lang)
        return _format_flight_date_ddmmyyyy(day, month, y)

    # "12 janvier 2024" / "12 janv 24"
    m = re.match(
        r"^(\d{1,2})\s+([a-zA-ZéèêëàùâîôûçÉÈÊËÀÙÂÎÔÛÇ]+)\s+(\d{2,4})\s*$",
        s,
        re.I,
    )
    if m:
        d = int(m.group(1))
        mo = month_number_from_word_token(m.group(2), lang)
        if not mo:
            return None
        y_raw = m.group(3)
        y = int(y_raw)
        if len(y_raw) == 2:
            y = _expand_two_digit_year(y)
        return _format_flight_date_ddmmyyyy(d, mo, y)

    # "january 5, 2024" / "january 5 2024"
    m = re.match(
        r"^([a-zA-Z]+)\s+(\d{1,2}),?\s+(\d{2,4})\s*$",
        s,
        re.I,
    )
    if m and lang == "en":
        mo = month_number_from_word_token(m.group(1), lang)
        if not mo:
            return None
        d = int(m.group(2))
        y_raw = m.group(3)
        y = int(y_raw)
        if len(y_raw) == 2:
            y = _expand_two_digit_year(y)
        return _format_flight_date_ddmmyyyy(d, mo, y)

    # "5 january 2024" (EN)
    m = re.match(
        r"^(\d{1,2})\s+([a-zA-Z]+)\s+(\d{2,4})\s*$",
        s,
        re.I,
    )
    if m and lang == "en":
        d = int(m.group(1))
        mo = month_number_from_word_token(m.group(2), lang)
        if not mo:
            return None
        y_raw = m.group(3)
        y = int(y_raw)
        if len(y_raw) == 2:
            y = _expand_two_digit_year(y)
        return _format_flight_date_ddmmyyyy(d, mo, y)

    # « 15 mai » sans année (souvent en Afrique / WhatsApp) : années récentes plausibles
    m = re.match(
        r"^(\d{1,2})\s+([a-zA-ZéèêëàùâîôûçÉÈÊËÀÙÂÎÔÛÇ]+)\s*$",
        s,
        re.I,
    )
    if m:
        d = int(m.group(1))
        mo = month_number_from_word_token(m.group(2), lang)
        if mo:
            cy = datetime.now().year
            for y in (cy, cy - 1, cy - 2, cy - 3):
                fd = _format_flight_date_ddmmyyyy(d, mo, y)
                if fd:
                    return fd

    return None

def split_itinerary_for_mandat(itin):
    """Découpe une ligne d'itinéraire pour préremplir départ / arrivée / escale(s)."""
    itin = (itin or "").strip()
    if not itin:
        return "", "", ""
    compact = re.sub(r"\s+", "", itin)
    m = re.match(r"^([A-Za-z]{3})[-–—>]([A-Za-z]{3})$", compact)
    if m:
        return m.group(1).upper(), m.group(2).upper(), ""
    if re.match(r"^[A-Za-z]{6}$", compact):
        return compact[:3].upper(), compact[3:].upper(), ""
    parts = re.split(r"\s*(?:→|->|—|–|\s-\s| vers | to )\s*", itin, flags=re.I)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    dep, arr = parts[0], parts[-1]
    via = " ; ".join(parts[1:-1])
    return dep, arr, via

def try_set_itinerary_from_freeform(conv, text):
    """Interprète une saisie libre (ex. BRU → CDG → ABJ ou BRU CDG ABJ) et remplit data.itinerary."""
    raw = (text or "").strip()
    if len(raw) < 2:
        return False
    d = conv["data"]
    if "|" in text:
        a, b = text.split("|", 1)
        raw = a.strip()
        if b.strip():
            d["itinerary_compl_note"] = b.strip()[:220]
    it0 = _unify_route_display(raw)
    parts = re.split(r"\s*(?:→|->|—|–|\s-\s| vers | to )\s*", it0, flags=re.I)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        tokens = re.findall(r"\b[A-Za-z]{3}\b", raw.upper())
        if len(tokens) >= 2:
            parts = list(tokens)
        else:
            return False
    if len(parts) < 2:
        return False
    d["itinerary"] = " → ".join(parts)
    dep, arr, _ = split_itinerary_for_mandat(d["itinerary"])
    return bool(dep and arr)

# ===== FLUX (étapes) =====
# 1. passengers → pax_ack_route (direct / escale) → langue contact expert → confirmation vocale
# 2. incident_type …

STEPS = [
    "passengers", "pax_ack_route", "pax_contact_lang", "pax_voice_confirm",
    "incident_type", "boarding_after_pax", "airline", "airline_other",
    "pnr_input", "flight_number",
    "flight_date", "flight_month", "flight_day",
    "itinerary_kind", "itinerary_rt_pick", "itinerary_freeline",
    "itinerary_dep", "itinerary_arr",
    "carte_confirm", "carte_pick_field", "carte_edit_value",
    "passenger_names", "passenger_name_post_add", "passenger_names_confirm", "minor_check",
    "summary", "completed",
]

# Progression en tête des messages WhatsApp : 🟢 / ⚪ (7 segments max, hors completed / paused).
TUNNEL_PROGRESS_FILLED = "🟢"
TUNNEL_PROGRESS_EMPTY = "⚪"
TUNNEL_PROGRESS_VISUAL_SEGMENTS = 7
TUNNEL_PROGRESS_BUCKETS = (
    ("passengers",),
    ("pax_ack_route",),
    ("pax_contact_lang",),
    ("pax_voice_confirm",),
    ("incident_type",),
    ("boarding_after_pax",),
    ("airline", "airline_other"),
    ("pnr_input",),
    ("flight_number",),
    ("flight_date", "flight_month", "flight_day"),
    (
        "itinerary_kind",
        "itinerary_rt_pick",
        "itinerary_freeline",
        "itinerary_dep",
        "itinerary_arr",
    ),
    ("carte_confirm", "carte_pick_field", "carte_edit_value"),
    ("passenger_names", "passenger_name_post_add"),
    ("passenger_names_confirm", "minor_check", "summary"),
)
TUNNEL_PROGRESS_TOTAL = len(TUNNEL_PROGRESS_BUCKETS)
_STEP_TO_TUNNEL_PROGRESS = {}
for _i, _bucket in enumerate(TUNNEL_PROGRESS_BUCKETS, start=1):
    for _st in _bucket:
        _STEP_TO_TUNNEL_PROGRESS[_st] = (_i, TUNNEL_PROGRESS_TOTAL)

def tunnel_progress_prefix(conv):
    if not conv or not isinstance(conv, dict):
        return ""
    step = conv.get("step")
    if not step or step in ("completed", "paused"):
        return ""
    info = _STEP_TO_TUNNEL_PROGRESS.get(step)
    if not info:
        return ""
    cur, total = info
    n = TUNNEL_PROGRESS_VISUAL_SEGMENTS
    vis = max(1, min(n, round(cur * n / total)))
    bar = TUNNEL_PROGRESS_FILLED * vis + TUNNEL_PROGRESS_EMPTY * (n - vis)
    return f"{bar}\n"

def _first_line_looks_like_tunnel_progress_bar(line):
    """Évite de préfixer deux fois si le message commence déjà par la barre."""
    s = (line or "").strip()
    if len(s) != TUNNEL_PROGRESS_VISUAL_SEGMENTS:
        return False
    ok = frozenset((TUNNEL_PROGRESS_FILLED, TUNNEL_PROGRESS_EMPTY))
    return all(c in ok for c in s)

# Étapes où une photo de billet remplit la suite : on demande confirmation avant d'enchaîner
# (évite de redemander compagnie / n° de vol déjà lus sur la carte).
CARTE_CONFIRM_ELIGIBLE_STEPS = frozenset({
    "boarding_after_pax",  # preuves : toujours valider la lecture avant d’enchaîner
    "airline", "airline_other", "pnr_input", "flight_number",
    "flight_date", "flight_month", "flight_day",
    "itinerary_kind", "itinerary_rt_pick", "itinerary_freeline",
    "itinerary_dep", "itinerary_arr",
})
# Même pause après « corriger » ou nouvelle photo pendant la vérification.
CARTE_CONFIRM_PAUSE_STEPS = CARTE_CONFIRM_ELIGIBLE_STEPS | frozenset({"carte_confirm", "carte_pick_field", "carte_edit_value"})
CARTE_FIELD_KEYS = ("airline", "flight_number", "flight_date", "pnr", "itinerary", "passenger_names", "operating_airline")

# ===== MEMOIRE =====
conversations    = {}
recent_event_ids = {}
MEMORY_HOURS     = 24
# Liens courts /sign/<token> → URL mandat (évite URL compressée longue dans WhatsApp)
SIGN_REDIRECTS   = {}
SIGN_REDIRECT_TTL_S = 86400
SIGN_REDIRECT_MAX  = 5000

def _sign_redirect_cleanup():
    now = time.time()
    for k, ent in list(SIGN_REDIRECTS.items()):
        if float(ent.get("exp") or 0) < now:
            SIGN_REDIRECTS.pop(k, None)
    while len(SIGN_REDIRECTS) > SIGN_REDIRECT_MAX:
        k = next(iter(SIGN_REDIRECTS))
        SIGN_REDIRECTS.pop(k, None)

def register_mandate_short_link(target_url: str) -> str:
    """Enregistre une redirection courte /sign/<token> → URL mandat réelle (WhatsApp)."""
    u = (target_url or "").strip()
    if not u:
        return ""
    _sign_redirect_cleanup()
    tok = secrets.token_urlsafe(10)
    SIGN_REDIRECTS[tok] = {"url": u, "exp": time.time() + SIGN_REDIRECT_TTL_S}
    return f"{_mandat_public_origin()}/sign/{tok}"

# Langues pour vocaux / suivi (ordre = chiffres 1–N au tunnel ; max 10 lignes liste WATI)
EXPERT_LANG_OPTIONS = [
    ("fr", "🇫🇷", "Français", "French"),
    ("en", "🇬🇧", "Anglais", "English"),
    ("wo", "🇸🇳", "Wolof", "Wolof"),
    ("tw", "🇬🇭", "Twi", "Twi"),
    ("yo", "🇳🇬", "Yoruba", "Yoruba"),
    ("sw", "🇹🇿", "Kiswahili", "Kiswahili"),
    ("pt", "🇵🇹", "Portugais", "Portuguese"),
    ("ar", "🇲🇦", "Arabe", "Arabic"),
    ("ha", "🇳🇬", "Haoussa", "Hausa"),
    ("ma", "🇬🇲", "Mandinka", "Mandinka"),
]

def _expert_lang_display(code: str, lang_ui: str) -> str:
    for c, fl, fr, en in EXPERT_LANG_OPTIONS:
        if c == code:
            lab = fr if lang_ui == "fr" else en
            return f"{fl} {lab}"
    return code or "—"

def _early_route_caption(shape: str, lang_ui: str) -> str:
    if not shape:
        return ""
    if lang_ui == "en":
        return (
            "✈️ *Route type:* direct (no connection)"
            if shape == "direct"
            else "✈️ *Route type:* with connection / stopover"
        )
    return (
        "✈️ *Parcours :* vol *direct* (sans correspondance)"
        if shape == "direct"
        else "✈️ *Parcours :* *avec correspondance / escale*"
    )

def _summary_recap_lines(d: dict, lang_ui: str) -> list:
    """Lignes récap pour le message « dossier prêt » (lisible sur WhatsApp)."""
    lines = []
    L = lambda fr, en: fr if lang_ui == "fr" else en
    pax = d.get("passengers") or 1
    lines.append(pax_recap_line(pax, lang_ui))
    names = d.get("passenger_names") or []
    if names:
        joined = ", ".join(n.strip() for n in names[:8] if (n or "").strip())
        if len(names) > 8:
            joined += "…"
        if joined:
            lines.append(f"🪪 *{L('Noms sur le dossier', 'Names on file')} :* {joined}")
    sh = d.get("early_route_shape")
    if sh:
        lines.append(_early_route_caption(sh, lang_ui))
    code = d.get("expert_phone_lang")
    if code:
        lines.append(f"📞 *{L('Langue des experts (vocal)', 'Expert contact language')} :* {_expert_lang_display(code, lang_ui)}")
    inc = d.get("incident_type")
    if inc:
        lab = INCIDENT_LABELS.get(inc) if lang_ui == "fr" else INCIDENT_LABELS_EN.get(inc)
        if lab:
            lines.append(f"⚖️ *{L('Incident déclaré', 'Reported disruption')} :* {lab}")
    if d.get("airline"):
        lines.append(f"🛫 *{L('Compagnie', 'Airline')} :* {d['airline']}")
    if d.get("operating_airline"):
        lines.append(f"🔧 *{L('Exploitant (si différent)', 'Operating carrier (if different)')} :* {d['operating_airline']}")
    if d.get("flight_number"):
        lines.append(f"🔢 *{L('N° de vol', 'Flight no.')} :* {d['flight_number']}")
    if d.get("flight_date"):
        lines.append(f"📅 *{L('Date du vol', 'Flight date')} :* {d['flight_date']}")
    if d.get("pnr"):
        lines.append(f"🎫 *PNR :* {str(d['pnr']).strip().upper()}")
    it = (d.get("itinerary") or "").strip()
    if it:
        lines.append(f"🌍 *{L('Itinéraire', 'Itinerary')} :* {it}")
    return lines

# PNR / record locator : certaines compagnies utilisent 4 caractères alphanumériques.
MIN_PNR_LEN = 4

def fresh_data(lang="fr"):
    return {
        "lang": lang,
        "passengers": None,
        "incident_type": None,
        "airline": None,
        "pnr": None,
        "flight_number": None,
        "flight_date": None,
        "temp_year": None,
        "temp_month": None,
        "temp_years": [],
        "passenger_names": [],
        "pax_collect_idx": 1,
        "has_minors": None,
        "itinerary": None,
        "itin_collect_mode": None,  # "connection" | "rt_out" | "rt_in" | "rt_both"
        "claim_rt_leg": None,  # "outbound" | "return" | "both" (choix utilisateur)
        "vision_leg_hint": None,  # indice lecture billet (outbound|return|unknown)
        "itinerary_compl_note": None,  # 2e segment saisi après « | »
        "temp_itin_dep": None,
        "pending_ticket_dm": None,  # ("DD","MM") si jour/mois lus sur billet sans année
        "operating_airline": None,  # compagnie qui exploite le vol si ≠ commercial (code-share)
        "boarding_evidence_in_flow": False,  # carte / billet reçu pendant le tunnel (relances post-dépôt)
        "early_route_shape": None,  # "direct" | "connection" (dès le choix du nombre de passagers)
        "expert_phone_lang": None,  # code langue vocaux / suivi (fr,en,wo,tw,yo,sw,pt,ar,ha,ma)
    }

def normalize_phone_key(phone):
    """Clé stable pour conversations (évite 336… vs +336…)."""
    return re.sub(r"\D", "", str(phone or ""))

def get_conv(phone):
    phone = normalize_phone_key(phone)
    now = datetime.now()
    if phone in conversations:
        if (now - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
            try:
                lang = conversations[phone].get("data", {}).get("lang", "fr")
                st = conversations[phone].get("step")
                if st and st != "completed":
                    send(
                        phone,
                        (
                            "⏱️ *Votre session a expiré* (24 h sans activité). Le dossier en cours a été effacé — vous pouvez recommencer en envoyant *menu* ou le nombre de passagers."
                            if lang == "fr"
                            else "⏱️ *Your session expired* (24h idle). The in-progress claim was cleared — send *menu* or your passenger count to start again."
                        )
                        + site_mandat_links_footer(lang),
                    )
            except Exception as e:
                print(f"get_conv expiry notify: {e}")
            del conversations[phone]
    if phone not in conversations:
        conversations[phone] = {
            "step": None,
            "ref":  None,
            "data": fresh_data(),
            "created": now,
        }
    return conversations[phone]

# ===== HELPERS =====

def make_ref(phone):
    today  = datetime.now().strftime("%Y%m%d")
    suffix = hashlib.md5(f"{phone}{today}".encode()).hexdigest()[:4].upper()
    return f"RDA-{today}-{suffix}"

def calc_amounts(pax, band="band_600"):
    per_pax = EU261_BANDS.get(band, EU261_BANDS["band_600"])["amount_eur"]
    brut    = per_pax * pax
    net     = round(brut * NET_PCT)
    com     = round(brut * COMMISSION_PCT)
    return brut, net, com, per_pax

def fmt_money_space(n):
    """Montants entiers avec espaces milliers (ex. 1 350)."""
    n = int(round(n))
    neg = n < 0
    s = str(abs(n))
    parts = []
    while len(s) > 3:
        parts.insert(0, s[-3:])
        s = s[:-3]
    if s:
        parts.insert(0, s)
    r = " ".join(parts)
    return ("-" if neg else "") + r

def _pax_int(pax):
    try:
        n = int(pax or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)

def pax_count_phrase(pax, lang):
    """« 1 passager » / « 3 passagers » (FR ou EN)."""
    n = _pax_int(pax)
    if lang == "en":
        return f"{n} passenger" if n == 1 else f"{n} passengers"
    return f"{n} passager" if n == 1 else f"{n} passagers"

def pax_registered_ack(pax, lang):
    """Message court après choix du nombre (accord singulier/pluriel)."""
    n = _pax_int(pax)
    if lang == "en":
        return f"✅ *{pax_count_phrase(n, 'en').capitalize()} registered.*"
    if n == 1:
        return "✅ *1 passager enregistré.*"
    return f"✅ *{n} passagers enregistrés.*"

def pax_names_confirm_heading(pax, lang, partial=False, n_done=0):
    """Titre de l’étape confirmation des noms."""
    n = _pax_int(pax)
    nd = int(n_done or 0)
    if partial:
        if lang == "en":
            if n == 1:
                return "👥 *Check the passenger*"
            return f"👥 *Check passengers 1–{nd}* (of {n})"
        if n == 1:
            return "👥 *Vérifiez le passager*"
        return f"👥 *Vérifiez les passagers 1 à {nd}* (sur {n})"
    if lang == "en":
        if n == 1:
            return "👥 *1 passenger — confirm*"
        return f"👥 *All {n} passengers — confirm*"
    if n == 1:
        return "👥 *1 passager — confirmez*"
    return f"👥 *Les {n} passagers — confirmez*"

def pax_recap_line(pax, lang):
    """Ligne récap 👥 Passager(s) : N."""
    n = _pax_int(pax)
    if lang == "en":
        label = "Passenger" if n == 1 else "Passengers"
    else:
        label = "Passager" if n == 1 else "Passagers"
    return f"👥 *{label} :* {n}"

def pax_minors_intro(pax, lang):
    """Intro question mineurs selon le nombre de passagers."""
    n = _pax_int(pax)
    if lang == "en":
        if n == 1:
            return "For the passenger below, is anyone under 18?"
        return "Among the passengers listed below, are any minors (under 18)?"
    if n == 1:
        return "Pour le passager ci-dessous, s’agit-il d’un mineur (moins de 18 ans) ?"
    return "Parmi les passagers suivants, y a-t-il des mineurs (moins de 18 ans) ?"

def fmt_date_for_airtable(date_str):
    """JJ/MM/AAAA → AAAA-MM-JJ"""
    parts = (date_str or "").split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None

def detect_lang(text):
    en = ["hello", "hi", "my", "flight", "delay", "yes", "no", "thanks", "cancel"]
    fr = ["bonjour", "salut", "mon", "vol", "retard", "oui", "non", "merci", "annul"]
    t  = text.lower()
    if sum(1 for w in en if w in t) > sum(1 for w in fr if w in t):
        return "en"
    return "fr"

def is_dup(phone, data, sig, step):
    now    = datetime.now()
    to_del = [k for k, ts in recent_event_ids.items() if (now - ts).total_seconds() > 900]
    for k in to_del:
        recent_event_ids.pop(k, None)
    eid = data.get("messageId") or data.get("id") or data.get("whatsappMessageId")
    if eid:
        if eid in recent_event_ids:
            return True
        recent_event_ids[eid] = now
    key = hashlib.sha256(f"{phone}|{sig}|{step}".encode()).hexdigest()
    if key in recent_event_ids and (now - recent_event_ids[key]).total_seconds() < 25:
        return True
    recent_event_ids[key] = now
    return False

# ===== WATI =====

def send(phone, msg):
    msg = msg.strip()
    if not msg:
        return False
    conv = conversations.get(phone)
    pre = tunnel_progress_prefix(conv) if conv else ""
    if pre:
        first_line = msg.split("\n", 1)[0]
        if not _first_line_looks_like_tunnel_progress_bar(first_line):
            msg = pre + msg
    if not WATI_API_TOKEN or not WATI_BASE_URL:
        print("send: WATI_API_TOKEN ou WATI_BASE_URL manquant", flush=True)
        return False
    wa = _norm_wa_number(phone)
    if not wa:
        print(f"send: numéro invalide {phone!r}", flush=True)
        return False
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{wa}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    try:
        r = requests.post(url, headers=headers, params={"messageText": msg}, timeout=30)
        ok = 200 <= r.status_code < 300
        if ok:
            print(f"Wati send OK {r.status_code} → {wa}", flush=True)
        else:
            print(f"Wati send FAIL {r.status_code} → {wa}: {r.text[:400]}", flush=True)
        return ok
    except Exception as e:
        print(f"Wati send error → {wa}: {e}", flush=True)
        return False

def _norm_wa_number(phone):
    return re.sub(r"\D", "", str(phone or ""))

def _norm_choice_key(s):
    s = (s or "").strip().lower()
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def register_choice_map(conv, options):
    """Associe libellés de boutons/liste WATI → id logique (« 1 », « 2 », …)."""
    cmap = {}
    for opt in options:
        oid = str(opt["id"])
        cmap[oid] = oid
        for key in (opt.get("id"), opt.get("label"), opt.get("button"), opt.get("list_title")):
            if not key:
                continue
            nk = _norm_choice_key(str(key))
            if nk:
                cmap[nk] = oid
            short = str(key).strip()[:24]
            if short:
                cmap[_norm_choice_key(short)] = oid
    conv.setdefault("data", {})["_choice_map"] = cmap

def _iter_wati_interactive_reply_objects(data):
    """Parcourt listReply / buttonReply (racine, champ data JSON, sous-objets)."""
    if not isinstance(data, dict):
        return
    seen = set()

    def walk(node):
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        for field in (
            "interactiveButtonReply",
            "listReply",
            "buttonReply",
            "interactiveListReply",
            "list_reply",
        ):
            obj = node.get(field)
            if isinstance(obj, dict):
                yield obj
        inner = node.get("data")
        if isinstance(inner, str) and inner.strip().startswith("{"):
            try:
                inner = json.loads(inner)
            except (json.JSONDecodeError, TypeError):
                inner = None
        if isinstance(inner, dict):
            walk(inner)
        for key in ("reply", "context", "message", "interactive", "messageData"):
            sub = node.get(key)
            if isinstance(sub, dict):
                yield from walk(sub)

    yield from walk(data)


def _has_wati_interactive(data):
    if not isinstance(data, dict):
        return False
    if data.get("type") in ("button", "interactive", "list", "listReply"):
        return True
    if any(isinstance(data.get(f), dict) for f in ("interactiveButtonReply", "listReply", "buttonReply")):
        return True
    for _ in _iter_wati_interactive_reply_objects(data):
        return True
    return False


def enrich_message_from_wati_interactive(data, message_text):
    """Récupère le libellé du clic liste/bouton quand le champ text est vide."""
    mt = (message_text or "").strip()
    if mt:
        return mt
    if not isinstance(data, dict):
        return mt
    for obj in _iter_wati_interactive_reply_objects(data):
        for key in ("id", "title", "text", "description", "rowId", "selectedRowId", "message"):
            val = obj.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    top = (data.get("text") or "").strip()
    if top and data.get("type") in ("interactive", "button", "list", "listReply"):
        return top
    return mt

def _map_raw_to_choice_id(raw, cmap):
    if not raw:
        return None
    if looks_like_full_date_input(raw):
        return None
    if raw in cmap:
        return cmap[raw]
    nk = _norm_choice_key(raw)
    if nk in cmap:
        return cmap[nk]
    for k, v in cmap.items():
        if len(k) >= 2 and (nk == k or nk.startswith(k) or k.startswith(nk)):
            return v
    m = re.match(r"^(\d{1,2})\s*(?:[·•.:\-–]|$|\s)", raw.strip())
    if not m:
        m = re.match(r"^(\d{1,2})\b", raw.strip())
    if m:
        digit = m.group(1)
        if not cmap or digit in cmap:
            return digit
    return None

def extract_poll_choice(conv, data, message_text):
    """Réponse clic sondage WATI → id « 1 », « 2 », … (ou None)."""
    if not isinstance(conv, dict):
        return None
    cmap = (conv.get("data") or {}).get("_choice_map") or {}
    if not isinstance(cmap, dict):
        cmap = {}
    candidates = []
    if message_text and str(message_text).strip():
        candidates.append(str(message_text).strip())
    if isinstance(data, dict):
        for obj in _iter_wati_interactive_reply_objects(data):
            for key in ("id", "title", "text", "description", "rowId", "selectedRowId", "message"):
                val = obj.get(key)
                if val is not None and str(val).strip():
                    candidates.append(str(val).strip())
        top = (data.get("text") or "").strip()
        if top:
            candidates.append(top)
    for raw in candidates:
        mapped = _map_raw_to_choice_id(raw, cmap)
        if mapped:
            return mapped
    # Repli sans carte (redémarrage Railway) : chiffre 1–6 dans le libellé
    for raw in candidates:
        m = re.search(r"\b([1-6])\b", raw)
        if m:
            return m.group(1)
    return None

def resolve_tunnel_choice(conv, data, message_text):
    """Choix tunnel : clic WATI puis repli chiffre en tête de texte."""
    message_text = message_text or ""
    c = extract_poll_choice(conv, data, message_text)
    if c:
        return c
    if looks_like_full_date_input(message_text):
        return None
    for raw in [message_text] + list(
        str(obj.get(k) or "")
        for obj in _iter_wati_interactive_reply_objects(data or {})
        for k in ("title", "id", "text", "description")
    ):
        if not raw or not str(raw).strip():
            continue
        m = re.match(r"^(\d{1,2})\s*(?:[·•.:\-–]|$|\s)", str(raw).strip())
        if not m:
            m = re.match(r"^(\d{1,2})\b", str(raw).strip())
        if m:
            return m.group(1)
    return None

def parse_wati_inbound_message(data):
    """Texte + image depuis le webhook WATI (hors réponses interactives)."""
    image_b64 = None
    message_text = ""
    if not isinstance(data, dict):
        return message_text, image_b64
    # Formats Wati courants (message reçu)
    t = data.get("text")
    if isinstance(t, str) and t.strip():
        message_text = t.strip()
    elif isinstance(t, dict):
        message_text = (t.get("body") or t.get("text") or "").strip()
    inner = data.get("data")
    if not message_text and isinstance(inner, dict):
        it = inner.get("text")
        if isinstance(it, str):
            message_text = it.strip()
        elif isinstance(it, dict):
            message_text = (it.get("body") or it.get("text") or "").strip()
    if not message_text and isinstance(data.get("message"), dict):
        mm = data["message"]
        message_text = (mm.get("text") or mm.get("body") or "").strip()
        if isinstance(message_text, dict):
            message_text = (message_text.get("body") or "").strip()
    if data.get("type") == "image" or data.get("image"):
        media_url = data.get("data") or data.get("mediaUrl")
        if media_url:
            try:
                r = requests.get(
                    media_url,
                    headers={"Authorization": f"Bearer {WATI_API_TOKEN}"},
                    timeout=30,
                )
                if r.status_code == 200:
                    image_b64 = base64.b64encode(r.content).decode()
            except Exception:
                pass
        message_text = data.get("caption", "") or ""
    else:
        if isinstance(data.get("text"), dict):
            message_text = data["text"].get("body", "") or ""
        elif isinstance(data.get("text"), str):
            message_text = data["text"]
        elif data.get("body"):
            body = data["body"]
            if isinstance(body, dict):
                message_text = body.get("body", "") or body.get("text", "") or ""
            else:
                message_text = str(body)
    return (message_text or "").strip(), image_b64

def _poll_body_with_progress(conv, body):
    body = (body or "").strip()
    pre = tunnel_progress_prefix(conv) if conv else ""
    if pre:
        fl = body.split("\n", 1)[0]
        if not _first_line_looks_like_tunnel_progress_bar(fl):
            body = pre + body
    return body

def send_wati_buttons(phone, body, button_texts, footer=None):
    if not WATI_API_TOKEN or not WATI_BASE_URL:
        return False
    wa = _norm_wa_number(phone)
    if not wa:
        return False
    payload = {
        "body": (body or "")[:1024],
        "buttons": [{"text": (t or "")[:20]} for t in button_texts[:3] if (t or "").strip()],
    }
    if footer:
        payload["footer"] = footer[:60]
    if not payload["buttons"]:
        return False
    url = f"{WATI_BASE_URL}/api/v1/sendInteractiveButtonsMessage"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "application/json"}
    try:
        r = requests.post(
            url, headers=headers, params={"whatsappNumber": wa}, json=payload, timeout=30
        )
        print(f"Wati buttons {r.status_code}")
        return 200 <= r.status_code < 300
    except Exception as e:
        print(f"Wati buttons error: {e}")
        return False

def send_wati_list(phone, body, button_text, rows, footer=None, header=None, section_title=None):
    if not WATI_API_TOKEN or not WATI_BASE_URL:
        return False
    wa = _norm_wa_number(phone)
    if not wa:
        return False
    section_rows = []
    for row in rows[:10]:
        title = (row.get("title") or "")[:24]
        if not title:
            continue
        ent = {"title": title}
        desc = (row.get("description") or "").strip()
        if desc:
            ent["description"] = desc[:72]
        section_rows.append(ent)
    if not section_rows:
        return False
    payload = {
        "body": (body or "")[:1024],
        "buttonText": (button_text or "Choisir")[:20],
        "sections": [
            {
                "title": ((section_title or "Options")[:24]),
                "rows": section_rows,
            }
        ],
    }
    if header:
        payload["header"] = header[:60]
    if footer:
        payload["footer"] = footer[:60]
    url = f"{WATI_BASE_URL}/api/v1/sendInteractiveListMessage"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "application/json"}
    try:
        r = requests.post(
            url, headers=headers, params={"whatsappNumber": wa}, json=payload, timeout=30
        )
        print(f"Wati list {r.status_code}")
        return 200 <= r.status_code < 300
    except Exception as e:
        print(f"Wati list error: {e}")
        return False

def send_tunnel_poll(phone, conv, body, options, footer=None, list_button_text=None, header=None):
    """
    Mode sondage : boutons (1–3 choix) ou liste WATI (4–10).
    options: [{"id":"1", "label":"…", "button":"…", "list_title":"…", "desc":"…"}]
    """
    conv = conv or conversations.get(phone) or {"data": {}}
    lang = (conv.get("data") or {}).get("lang", "fr")
    body = _poll_body_with_progress(conv, body)
    register_choice_map(conv, options)
    if WATI_USE_INTERACTIVE and WATI_API_TOKEN and WATI_BASE_URL:
        if 1 <= len(options) <= 3:
            btns = [(o.get("button") or o.get("label") or str(o["id"]))[:20] for o in options]
            if send_wati_buttons(phone, body, btns, footer=footer):
                return True
        if 4 <= len(options) <= 10:
            rows = [
                {
                    "title": (o.get("list_title") or o.get("label") or str(o["id"]))[:24],
                    "description": (o.get("desc") or "")[:72],
                }
                for o in options
            ]
            lbtn = (list_button_text or ("Choisir" if lang == "fr" else "Choose"))[:20]
            if send_wati_list(phone, body, lbtn, rows, footer=footer, header=header):
                return True
    lines = "\n".join(f"{o['id']}️⃣  {o['label']}" for o in options)
    hint = (
        "\n\n👆 *Appuyez sur un bouton ou ouvrez la liste.*"
        if lang == "fr"
        else "\n\n👆 *Tap a button or open the list below.*"
    )
    send(phone, f"{body}\n\n{lines}{hint}")
    return True

def _gpt_hors_tunnel_format_rules(lang):
    """Consignes de forme pour les réponses IA hors tunnel (step None ou completed)."""
    if lang == "en":
        return (
            "Mandatory format: WhatsApp bullets starting with « • »; *each bullet line starts with one emoji* "
            "(never a bullet without emoji). Use 2 to 6 short bullets; no long paragraphs."
        )
    return (
        "Format obligatoire : puces WhatsApp « • » ; *chaque ligne commence par un emoji* "
        "(jamais de puce sans emoji). 2 à 6 puces courtes ; pas de paragraphe long."
    )

def site_mandat_links_footer(lang="fr"):
    """Liens courts hors tunnel — site et suivi (sans mandat / dépôt express en liste)."""
    site = RDA_DOMAIN.rstrip("/")
    if lang == "en":
        return f"\n\n• 🌐 *Website:* {site}\n• 📂 *Track your file:* {SUIVI_URL}"
    return f"\n\n• 🌐 *Site :* {site}\n• 📂 *Suivi dossier :* {SUIVI_URL}"

# ===== RELANCES POST-DÉPÔT (mandat + pièces) =====
# Fenêtre session WhatsApp / Meta : messages libres tant que last_user_inbound < META_SESSION_HOURS.
# Après : uniquement templates (WATI_POST_SUBMIT_TEMPLATE_NAME + WATI_TEMPLATE_CHANNEL_NUMBER).

def touch_last_user_inbound(conv):
    conv.setdefault("data", {})["last_user_inbound_at"] = datetime.now().isoformat(timespec="seconds")

def session_message_allowed(conv):
    raw = (conv.get("data") or {}).get("last_user_inbound_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except Exception:
        return True
    return datetime.now() - last < timedelta(hours=META_SESSION_HOURS)

def _first_name_from_conv(conv):
    names = (conv.get("data") or {}).get("passenger_names") or []
    if not names:
        return ""
    first = (names[0] or "").strip().split()
    return first[0] if first else ""

def _resume_mandat_link(phone, conv):
    d = conv.get("data") or {}
    ps = d.get("_post_submit") or {}
    su = ps.get("short_sign_url")
    if isinstance(su, str) and su.startswith("http"):
        return su
    params = ps.get("mandat_params")
    if isinstance(params, dict) and params:
        return mandat_signing_link(params)
    ref = conv.get("ref") or make_ref(phone)
    pax = d.get("passengers") or 1
    names = d.get("passenger_names") or []
    ph = re.sub(r"\D", "", str(phone or "")) or str(phone or "").strip()
    params2 = {
        "ref": ref,
        "pax": pax,
        "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""),
        "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""),
        "noms": ",".join(names),
        "source": "whatsapp_bot",
    }
    if d.get("pnr"):
        params2["pnr"] = d["pnr"]
    dep, arr, via = split_itinerary_for_mandat(d.get("itinerary") or "")
    if dep:
        params2["dep"] = dep
    if arr:
        params2["arr"] = arr
    if via:
        params2["esc1"] = via
    if ph:
        params2["phone"] = ph
    if d.get("has_minors"):
        params2["mineurs"] = "1"
    return mandat_signing_link(params2)

def _openai_doc_classify_available():
    return bool((OPENAI_API_KEY or "").strip())

def post_submit_mandate_satisfied(ps):
    return bool(
        ps.get("mandate_ack")
        or ps.get("mandate_signed_server")
        or ps.get("air_mandat_signed")
    )

def post_submit_id_satisfied(ps):
    if ps.get("air_id_attachment"):
        return True
    if _openai_doc_classify_available():
        return bool(ps.get("post_submit_has_id_image"))
    # Sans vision : on ne devine pas la CNI à partir d’une carte d’embarquement seule.
    return False

def post_submit_boarding_satisfied(ps):
    if not ps.get("needs_boarding_hint"):
        return True
    if ps.get("air_boarding_attachment"):
        return True
    if ps.get("post_submit_has_boarding_image"):
        return True
    n = int(ps.get("images_after_summary") or 0)
    if not _openai_doc_classify_available():
        return n >= 2
    return False

def post_submit_fully_done(conv):
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict) or not ps.get("active"):
        return True
    refresh_post_submit_airtable_flags(conv)
    # « C’est bon » / « tout envoyé » ne court-circuite plus le mandat ou la CNI.
    return (
        post_submit_mandate_satisfied(ps)
        and post_submit_id_satisfied(ps)
        and post_submit_boarding_satisfied(ps)
    )

def update_post_submit_inbound(phone, conv, message_text, image_b64):
    """À l'étape completed : met à jour l'état des pièces / mandat pour arrêter les relances."""
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict) or not ps.get("active"):
        return
    low = (message_text or "").lower()
    if image_b64:
        ps["images_after_summary"] = int(ps.get("images_after_summary") or 0) + 1
        if _openai_doc_classify_available():
            try:
                info = read_boarding_pass_merged(image_b64)
                if boarding_pass_info_usable(info):
                    ps["post_submit_has_boarding_image"] = True
                else:
                    # Photo illisible ou autre doc : ne pas compter comme CNI automatiquement.
                    ps["post_submit_has_unclassified_image"] = True
            except Exception:
                ps["post_submit_has_unclassified_image"] = True
    if any(
        x in low
        for x in (
            "signé",
            "signe",
            "signed",
            "j'ai sign",
            "mandat sign",
            "mandat ok",
            "signature faite",
        )
    ):
        ps["mandate_ack"] = True
    if any(
        x in low
        for x in (
            "tout envoy",
            "tout envoye",
            "j'ai tout",
            "terminé",
            "termine",
            "c'est bon",
            "cest bon",
            "all sent",
            "everything sent",
            "done everything",
        )
    ):
        ps["user_declared_done"] = True
    if post_submit_fully_done(conv):
        ps["active"] = False

def _post_submit_checklist(conv, lang, ref, net_s):
    """Lignes ✅/❌ alignées sur la même logique que l'arrêt des relances."""
    refresh_post_submit_airtable_flags(conv)
    ps = conv.get("data", {}).get("_post_submit") or {}
    d_m = post_submit_mandate_satisfied(ps)
    d_id = post_submit_id_satisfied(ps)
    need_b = bool(ps.get("needs_boarding_hint"))
    d_bp = post_submit_boarding_satisfied(ps) if need_b else True

    def line(ok, fr, en):
        return (f"{'✅' if ok else '❌'} *{fr}*" if lang == "fr" else f"{'✅' if ok else '❌'} *{en}*")

    rows = [
        line(True, "Infos du vol", "Flight details"),
        line(True, "Calcul de l'indemnité", "Compensation estimate"),
        line(d_m, "Signature du mandat", "Mandate signature"),
        line(d_id, "Photo passeport / CNI", "Passport / ID photo"),
    ]
    if need_b:
        rows.append(line(d_bp, "Carte d'embarquement / billet", "Boarding pass / ticket"))
    return "\n".join(rows)

def post_submit_after_image_ack(conv, lang):
    """Retour court après envoi d’une photo en phase completed (évite « dossier reçu » ambigu)."""
    refresh_post_submit_airtable_flags(conv)
    ps = conv.get("data", {}).get("_post_submit") or {}
    if not ps.get("active"):
        return None
    ref = (conv.get("ref") or "").strip()
    ref_bit = f" ({ref})" if ref else ""
    got_bp = post_submit_boarding_satisfied(ps)
    got_id = post_submit_id_satisfied(ps)
    got_m = post_submit_mandate_satisfied(ps)
    if post_submit_fully_done(conv):
        return None
    if lang == "fr":
        lines = [f"📎 *Pièce reçue{ref_bit}*"]
        if ps.get("post_submit_has_boarding_image") or got_bp:
            lines.append("• ✈️ Carte d’embarquement / billet noté")
        if ps.get("post_submit_has_unclassified_image"):
            lines.append(
                "• 📷 Photo reçue — renvoyez une *CNI / passeport* lisible si ce n’était pas votre pièce d’identité"
            )
        missing = []
        if not got_m:
            missing.append("✍️ *Mandat signé* (lien envoyé plus haut)")
        if not got_id:
            missing.append("🪪 *Passeport ou CNI* en photo lisible")
        if ps.get("needs_boarding_hint") and not got_bp:
            missing.append("✈️ *Carte d’embarquement*")
        if missing:
            lines.append("• ⏳ *Il manque encore :*")
            for m in missing:
                lines.append(f"  • {m}")
        lines.append("• ⚖️ Le dossier n’est *pas complet* tant que le mandat n’est pas signé")
        return "\n".join(lines)
    lines = [f"📎 *Document received{ref_bit}*"]
    if ps.get("post_submit_has_boarding_image") or got_bp:
        lines.append("• ✈️ Boarding pass / ticket noted")
    if ps.get("post_submit_has_unclassified_image"):
        lines.append("• 📷 Photo received — resend a clear *passport / ID* if that wasn’t ID")
    missing = []
    if not got_m:
        missing.append("✍️ *Signed mandate* (link above)")
    if not got_id:
        missing.append("🪪 *Passport or national ID* photo")
    if ps.get("needs_boarding_hint") and not got_bp:
        missing.append("✈️ *Boarding pass*")
    if missing:
        lines.append("• ⏳ *Still needed:*")
        for m in missing:
            lines.append(f"  • {m}")
    lines.append("• ⚖️ File *not complete* until the mandate is signed")
    return "\n".join(lines)

def completed_phase_fallback_reply(conv, lang):
    """
    Réponse par défaut quand step=completed (le client écrit après le message « dossier prêt »).
    Ne signifie pas que mandat + pièces sont reçus — voir post_submit_fully_done().
    """
    refresh_post_submit_airtable_flags(conv)
    ref = (conv.get("ref") or "").strip()
    ref_bit = f" (*{ref}*)" if ref else ""
    if post_submit_fully_done(conv):
        if lang == "fr":
            return (
                f"✅ *Dossier complet{ref_bit}*\n\n"
                "• 💬 Posez votre question ici (court de préférence)\n"
                "• 🔄 Nouveau dossier : *menu* ou *recommencer*"
            )
        return (
            f"✅ *File complete{ref_bit}*\n\n"
            "• 💬 Ask your question here (keep it short)\n"
            "• 🔄 New claim: *menu* or *restart*"
        )
    checklist = _post_submit_checklist(conv, lang, ref, "")
    if lang == "fr":
        return (
            f"✅ *Infos enregistrées{ref_bit}*\n\n"
            "📋 *État du dossier :*\n"
            f"{checklist}\n\n"
            "• 💬 Répondez ici ou envoyez mandat / pièces sur ce fil\n"
            "• 🔄 Nouveau dossier : *menu* ou *recommencer*"
        )
    return (
        f"✅ *Details saved{ref_bit}*\n\n"
        "📋 *File status:*\n"
        f"{checklist}\n\n"
        "• 💬 Reply here or send mandate / documents in this chat\n"
        "• 🔄 New claim: *menu* or *restart*"
    )

def _build_relance_body(phone, conv, stage):
    d = conv["data"]
    lang = d.get("lang", "fr")
    pax = d.get("passengers") or 1
    ref = conv.get("ref") or ""
    _, net, _, _ = calc_amounts(pax)
    net_s = fmt_money_space(net)
    ps = d.get("_post_submit") or {}
    prenom = _first_name_from_conv(conv) or ("vous" if lang == "fr" else "there")
    link = _resume_mandat_link(phone, conv)
    checklist = _post_submit_checklist(conv, lang, ref, net_s)

    if stage == "30m":
        if lang == "en":
            return (
                f"⌛ *Almost done, {prenom}!*\n\n"
                f"Your file for *{net_s} €* is on our side — it only needs a quick validation.\n\n"
                "*Status:*\n"
                f"{checklist}\n\n"
                f"👇 *Tap here to finish in ~30 seconds:*\n{link}\n\n"
                "🏹 The Robin des Airs team is ready to start the procedure as soon as you confirm!"
                + site_mandat_links_footer("en")
            )
        return (
            f"⌛ *Presque fini, {prenom} !*\n\n"
            f"Votre dossier pour *{net_s} €* est prêt de notre côté — il ne manque qu'une validation rapide.\n\n"
            "*État de votre dossier :*\n"
            f"{checklist}\n\n"
            f"👇 *Cliquez ici pour finaliser en ~30 secondes :*\n{link}\n\n"
            "🏹 *L'équipe Robin des Airs* est prête à lancer la procédure dès que vous validez !"
            + site_mandat_links_footer("fr")
        )

    if stage == "4h":
        if lang == "en":
            return (
                "🔔 *We wouldn't want you to miss this…*\n\n"
                f"You still have *{net_s} €* pending on your claim. *Is a technical issue blocking you?*\n\n"
                "📸 *Unsure about the passport / ID?* A simple, readable photo with your phone is enough — "
                "no official scan needed.\n\n"
                f"👉 *Pick up where you left off:*\n{link}\n\n"
                "⚖️ The sooner we receive your documents, the sooner the airline receives our formal notice."
                + site_mandat_links_footer("en")
            )
        return (
            "🔔 *On ne voudrait pas que vous passiez à côté…*\n\n"
            f"Vous avez laissé une indemnité d'environ *{net_s} €* en attente. *Un souci technique vous bloque ?*\n\n"
            "📸 *Un doute sur le passeport / la CNI ?* Une simple photo bien lisible avec votre téléphone suffit, "
            "pas besoin de scan officiel !\n\n"
            f"👉 *Reprendre là où j'en étais :*\n{link}\n\n"
            "⚖️ Plus vite nous avons vos documents, plus vite la compagnie reçoit notre mise en demeure."
            + site_mandat_links_footer("fr")
        )

    if stage == "23h":
        if lang == "en":
            return (
                f"⚠️ *LAST REMINDER — File {ref}*\n\n"
                f"{prenom}, we may need to *put your file on hold* and focus our capacity on other claims "
                "if we do not receive your mandate and proof *very soon*.\n\n"
                "*Status:*\n"
                f"{checklist}\n\n"
                f"👇 *Finalize here (mandate + photos):*\n{link}\n\n"
                "🏹 *Robin des Airs* — we're ready to launch as soon as you validate."
                + site_mandat_links_footer("en")
            )
        return (
            f"⚠️ *DERNIER RAPPEL — Dossier {ref}*\n\n"
            f"{prenom}, nous allons devoir *mettre votre dossier en attente* et libérer notre capacité "
            "pour d'autres dossiers si nous ne recevons *pas très vite* votre mandat et vos pièces.\n\n"
            "*État de votre dossier :*\n"
            f"{checklist}\n\n"
            f"👇 *Finaliser ici (mandat + photos) :*\n{link}\n\n"
            "🏹 *Robin des Airs* — nous sommes prêts à lancer dès votre validation."
            + site_mandat_links_footer("fr")
        )
    return ""

def send_wati_template_v2(phone, conv):
    """Envoie le template Meta/Wati (hors fenêtre 24 h). Paramètres : noms alignés sur le template Wati."""
    if not WATI_API_TOKEN or not WATI_BASE_URL:
        return False
    if not WATI_POST_SUBMIT_TEMPLATE_NAME or not WATI_TEMPLATE_CHANNEL_NUMBER:
        print("send_wati_template_v2: WATI_POST_SUBMIT_TEMPLATE_NAME ou WATI_TEMPLATE_CHANNEL_NUMBER manquant")
        return False
    d = conv["data"]
    lang = d.get("lang", "fr")
    pax = d.get("passengers") or 1
    ref = conv.get("ref") or ""
    _, net, _, _ = calc_amounts(pax)
    net_s = fmt_money_space(net)
    prenom = _first_name_from_conv(conv) or ("Client" if lang == "fr" else "Client")
    link = _resume_mandat_link(phone, conv)
    names_csv = os.environ.get("WATI_POST_SUBMIT_TEMPLATE_PARAM_NAMES", "").strip()
    if names_csv:
        keys = [k.strip() for k in names_csv.split(",") if k.strip()]
    else:
        keys = ["first_name", "amount", "reference", "link"]
    values = [prenom, f"{net_s} €", ref, link]
    parameters = []
    for i, k in enumerate(keys):
        v = values[i] if i < len(values) else ""
        if v:
            parameters.append({"name": k, "value": str(v)[:1024]})
    url = f"{WATI_BASE_URL}/api/v2/sendTemplateMessage"
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "Content-Type": "application/json",
        "accept": "*/*",
    }
    body = {
        "template_name": WATI_POST_SUBMIT_TEMPLATE_NAME,
        "broadcast_name": WATI_POST_SUBMIT_TEMPLATE_BROADCAST,
        "channel_number": WATI_TEMPLATE_CHANNEL_NUMBER,
        "parameters": parameters,
    }
    try:
        r = requests.post(
            url,
            headers=headers,
            params={"whatsappNumber": phone},
            json=body,
            timeout=45,
        )
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

def process_post_submit_reminders():
    """Un tick par minute : au plus une relance par conversation."""
    now = time.time()
    for phone, conv in list(conversations.items()):
        d = conv.get("data") or {}
        ps = d.get("_post_submit")
        if not isinstance(ps, dict) or not ps.get("active"):
            continue
        if conv.get("step") != "completed":
            continue
        if post_submit_fully_done(conv):
            ps["active"] = False
            continue

        _sync_relance_halt_from_airtable(conv)
        if ps.get("air_relance_halt_airtable"):
            ps["active"] = False
            continue

        summary_at = float(ps.get("summary_at") or 0)
        if summary_at <= 0:
            continue
        elapsed = now - summary_at
        sent = list(ps.get("relances_sent") or [])
        sent_set = set(sent)
        session_ok = session_message_allowed(conv)

        if not ps.get("template_mode") and not session_ok:
            ps["template_mode"] = True

        if not ps.get("template_mode") and session_ok:
            stages = [("30m", 1800), ("4h", 4 * 3600), ("23h", 23 * 3600)]
            for key, sec in stages:
                if key in sent_set:
                    continue
                if elapsed < sec:
                    break
                msg = _build_relance_body(phone, conv, key)
                if msg and send(phone, msg):
                    sent.append(key)
                    ps["relances_sent"] = sent
                break
            continue

        if ps.get("template_mode") and WATI_POST_SUBMIT_TEMPLATE_NAME and WATI_TEMPLATE_CHANNEL_NUMBER:
            last_tpl = float(ps.get("last_template_at") or 0)
            interval = max(6, POST_SUBMIT_TEMPLATE_REPEAT_H) * 3600
            if last_tpl <= 0 or (now - last_tpl) >= interval:
                if send_wati_template_v2(phone, conv):
                    ps["last_template_at"] = now
        elif ps.get("template_mode"):
            warn_key = "_tpl_missing_config_warned"
            if not ps.get(warn_key):
                ps[warn_key] = True
                print(
                    "process_post_submit_reminders: template_mode actif mais "
                    "WATI_POST_SUBMIT_TEMPLATE_NAME / WATI_TEMPLATE_CHANNEL_NUMBER non configurés — "
                    "impossible d'écrire au client hors fenêtre 24 h."
                )

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
    t = threading.Thread(target=_post_submit_reminder_loop, daemon=True, name="post_submit_relances")
    t.start()

# ===== AIRTABLE =====

def at_headers():
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

def at_url():
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AT_TABLE_ID}"

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

def _airtable_field_has_attachment(records, field_id):
    if not field_id or not records:
        return False
    for rec in records:
        flds = rec.get("fields") or {}
        val = flds.get(field_id)
        if isinstance(val, list) and len(val) > 0:
            return True
    return False

def _airtable_relance_halt_from_records(records):
    """
    True = ne plus envoyer de relances pour ce dossier.
    - Stop_Relance (AIRTABLE_F_STOP_RELANCE) : coché sur n'importe quelle ligne → arrêt.
    - Sequence_Active (AIRTABLE_F_SEQUENCE_ACTIVE) : lu sur la 1re ligne ; décoché → arrêt ; absent → pas d'arrêt.
    """
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

def _sync_relance_halt_from_airtable(conv):
    """Lit la case Airtable (throttle 60 s) pour arrêter les relances sans toucher au reste du dossier."""
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict) or not ps.get("active"):
        return
    if not (F_STOP_RELANCE or F_SEQUENCE_ACTIVE):
        return
    ref = (conv.get("ref") or "").strip()
    if not ref or not AIRTABLE_API_KEY:
        return
    now = time.time()
    if now - float(ps.get("_at_relance_halt_sync_at") or 0) < 60:
        return
    ps["_at_relance_halt_sync_at"] = now
    try:
        recs = at_find(ref)
        ps["air_relance_halt_airtable"] = _airtable_relance_halt_from_records(recs)
    except Exception as e:
        print(f"_sync_relance_halt_from_airtable: {e}")

def refresh_post_submit_airtable_flags(conv):
    """Met à jour _post_submit depuis Airtable (pièces jointes). Throttlé (~90 s)."""
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict) or not ps.get("active"):
        return
    now = time.time()
    if now - float(ps.get("_at_sync_at") or 0) < 90:
        return
    ps["_at_sync_at"] = now
    ref = (conv.get("ref") or "").strip()
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

def at_save(phone, conv):
    """Sauvegarde progressive — crée ou met à jour les records Airtable."""
    if not AIRTABLE_API_KEY:
        print(
            f"❌ CRITICAL at_save: AIRTABLE_API_KEY manquant — aucune persistance Airtable "
            f"(tel={phone}, ref={conv.get('ref')}, step={conv.get('step')})"
        )
        return

    try:
        d   = conv["data"]
        ref = conv.get("ref") or make_ref(phone)
        conv["ref"] = ref

        pax   = d.get("passengers") or 1
        names = d.get("passenger_names") or []
        brut, net, com, per_pax = calc_amounts(pax)

        # Date vol → YYYY-MM-DD
        date_vol_at = fmt_date_for_airtable(d.get("flight_date") or "")

        # Incident → option singleSelect exacte
        incident_at = INCIDENT_AT.get(d.get("incident_type") or "", "")

        # Champs communs à toutes les lignes du dossier
        common = {
            F_REF_DOSSIER:    ref,
            F_DATE_DOSSIER:   datetime.now().strftime("%Y-%m-%d"),
            F_WHATSAPP:       str(phone),
            F_STATUT_DOSSIER: STATUT_DOSSIER_DEFAUT,
            F_STATUT_SUIVI:   STATUT_SUIVI_DEFAUT,
        }
        if d.get("airline"):
            common[F_COMPAGNIE] = d["airline"]
        if d.get("flight_number"):
            common[F_NUMERO_VOL] = d["flight_number"]
        if date_vol_at:
            common[F_DATE_VOL] = date_vol_at
        if d.get("pnr"):
            common[F_PNR] = d["pnr"].strip().upper()
        if incident_at:
            common[F_TYPE_INCIDENT] = incident_at
        if F_ITINERAIRE:
            it_main = (d.get("itinerary") or "").strip()
            it_compl = (d.get("itinerary_compl_note") or "").strip()
            if it_main and it_compl:
                it_persist = f"{it_main} | {it_compl}"[:8000]
            elif it_main:
                it_persist = it_main[:8000]
            elif it_compl:
                it_persist = it_compl[:8000]
            else:
                it_persist = ""
            if it_persist:
                common[F_ITINERAIRE] = it_persist

        existing = at_find(ref)

        extra_bits = []
        if d.get("itinerary"):
            extra_bits.append(f"Itinéraire: {d['itinerary']}")
        if d.get("itinerary_compl_note") and not d.get("itinerary"):
            extra_bits.append(f"Compl. trajet: {d['itinerary_compl_note']}")
        if d.get("codeshare_note"):
            extra_bits.append(d["codeshare_note"])
        if d.get("operating_airline"):
            extra_bits.append(f"Opéré par: {d['operating_airline']}")
        if d.get("has_minors"):
            extra_bits.append("Mineur(s): oui")
        if d.get("claim_rt_leg"):
            extra_bits.append(f"Sens voyage déclaré: {d['claim_rt_leg']}")
        if d.get("itinerary_compl_note") and d.get("itinerary"):
            extra_bits.append(f"Compl. trajet: {d['itinerary_compl_note']}")
        if d.get("early_route_shape"):
            extra_bits.append(f"Parcours déclaré: {d['early_route_shape']}")
        if d.get("expert_phone_lang"):
            extra_bits.append(f"Langue expert (vocal): {d['expert_phone_lang']}")
        rem_extra = (" | " + " ; ".join(extra_bits)) if extra_bits else ""

        if not existing:
            # CRÉATION — 1 ligne par passager
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

            r = requests.post(at_url(), headers=at_headers(),
                              json={"records": records}, timeout=15)
            if r.status_code in (200, 201):
                print(f"✅ Airtable CREATE {pax} records (ref={ref})")
            else:
                print(f"❌ Airtable CREATE {r.status_code}: {r.text[:400]}")

        else:
            # UPDATE — patch les records existants
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

            r = requests.patch(at_url(), headers=at_headers(),
                               json={"records": updates}, timeout=15)
            if r.status_code == 200:
                print(f"✅ Airtable UPDATE {len(updates)} records (ref={ref})")
            else:
                print(f"❌ Airtable PATCH {r.status_code}: {r.text[:400]}")

    except Exception as e:
        print(f"❌ Airtable exception: {e}")
        import traceback
        traceback.print_exc()

def _boarding_image_bytes(image_b64):
    if not image_b64:
        return None
    try:
        return base64.b64decode(image_b64, validate=False)
    except Exception:
        return None

def _guess_image_suffix(raw_bytes):
    if not raw_bytes or len(raw_bytes) < 8:
        return ".jpg", "image/jpeg"
    if raw_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if raw_bytes[:2] == b"\xff\xd8":
        return ".jpg", "image/jpeg"
    if raw_bytes[:4] == b"GIF8":
        return ".gif", "image/gif"
    if raw_bytes[:4] == b"RIFF" and len(raw_bytes) >= 12 and raw_bytes[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".jpg", "image/jpeg"

def at_upload_boarding_attachment(record_id, raw_bytes, ref_tag, pax_index):
    """POST uploadAttachment (API Airtable) sur le champ F_CARTE_EMBARQUEMENT."""
    fid = F_CARTE_EMBARQUEMENT
    if not AIRTABLE_API_KEY or not fid or not record_id or not raw_bytes:
        return False
    if len(raw_bytes) > 5 * 1024 * 1024:
        print("at_upload_boarding_attachment: fichier > 5 Mo (limite Airtable)")
        return False
    ext, mime = _guess_image_suffix(raw_bytes)
    fname = f"carte_{ref_tag or 'dossier'}_p{int(pax_index) + 1}{ext}"
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    payload = {"contentType": mime, "file": b64, "filename": fname}
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}
    # Airtable sert souvent l’upload sur content.airtable.com ; api… peut renvoyer 404 selon les bases.
    urls = (
        f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/{fid}/uploadAttachment",
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/{fid}/uploadAttachment",
    )
    last_err = None
    for url in urls:
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code in (200, 201):
                return True
            last_err = f"{url.split('/')[2]} {r.status_code}: {r.text[:400]}"
        except Exception as e:
            last_err = f"{url.split('/')[2]} exc: {e}"
    print(f"Airtable uploadAttachment failed: {last_err}")
    return False

def at_pick_record_id_for_passenger(recs, names, i_0based):
    """Record Airtable pour le passager i (0-based)."""
    if not recs or i_0based < 0:
        return None
    target = ""
    if isinstance(names, list) and i_0based < len(names):
        target = str(names[i_0based]).strip()
    if target:
        for r in recs:
            fn = (r.get("fields") or {}).get(F_NOM_PASSAGER)
            if fn and str(fn).strip() == target:
                return r.get("id")
    if i_0based < len(recs):
        return recs[i_0based].get("id")
    return None

def at_boarding_attach_to_indices(phone, conv, image_b64, indices_0based, lang):
    """
    Enregistre la même image sur les lignes passagers indiquées (indices 0-based).
    Crée les lignes Airtable si le dossier n'existe pas encore.
    Retourne le nombre d'uploads réussis.
    """
    if not F_CARTE_EMBARQUEMENT or not image_b64:
        if image_b64 and not F_CARTE_EMBARQUEMENT:
            print("at_boarding_attach_to_indices: AIRTABLE_F_CARTE_EMB vide — skip upload")
        return 0
    raw = _boarding_image_bytes(image_b64)
    if not raw:
        return 0
    if len(raw) > 5 * 1024 * 1024:
        send(
            phone,
            "⚠️ *Image trop lourde* (max 5 Mo pour l’enregistrement du billet sur notre dossier). "
            "Réduisez la taille ou renvoyez une capture plus compressée."
            if lang == "fr"
            else "⚠️ *Image too large* (max 5 MB for boarding-pass storage). Please send a smaller or more compressed photo.",
        )
        print(f"at_boarding_attach_to_indices: image {len(raw)} bytes > 5 Mo (tel={phone})")
        return 0
    d = conv["data"]
    ref = conv.get("ref") or make_ref(phone)
    conv["ref"] = ref
    names = list(d.get("passenger_names") or [])
    clean = []
    seen = set()
    for x in indices_0based or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i < 0 or i in seen:
            continue
        seen.add(i)
        clean.append(i)
    if not clean:
        return 0
    recs = at_find(ref)
    if not recs:
        at_save(phone, conv)
        recs = at_find(ref)
    if not recs:
        print(f"at_boarding_attach_to_indices: aucun record Airtable pour ref={ref} après at_save — skip upload")
        return 0
    n_ok = 0
    for i in sorted(clean):
        rid = at_pick_record_id_for_passenger(recs, names, i)
        if rid and at_upload_boarding_attachment(rid, raw, ref, i):
            n_ok += 1
    return n_ok

def _boarding_attach_idx_plan(d, info, step_before, after_passengers, idx_for_attach):
    """Indices 0-based des lignes passager où attacher la même image de billet."""
    max_p = int(d.get("passengers") or 6)
    if max_p < 1:
        max_p = 1
    pax = int(d.get("passengers") or 1)
    inf = info if isinstance(info, dict) else {}
    vis_list = _passenger_names_from_vision(inf, max_p)
    if step_before == "passenger_names":
        return [min(idx_for_attach, max(0, pax - 1))]
    if step_before == "passenger_names_confirm":
        nm = d.get("passenger_names") or []
        return list(range(min(len(nm), pax)))
    if after_passengers:
        return [0]
    if vis_list:
        return list(range(min(len(vis_list), pax)))
    return [0]

# ===== MESSAGES DU FLUX =====

def _privacy_consent_footer(lang):
    """
    Bloc légal + confidentialité au démarrage de la collecte « utile » (après choix passagers).
    Structure inspirée des plateformes type AirHelp : nature du service, honoraires, alternatives,
    droit de rétractation, données — adapté Robin des Airs / robindesairs.eu (pas un copier-coller).
    """
    if lang == "en":
        return (
            "──────────────\n"
            "📜 *What this service is*\n\n"
            "*Robin des Airs* (" + RDA_DOMAIN.rstrip("/") + ") helps you **open and follow** a passenger "
            "compensation file under **EU Regulation 261/2004** (and related rules where applicable). "
            "This WhatsApp flow collects **information** only; the **contractual relationship and mandate** "
            "are those on **our website / mandate document**.\n\n"
            "We are **not a law firm** and this bot does **not** provide tailored legal advice. Amounts shown "
            "(e.g. *600 € gross* tier) are **indicative**; any payment depends on **facts**, **distance** and the airline.\n\n"
            "💶 *Fees:* you owe **nothing** unless compensation is **actually obtained**; if we succeed, "
            "Robin des Airs’ fee is as set out in the **mandate** (e.g. **25%** commission on the amount recovered — "
            "the net share shown in the flow is an **example**).\n\n"
            "🔄 *Other options:* you may claim **directly** from the airline, use **public ADR / mediation** schemes "
            "(varies by country), or a lawyer. Some routes are **free**; airline participation is not always mandatory.\n\n"
            "🛡️ *Right of withdrawal (EU / UK-style consumers):* if you are a **consumer**, you generally have "
            "**14 days** to cancel a distance contract — **unless** you expressly request **immediate performance** "
            "before that period ends (*Consumer Code / similar*).\n\n"
            "──────────────\n"
            "🔒 *Personal data*\n\n"
            "We are about to open your claim file. Your data are processed **for this claim**, stored securely "
            "and in line with **GDPR**. They are **not sold** and are shared **only with the airline** as needed.\n\n"
            "By continuing you accept this processing and confirm you have seen our **Terms** and **Privacy policy**:\n"
            f"👉 *Terms:* {TERMS_URL}\n"
            f"👉 *Privacy:* {PRIVACY_POLICY_URL}"
        )
    return (
        "──────────────\n"
        "📜 *Nature du service (résumé)*\n\n"
        "*Robin des Airs* (" + RDA_DOMAIN.rstrip("/") + ") vous assiste pour **constituer et suivre** un dossier "
        "d’indemnisation passagers sur la base du **règlement (UE) n°261/2004** (et textes connexes le cas échéant). "
        "Ce fil WhatsApp sert à la **collecte d’informations** ; la **relation contractuelle** et les **pouvoirs** "
        "figurent sur **robindesairs.eu** et dans le **document de mandat**.\n\n"
        "Robin des Airs **n’est pas un cabinet d’avocats** : pas de **conseil juridique personnalisé** via ce bot. "
        "Les montants affichés (ex. *600 € bruts* / palier) sont **indicatifs** ; tout versement dépend des **faits**, "
        "des **distances** et de la **compagnie**.\n\n"
        "💶 *Rémunération* : vous ne devez **rien** tant qu’aucune indemnité n’est **effectivement obtenue** ; "
        "en cas de succès, la rémunération est celle du **mandat** (p.ex. commission **25 %** sur l’indemnité — "
        "le **net** affiché dans le tunnel est un **exemple**).\n\n"
        "🔄 *Autres voies* : réclamation **directe** auprès de la compagnie, **médiation / conciliation** des transports "
        "(selon pays), avocat. Souvent **gratuites** ; la participation des compagnies n’est pas toujours obligatoire.\n\n"
        "🛡️ *Rétractation (consommateur)* : si vous êtes **consommateur**, vous disposez en principe de **14 jours** "
        "pour revenir sur un contrat à distance — **sauf** si vous demandez l’**exécution immédiate** avant la fin de ce délai "
        "(*art. L. 221-25 Code de la consommation*).\n\n"
        "──────────────\n"
        "🔒 *Données personnelles*\n\n"
        "Nous allons constituer votre dossier. Vos données sont traitées **pour cette réclamation**, hébergées de façon "
        "sécurisée et **conformes au RGPD**. Elles ne sont **pas vendues** et ne sont transmises **qu’à la compagnie aérienne** "
        "dans le cadre utile au dossier.\n\n"
        "En poursuivant, vous acceptez ce traitement et prenez connaissance des **CGU** et de la **politique de confidentialité** :\n"
        f"👉 *Conditions générales* : {TERMS_URL}\n"
        f"👉 *Confidentialité* : {PRIVACY_POLICY_URL}"
    )

def _privacy_short_notice(lang):
    """
    Mention courte au tout premier message (comme un bandeau + lien sur un site),
    avant le simple choix du nombre de passagers — pas le paragraphe détaillé.
    """
    if os.environ.get("PRIVACY_HIDE_SHORT_NOTICE", "").strip().lower() in ("1", "true", "yes", "on"):
        return ""
    if lang == "en":
        return (
            "\n\n📋 *Legal (short):* this channel helps open an EU261-related file on robindesairs.eu — "
            "not legal advice; fees only if we obtain compensation. "
            f"*Terms:* {TERMS_URL} · *Privacy:* {PRIVACY_POLICY_URL}"
        )
    return (
        "\n\n📋 *Rappel légal (court) :* ce canal sert à ouvrir un dossier lié au règlement UE 261 sur robindesairs.eu — "
        "pas de conseil juridique personnalisé ; rémunération uniquement en cas de succès. "
        f"*Conditions :* {TERMS_URL} · *Confidentialité :* {PRIVACY_POLICY_URL}"
    )

def q_passengers(phone, lang, conv=None):
    """Étape 1 — Accueil + choix passagers (1–5 dossier auto, 6 = rappel expert 6+)."""
    conv = conv or conversations.get(phone) or {"data": {"lang": lang}}
    options = []
    for n in range(1, 6):
        brut, net, _, _ = calc_amounts(n)
        bs = fmt_money_space(brut)
        ns = fmt_money_space(net)
        if lang == "en":
            lab = f"{n} person{'s' if n > 1 else ''} · up to {bs} €"
            desc = f"~{ns} € net for you"
        else:
            lab = f"{n} pers. · jusqu'à {bs} €" if n > 1 else "1 pers. · 600 €"
            desc = f"~{ns} € nets pour vous"
        options.append(
            {
                "id": str(n),
                "label": lab,
                "list_title": f"{n} · {bs} €",
                "desc": desc,
            }
        )
    options.append(
        {
            "id": "6",
            "label": "6+ · expert vous rappelle" if lang == "fr" else "6+ · expert calls you",
            "list_title": "6+ · expert 📞",
            "desc": "Climbie" if lang == "fr" else "Expert callback",
        }
    )
    if lang == "en":
        body = (
            "👋 Welcome to *Robin des Airs* 🏹\n\n"
            "Don't leave money with the airline.\n\n"
            "A delayed or cancelled flight may entitle you to\n"
            "*up to €600 per person* in legal compensation (long-haul example).\n\n"
            "💶 *Examples (indicative):*\n"
            "• 1 person → *€600*\n"
            "• 2 people → *€1,200*\n"
            "• 3 people → *€1,800*\n"
            "_(You keep ~75% net if we recover — 25% fee only on success.)_\n\n"
            "⚖️ *Zero upfront fees.* We take *25%*\n"
            "only when you receive your money.\n\n"
            "⏱️ *Your file opens in about 2 minutes* — a few short questions here.\n\n"
            "👥 *How many people are you claiming for?*"
        )
        lbtn = "Choose"
    else:
        body = (
            "👋 Bienvenue chez *Robin des Airs* 🏹\n\n"
            "Ne laissez pas votre argent\n"
            "à la compagnie aérienne.\n\n"
            "Votre vol retardé ou annulé peut donner droit\n"
            "à *600 € par personne* (indemnité légale, vol long-courrier).\n\n"
            "💶 *Exemples indicatifs :*\n"
            "• 1 personne → *600 €*\n"
            "• 2 personnes → *1 200 €*\n"
            "• 3 personnes → *1 800 €*\n"
            "_(Vous touchez ~75 % nets si indemnité obtenue — 25 % de commission seulement en cas de succès.)_\n\n"
            "⚖️ *Zéro frais.* On prend 25%\n"
            "uniquement si vous recevez votre argent.\n\n"
            "⏱️ *Votre dossier s’ouvre en environ 2 minutes* — quelques questions courtes ici.\n\n"
            "👥 *Pour combien réclamez-vous ?*"
        )
        lbtn = "Choisir"
    ok_poll = send_tunnel_poll(phone, conv, body, options, list_button_text=lbtn)
    if not ok_poll:
        lines = "\n".join(f"*{o['id']}* — {o['label']}" for o in options)
        send(
            phone,
            f"{body}\n\n{lines}\n\n"
            + (
                "Répondez par un chiffre *1* à *6*."
                if lang == "fr"
                else "Reply with a number *1* to *6*."
            ),
        )

def user_wants_fresh_start(text):
    """Recommencer le dossier depuis le début (hors étape terminée)."""
    if not text or len(text.strip()) < 3:
        return False
    low = text.lower().strip()
    needles = (
        "recommencer", "recommence", "nouveau dossier",
        "effacer le dossier", "effacer dossier", "efface le dossier", "efface dossier",
        "tout effacer", "repartir de zero", "repartir à zéro", "repartir a zero",
        "reset dossier", "restart", "start over", "new claim", "new dossier",
        "annule le dossier", "j annule", "j'annule", "annuler le dossier",
        "on recommence", "refaire le dossier", "efface tout", "effacer tout",
        "efface conversation", "zapper le dossier", "perte de temps recommencer",
        "menu dossier", "raz dossier", "from scratch",
    )
    if low in ("recommencer", "restart", "reset") or re.search(r"\bmenu\b", low):
        return True
    return any(n in low for n in needles)

def user_wants_expertise_rappel(text):
    """Demande explicite de rappel / expert (option 6 = 6 personnes ou plus)."""
    low = (text or "").lower().strip()
    needles = (
        "rappel expertise", "rappel d'expertise", "rappel expert",
        "me rappeler", "rappel téléphonique", "rappel telephonique",
        "appelez-moi", "appelez moi", "être rappelé", "etre rappele", "etre rappelé",
        "rappel climbie", "parler à un expert", "parler a un expert",
        "besoin d'un expert", "besoin dun expert", "conseiller humain",
        "expertise prioritaire",
    )
    return any(n in low for n in needles)

def amount_potential_box(pax):
    """Bloc montant indicatif (même rendu que l'étape incident)."""
    brut, net, _, _ = calc_amounts(pax)
    box = (
        f"╔══════════════════════╗\n"
        f"║  💶 MONTANT POTENTIEL  ║\n"
        f"║                        ║\n"
        f"║   *{brut} EUR*{' ' * (8 - len(str(brut)))}         ║\n"
        f"║                        ║\n"
        f"║  ✅ NET POUR VOUS :    ║\n"
        f"║   *{net} EUR* (75%)    ║\n"
        f"╚══════════════════════╝"
    )
    return brut, net, box

def q_boarding_after_pax(phone, lang, conv, pax=None):
    """Message 3 : preuves carte / billet — sondage photo / saisie manuelle."""
    if lang == "en":
        body = (
            "*📸 PROOF — Boarding pass / booking confirmation*\n\n"
            "👍 We'll save you time.\n\n"
            "*The upside:* our system reads a sharp photo and fills in your file — it's the fastest step!\n\n"
            "*How would you like to continue?*"
        )
        options = [
            {"id": "1", "label": "Send a boarding pass photo", "button": "📸 Photo"},
            {"id": "2", "label": "Enter details manually", "button": "⌨️ Manual"},
        ]
    else:
        body = (
            "*📸 PREUVES — Carte d'embarquement / confirmation*\n\n"
            "👍 On va vous faire gagner du temps.\n\n"
            "*L'avantage* : une photo nette permet de remplir le dossier automatiquement — c'est le plus rapide !\n\n"
            "*Comment souhaitez-vous continuer ?*"
        )
        options = [
            {"id": "1", "label": "Envoyer une photo de carte", "button": "📸 Photo"},
            {"id": "2", "label": "Saisir les infos à la main", "button": "⌨️ Manuel"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_boarding_next_passenger(phone, lang, pax_index, total_pax):
    """Après le 1er billet : même logique pour les passagers suivants (photo ou nom seul, billet demandé plus tard)."""
    if lang == "en":
        msg = (
            f"*📸 PROOF — Boarding pass / booking confirmation (passenger {pax_index}/{total_pax})*\n\n"
            "👍 Same idea as before: send a *photo* of this passenger’s boarding pass or e-ticket "
            "*(flat, no glare)*.\n\n"
            "*The upside:* our system can still fill in flight details automatically.\n\n"
            "⌨️ *No pass handy?* Reply *2* or *B*: you can send *First LAST* right away — "
            "we’ll ask for a *boarding-pass photo later* to complete the file.\n\n"
            "_A photo is still useful when you have it (airline / PNR / route)._"
        )
    else:
        msg = (
            f"*📸 PREUVES — Carte d'embarquement / confirmation de réservation (passager {pax_index}/{total_pax})*\n\n"
            "👍 Même principe que pour le premier passager : envoyez une *photo* de la carte ou du billet "
            "de cette personne *(bien à plat, sans reflets)*.\n\n"
            "*L'avantage* : notre système peut encore compléter automatiquement les infos du vol.\n\n"
            "⌨️ *Pas la carte sous la main ?* Tapez *2* ou *B* : vous pouvez envoyer tout de suite le "
            "*prénom* et le *nom* — nous vous demanderons une *photo du billet plus tard* pour finaliser le dossier.\n\n"
            "_La photo reste utile dès que vous l’avez (compagnie, PNR, trajet)._"
        )
    send(phone, msg)

def q_passenger_name_post_add_confirm(phone, lang, conv, recorded_name):
    """Après chaque nom saisi : confirmer ou corriger avant le passager suivant."""
    if lang == "en":
        body = f"✅ *{recorded_name}* saved."
        options = [
            {"id": "1", "label": "Correct — continue", "button": "✅ Correct"},
            {"id": "2", "label": "Fix this name", "button": "✏️ Fix"},
        ]
    else:
        body = f"✅ *{recorded_name}* enregistré."
        options = [
            {"id": "1", "label": "C'est correct", "button": "✅ Correct"},
            {"id": "2", "label": "Corriger ce nom", "button": "✏️ Corriger"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_pax_ack_route(phone, lang, conv):
    """Juste après le nombre de passagers : direct vs correspondance."""
    pax = (conv.get("data") or {}).get("passengers") or 1
    if lang == "en":
        body = (
            f"{pax_registered_ack(pax, 'en')}\n\n"
            "✈️ For this trip: *direct flight* or *with a connection*?"
        )
        options = [
            {"id": "1", "label": "Direct flight", "button": "✈️ Direct"},
            {"id": "2", "label": "With connection", "button": "🔄 Connection"},
        ]
    else:
        body = (
            f"{pax_registered_ack(pax, 'fr')}\n\n"
            "✈️ Sur ce trajet : vol *direct* ou *avec correspondance* ?"
        )
        options = [
            {"id": "1", "label": "Vol direct", "button": "✈️ Direct"},
            {"id": "2", "label": "Avec correspondance", "button": "🔄 Escale"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_pax_contact_lang(phone, lang, conv):
    """Après le type de parcours : montant indicatif + choix de langue pour les experts."""
    d = conv.get("data") or {}
    pax = d.get("passengers") or 1
    _, net, _, _ = calc_amounts(pax)
    net_s = fmt_money_space(net)
    options = []
    for i, (_c, fl, lab_fr, lab_en) in enumerate(EXPERT_LANG_OPTIONS, start=1):
        lab = lab_fr if lang == "fr" else lab_en
        options.append(
            {
                "id": str(i),
                "label": f"{fl} {lab}",
                "list_title": f"{fl} {lab}"[:24],
            }
        )
    if lang == "en":
        body = (
            f"✅ *Got it* — we'll push for *up to {net_s} € net* for your group. 🚀\n\n"
            "Which language should our experts use for voice notes and follow-up?"
        )
        lbtn = "Language"
    else:
        body = (
            f"✅ *C'est noté* — nous visons jusqu'à *{net_s} € net* pour votre groupe. 🚀\n\n"
            "Dans quelle langue nos experts doivent-ils vous contacter *(vocal, suivi)* ?"
        )
        lbtn = "Langue"
    send_tunnel_poll(phone, conv, body, options, list_button_text=lbtn)

def q_pax_voice_confirm(phone, lang, conv):
    """Confirmation courte après le choix de langue expert — avant l’incident."""
    d = conv.get("data") or {}
    code = d.get("expert_phone_lang") or "fr"
    label = _expert_lang_display(code, lang)
    if lang == "en":
        body = (
            f"Perfect! ✅ Voice notes, if needed, will be in *{label}*.\n\n"
            "Ready to continue?"
        )
        options = [{"id": "1", "label": "Continue", "button": "✅ Continue"}]
    else:
        body = (
            f"Parfait ! ✅ Les vocaux, si besoin, seront en *{label}*.\n\n"
            "On continue ?"
        )
        options = [{"id": "1", "label": "Continuer", "button": "✅ Continuer"}]
    send_tunnel_poll(phone, conv, body, options)

def q_incident(phone, lang, conv, pax=None):
    """Choix du type d’incident (après langue / confirmation vocale)."""
    if lang == "en":
        body = "⚖️ *What happened on this flight?*"
        options = [
            {"id": "1", "label": "Delay (+3 h at arrival)", "button": "⏱ Delay"},
            {"id": "2", "label": "Flight cancelled", "button": "❌ Cancelled"},
            {"id": "3", "label": "Overbooking", "button": "🚫 Overbooking"},
        ]
    else:
        body = "⚖️ *Que s'est-il passé sur ce vol ?*"
        options = [
            {"id": "1", "label": "Retard (+3 h à l'arrivée)", "button": "⏱ Retard"},
            {"id": "2", "label": "Vol annulé", "button": "❌ Annulé"},
            {"id": "3", "label": "Surbooking", "button": "🚫 Surbooking"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_airline(phone, lang, conv=None):
    conv = conv or conversations.get(phone) or {"data": {"lang": lang}}
    options = []
    for k, v in AIRLINES_MAP.items():
        short = v[:22] if len(v) > 22 else v
        options.append(
            {
                "id": k,
                "label": v,
                "list_title": short,
            }
        )
    options.append(
        {
            "id": "9",
            "label": "Autre compagnie" if lang == "fr" else "Other airline",
            "list_title": "Autre ✏️" if lang == "fr" else "Other ✏️",
            "desc": "Tapez le nom" if lang == "fr" else "Type the name",
        }
    )
    if lang == "en":
        body = "🛫 *Which airline?*\n\n_Open the list — or type the name / send a boarding pass photo._"
        lbtn = "Airlines"
    else:
        body = "🛫 *Quelle compagnie aérienne ?*\n\n_Ouvrez la liste — ou tapez le nom / envoyez une photo de carte._"
        lbtn = "Compagnies"
    send_tunnel_poll(phone, conv, body, options, list_button_text=lbtn)

def q_pnr(phone, lang, airline):
    if lang == "en":
        msg = (
            f"✅ *{airline}* noted!\n\n"
            "📋 *PNR / Booking reference*\n"
            "(often 5–6 characters on your confirmation email; sometimes 4)\n\n"
            "Example: *ABC12* or *ABC123*\n\n"
            "_(Don't have it? Reply *SKIP* — photo also works.)_"
        )
    else:
        msg = (
            f"✅ *{airline}* noté !\n\n"
            "📋 *PNR / Code de réservation*\n"
            "(souvent 5–6 caractères sur l'email de confirmation ; parfois 4)\n\n"
            "Exemple : *ABC12* ou *ABC123*\n\n"
            "_(Pas le code ? Répondez *SKIP* — une photo de billet suffit aussi.)_"
        )
    send(phone, msg)

def next_after_airline_pick(phone, lang, conv):
    """Après choix de la compagnie : ne redemande pas PNR / n° de vol / date déjà fusionnés depuis la carte."""
    d = conv["data"]
    air = d.get("airline") or ""
    pnr_clean = re.sub(r"[^A-Za-z0-9]", "", (d.get("pnr") or "").upper())
    if len(pnr_clean) >= MIN_PNR_LEN:
        d["pnr"] = pnr_clean[:8]
    if d.get("flight_number") and d.get("flight_date"):
        advance_after_flight_date_complete(phone, conv, lang)
        return
    if d.get("flight_number"):
        conv["step"] = "flight_date"
        q_flight_date(phone, lang, conv)
        return
    if len(pnr_clean) >= MIN_PNR_LEN:
        conv["step"] = "flight_number"
        q_flight_number(phone, lang)
    else:
        conv["step"] = "pnr_input"
        q_pnr(phone, lang, air)

def advance_after_incident(phone, lang, conv):
    """
    Après la preuve billet (ou saisie manuelle sans photo) : ne pas afficher le menu compagnie si déjà connu
    (données fusionnées depuis la carte, ou n° de vol suffisant pour deviner l'IATA).
    """
    d = conv["data"]
    if not (d.get("airline") or "").strip():
        fn_raw = re.sub(r"[\s]+", "", (d.get("flight_number") or "").upper())
        guess = airline_guess_from_flight_number(fn_raw) if fn_raw else None
        if guess:
            d["airline"] = guess
    if (d.get("airline") or "").strip():
        nm = (d.get("airline") or "").strip()
        send(
            phone,
            f"✅ *{nm}* retenue pour la suite _(billet ou n° de vol)_ — pas besoin de la resaisir."
            if lang == "fr"
            else f"✅ Keeping *{nm}* _(from your pass or flight number)_ — no need to enter it again.",
        )
        next_after_airline_pick(phone, lang, conv)
    else:
        conv["step"] = "airline"
        q_airline(phone, lang, conv)

def q_flight_number(phone, lang):
    if lang == "en":
        msg = "✈️ *Flight number?*\nE.g. *SN271* — or send a boarding pass photo."
    else:
        msg = "✈️ *Numéro de vol ?*\nEx. *SN271* — ou une photo de carte d'embarquement."
    send(phone, msg)

def _claim_window_min_date(today=None):
    """Date minimale indicative (rétroactivité ~5 ans)."""
    t = today or date.today()
    return t - timedelta(days=5 * 366)

def _years_for_partial_ticket_dm(dm, today=None):
    """
    Jour/mois sans année : années où la date complète est déjà passée et pas hors fenêtre 5 ans.
    Ex. 7 oct. + aujourd'hui 12 mai 2026 → 2026 exclu (vol « dans le futur »).
    """
    t = today or date.today()
    tmin = _claim_window_min_date(t)
    if not (dm and isinstance(dm, (list, tuple)) and len(dm) == 2):
        return None
    day_s, mon_s = dm[0], dm[1]
    try:
        di = int(str(day_s).lstrip("0") or "0")
        mi = int(str(mon_s).lstrip("0") or "0")
    except (TypeError, ValueError):
        return None
    if not (1 <= mi <= 12 and 1 <= di <= 31):
        return None
    out = []
    for y in range(t.year, t.year - 14, -1):
        try:
            dt = date(y, mi, di)
        except ValueError:
            continue
        if dt > t:
            continue
        if dt < tmin:
            continue
        out.append(y)
        if len(out) >= 5:
            break
    return out

def _parsed_dd_mm_yyyy_to_date(s):
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s*$", (s or "").strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None

def _is_valid_claim_flight_date_str(parsed):
    """Date JJ/MM/AAAA : passée et pas manifestement hors fenêtre 5 ans."""
    dt = _parsed_dd_mm_yyyy_to_date(parsed)
    if not dt:
        return True
    t = date.today()
    if dt > t:
        return False
    if dt < _claim_window_min_date(t):
        return False
    return True

def q_flight_date(phone, lang, conv):
    cy = datetime.now().year
    today = date.today()
    dm = conv["data"].get("pending_ticket_dm")
    ticket_dm = bool(dm and isinstance(dm, (list, tuple)) and len(dm) == 2)

    # Sans indice jour/mois billet : privilégier la date complète en un message (exemples sur une ligne).
    if not ticket_dm:
        conv["data"]["temp_years"] = [cy, cy - 1, cy - 2, cy - 3, cy - 4]
        lines = "\n".join(f"{i + 1}️⃣  {conv['data']['temp_years'][i]}" for i in range(5))
        tail_fr = f"\n\n6️⃣  Avant {cy - 4} _(hors rétroactivité 5 ans)_"
        tail_en = f"\n\n6️⃣  Before {cy - 4} _(outside 5-year limit)_"
        if lang == "en":
            msg = (
                "📅 *Flight date*\n\n"
                "In *one message*, e.g. `12/05/2024`, `2024-05-12` or `12 May 2024`.\n\n"
                "_Or_ pick the *year* first with the buttons below.\n\n"
            ) + lines + tail_en
        else:
            msg = (
                "📅 *Date du vol*\n\n"
                "En *un message* : `12/05/2024`, `2024-05-12` ou `12 mai 2024`.\n\n"
                "_Sinon_, choisissez d’abord l’*année* avec les touches ci-dessous.\n\n"
            ) + lines + tail_fr
        send(phone, msg)
        return

    partial_years = _years_for_partial_ticket_dm(dm, today)
    if partial_years is not None:
        yrs = partial_years if partial_years else [cy - 1, cy - 2, cy - 3]
        conv["data"]["temp_years"] = yrs
        lines = "\n".join(f"{i + 1}️⃣  {y}" for i, y in enumerate(yrs))
    else:
        conv["data"]["temp_years"] = [cy, cy - 1, cy - 2, cy - 3, cy - 4]
        lines = "\n".join(f"{i + 1}️⃣  {conv['data']['temp_years'][i]}" for i in range(5))

    intro_en = intro_fr = ""
    if ticket_dm:
        day_s, mon_s = dm[0], dm[1]
        try:
            mw = month_word(mon_s, lang)
            di = int(day_s.lstrip("0") or "0")
        except ValueError:
            mw, di = mon_s, day_s
        if lang == "en":
            intro_en = (
                f"🎫 Your boarding pass shows *{mw} {di}*, but **not the year**.\n"
                f"*Which calendar year* was this flight?\n\n"
            )
        else:
            intro_fr = (
                f"🎫 *{di} {mw}* est indiqué sur votre carte d'embarquement, **sans l'année**.\n"
                f"*De quelle année* s'agit-il ?\n\n"
            )
    shortcut_en = (
        "\n\n💡 *Or type the full date in one message:* `DD/MM/YYYY` or `YYYY-MM-DD` "
        "_(e.g. `12/05/2024` or `2024-05-12`) — skips the menus._"
    )
    shortcut_fr = (
        "\n\n💡 *Ou tapez la date complète d’un coup :* `JJ/MM/AAAA` ou `AAAA-MM-JJ` "
        "_(ex. `12/05/2024` ou `2024-05-12`, ou `12 mai 2024`) — sans passer par les menus._"
    )

    if partial_years is not None:
        tail_fr = "\n\n💡 _Si votre année ne figure pas, envoyez la *date complète* (JJ/MM/AAAA)._"
        tail_en = "\n\n💡 _If your year isn’t listed, send the *full date* (DD/MM/YYYY)._"
    else:
        tail_fr = f"\n\n6️⃣  Avant {cy - 4} _(hors rétroactivité 5 ans)_"
        tail_en = f"\n\n6️⃣  Before {cy - 4} _(outside 5-year limit)_"

    if lang == "en":
        msg = (intro_en or "📅 *Which year* did you take this flight?\n\n") + lines + tail_en
        msg += shortcut_en
    else:
        msg = (intro_fr or "📅 *Quelle année* avez-vous pris ce vol ?\n\n") + lines + tail_fr
        msg += shortcut_fr
    send(phone, msg)

def q_flight_month(phone, lang, year):
    y = (year or "").strip() or "?"
    if lang == "en":
        msg = (
            f"📅 *Month?* ({y})\n\n"
            "1️⃣ Jan  2️⃣ Feb  3️⃣ Mar  4️⃣ Apr\n"
            "5️⃣ May  6️⃣ Jun  7️⃣ Jul  8️⃣ Aug\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Dec\n\n"
            "💡 Or type the name, e.g. *October*."
        )
    else:
        msg = (
            f"📅 *Mois ?* ({y})\n\n"
            "1️⃣ Jan  2️⃣ Fév  3️⃣ Mar  4️⃣ Avr\n"
            "5️⃣ Mai  6️⃣ Juin  7️⃣ Juil  8️⃣ Août\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Déc\n\n"
            "💡 Ou le nom du mois, ex. *mai*."
        )
    send(phone, msg)

def q_flight_day(phone, lang, year, month_mm):
    y = (year or "").strip() or "?"
    mw = month_word(month_mm, lang)
    if lang == "en":
        msg = f"📅 *Day?* ({mw} {y}) — reply *1–31*"
    else:
        msg = f"📅 *Jour ?* ({mw} {y}) — *1 à 31*"
    send(phone, msg)

def _flight_brief_for_prompt(conv):
    """Résumé vol + date pour les questions itinéraire (évite la confusion avec le récap carte)."""
    d = conv["data"]
    lang = d.get("lang", "fr")
    fn = (d.get("flight_number") or "").strip() or "—"
    fd = (d.get("flight_date") or "").strip()
    if not fd:
        dm = d.get("pending_ticket_dm")
        if dm and isinstance(dm, (list, tuple)) and len(dm) == 2:
            try:
                di = int(dm[0].lstrip("0") or "0")
                mw = month_word(dm[1], lang)
                fd = f"{di} {mw} (?)" if lang == "fr" else f"{mw} {di} (?)"
            except (ValueError, TypeError):
                fd = "?"
        else:
            fd = "?" if lang == "fr" else "?"
    return fn, fd

def q_itinerary_route_kind(phone, lang, conv):
    """Avant départ/arrivée manuels : type de parcours (direct / escale / aller-retour sur même doc)."""
    fn, fd = _flight_brief_for_prompt(conv)
    if lang == "en":
        body = (
            "🛤️ *How was your trip routed?*\n\n"
            f"We have *{fn}* · *{fd}* (flight + date). We still need the *airports* for the segment you claim."
        )
        options = [
            {"id": "1", "label": "Non-stop", "button": "✈️ Direct"},
            {"id": "2", "label": "With connection", "button": "🔄 Connection"},
            {"id": "3", "label": "Round trip", "button": "🔁 Round trip"},
        ]
    else:
        body = (
            "🛤️ *Comment était le trajet ?*\n\n"
            f"Nous avons *{fn}* · *{fd}* (vol + date). Il nous manque les *aéroports* du segment à indemniser."
        )
        options = [
            {"id": "1", "label": "Vol direct", "button": "✈️ Direct"},
            {"id": "2", "label": "Avec escale", "button": "🔄 Escale"},
            {"id": "3", "label": "Aller-retour", "button": "🔁 A/R"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_itinerary_rt_pick(phone, lang, conv):
    if lang == "en":
        body = "🔁 *Which leg was affected?*"
        options = [
            {"id": "1", "label": "Outbound", "button": "→ Outbound"},
            {"id": "2", "label": "Return", "button": "← Return"},
            {"id": "3", "label": "Both directions", "button": "↔ Both"},
        ]
    else:
        body = "🔁 *Quel sens du voyage est concerné ?*"
        options = [
            {"id": "1", "label": "Vol aller", "button": "→ Aller"},
            {"id": "2", "label": "Vol retour", "button": "← Retour"},
            {"id": "3", "label": "Les deux sens", "button": "↔ Les deux"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def q_itinerary_freeline(phone, lang, conv):
    d = conv["data"]
    mode = d.get("itin_collect_mode") or "connection"
    fn, fd = _flight_brief_for_prompt(conv)
    if mode == "connection":
        if lang == "en":
            msg = (
                "📎 *Connecting / multi-segment route*\n\n"
                f"Ticket line: *{fn}* · *{fd}*.\n\n"
                "Send a **photo of the boarding pass for the disrupted segment**, "
                "or type the **full route** with arrows, e.g.:\n"
                "*BRU → CDG → ABJ*\n"
                "or IATA only: *BRU CDG ABJ*"
            )
        else:
            msg = (
                "📎 *Escale / plusieurs segments*\n\n"
                f"Ligne sur le billet : *{fn}* · *{fd}*.\n\n"
                "Envoyez la **photo de la carte d’embarquement du tronçon où le problème s’est produit**, "
                "ou tapez le **trajet complet** avec des flèches, par ex. :\n"
                "*BRU → CDG → ABJ*\n"
                "ou uniquement les codes IATA : *BRU CDG ABJ*"
            )
    elif mode == "rt_both":
        if lang == "en":
            msg = (
                "📎 *Both outbound and return on the same booking*\n\n"
                f"Flight line on ticket: *{fn}* · *{fd}*.\n\n"
                "Type **both routes** separated by *|*, **most impacted leg first**, e.g.:\n"
                "*CDG → ABJ | ABJ → CDG*\n\n"
                "Or send **two boarding-pass photos** (we’ll ask you to confirm after the first read).\n\n"
                "_For the claim file we mainly register the **first segment you type** before *|*._"
            )
        else:
            msg = (
                "📎 *Aller-retour sur la même réservation*\n\n"
                f"Ligne sur le billet : *{fn}* · *{fd}*.\n\n"
                "Indiquez les **deux trajets** séparés par *|*, **le vol le plus impacté en premier**, ex. :\n"
                "*CDG → ABJ | ABJ → CDG*\n\n"
                "Vous pouvez aussi envoyer **deux cartes d’embarquement** à la suite (nous vous ferons confirmer après la 1ʳᵉ lecture).\n\n"
                "_Pour le dossier, on enregistre d’abord le **segment écrit avant le « | »** (vol principal de la réclamation)._"
            )
    elif mode in ("rt_out", "rt_in"):
        if mode == "rt_out":
            leg_fr, leg_en = "aller", "outbound"
        else:
            leg_fr, leg_en = "retour", "return"
        if lang == "en":
            msg = (
                f"📎 *{leg_en.title()} — route for that leg only*\n\n"
                f"Flight line on ticket: *{fn}* · *{fd}*.\n\n"
                "Send the **boarding pass for that leg**, or type **departure → arrival** "
                "for that segment only (e.g. *CDG → ABJ*)."
            )
        else:
            msg = (
                f"📎 *Vol {leg_fr} — trajet de ce segment uniquement*\n\n"
                f"Ligne sur le billet : *{fn}* · *{fd}*.\n\n"
                "Envoyez la **carte d’embarquement de ce vol**, ou tapez **départ → arrivée** "
                "uniquement pour ce segment (ex. *CDG → ABJ*)."
            )
    else:
        if lang == "en":
            msg = (
                "📎 *Route*\n\n"
                f"Flight line: *{fn}* · *{fd}*.\n\n"
                "Send a **boarding pass photo** or type a route with arrows."
            )
        else:
            msg = (
                "📎 *Trajet*\n\n"
                f"Ligne sur le billet : *{fn}* · *{fd}*.\n\n"
                "Envoyez une **photo de carte** ou tapez un trajet avec des flèches."
            )
    send(phone, msg)

def q_itinerary_departure(phone, lang, conv):
    fn, fd = _flight_brief_for_prompt(conv)
    if lang == "en":
        msg = (
            "🛫 *Departure — disrupted segment*\n\n"
            f"From your ticket we have: ✈️ *{fn}* on *{fd}*.\n"
            "That is the **flight identity**; we still need the **departure airport** of the leg you claim for.\n\n"
            "👉 Type **city or IATA code** (e.g. *BRU* or *Brussels*)."
        )
    else:
        msg = (
            "🛫 *Départ — segment du vol à indemniser*\n\n"
            f"Sur votre billet nous avons : ✈️ *{fn}* · *{fd}*.\n"
            "Ce sont le **numéro et la date** du vol ; il nous manque encore **l’aéroport de départ** du **même segment** que vous réclamez (celui du retard / annulation / surbooking).\n\n"
            "👉 Indiquez la **ville ou le code IATA** (ex. *BRU*, *Bruxelles*)."
        )
    send(phone, msg)

def q_itinerary_arrival(phone, lang, conv):
    dep = (conv["data"].get("temp_itin_dep") or "").strip() or "?"
    if lang == "en":
        msg = (
            "🛬 *Arrival — same segment*\n\n"
            f"Departure you entered: *{dep}*.\n"
            "👉 Now the **arrival** of this **same** flight (final airport of the segment, not your next connection unless that’s the claim).\n\n"
            "_Example: *ABJ* or *Abidjan*_"
        )
    else:
        msg = (
            "🛬 *Arrivée — même segment*\n\n"
            f"Départ indiqué : *{dep}*.\n"
            "👉 Indiquez maintenant l’**arrivée** de **ce même vol** (aéroport final du segment indemnisable — pas la correspondance suivante, sauf si c’est elle qui fait l’objet du litige).\n\n"
            "_Ex. *ABJ* ou *Abidjan*_"
        )
    send(phone, msg)

def q_passenger_name(phone, lang, idx, pax, names_so_far):
    already = ""
    if names_so_far:
        already = "\n".join([f"✅ {i+1}. {n}" for i, n in enumerate(names_so_far)]) + "\n\n"
    same_vol_note = ""
    if pax > 1 and names_so_far:
        same_vol_note = (
            "\nℹ️ _Même vol pour tout le monde : indiquez seulement le *prénom* et le *nom* de ce passager._\n"
            if lang == "fr"
            else "\nℹ️ _Same flight for everyone — just this passenger’s *first and last name*._\n"
        )
    if pax > 1:
        pax_hdr = (
            f"👤 *Passenger {idx} of {pax}*"
            if lang == "en"
            else f"👤 *Passager {idx} sur {pax}*"
        )
    else:
        pax_hdr = "👤 *Passenger*" if lang == "en" else "👤 *Passager*"
    if lang == "en":
        msg = (
            f"{already}"
            f"{pax_hdr}{same_vol_note}\n"
            "Send *First LAST* (last name in caps)\n"
            "Example: *Fatou SALL*"
        )
    else:
        msg = (
            f"{already}"
            f"{pax_hdr}{same_vol_note}\n"
            "Envoyez *Prénom NOM* (nom en majuscules)\n"
            "Exemple : *Aminata TRAORE*"
        )
    send(phone, msg)

def q_passenger_names_confirm(phone, lang, conv):
    """Après le 3ᵉ passager (si pax>3) ou après le dernier : liste à valider avant la suite."""
    d = conv["data"]
    names = d.get("passenger_names") or []
    pax = d.get("passengers") or 1
    lines = "\n".join(f"  {i+1}. *{n}*" for i, n in enumerate(names))
    n_done = len(names)
    partial = n_done < pax
    if lang == "en":
        if partial:
            body = f"{pax_names_confirm_heading(pax, 'en', partial=True, n_done=n_done)}\n\n{lines}"
            options = [
                {"id": "1", "label": f"OK — passenger {n_done + 1}", "button": "✅ Continue"},
                {"id": "2", "label": f"Fix passenger {n_done}", "button": "✏️ Fix"},
            ]
        else:
            body = f"{pax_names_confirm_heading(pax, 'en')}\n\n{lines}"
            options = [
                {"id": "1", "label": "All correct", "button": "✅ Continue"},
                {"id": "2", "label": "Fix last name", "button": "✏️ Fix"},
            ]
    else:
        if partial:
            body = f"{pax_names_confirm_heading(pax, 'fr', partial=True, n_done=n_done)}\n\n{lines}"
            options = [
                {"id": "1", "label": f"OK — passager {n_done + 1}", "button": "✅ Continuer"},
                {"id": "2", "label": f"Corriger passager {n_done}", "button": "✏️ Corriger"},
            ]
        else:
            body = f"{pax_names_confirm_heading(pax, 'fr')}\n\n{lines}"
            options = [
                {"id": "1", "label": "Tout est correct", "button": "✅ Continuer"},
                {"id": "2", "label": "Corriger le dernier", "button": "✏️ Corriger"},
            ]
    send_tunnel_poll(phone, conv, body, options)

def goto_passenger_names_confirm(phone, conv, lang):
    conv["step"] = "passenger_names_confirm"
    q_passenger_names_confirm(phone, lang, conv)

def q_minors(phone, lang, conv):
    """Question mineurs (mandat / représentation légale) avant le message de dépôt final."""
    d = conv["data"]
    names = d.get("passenger_names") or []
    names_lines = "\n".join(f"• *{n}*" for n in names) if names else "• …"
    pax = d.get("passengers") or 1
    if lang == "en":
        body = (
            "👶 *Important legal question*\n\n"
            f"{pax_minors_intro(pax, 'en')}\n\n"
            f"{names_lines}\n\n"
            "⚖️ Required to prepare the correct signing mandate."
        )
        options = [
            {"id": "1", "label": "No — all adults", "button": "✅ All adults"},
            {"id": "2", "label": "Yes — at least one minor", "button": "👶 Minor(s)"},
        ]
    else:
        body = (
            "👶 *Question juridique importante*\n\n"
            f"{pax_minors_intro(pax, 'fr')}\n\n"
            f"{names_lines}\n\n"
            "⚖️ Obligation légale pour préparer le bon mandat de signature."
        )
        options = [
            {"id": "1", "label": "Non — tous majeurs", "button": "✅ Tous majeurs"},
            {"id": "2", "label": "Oui — au moins un mineur", "button": "👶 Mineur(s)"},
        ]
    send_tunnel_poll(phone, conv, body, options)

def show_summary(phone, conv):
    """
    Message « dossier prêt » : mandat + demande de pièces (identité + billet si besoin),
    aligné LegalTech / UX (2 étapes rapides).
    """
    d    = conv["data"]
    lang = d.get("lang", "fr")
    pax  = d.get("passengers") or 1
    ref  = conv.get("ref") or make_ref(phone)
    conv["ref"] = ref

    _, net, _, _ = calc_amounts(pax)
    net_s = fmt_money_space(net)
    names = d.get("passenger_names") or []

    params = {
        "ref":       ref,
        "pax":       pax,
        "vol":       d.get("flight_number", ""),
        "date":      d.get("flight_date", ""),
        "compagnie": d.get("airline", ""),
        "incident":  d.get("incident_type", ""),
        "noms":      ",".join(names),
        "source":    "whatsapp_bot",
    }
    if d.get("pnr"):
        params["pnr"] = d["pnr"]
    dep, arr, via = split_itinerary_for_mandat(d.get("itinerary") or "")
    if dep:
        params["dep"] = dep
    if arr:
        params["arr"] = arr
    if via:
        params["esc1"] = via
    if d.get("has_minors"):
        params["mineurs"] = "1"
    mandat_link = mandat_signing_link(params)
    sign_link = register_mandate_short_link(mandat_link) or mandat_link
    recap_lines = _summary_recap_lines(d, lang)
    recap_block = ""
    if recap_lines:
        title = "*Récapitulatif dossier*" if lang == "fr" else "*Your claim summary*"
        recap_block = f"📋 {title}\n" + "\n".join(recap_lines) + "\n\n"

    if lang == "en":
        msg = (
            "🎉 *Your file is ready to be filed!*\n\n"
            f"{recap_block}"
            f"📁 *File ref:* *{ref}*\n"
            f"💵 *Target net amount (group, indicative):* *{net_s} €*\n\n"
            "Two quick steps left:\n\n"
            "1️⃣ *Sign the mandate* — secure Robin des Airs page, then your signature:\n"
            f"{sign_link}\n\n"
            "2️⃣ *Send proof in this chat:* a readable photo of your *passport or national ID* "
            "+ *boarding pass* or booking confirmation *if we still need it*.\n\n"
            f"🔒 We use your documents *only* for this claim. *Privacy:* {PRIVACY_POLICY_URL}"
        )
    else:
        msg = (
            "🎉 *Dossier prêt à être déposé !*\n\n"
            f"{recap_block}"
            f"📁 *Réf. dossier :* *{ref}*\n"
            f"💵 *Montant net visé (groupe, indicatif) :* *{net_s} €*\n\n"
            "Il reste *2 étapes rapides* :\n\n"
            "1️⃣ *Signature du mandat* — page sécurisée *Robin des Airs*, puis votre signature :\n"
            f"{sign_link}\n\n"
            "2️⃣ *Justificatifs en photos* sur ce fil : *passeport ou CNI lisible* "
            "+ *carte d’embarquement* ou confirmation *si nécessaire*.\n\n"
            f"🔒 Vos pièces ne servent *qu’à ce dossier*. *Confidentialité :* {PRIVACY_POLICY_URL}"
        )
    send(phone, msg)
    at_save(phone, conv)
    conv["step"] = "completed"
    d["_post_submit"] = {
        "active": True,
        "summary_at": time.time(),
        "relances_sent": [],
        "template_mode": False,
        "last_template_at": 0,
        "mandate_ack": False,
        "mandate_signed_server": False,
        "images_after_summary": 0,
        "post_submit_has_id_image": False,
        "post_submit_has_boarding_image": False,
        "air_boarding_attachment": False,
        "air_id_attachment": False,
        "air_mandat_signed": False,
        "_at_sync_at": 0,
        "needs_boarding_hint": not bool(d.get("boarding_evidence_in_flow")),
        "mandat_params": dict(params),
        "short_sign_url": sign_link,
        "air_relance_halt_airtable": False,
    }
    refresh_post_submit_airtable_flags(conv)

# ===== OPENAI (photo carte d'embarquement) =====

def _extract_json_from_gpt(txt):
    """Parse un objet JSON dans la réponse modèle (fences ``` ou bloc brut)."""
    if not txt:
        return None
    s = txt.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s).strip()
    i0, i1 = s.find("{"), s.rfind("}")
    if i0 < 0 or i1 <= i0:
        return None
    try:
        return json.loads(s[i0 : i1 + 1])
    except Exception:
        return None

def _pnr_from_vision_info(info):
    """PNR / record locator depuis le JSON vision (plusieurs clés possibles)."""
    if not info or not isinstance(info, dict):
        return ""
    raw = (info.get("pnr") or info.get("booking_reference") or info.get("record_locator") or "")
    return re.sub(r"[^A-Za-z0-9]", "", (raw or "").upper())

def gpt_read_boarding_pass(image_b64):
    if not OPENAI_API_KEY:
        return {}
    prompt = (
        "Extract data from this boarding pass, mobile boarding pass, or e-ticket screenshot. "
        "Reply with ONLY a JSON object (no markdown), keys:\n"
        '{"flight_number":"","date":"","flight_day":null,"flight_month":null,"airline":"","airline_iata":"","marketing_carrier_iata":"","operating_carrier_iata":"","pnr":"","booking_reference":"","departure":"","arrival":"","route":"","passenger_names":[],"service_direction_guess":"unknown","other_legs_summary":""}\n'
        "- flight_number: exactly as printed (usually the **marketing** flight number; its airline prefix often matches the **ticketing** carrier even if another airline **operates** the flight).\n"
        "- date: ONLY if the full calendar date with year is printed, as DD/MM/YYYY (European). "
        "If the ticket shows day+month but NO year (very common), set date to \"\" and set flight_day + flight_month as integers.\n"
        "- flight_day / flight_month: integers 1-31 and 1-12 when day and month are visible but year is missing or unclear; else null.\n"
        "- airline: **marketing / ticketing** carrier name as printed (larger logo or \"flight by X\"); NOT the \"operated by\" line alone.\n"
        "- airline_iata: 2-letter code of the **marketing** (ticketing) carrier if visible.\n"
        "- marketing_carrier_iata: same as airline_iata if codeshare; else \"\".\n"
        "- operating_carrier_iata: if **codeshare** (\"Operated by / Opéré par / wet lease\"), 2-letter code of the **operating** carrier; else \"\".\n"
        '- pnr: 6-character record locator if visible. Use booking_reference only if the label on the ticket says so; otherwise put the code in pnr (same value in both is OK).\n'
        "- departure / arrival: city or IATA if readable (also fill if you can infer from route).\n"
        "- route: one line e.g. BRU → ABJ or BRU-ABJ or BRUABJ (two IATA codes); use → when possible.\n"
        "- passenger_names: array of ALL passenger names visible on the document, each as \"FirstName LASTNAME\" "
        "(Latin script; if 2 passengers on same pass, two strings; if only surname visible, still include best guess). "
        "Use [] if none readable.\n"
        "- service_direction_guess: if this boarding pass is clearly the **outbound** leg of a round trip on the same booking, \"outbound\"; if clearly **return/inbound**, \"return\"; otherwise \"unknown\".\n"
        "- other_legs_summary: if the same document shows **another flight** (return leg, connection, or second coupon), one short line per extra leg, e.g. \"AF702 CDG-ABJ 10/12\"; else \"\".\n"
        'Use "" for unknown string fields; null for unknown flight_day/flight_month; [] for passenger_names if unknown.'
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]
                }],
                "max_tokens": 520,
            },
            timeout=45,
        )
        txt = r.json()["choices"][0]["message"]["content"]
        return _extract_json_from_gpt(txt) or {}
    except Exception as e:
        print(f"GPT vision error: {e}")
        return {}

def _julian_to_dd_mm_yyyy(jul3: str):
    """
    Date de vol BCBP (jour julien 001–366 sur 3 chiffres) → DD/MM/AAAA.
    Heuristique d'année : parmi les années proches, celle qui minimise l'écart à aujourd'hui.
    """
    try:
        j = int((jul3 or "").strip())
        if j < 1 or j > 366:
            return None
    except (TypeError, ValueError):
        return None
    today = datetime.now().date()
    best_dt = None
    best_abs = None
    for ydelta in range(-3, 4):
        y = today.year + ydelta
        try:
            dt = datetime.strptime(f"{y}-{j:03d}", "%Y-%j")
            d = dt.date()
            ad = abs((d - today).days)
            if best_abs is None or ad < best_abs:
                best_abs = ad
                best_dt = dt
        except ValueError:
            continue
    if not best_dt:
        return None
    return best_dt.strftime("%d/%m/%Y")

def _parse_bcbp_payload_to_dict(pdata: str):
    """
    Extrait vol / PNR / trajet / date / nom depuis une chaîne BCBP type M (ex. IATA test data).
    Le champ « compagnie » dans le segment M1 est le **transporteur opérationnel** → operating_carrier_iata.
    """
    out = {}
    if not pdata or ("M1" not in pdata and "M2" not in pdata):
        return out
    raw = re.sub(r"[\r\n]+", "", pdata).upper()
    # Exemple IATA : M1DESMARAIS/LUC       EABC123 YULFRAAC 0834 326J001A0025 100
    m = re.search(
        r"M[12]([A-Z]+/[A-Z]+)\s+E([A-Z0-9]{6})\s+([A-Z]{3})([A-Z]{3})([A-Z]{2})\s*(\d{4})\s+(\d{3})",
        raw,
    )
    if not m:
        # Variante sans « E » explicite (rare)
        m = re.search(
            r"M[12]([A-Z]+/[A-Z]+)\s+([A-Z0-9]{6})\s+([A-Z]{3})([A-Z]{3})([A-Z]{2})\s*(\d{4})\s+(\d{3})",
            raw,
        )
        if not m:
            return out
        last, pnr, dep, arr, cx, fn, jul = m.groups()
    else:
        last, pnr, dep, arr, cx, fn, jul = m.groups()
    nm = _bcbp_name_field_to_passenger(last)
    if nm:
        out["passenger_names"] = [nm]
    if pnr and len(pnr) >= 6:
        out["pnr"] = pnr[:8]
    if dep and arr:
        out["departure"] = dep
        out["arrival"] = arr
        out["route"] = f"{dep} → {arr}"
    cx2 = (cx or "").strip().upper()[:2]
    if cx2 and fn:
        fn4 = fn[-4:] if len(fn) >= 4 else fn.zfill(4)
        out["flight_number"] = f"{cx2}{fn4}"
        out["operating_carrier_iata"] = cx2
    dfull = _julian_to_dd_mm_yyyy(jul)
    if dfull:
        out["date"] = dfull
    return out

def _bcbp_name_field_to_passenger(last_first_field: str):
    """Champ nom BCBP LAST/FIRST… → 'Prénom NOM' comme le tunnel."""
    s = (last_first_field or "").strip()
    if "/" not in s:
        return None
    last, first = s.split("/", 1)
    last = re.sub(r"\s+", " ", last).strip()
    first = re.sub(r"\s+", " ", first).strip()
    if len(last) < 2 or len(first) < 1:
        return None
    return f"{first.title()} {last.upper()}"

def try_decode_bcbp_from_image_b64(image_b64):
    """
    Tente de lire un QR / code-barres IATA (BCBP type M1/M2) sur l'image.
    Nécessite pyzbar + Pillow (pip install pyzbar pillow). Aucun appel Internet.
    Extrait : vol, PNR, départ/arrivée, date (julien), nom passager, opérateur IATA si présent.
    """
    out = {}
    try:
        import io
        from PIL import Image
        from pyzbar.pyzbar import decode as zdecode
    except ImportError:
        return out
    try:
        raw = base64.b64decode(image_b64, validate=False)
        im = Image.open(io.BytesIO(raw))
        for sym in zdecode(im):
            pdata = None
            for enc in ("utf-8", "latin-1", "iso-8859-1"):
                try:
                    pdata = sym.data.decode(enc, "replace")
                    break
                except Exception:
                    continue
            if not pdata or ("M1" not in pdata and "M2" not in pdata):
                continue
            parsed = _parse_bcbp_payload_to_dict(pdata)
            for k, v in parsed.items():
                if v in (None, "", [], {}):
                    continue
                if k not in out or out.get(k) in (None, "", []):
                    out[k] = v
                elif k == "passenger_names" and isinstance(v, list):
                    combined, seen = [], set()
                    for n in (out.get("passenger_names") or []) + v:
                        if n and str(n).strip().lower() not in seen:
                            seen.add(str(n).strip().lower())
                            combined.append(n)
                    out["passenger_names"] = combined
            # Fallback historique : n° de vol + nom si parseur structuré incomplet
            if not out.get("flight_number"):
                for m in re.finditer(r"\b([A-Z]{2})(\d{3,4})\b", pdata.upper()):
                    if airline_from_iata(m.group(1)):
                        out["flight_number"] = m.group(1) + m.group(2)
                        break
            if not out.get("passenger_names"):
                mname = re.search(r"M[12]([A-Z]+)/([A-Z]+)", pdata.upper())
                if mname:
                    last, first = mname.group(1).strip(), mname.group(2).strip()
                    if len(last) >= 2 and len(first) >= 1:
                        nm = f"{first.title()} {last.upper()}"
                        out.setdefault("passenger_names", []).append(nm)
    except Exception as e:
        print(f"QR/BCBP scan: {e}")
    return out

def read_boarding_pass_merged(image_b64):
    """Vision OpenAI + QR/BCBP local (pyzbar) : le BCBP structure prime sur l'OCR pour les champs stables."""
    qr = try_decode_bcbp_from_image_b64(image_b64)
    gpt = gpt_read_boarding_pass(image_b64) if OPENAI_API_KEY else {}
    merged = dict(gpt) if isinstance(gpt, dict) else {}
    # Données machine-lisibles : priorité au QR quand présent (souvent plus fiable que l'image seule).
    bcbp_priority_keys = frozenset({
        "pnr", "date", "departure", "arrival", "route",
        "flight_number", "operating_carrier_iata",
    })
    for k, v in (qr or {}).items():
        if v in (None, "", [], {}):
            continue
        if k in bcbp_priority_keys:
            merged[k] = v
        elif k == "passenger_names" and isinstance(v, list):
            combined, seen = [], set()
            for n in (merged.get("passenger_names") or []) + v:
                if n and str(n).strip().lower() not in seen:
                    seen.add(str(n).strip().lower())
                    combined.append(n)
            merged["passenger_names"] = combined
        elif k not in merged or merged.get(k) in (None, "", []):
            merged[k] = v
    return merged

def boarding_pass_info_usable(info):
    if not info or not isinstance(info, dict):
        return False
    if (info.get("flight_number") or "").strip():
        return True
    dt = (info.get("date") or "").strip()
    if dt and re.match(r"^\d{2}/\d{2}/\d{4}$", dt):
        return True
    fd = info.get("flight_day")
    if fd is None:
        fd = info.get("day")
    fm = info.get("flight_month")
    if fm is None:
        fm = info.get("month")
    try:
        if fd is not None and fm is not None and 1 <= int(fd) <= 31 and 1 <= int(fm) <= 12:
            return True
    except (TypeError, ValueError):
        pass
    if dt and re.match(r"^\d{1,2}/\d{1,2}$", dt):
        return True
    if len((info.get("airline") or "").strip()) > 2:
        return True
    for key in ("airline_iata", "marketing_carrier_iata", "operating_carrier_iata"):
        raw = re.sub(r"[^A-Z]", "", (info.get(key) or "").upper())
        if len(raw) >= 2 and airline_from_iata(raw):
            return True
    air_short = re.sub(r"[^A-Za-z]", "", (info.get("airline") or "").upper())
    if 2 <= len((info.get("airline") or "").strip()) <= 3 and len(air_short) <= 3 and airline_from_iata(air_short):
        return True
    pnr = _pnr_from_vision_info(info)
    return len(pnr) >= MIN_PNR_LEN

def _format_passenger_name_token(s):
    """Une ligne 'Prénom NOM' / 'FIRST LAST' → même format que le tunnel manuel."""
    s = (s or "").strip()
    if not s:
        return None
    parts = re.split(r"\s+", s)
    if len(parts) < 2:
        return None
    prenom = parts[0].title()
    nom    = " ".join(parts[1:]).upper()
    return f"{prenom} {nom}"

def _passenger_names_from_vision(info, max_pax):
    """Extrait 0..max_pax noms depuis le JSON vision (liste, dicts, ou chaîne)."""
    out = []
    raw = info.get("passenger_names")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                f = _format_passenger_name_token(item)
                if f:
                    out.append(f)
            elif isinstance(item, dict):
                fn = (item.get("first") or item.get("firstName") or item.get("prenom") or "").strip()
                ln = (item.get("last") or item.get("lastName") or item.get("nom") or "").strip()
                if fn and ln:
                    out.append(f"{fn.title()} {ln.upper()}")
                elif (item.get("full") or item.get("name") or "").strip():
                    f = _format_passenger_name_token(str(item.get("full") or item.get("name")))
                    if f:
                        out.append(f)
    elif isinstance(raw, str) and raw.strip():
        for part in re.split(r"[,;/]|(?:\s+et\s+)", raw, flags=re.I):
            part = part.strip()
            if part:
                f = _format_passenger_name_token(part)
                if f:
                    out.append(f)
    seen = set()
    uniq = []
    for n in out:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(n)
        if len(uniq) >= max_pax:
            break
    return uniq[:max_pax]

def _passenger_names_complete(data):
    pax   = data.get("passengers") or 1
    names = data.get("passenger_names") or []
    return len(names) >= pax

def _parse_date_from_vision(info):
    """
    Retourne ("full", "DD/MM/YYYY") | ("partial", ("DD","MM")) | (None, None).
    Beaucoup de billets n'affichent pas l'année : jour/mois seuls → partial.
    """
    date = (info.get("date") or "").strip()
    if date:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s*$", date)
        if m:
            d, mo, y = m.groups()
            return "full", f"{int(d):02d}/{int(mo):02d}/{y}"
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})\s*$", date)
        if m:
            d, mo, yy = m.groups()
            yi = int(yy)
            yi = 2000 + yi if yi < 70 else 1900 + yi
            return "full", f"{int(d):02d}/{int(mo):02d}/{yi}"
        m = re.match(r"^(\d{1,2})/(\d{1,2})\s*$", date)
        if m:
            return "partial", (f"{int(m.group(1)):02d}", f"{int(m.group(2)):02d}")
    fd = info.get("flight_day")
    if fd is None:
        fd = info.get("day")
    fm = info.get("flight_month")
    if fm is None:
        fm = info.get("month")
    try:
        if fd is not None and fm is not None:
            d = int(fd)
            mo = int(fm)
            if 1 <= d <= 31 and 1 <= mo <= 12:
                return "partial", (f"{d:02d}", f"{mo:02d}")
    except (TypeError, ValueError):
        pass
    return None, None

def advance_after_itinerary_collected(phone, conv, lang):
    """Itinéraire complet : noms passagers ou question mineurs."""
    d = conv["data"]
    d.pop("itin_collect_mode", None)
    pax = d.get("passengers") or 1
    if _passenger_names_complete(d):
        goto_passenger_names_confirm(phone, conv, lang)
    else:
        names = list(d.get("passenger_names") or [])
        conv["step"] = "passenger_names"
        if names:
            nxt = len(names) + 1
            d["pax_collect_idx"] = nxt
            if nxt >= 2:
                q_boarding_next_passenger(phone, lang, nxt, pax)
            q_passenger_name(phone, lang, nxt, pax, names)
        else:
            d["pax_collect_idx"] = 1
            q_passenger_name(phone, lang, 1, pax, [])

def advance_after_flight_date_complete(phone, conv, lang):
    """Vol + date complètes : enchaîne itinéraire / noms / mineurs selon le remplissage."""
    d = conv["data"]
    if not (d.get("flight_number") and d.get("flight_date")):
        return
    itin = (d.get("itinerary") or "").strip()
    dep_m, arr_m, _ = split_itinerary_for_mandat(itin)
    route_ok = bool(itin and dep_m and arr_m)
    if route_ok:
        d.pop("itin_collect_mode", None)
    pax = d.get("passengers") or 1
    names = d.get("passenger_names") or []
    if route_ok:
        if _passenger_names_complete(d):
            goto_passenger_names_confirm(phone, conv, lang)
        elif names:
            conv["step"] = "passenger_names"
            nxt = len(names) + 1
            conv["data"]["pax_collect_idx"] = nxt
            if nxt >= 2:
                q_boarding_next_passenger(phone, lang, nxt, pax)
            q_passenger_name(phone, lang, nxt, pax, names)
        else:
            conv["step"] = "passenger_names"
            conv["data"]["pax_collect_idx"] = 1
            q_passenger_name(phone, lang, 1, pax, [])
    else:
        conv["step"] = "itinerary_kind"
        q_itinerary_route_kind(phone, lang, conv)

def _unify_route_display(s):
    """Normalise affichage trajet (→) pour stockage / mandat / Airtable."""
    s = (s or "").strip()
    if not s:
        return s
    s = re.sub(r"\b([A-Za-z]{3})\s*-\s*([A-Za-z]{3})\b", r"\1 → \2", s)
    s = s.replace("➝", "→").replace("⇒", "→")
    s = re.sub(r"\s*->\s*", " → ", s, flags=re.I)
    s = re.sub(r"\s*[–—]\s*", " → ", s)
    compact = re.sub(r"\s+", "", s)
    if re.match(r"^[A-Za-z]{6}$", compact) and "→" not in s:
        u = compact.upper()
        return f"{u[:3]} → {u[3:]}"
    return s

def _itinerary_from_vision_info(info):
    """Construit une ligne d’itinéraire depuis le JSON vision (clés / formats variables)."""
    if not isinstance(info, dict):
        return None
    dep = (
        (info.get("departure") or info.get("from") or info.get("origin") or info.get("departure_airport")
         or info.get("from_airport") or info.get("dep") or info.get("departure_iata") or "")
    )
    arr = (
        (info.get("arrival") or info.get("to") or info.get("destination") or info.get("arrival_airport")
         or info.get("to_airport") or info.get("arr") or info.get("arrival_iata") or "")
    )
    if isinstance(dep, str):
        dep = dep.strip()
    else:
        dep = ""
    if isinstance(arr, str):
        arr = arr.strip()
    else:
        arr = ""
    rt = (info.get("route") or info.get("routing") or info.get("itinéraire") or info.get("itineraire") or "")
    if isinstance(rt, str):
        rt = rt.strip()
    else:
        rt = ""
    if not rt and isinstance(info.get("itinerary"), str):
        rt = (info.get("itinerary") or "").strip()
    if rt:
        rt = _unify_route_display(rt)
        if len(rt) >= 3:
            return rt
    if dep and arr:
        return f"{dep} → {arr}"
    return None

def merge_boarding_pass_info(conv, info):
    """Fusionne les champs reconnus (écrase si la vision renvoie une valeur non vide)."""
    d = conv["data"]
    fn = (info.get("flight_number") or "").strip().upper()
    fn = re.sub(r"[\s]+", "", fn)
    if fn:
        m = re.search(r"\b([A-Z]{1,3}\d{1,4}[A-Z]?)\b", fn)
        d["flight_number"] = m.group(1) if m else fn[:12]
    d.pop("codeshare_note", None)
    d.pop("flight_number_prefix_hint", None)
    mark_raw = re.sub(r"[^A-Za-z]", "", (info.get("marketing_carrier_iata") or info.get("airline_iata") or "").upper())
    op_raw = re.sub(r"[^A-Za-z]", "", (info.get("operating_carrier_iata") or "").upper())
    air = (info.get("airline") or "").strip()

    marketing_name = None
    if air:
        al = re.sub(r"[^A-Za-z]", "", air).upper()
        if len(al) <= 3 and len(air) <= 4 and air.replace(" ", "").upper() == al:
            marketing_name = airline_from_iata(al) or air
        else:
            marketing_name = air
    elif len(mark_raw) >= 2:
        marketing_name = airline_from_iata(mark_raw) or mark_raw[:2]

    operating_name = None
    if len(op_raw) >= 2:
        operating_name = airline_from_iata(op_raw) or op_raw[:2]

    if marketing_name:
        d["airline"] = marketing_name
    elif operating_name:
        d["airline"] = operating_name
    elif d.get("flight_number"):
        # Pas d'indice commercial/opérateur sur la carte : devinette par préfixe du n° de vol uniquement dans ce cas
        guess = airline_guess_from_flight_number(d["flight_number"])
        if guess:
            d["airline"] = guess

    d.pop("operating_airline", None)
    if operating_name and marketing_name and operating_name.strip().lower() != marketing_name.strip().lower():
        d["operating_airline"] = operating_name
    elif operating_name and not marketing_name:
        d.pop("operating_airline", None)

    if mark_raw and op_raw and mark_raw[:2] != op_raw[:2]:
        mn = airline_from_iata(mark_raw) or mark_raw[:2]
        on = airline_from_iata(op_raw) or op_raw[:2]
        d["codeshare_note"] = f"Code-share : commercial {mn} ({mark_raw[:2]}) / opéré par {on} ({op_raw[:2]})"

    guess_air = airline_guess_from_flight_number(d.get("flight_number") or "")
    if (
        guess_air
        and d.get("airline")
        and guess_air.strip().lower() != d["airline"].strip().lower()
        and not d.get("operating_airline")
        and not (mark_raw and op_raw and mark_raw[:2] != op_raw[:2])
    ):
        d["flight_number_prefix_hint"] = guess_air

    d.pop("pending_ticket_dm", None)
    kind, dval = _parse_date_from_vision(info)
    if kind == "full":
        d["flight_date"] = dval
    elif kind == "partial":
        d["pending_ticket_dm"] = dval
    pnr = _pnr_from_vision_info(info)
    if len(pnr) >= MIN_PNR_LEN:
        d["pnr"] = pnr[:8]
    itin_merged = _itinerary_from_vision_info(info)
    if itin_merged:
        d["itinerary"] = itin_merged
        d.pop("temp_itin_dep", None)
        d.pop("itinerary_compl_note", None)
    d["vision_leg_hint"] = None
    d.pop("ticket_other_legs_hint", None)
    sdg = (info.get("service_direction_guess") or info.get("service_direction") or "").strip().lower()
    if sdg in ("outbound", "out", "aller", "all"):
        d["vision_leg_hint"] = "outbound"
    elif sdg in ("return", "inbound", "in", "retour"):
        d["vision_leg_hint"] = "return"
    oth = (info.get("other_legs_summary") or "").strip()
    if oth and len(oth) > 5:
        d["ticket_other_legs_hint"] = oth[:280]
    # Noms passagers (1 ou plusieurs sur la même carte)
    max_pax = int(d.get("passengers") or 6)
    if max_pax < 1:
        max_pax = 1
    vis_names = _passenger_names_from_vision(info, max_pax)
    if vis_names:
        d["passenger_names"] = vis_names
        if len(vis_names) < max_pax:
            d["pax_collect_idx"] = len(vis_names) + 1
        else:
            d["pax_collect_idx"] = max_pax

def _carte_field_disp(d, key):
    if key == "passenger_names":
        names = d.get("passenger_names") or []
        return ", ".join(names) if names else "—"
    if key == "operating_airline":
        return str(d.get("operating_airline") or "").strip() or "—"
    v = d.get(key)
    return str(v).strip() if v else "—"

def _carte_date_recap_line(d, lang):
    """Une ligne 🎫 pour la date (lisible + gras ; si jour/mois sans année → rappel année)."""
    if d.get("flight_date"):
        fd = (d["flight_date"] or "").strip()
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s*$", fd)
        if m:
            di, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            mw = month_word(f"{mo:02d}", lang)
            if lang == "en":
                return f"🎫 *{mw} {di}, {y}*"
            return f"🎫 *{di} {mw} {y}*"
        return f"🎫 *{fd}*"
    dm = d.get("pending_ticket_dm")
    if dm and isinstance(dm, (list, tuple)) and len(dm) == 2:
        try:
            di = int(dm[0].lstrip("0") or "0")
            mw = month_word(dm[1], lang)
            if lang == "en":
                return (
                    f"🎫 *{mw} {di}*\n"
                    "_(day/month on the ticket — we'll ask the year next.)_"
                )
            return (
                f"🎫 *{di} {mw}*\n"
                "_(jour/mois sur le billet — l’année est demandée juste après.)_"
            )
        except (ValueError, TypeError):
            return "🎫 *…*"
    return None

def _carte_recap_lines(d, lang, extra_lines=None):
    """Lignes de récap lecture carte / billet (sans titre « lu ! »)."""
    bits = []
    if d.get("flight_number"):
        bits.append(f"✈️ *{d['flight_number']}*")
    if d.get("airline"):
        if d.get("operating_airline"):
            bits.append(
                f"🏷️ *{'Commercial' if lang == 'fr' else 'Ticketing'} :* {d['airline']}"
            )
            bits.append(
                f"🛫 *{'Opéré par' if lang == 'fr' else 'Operated by'} :* {d['operating_airline']}"
            )
        else:
            bits.append(str(d["airline"]))
    if d.get("flight_number_prefix_hint"):
        gh = d["flight_number_prefix_hint"]
        bits.append(
            f"ℹ️ *{'Le préfixe du n° de vol évoque' if lang == 'fr' else 'Flight number prefix suggests'}* {gh} "
            f"*{'(souvent le commercial en code-share)' if lang == 'fr' else '(often the marketing carrier on codeshares)'}*"
        )
    if d.get("codeshare_note") and not d.get("operating_airline"):
        bits.append(d["codeshare_note"])
    date_line = _carte_date_recap_line(d, lang)
    if date_line:
        bits.append(date_line)
    if d.get("pnr"):
        bits.append(f"📋 *{d['pnr']}*")
    if d.get("itinerary"):
        bits.append(f"🛤️ *{d['itinerary']}*")
    if d.get("ticket_other_legs_hint"):
        bits.append(
            f"ℹ️ _Autres vols visibles sur le document : {d['ticket_other_legs_hint']}_"
            if lang == "fr"
            else f"ℹ️ _Other flights visible on the document: {d['ticket_other_legs_hint']}_"
        )
    vh = d.get("vision_leg_hint")
    if vh == "outbound":
        bits.append(
            "ℹ️ _Segment détecté comme **aller** (outbound)._"
            if lang == "fr"
            else "ℹ️ _Detected as **outbound** segment._"
        )
    elif vh == "return":
        bits.append(
            "ℹ️ _Segment détecté comme **retour**._"
            if lang == "fr"
            else "ℹ️ _Detected as **return** segment._"
        )
    for n in d.get("passenger_names") or []:
        bits.append(f"👤 *{n}*")
    if extra_lines:
        bits.extend(extra_lines)
    return bits

def _carte_confirm_poll_options(lang):
    if lang == "en":
        return [
            {"id": "1", "label": "Yes, continue", "button": "✅ Continue"},
            {"id": "2", "label": "Fix something", "button": "✏️ Fix"},
        ]
    return [
        {"id": "1", "label": "Oui, continuer", "button": "✅ Continuer"},
        {"id": "2", "label": "Corriger", "button": "✏️ Corriger"},
    ]

def _carte_modify_later_hint(lang):
    """Rappel court sous le récap carte (correction immédiate)."""
    if lang == "en":
        return "\n\nℹ️ _If something is wrong, tap *Fix* below._"
    return "\n\nℹ️ _Si une information est incorrecte : touchez *Corriger* ci-dessous._"

def _carte_confirm_yes(text, choice, lang):
    u = (text or "").strip()
    low = u.lower()
    if choice == "1":
        return True
    if re.match(r"^\s*1(?:\uFE0F\u20E3)?\s*$", u):
        return True
    if re.match(r"^\s*1[\.\)\:]", u):
        return True
    nk = _norm_choice_key(u)
    if nk in ("continuer", "continue", "oui", "yes", "ok", "correct"):
        return True
    if lang == "fr":
        return bool(re.match(r"^(oui|ok|exact|correct|dac|d'accord)\b", low))
    return bool(re.match(r"^(yes|ok|correct|yep|yup)\b", low))

def _carte_confirm_no(text, choice, lang):
    u = (text or "").strip()
    low = u.lower()
    if choice == "2":
        return True
    if re.match(r"^\s*2(?:\uFE0F\u20E3)?\s*$", u):
        return True
    if re.match(r"^\s*2[\.\)\:]", u):
        return True
    nk = _norm_choice_key(u)
    if nk in ("corriger", "fix", "modifier", "edit"):
        return True
    if lang == "fr":
        return bool(re.match(r"^(non|nn|nope|erreur|faux|corriger)\b", low))
    return bool(re.match(r"^(no|nope|wrong|incorrect|fix)\b", low))

def finalize_boarding_pass_navigation(phone, conv, lang):
    """Après validation des infos lues sur la carte : enchaîne sans redemander ce qui est déjà rempli."""
    d = conv["data"]
    if d.get("flight_number") and d.get("flight_date"):
        advance_after_flight_date_complete(phone, conv, lang)
    elif d.get("flight_number"):
        conv["step"] = "flight_date"
        q_flight_date(phone, lang, conv)
    elif d.get("airline"):
        if d.get("pnr"):
            conv["step"] = "flight_number"
            q_flight_number(phone, lang)
        else:
            conv["step"] = "pnr_input"
            q_pnr(phone, lang, d["airline"])
    else:
        conv["step"] = "airline"
        q_airline(phone, lang, conv)

def q_carte_pick_field(phone, conv, lang):
    """Menu pour choisir le champ à corriger (incl. opérateur en code-share)."""
    d = conv["data"]
    if lang == "en":
        labels = (
            "Ticketing / marketing airline",
            "Flight number",
            "Flight date (DD/MM/YYYY)",
            "PNR / booking ref",
            "Route (e.g. CDG → ABJ)",
            "Passenger names (comma-separated)",
            "Operating carrier (if different — codeshare)",
        )
    else:
        labels = (
            "Compagnie commerciale (sur le billet)",
            "N° de vol",
            "Date du vol (JJ/MM/AAAA)",
            "PNR",
            "Trajet (ex. CDG → ABJ)",
            "Noms des passagers (séparés par des virgules)",
            "Opérateur réel / « opéré par » (si différent)",
        )
    keys = list(CARTE_FIELD_KEYS)
    lines = []
    for i, (k, lab) in enumerate(zip(keys, labels), 1):
        lines.append(f"{i}️⃣ {lab} — *{_carte_field_disp(d, k)}*")
    d["_carte_field_keys"] = keys
    conv["step"] = "carte_pick_field"
    n = len(keys)
    msg = (
        f"✏️ *Quel champ voulez-vous corriger ?*\n\n" + "\n".join(lines) + f"\n\nRépondez par un *chiffre de 1 à {n}*."
        if lang == "fr"
        else f"✏️ *Which field should we fix?*\n\n" + "\n".join(lines) + f"\n\nReply with a *number from 1 to {n}*."
    )
    send(phone, msg)

def q_carte_edit_prompt(phone, lang, key):
    if key == "airline":
        send(phone, "✍️ Indiquez la *compagnie* (nom complet ou code IATA, ex. *AF* ou *Air France*)." if lang == "fr" else "✍️ Type the *airline* (full name or IATA code, e.g. *AF* or *Air France*).")
    elif key == "flight_number":
        send(phone, "✍️ Indiquez le *numéro de vol* (ex. *AF703* ou *SN3638*)." if lang == "fr" else "✍️ Type the *flight number* (e.g. *AF703*).")
    elif key == "flight_date":
        send(
            phone,
            "✍️ Indiquez la *date du vol* (*JJ/MM/AAAA*, ou ex. *12 mai 2024*, *15 mai*)."
            if lang == "fr"
            else "✍️ Enter the *flight date* (*DD/MM/YYYY*, or e.g. *12 May 2024*).",
        )
    elif key == "pnr":
        send(
            phone,
            f"✍️ Indiquez le *PNR* / code réservation (au moins *{MIN_PNR_LEN}* caractères alphanumériques)."
            if lang == "fr"
            else f"✍️ Type the *PNR* / booking code (at least *{MIN_PNR_LEN}* alphanumeric characters).",
        )
    elif key == "itinerary":
        send(phone, "✍️ Indiquez le *trajet* (ex. *Paris CDG → Abidjan* ou *CDG → ABJ*)." if lang == "fr" else "✍️ Type the *route* (e.g. *Paris CDG → Abidjan*).")
    elif key == "passenger_names":
        send(
            phone,
            "✍️ Indiquez les *noms sur le billet*, séparés par des *virgules* (ex. *Aminata TRAORE, Kadiatou DIALLO*)."
            if lang == "fr"
            else "✍️ Type *passenger names* as on the ticket, *comma-separated* (e.g. *Fatou SALL, Amadou DIALLO*).",
        )
    elif key == "operating_airline":
        send(
            phone,
            "✍️ Indiquez la compagnie *qui exploite le vol* (nom ou code IATA), ou *rien* / *—* si identique au commercial."
            if lang == "fr"
            else "✍️ Type the *operating carrier* (name or IATA), or *none* / *—* if same as ticketing.",
        )

def _apply_carte_field_edit(d, key, text, lang):
    """Applique une correction saisie ; retourne (ok, message_erreur_fr_or_en)."""
    raw = (text or "").strip()
    if not raw:
        return False, ("Texte vide." if lang == "fr" else "Empty text.")
    if key == "airline":
        d["airline"] = raw
        d.pop("flight_number_prefix_hint", None)
        return True, None
    if key == "flight_number":
        u = re.sub(r"[\s]+", "", raw.upper())
        m = re.search(r"\b([A-Z]{1,3}\d{1,4}[A-Z]?)\b", u)
        d["flight_number"] = (m.group(1) if m else u[:12])
        d.pop("flight_number_prefix_hint", None)
        return True, None
    if key == "flight_date":
        s = raw.strip()
        parsed = try_parse_flight_date_message(s, lang)
        if not parsed and re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            parsed = s
        if not parsed:
            return False, (
                "Date non reconnue. Ex. *12/05/2024*, *2024-05-12* ou *15 mai*."
                if lang == "fr"
                else "Date not recognized. E.g. *12/05/2024*, *2024-05-12*, or *15 May*."
            )
        d["flight_date"] = parsed
        d.pop("pending_ticket_dm", None)
        return True, None
    if key == "pnr":
        p = re.sub(r"[^A-Za-z0-9]", "", raw.upper())
        if len(p) < MIN_PNR_LEN:
            return False, (
                f"Le PNR doit contenir au moins {MIN_PNR_LEN} caractères."
                if lang == "fr"
                else f"PNR must be at least {MIN_PNR_LEN} characters."
            )
        d["pnr"] = p[:8]
        return True, None
    if key == "itinerary":
        d["itinerary"] = raw
        d.pop("temp_itin_dep", None)
        return True, None
    if key == "passenger_names":
        parts = re.split(r"[,;\n]+", raw)
        names = []
        for p in parts:
            fn = _format_passenger_name_token(p.strip())
            if fn:
                names.append(fn)
        if not names:
            return False, ("Au moins un nom *Prénom NOM* est nécessaire." if lang == "fr" else "At least one *First LAST* name is required.")
        d["passenger_names"] = names
        pax = int(d.get("passengers") or 1)
        if len(names) >= pax:
            d["pax_collect_idx"] = pax
        else:
            d["pax_collect_idx"] = len(names) + 1
        return True, None
    if key == "operating_airline":
        low = raw.lower()
        if low in ("-", "—", "none", "n/a", "rien", "egal", "égale", "identique", "same"):
            d.pop("operating_airline", None)
        else:
            al = re.sub(r"[^A-Za-z]", "", raw).upper()
            if len(al) <= 3 and len(raw.replace(" ", "")) <= 4:
                d["operating_airline"] = airline_from_iata(al) or raw.strip()
            else:
                d["operating_airline"] = raw.strip()
        return True, None
    return False, ("Champ inconnu." if lang == "fr" else "Unknown field.")

def send_carte_confirm_panel(phone, conv, lang):
    """Rappel des infos + sondage Oui / Corriger."""
    d = conv["data"]
    bits = _carte_recap_lines(d, lang)
    head = "📋 *Voici ce qu'on retient :*" if lang == "fr" else "📋 *Here's what we have:*"
    recap = "\n".join(bits) if bits else (
        "_(rien de détecté — corrigez ou renvoyez une photo)_"
        if lang == "fr"
        else "_(nothing detected — fix a field or send another photo)_"
    )
    body = f"{head}\n{recap}{_carte_modify_later_hint(lang)}\n\n*{'Tout est correct ?' if lang == 'fr' else 'Everything correct?'}*"
    send_tunnel_poll(phone, conv, body, _carte_confirm_poll_options(lang))

def send_boarding_read_failed_escapes(phone, lang, recap_followup=False, variant="blur"):
    """
    Photo floue / lecture ratée : ton calme, sans culpabiliser le client.
    variant: 'blur' (OCR peu fiable) | 'no_vision' (API vision absente sur ce serveur).
    recap_followup=True : rappel *1* / *2* pour l’écran de confirmation carte déjà affiché.
    """
    if variant == "no_vision":
        intro_fr = "Oups ! 😅 Sur ce serveur, la lecture automatique des photos n’est pas disponible pour l’instant."
        intro_en = "Oops! 😅 Auto-reading of photos isn’t available on this server right now."
    else:
        intro_fr = "Oups ! Désolé, la lecture automatique a fait une petite erreur de lecture. 😅"
        intro_en = "Oops! Sorry — auto-read hit a small snag. 😅"

    if lang == "en":
        core = (
            f"{intro_en}\n\n"
            "No worries — two simple ways out:\n\n"
            "*Option A*: Send another sharp photo — flat on the table, no glare.\n\n"
            "⌨️ *Option B*: Enter the details yourself (reply *B* or *2* — a few quick questions).\n\n"
            "What would you like to do?"
        )
        tail = (
            "\n\n_If the summary we showed above is still fine, reply *1*. To fix a field, reply *2*._"
            if recap_followup
            else ""
        )
    else:
        core = (
            f"{intro_fr}\n\n"
            "Pas de souci, on a deux solutions :\n\n"
            "*Option A* : Reprenez une photo bien nette, bien à plat et sans reflets.\n\n"
            "⌨️ *Option B* : Entrez les informations à la main (répondez *B* ou *2* et je vous pose 3 questions rapides).\n\n"
            "Que préférez-vous ?"
        )
        tail = (
            "\n\n_Si le récap affiché plus haut vous convient : *1*. Pour corriger une information : *2*._"
            if recap_followup
            else ""
        )
    send(phone, core + tail)

def apply_boarding_pass_image(phone, conv, image_b64, lang, after_passengers=False):
    """
    Lit une carte / billet, fusionne les champs, envoie le récap puis enchaîne selon l’étape.

    Si after_passengers=True : photo à l’étape « preuves » (incident déjà choisi) — même logique
    de lecture ; l’upload Airtable cible surtout la 1ʳᵉ ligne passager.

    Pour les étapes listées dans CARTE_CONFIRM_PAUSE_STEPS (dont boarding_after_pax), on demande
    toujours *1* = tout bon / *2* = corriger avant finalize_boarding_pass_navigation.

    Retourne True si au moins une info exploitable a été extraite.
    """
    step_before = conv.get("step")
    idx_for_attach = max(0, (conv["data"].get("pax_collect_idx") or 1) - 1)
    info = read_boarding_pass_merged(image_b64)
    raw_probe = _boarding_image_bytes(image_b64)
    if not boarding_pass_info_usable(info):
        if raw_probe and len(raw_probe) > 5 * 1024 * 1024:
            send(
                phone,
                "⚠️ *Image trop lourde* (max 5 Mo pour l’archivage du billet). Réduisez la taille ou renvoyez une capture plus légère."
                if lang == "fr"
                else "⚠️ *Image too large* (max 5 MB for boarding-pass storage). Please send a smaller file.",
            )
            return False
        if image_b64 and not (OPENAI_API_KEY or "").strip():
            # Message détaillé + sorties A/B : étape preuves (évite doublon avec send_boarding_read_failed_escapes).
            if step_before != "boarding_after_pax":
                send(
                    phone,
                    "📸 *Lecture automatique limitée* : le service « vision » n’est pas configuré sur ce serveur. "
                    "Répondez *2* ou *B* pour saisir à la main, ou renvoyez une photo *très nette* avec le *QR code* du billet si présent."
                    if lang == "fr"
                    else "📸 *Auto-read is limited*: the vision API is not configured on this server. "
                    "Reply *2* or *B* to type details manually, or resend a very sharp photo including the ticket *QR code* if visible.",
                )
            return False
        # Lecture auto impossible : on enregistre quand même l’image sur Airtable (preuve), si configuré.
        if image_b64 and raw_probe and len(raw_probe) <= 5 * 1024 * 1024:
            if F_CARTE_EMBARQUEMENT and AIRTABLE_API_KEY:
                d0 = conv["data"]
                idx_plan = _boarding_attach_idx_plan(
                    d0, info if isinstance(info, dict) else {}, step_before, after_passengers, idx_for_attach
                )
                h_att = hashlib.sha256(raw_probe).hexdigest()
                if not (h_att and d0.get("_last_boarding_attach_hash") == h_att):
                    n_arc = at_boarding_attach_to_indices(phone, conv, image_b64, idx_plan, lang)
                    if n_arc:
                        d0["boarding_evidence_in_flow"] = True
                    if n_arc and h_att:
                        d0["_last_boarding_attach_hash"] = h_att
                    elif not n_arc:
                        print(
                            f"at_boarding_attach_to_indices: 0 uploads after unreadable pass "
                            f"(step={step_before} ref={conv.get('ref')} tel={phone})"
                        )
        return False
    merge_boarding_pass_info(conv, info)
    conv["data"]["boarding_evidence_in_flow"] = True
    d = conv["data"]
    extras = []
    n_att = 0
    if F_CARTE_EMBARQUEMENT and image_b64:
        idx_plan = _boarding_attach_idx_plan(d, info, step_before, after_passengers, idx_for_attach)
        raw_att = _boarding_image_bytes(image_b64)
        h_att = hashlib.sha256(raw_att).hexdigest() if raw_att else ""
        if h_att and d.get("_last_boarding_attach_hash") == h_att:
            n_att = 0
            extras.append(
                "📎 _Même fichier qu’à l’envoi précédent — pas de nouvel enregistrement Airtable._"
                if lang == "fr"
                else "📎 _Same file as before — no duplicate Airtable upload._"
            )
        else:
            if h_att:
                d["_last_boarding_attach_hash"] = h_att
            n_att = at_boarding_attach_to_indices(phone, conv, image_b64, idx_plan, lang)
    if n_att:
        extras.append(
            f"📎 Carte enregistrée dans Airtable ({n_att} ligne{'s' if n_att > 1 else ''})."
            if lang == "fr"
            else f"📎 Boarding pass saved to Airtable ({n_att} row{'s' if n_att > 1 else ''})."
        )
    recap_lines = _carte_recap_lines(d, lang, extras)
    body = "\n".join(recap_lines)
    head = "📸 *Carte / billet lu !*" if lang == "fr" else "📸 *Boarding pass read!*"

    if step_before in CARTE_CONFIRM_PAUSE_STEPS:
        intro = (
            "\n\nVoici ce que nous avons détecté — *vérifiez avant de poursuivre* :"
            if lang == "fr"
            else "\n\nHere's what we detected — *please check before continuing*:"
        )
        hint = _carte_modify_later_hint(lang)
        poll_body = f"{head}{intro}\n\n{body}{hint}\n\n*{'Tout est correct ?' if lang == 'fr' else 'Everything correct?'}*"
        if not body:
            poll_body = f"{head}{intro}{hint}\n\n*{'Tout est correct ?' if lang == 'fr' else 'Everything correct?'}*"
        send_tunnel_poll(phone, conv, poll_body, _carte_confirm_poll_options(lang))
        conv["step"] = "carte_confirm"
        at_save(phone, conv)
        return True

    send(phone, f"{head}\n{body}" if body else head)
    finalize_boarding_pass_navigation(phone, conv, lang)
    at_save(phone, conv)
    return True

def gpt_tunnel_assist(phone, text, step, lang):
    """
    Pendant le tunnel : le message ne matche pas l'étape (mauvais format) OU c'est une question libre.
    On répond utilement (EU261 / procédure / infos voyage) sans sortir brutalement du parcours.
    """
    if not OPENAI_API_KEY or not text or not step:
        return None
    system = (
        "Tu es l'assistant de Robin des Airs (indemnisation vol EU261 / règlement CE 261/2004), sur WhatsApp.\n"
        f"L'utilisateur est EN TRAIN de remplir le formulaire, à l'étape technique « {step} ». "
        "Son message ne correspond pas au format attendu pour cette étape, OU c'est une question ouverte "
        "(délais, droits, PNR, compagnie, surbooking, annulation, correspondance, mineurs, etc.).\n"
        "Comportement :\n"
        "- Si c'est une vraie question ou une demande d'information : réponds correctement et prudemment "
        "(principes généraux, pas de promesse sur SON dossier ni montant chiffré personnalisé), *6 phrases maximum*.\n"
        "- Si c'est surtout un mauvais format ou du bruit : explique calmement ce qu'il faut envoyer à cette étape "
        "(chiffre proposé, Prénom NOM, photo nette, etc.).\n"
        "- Ne colle pas de lien mandat / URL juridique longue ; termine par une courte phrase pour continuer le dossier "
        "ou taper *menu* / *recommencer*.\n"
        f"Langue : {'français' if lang == 'fr' else 'english'}. Pas de JSON. Pas de liste à puces excessive."
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text[:1200]},
                ],
                "max_tokens": 320,
                "temperature": 0.4,
            },
            timeout=25,
        )
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip() or None
    except Exception as e:
        print(f"GPT tunnel assist error: {e}")
        return None

def gpt_free_reply(phone, text, conv, dossier_done=False):
    """Réponse libre hors tunnel actif (nouveau contact ou dossier déjà terminé)."""
    if not OPENAI_API_KEY or not (text or "").strip():
        return None
    lang = conv["data"].get("lang", "fr")
    if dossier_done:
        done_note = (
            "mandat signé et pièces reçues côté Robin"
            if post_submit_fully_done(conv)
            else "tunnel WhatsApp terminé ; mandat et/ou pièces peuvent encore manquer"
        )
        system = (
            f"Tu es l'assistant Robin des Airs (EU261). Le client a fini le questionnaire WhatsApp ({done_note}). "
            f"Réponds en {'français' if lang == 'fr' else 'english'}. "
            f"{_gpt_hors_tunnel_format_rules(lang)} "
            "Contenu : réponds d’abord à sa question (pro, rassurant), sans montant personnalisé inventé. "
            "Ne dis jamais « dossier complet » ou « tout est reçu » si le mandat n’est pas signé ou s’il manque la CNI. "
            "Dernière puce si utile : 🔄 *menu* ou *recommencer* pour un nouveau dossier. "
            "Ne termine pas par des URLs (site / suivi ajoutés après ta réponse)."
        )
    else:
        system = (
            f"Tu es l'assistant Robin des Airs (EU261). Le client n’est *pas encore* dans le formulaire WhatsApp. "
            f"Réponds en {'français' if lang == 'fr' else 'english'}. "
            f"{_gpt_hors_tunnel_format_rules(lang)} "
            "Contenu : réponds à sa question ; pour ouvrir un dossier, puce avec 📋 *menu* ou *1* à *6* passagers. "
            "Pas de montants inventés. Pas de JSON. Pas d’URLs en fin (liens ajoutés après)."
        )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text[:1200]},
                ],
                "max_tokens": 320,
                "temperature": 0.5,
            },
            timeout=30,
        )
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip() or None
    except Exception as e:
        print(f"GPT error: {e}")
        return None

# ===== TRAITEMENT RÉPONSES =====

def handle_reply(phone, text, conv, image_b64=None):
    step = conv.get("step")
    lang = conv["data"].get("lang", "fr")
    t    = text.strip()
    low  = t.lower()

    # Choix : clic sondage, chiffre en tête (sauf date complète type 11/12/2025)
    if looks_like_full_date_input(t):
        choice = None
    else:
        m      = re.search(r"^(\d+)", t)
        choice = m.group(1) if m else None
    if not choice and step == "passengers" and t:
        m2 = re.search(r"\b([1-6])\b", t)
        if m2:
            choice = m2.group(1)

    print(f"[STEP={step}] text='{t[:30]}' choice={choice}")

    if t and user_wants_fresh_start(t) and step and step != "completed":
        conv["data"] = fresh_data(lang)
        conv["step"] = "passengers"
        conv["ref"]  = make_ref(phone)
        send(
            phone,
            "🔄 *D'accord — on repart de zéro.* Choisissez à nouveau le nombre de passagers."
            if lang == "fr"
            else "🔄 *OK — starting fresh.* Pick the number of passengers again.",
        )
        q_passengers(phone, lang, conv)
        return True

    if t and user_wants_expertise_rappel(t) and step and step != "completed":
        if step == "passengers":
            send(
                phone,
                (
                    "📞 *Rappel expertise*\n\n"
                    "Pour *6 personnes ou plus*, répondez *6* : *un expert vous rappelle* (*Climbie*).\n\n"
                    f"📱 {CLIMBIE_TEL}"
                )
                if lang == "fr"
                else (
                    "📞 *Expert callback*\n\n"
                    "For *6+ people*, reply *6* — *an expert will call you* (*Climbie*).\n\n"
                    f"📱 {CLIMBIE_TEL}"
                ),
            )
        else:
            send(
                phone,
                (
                    "📞 *Rappel / expertise*\n\n"
                    "Pour être rappelé ou échanger avec un conseiller :\n"
                    f"📱 {CLIMBIE_TEL}\n\n"
                    "_Vous pouvez aussi poursuivre votre dossier ici en répondant à la dernière question._"
                )
                if lang == "fr"
                else (
                    "📞 *Callback / expert*\n\n"
                    f"📱 {CLIMBIE_TEL}\n\n"
                    "_You can also continue your claim here by answering the last question._"
                ),
            )
        return True

    # ── VÉRIFICATION LECTURE CARTE / BILLET (réduit la friction) ─────
    if step == "carte_confirm":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                return True
            send_boarding_read_failed_escapes(phone, lang, recap_followup=True)
            return True
        if _carte_confirm_yes(t, choice, lang):
            conv["data"].pop("_edit_field", None)
            conv["data"].pop("_carte_field_keys", None)
            finalize_boarding_pass_navigation(phone, conv, lang)
            at_save(phone, conv)
            return True
        if _carte_confirm_no(t, choice, lang):
            q_carte_pick_field(phone, conv, lang)
            at_save(phone, conv)
            return True
        return False

    if step == "carte_pick_field":
        m_key = re.match(r"^\s*([1-9])(?:\uFE0F\u20E3)?\s*$", t.strip())
        if not m_key:
            return False
        ix = int(m_key.group(1))
        keys = conv["data"].get("_carte_field_keys") or list(CARTE_FIELD_KEYS)
        if ix < 1 or ix > len(keys):
            return False
        conv["data"]["_edit_field"] = keys[ix - 1]
        conv["step"] = "carte_edit_value"
        q_carte_edit_prompt(phone, lang, keys[ix - 1])
        at_save(phone, conv)
        return True

    if step == "carte_edit_value":
        key = conv["data"].get("_edit_field")
        if not key:
            conv["step"] = "carte_confirm"
            send_carte_confirm_panel(phone, conv, lang)
            at_save(phone, conv)
            return True
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                conv["data"].pop("_edit_field", None)
                conv["data"].pop("_carte_field_keys", None)
                return True
            send(
                phone,
                "📸 Photo illisible. Envoyez la correction en *texte* ou une image plus nette."
                if lang == "fr"
                else "📸 Unclear image. Send your fix as *text* or a sharper photo.",
            )
            return True
        ok, err = _apply_carte_field_edit(conv["data"], key, t, lang)
        if not ok:
            send(phone, f"⚠️ {err}" + ("\n\nRéessayez." if lang == "fr" else "\n\nPlease try again."))
            return True
        conv["data"].pop("_edit_field", None)
        conv["data"].pop("_carte_field_keys", None)
        conv["step"] = "carte_confirm"
        send_carte_confirm_panel(phone, conv, lang)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 1 : PASSAGERS ──────────────────────────────────────────
    if step == "passengers":
        if choice in ("1", "2", "3", "4", "5"):
            pax = int(choice)
            conv["data"]["passengers"] = pax
            print(f"[tunnel] passengers_choice phone={phone} pax={pax} lang={lang}")
            conv["step"] = "pax_ack_route"
            q_pax_ack_route(phone, lang, conv)
            at_save(phone, conv)
            return True
        if choice == "6":
            send(
                phone,
                (
                    "🙏 *6 personnes ou plus*\n\n"
                    "*Un expert* vous rappelle pour étudier votre dossier.\n\n"
                    f"📱 {CLIMBIE_TEL}"
                )
                if lang == "fr"
                else (
                    "🙏 *6+ people*\n\n"
                    "*An expert* will call you back to review your case.\n\n"
                    f"📱 {CLIMBIE_TEL}"
                ),
            )
            return True
        return False

    # ── Après passagers : parcours → langue expert → confirmation vocale ──
    if step == "pax_ack_route":
        if choice == "1":
            conv["data"]["early_route_shape"] = "direct"
            conv["step"] = "pax_contact_lang"
            q_pax_contact_lang(phone, lang, conv)
            at_save(phone, conv)
            return True
        if choice == "2":
            conv["data"]["early_route_shape"] = "connection"
            conv["step"] = "pax_contact_lang"
            q_pax_contact_lang(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    if step == "pax_contact_lang":
        n_lang = len(EXPERT_LANG_OPTIONS)
        if choice in tuple(str(i) for i in range(1, n_lang + 1)):
            ix = int(choice) - 1
            conv["data"]["expert_phone_lang"] = EXPERT_LANG_OPTIONS[ix][0]
            conv["step"] = "pax_voice_confirm"
            q_pax_voice_confirm(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    if step == "pax_voice_confirm":
        ok_voice = low in (
            "ok", "oui", "yes", "daccord", "dacord", "go", "continuer", "continue",
            "c'est bon", "cest bon", "parfait", "👍",
        ) or choice == "1"
        if ok_voice:
            conv["step"] = "incident_type"
            q_incident(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 2 : INCIDENT (avant la photo) ─────────────────────────
    if step == "incident_type":
        mapping = {"1": "delay", "2": "cancel", "3": "denied"}
        if choice in mapping:
            conv["data"]["incident_type"] = mapping[choice]
            conv["step"] = "boarding_after_pax"
            q_boarding_after_pax(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 3 : CARTE D'EMBARQUEMENT (preuves) ─────────────────────
    if step == "boarding_after_pax":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang, after_passengers=True):
                return True
            if not (OPENAI_API_KEY or "").strip():
                send_boarding_read_failed_escapes(phone, lang, variant="no_vision")
            else:
                send_boarding_read_failed_escapes(phone, lang)
            return True
        if choice == "1":
            send(
                phone,
                "👍 *Parfait.* Envoyez la *photo* maintenant : carte à plat, *sans reflets*."
                if lang == "fr"
                else "👍 *Great.* Send the *photo* now: pass flat on the table, *no glare*.",
            )
            return True
        if choice == "2" or re.match(r"^\s*b\s*$", low) or re.match(r"^\s*option\s*b\s*$", low) or low in (
            "continuer", "continue", "skip", "passer", "plus tard", "sans photo",
            "no photo", "later", "pas de photo", "questions", "sans image",
            "optionb", "manuel", "manuelle", "saisie manuelle", "à la main", "a la main",
        ):
            advance_after_incident(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 4 : COMPAGNIE ──────────────────────────────────────────
    if step == "airline":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                return True
            send(
                phone,
                "📸 Photo reçue mais peu lisible ou pas une carte/billet. Réessayez ou choisissez la compagnie (*1–9* / nom)."
                if lang == "fr"
                else "📸 Photo unclear or not a boarding pass. Retry or pick airline (*1–9* / name).",
            )
            return True
        u = t.strip()
        # Touche numérique seule (WhatsApp peut envoyer 1 + variation + keycap)
        m_key = re.match(r"^\s*([1-9])(?:\uFE0F\u20E3)?\s*$", u)
        d1    = m_key.group(1) if m_key else None
        if d1 and d1 in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[d1]
            next_after_airline_pick(phone, lang, conv)
            at_save(phone, conv)
            return True
        if d1 == "9" or low in ("autre", "other", "others", "otr"):
            conv["step"] = "airline_other"
            send(phone, "✍️ Tapez le nom de votre compagnie :" if lang=="fr" else "✍️ Type your airline name:")
            return True
        # Plusieurs chiffres seuls → pas un choix valide (évite blocage silencieux)
        if u.isdigit() and len(u) > 1:
            send(
                phone,
                "⚠️ *Ouvrez la liste* du message précédent, choisissez *Autre*, ou *tapez le nom* de la compagnie (ex. *EasyJet*)."
                if lang == "fr"
                else "⚠️ *Open the list* on the previous message, pick *Other*, or *type the airline name* (e.g. *EasyJet*).",
            )
            return True
        # Nom tapé (lettres : évite interpréter "12" comme compagnie)
        has_alpha = bool(re.search(r"[A-Za-zÀ-ÿ]", u))
        if has_alpha and len(u) >= 2:
            conv["data"]["airline"] = u
            next_after_airline_pick(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 4b : AUTRE COMPAGNIE ───────────────────────────────────
    if step == "airline_other":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                return True
            send(
                phone,
                "📸 Photo illisible. Tapez le nom de la compagnie en toutes lettres."
                if lang == "fr"
                else "📸 Photo unclear. Please type the airline name.",
            )
            return True
        conv["data"]["airline"] = t
        next_after_airline_pick(phone, lang, conv)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 5 : PNR ────────────────────────────────────────────────
    if step == "pnr_input":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                return True
            send(
                phone,
                "📸 Photo non reconnue. Envoyez le *code PNR* (6 caractères) ou *SKIP*."
                if lang == "fr"
                else "📸 Photo not recognized. Send your *PNR* (6 chars) or *SKIP*.",
            )
            return True
        pnr_clean = re.sub(r"[^A-Z0-9]", "", t.upper())
        if low in ("skip", "passer", "aucun", "non", "no") or not pnr_clean:
            conv["data"]["pnr"] = None
        else:
            conv["data"]["pnr"] = pnr_clean[:8]
        if conv["data"].get("flight_number"):
            conv["step"] = "flight_date"
            q_flight_date(phone, lang, conv)
        else:
            conv["step"] = "flight_number"
            q_flight_number(phone, lang)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 6 : NUMÉRO DE VOL ──────────────────────────────────────
    if step == "flight_number":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                return True
            send(
                phone,
                "📸 Impossible de lire la carte. Tapez le *numéro de vol* (ex. SN271) ou renvoyez une photo plus nette."
                if lang == "fr"
                else "📸 Could not read the pass. Type the *flight number* (e.g. SN271) or send a clearer photo.",
            )
            return True
        # Texte
        m2 = re.search(r"\b([A-Z]{1,2}\d{1,4})\b", t.upper())
        conv["data"]["flight_number"] = m2.group(1) if m2 else t.upper()[:10]
        conv["step"] = "flight_date"
        q_flight_date(phone, lang, conv)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 7a : ANNÉE (ou date complète au clavier) ───────────────
    if step == "flight_date":
        parsed = try_parse_flight_date_message(t, lang)
        if parsed:
            if not _is_valid_claim_flight_date_str(parsed):
                send(
                    phone,
                    "⚠️ Cette date est *dans le futur* ou *trop ancienne* pour la fenêtre habituelle (environ 5 ans). "
                    "Vérifiez jour, mois et année — ou choisissez une année dans la liste."
                    if lang == "fr"
                    else "⚠️ That date is *in the future* or *too far back* for the usual window (~5 years). "
                    "Check day, month and year — or pick a year from the list.",
                )
                return True
            conv["data"]["flight_date"] = parsed
            conv["data"].pop("pending_ticket_dm", None)
            conv["data"].pop("temp_year", None)
            conv["data"].pop("temp_years", None)
            conv["data"].pop("temp_month", None)
            advance_after_flight_date_complete(phone, conv, lang)
            at_save(phone, conv)
            return True
        years = conv["data"].get("temp_years", [])
        if choice == "6":
            if conv["data"].get("pending_ticket_dm"):
                send(
                    phone,
                    "⚠️ Répondez par un *chiffre de la liste* ou envoyez la *date complète* JJ/MM/AAAA."
                    if lang == "fr"
                    else "⚠️ Reply with a *listed number* or send the *full date* DD/MM/YYYY.",
                )
                return True
            send(phone, f"😔 Rétroactivité 5 ans max. Votre vol est trop ancien.\n\n👉 {RDA_DOMAIN}")
            return True
        idx = int(choice) - 1 if choice and choice.isdigit() else -1
        if 0 <= idx < len(years):
            conv["data"]["temp_year"] = str(years[idx])
            dm = conv["data"].get("pending_ticket_dm")
            if dm and isinstance(dm, (list, tuple)) and len(dm) == 2:
                day_s, month_s = dm[0], dm[1]
                y = conv["data"]["temp_year"]
                conv["data"]["flight_date"] = f"{day_s}/{month_s}/{y}"
                conv["data"].pop("pending_ticket_dm", None)
                conv["data"].pop("temp_year", None)
                conv["data"].pop("temp_years", None)
                advance_after_flight_date_complete(phone, conv, lang)
                at_save(phone, conv)
                return True
            conv["step"] = "flight_month"
            q_flight_month(phone, lang, conv["data"]["temp_year"])
            return True
        return False

    # ── ÉTAPE 7b : MOIS ──────────────────────────────────────────────
    if step == "flight_month":
        parsed = try_parse_flight_date_message(t, lang)
        if parsed:
            if not _is_valid_claim_flight_date_str(parsed):
                send(
                    phone,
                    "⚠️ Cette date est *dans le futur* ou *trop ancienne* pour la fenêtre habituelle (environ 5 ans). "
                    "Vérifiez jour, mois et année — ou choisissez le mois dans la liste."
                    if lang == "fr"
                    else "⚠️ That date is *in the future* or *too far back* for the usual window (~5 years). "
                    "Check day, month and year — or pick the month from the list.",
                )
                return True
            conv["data"]["flight_date"] = parsed
            conv["data"].pop("pending_ticket_dm", None)
            conv["data"].pop("temp_year", None)
            conv["data"].pop("temp_years", None)
            conv["data"].pop("temp_month", None)
            advance_after_flight_date_complete(phone, conv, lang)
            at_save(phone, conv)
            return True
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            conv["step"] = "flight_day"
            q_flight_day(phone, lang, conv["data"].get("temp_year", ""), conv["data"]["temp_month"])
            return True
        mo = month_number_from_word_token(t, lang)
        if mo is not None:
            conv["data"]["temp_month"] = f"{mo:02d}"
            conv["step"] = "flight_day"
            q_flight_day(phone, lang, conv["data"].get("temp_year", ""), conv["data"]["temp_month"])
            return True
        return False

    # ── ÉTAPE 7c : JOUR ──────────────────────────────────────────────
    if step == "flight_day":
        parsed = try_parse_flight_date_message(t, lang)
        if parsed:
            if not _is_valid_claim_flight_date_str(parsed):
                send(
                    phone,
                    "⚠️ Cette date est *dans le futur* ou *trop ancienne* pour la fenêtre habituelle (environ 5 ans). "
                    "Vérifiez jour, mois et année — ou répondez par un *jour* entre 1 et 31."
                    if lang == "fr"
                    else "⚠️ That date is *in the future* or *too far back* for the usual window (~5 years). "
                    "Check day, month and year — or reply with a *day* from 1 to 31.",
                )
                return True
            conv["data"]["flight_date"] = parsed
            conv["data"].pop("pending_ticket_dm", None)
            conv["data"].pop("temp_year", None)
            conv["data"].pop("temp_years", None)
            conv["data"].pop("temp_month", None)
            advance_after_flight_date_complete(phone, conv, lang)
            at_save(phone, conv)
            return True
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day   = f"{int(choice):02d}"
            year  = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            advance_after_flight_date_complete(phone, conv, lang)
            at_save(phone, conv)
            return True
        return False

    # ── ITINÉRAIRE : type de parcours puis détail (direct / escale / aller-retour) ──
    if step == "itinerary_kind":
        if choice in ("1", "2", "3"):
            d = conv["data"]
            d.pop("itin_collect_mode", None)
            d.pop("claim_rt_leg", None)
            d.pop("itinerary_compl_note", None)
            if choice == "1":
                conv["step"] = "itinerary_dep"
                q_itinerary_departure(phone, lang, conv)
                at_save(phone, conv)
                return True
            if choice == "2":
                d["itin_collect_mode"] = "connection"
                conv["step"] = "itinerary_freeline"
                q_itinerary_freeline(phone, lang, conv)
                at_save(phone, conv)
                return True
            conv["step"] = "itinerary_rt_pick"
            q_itinerary_rt_pick(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    if step == "itinerary_rt_pick":
        if choice == "1":
            conv["data"]["claim_rt_leg"] = "outbound"
            conv["data"]["itin_collect_mode"] = "rt_out"
            conv["step"] = "itinerary_freeline"
            q_itinerary_freeline(phone, lang, conv)
            at_save(phone, conv)
            return True
        if choice == "2":
            conv["data"]["claim_rt_leg"] = "return"
            conv["data"]["itin_collect_mode"] = "rt_in"
            conv["step"] = "itinerary_freeline"
            q_itinerary_freeline(phone, lang, conv)
            at_save(phone, conv)
            return True
        if choice == "3":
            conv["data"]["claim_rt_leg"] = "both"
            conv["data"]["itin_collect_mode"] = "rt_both"
            conv["step"] = "itinerary_freeline"
            q_itinerary_freeline(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    if step == "itinerary_freeline":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                conv["data"].pop("temp_itin_dep", None)
                return True
            send(
                phone,
                "📸 Photo non reconnue comme billet. Tapez un *trajet* (ex. *BRU → CDG → ABJ*) ou des codes *BRU CDG ABJ*."
                if lang == "fr"
                else "📸 Not read as a boarding pass. Type a *route* (e.g. *BRU → CDG → ABJ*) or codes *BRU CDG ABJ*.",
            )
            return True
        u = (t or "").strip()
        if try_set_itinerary_from_freeform(conv, u):
            conv["data"].pop("temp_itin_dep", None)
            advance_after_itinerary_collected(phone, conv, lang)
            at_save(phone, conv)
            return True
        send(
            phone,
            "🛤️ Je n’ai pas compris le trajet. Exemples : *BRU → CDG → ABJ*, *CDG ABJ*, "
            "ou *CDG → ABJ | ABJ → CDG* pour un aller-retour. Vous pouvez aussi envoyer une *photo de carte*."
            if lang == "fr"
            else "🛤️ I couldn’t read the route. Examples: *BRU → CDG → ABJ*, *CDG ABJ*, "
            "or *CDG → ABJ | ABJ → CDG* for round trip. You can also send a *boarding pass photo*.",
        )
        return True

    # ── ITINÉRAIRE (sans carte : départ puis arrivée ; avec carte déjà fusionné → étape sautée) ──
    if step == "itinerary_dep":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                conv["data"].pop("temp_itin_dep", None)
                return True
            send(
                phone,
                "📸 Photo non reconnue comme billet. Tapez la *ville ou code* de départ (ex. *BRU*)."
                if lang == "fr"
                else "📸 Not read as a boarding pass. Type *departure* city or code (e.g. *BRU*).",
            )
            return True
        u = t.strip()
        if len(u) < 2:
            send(
                phone,
                "🛫 Indiquez au moins le nom de la ville ou le code IATA (ex. *Bruxelles* ou *BRU*)."
                if lang == "fr"
                else "🛫 Please enter a city or IATA code (e.g. *Brussels* or *BRU*).",
            )
            return True
        conv["data"]["temp_itin_dep"] = u
        conv["step"] = "itinerary_arr"
        q_itinerary_arrival(phone, lang, conv)
        at_save(phone, conv)
        return True

    if step == "itinerary_arr":
        if image_b64:
            if apply_boarding_pass_image(phone, conv, image_b64, lang):
                conv["data"].pop("temp_itin_dep", None)
                return True
            send(
                phone,
                "📸 Photo non reconnue. Tapez la *ville ou code* d'arrivée (ex. *ABJ*)."
                if lang == "fr"
                else "📸 Not recognized. Type *arrival* city or code (e.g. *ABJ*).",
            )
            return True
        u = t.strip()
        if len(u) < 2:
            send(
                phone,
                "🛬 Indiquez la ville ou le code d'arrivée (ex. *Abidjan* ou *ABJ*)."
                if lang == "fr"
                else "🛬 Enter arrival city or code (e.g. *Abidjan* or *ABJ*).",
            )
            return True
        dep = (conv["data"].get("temp_itin_dep") or "").strip()
        conv["data"]["itinerary"] = f"{dep} → {u}"
        conv["data"]["temp_itin_dep"] = None
        advance_after_itinerary_collected(phone, conv, lang)
        at_save(phone, conv)
        return True

    # ── Après saisie d'un nom : confirmer ou corriger avant le suivant ─
    if step == "passenger_name_post_add":
        names = list(conv["data"].get("passenger_names") or [])
        pax = conv["data"].get("passengers") or 1
        if _carte_confirm_yes(t, choice, lang):
            nxt = len(names) + 1
            conv["step"] = "passenger_names"
            conv["data"]["pax_collect_idx"] = nxt
            if nxt >= 2 and nxt <= pax:
                q_boarding_next_passenger(phone, lang, nxt, pax)
            q_passenger_name(phone, lang, nxt, pax, names)
            at_save(phone, conv)
            return True
        if _carte_confirm_no(t, choice, lang):
            if not names:
                return False
            names.pop()
            conv["data"]["passenger_names"] = names
            prev = len(names) + 1
            conv["data"]["pax_collect_idx"] = prev
            conv["step"] = "passenger_names"
            q_passenger_name(phone, lang, prev, pax, names)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 9 : NOMS PASSAGERS ─────────────────────────────────────
    if step == "passenger_names":
        pax = conv["data"].get("passengers") or 1
        idx = conv["data"].get("pax_collect_idx") or 1

        if image_b64:
            merged = read_boarding_pass_merged(image_b64)
            usable = boarding_pass_info_usable(merged)
            if usable:
                if apply_boarding_pass_image(phone, conv, image_b64, lang):
                    return True
            n_att = at_boarding_attach_to_indices(phone, conv, image_b64, [max(0, idx - 1)], lang)
            if n_att:
                send(
                    phone,
                    (
                        "📎 *Photo / carte* enregistrée sur votre ligne passager dans Airtable.\n\n"
                        "_(Envoyez *Prénom NOM* pour ce passager si besoin.)_"
                        if lang == "fr"
                        else "📎 *Photo / boarding pass* saved to your passenger row in Airtable.\n\n"
                        "_(Send *First LAST* for this passenger if needed.)_"
                    ),
                )
                return True

        raw_in = (t or "").strip().split("\n")[0].strip()
        raw_in = re.sub(r"^[\d\.\)\-\s]+", "", raw_in).strip()
        low_in = raw_in.lower()
        if not image_b64 and idx >= 2 and low_in in (
            "2", "b", "option b", "optionb", "manuel", "manuelle",
            "pas de carte", "pas carte", "no pass", "no card",
        ):
            send(
                phone,
                "⌨️ *Pas de carte pour l'instant* : envoyez maintenant *Prénom NOM* (nom en majuscules). "
                "Nous vous demanderons une *photo du billet plus tard* pour compléter le dossier."
                if lang == "fr"
                else "⌨️ *No boarding pass right now*: send *First LAST* (LAST in caps). "
                "We’ll ask for a *boarding-pass photo later* to complete the file.",
            )
            return True

        # Nettoie la ligne
        first = t.split("\n")[0].strip()
        clean = re.sub(r"^[\d\.\)\-\s]+", "", first).strip()

        # Format Prénom NOM
        parts = re.split(r"\s+", clean)
        if len(parts) >= 2:
            prenom = parts[0].title()
            nom    = " ".join(parts[1:]).upper()
            formatted = f"{prenom} {nom}"
        else:
            # Nom trop court → redemande
            send(
                phone,
                "👤 Envoyez *Prénom NOM* (2 mots minimum)\nEx. : *Aminata TRAORE*"
                if lang == "fr"
                else "👤 Send *First LAST* (2 words min)\nEx.: *Fatou SALL*",
            )
            return True

        names = list(conv["data"].get("passenger_names") or [])
        names.append(formatted)
        conv["data"]["passenger_names"] = names

        if len(names) >= pax:
            goto_passenger_names_confirm(phone, conv, lang)
        elif len(names) == 3 and pax > 3:
            goto_passenger_names_confirm(phone, conv, lang)
        else:
            conv["step"] = "passenger_name_post_add"
            q_passenger_name_post_add_confirm(phone, lang, conv, formatted)
        at_save(phone, conv)
        return True

    if step == "passenger_names_confirm":
        names = list(conv["data"].get("passenger_names") or [])
        pax = conv["data"].get("passengers") or 1
        if image_b64:
            merged = read_boarding_pass_merged(image_b64)
            if boarding_pass_info_usable(merged):
                if apply_boarding_pass_image(phone, conv, image_b64, lang):
                    return True
            idxs = list(range(min(len(names), pax)))
            n_att = at_boarding_attach_to_indices(phone, conv, image_b64, idxs, lang) if idxs else 0
            if n_att:
                send(
                    phone,
                    (
                        (
                            f"📎 Carte / photo enregistrée sur le passager dans Airtable."
                            if n_att == 1 and lang == "fr"
                            else (
                                f"📎 *{n_att}* enregistrements carte / photo sur les passagers dans Airtable."
                                if lang == "fr"
                                else (
                                    "📎 Boarding pass / photo saved to the passenger row in Airtable."
                                    if n_att == 1
                                    else f"📎 *{n_att}* boarding pass / photo attachments saved to passenger rows in Airtable."
                                )
                            )
                        )
                    ),
                )
                return True
        partial = len(names) < pax
        if choice == "1":
            if partial:
                conv["step"] = "passenger_names"
                nxt = len(names) + 1
                conv["data"]["pax_collect_idx"] = nxt
                if nxt >= 2:
                    q_boarding_next_passenger(phone, lang, nxt, pax)
                q_passenger_name(phone, lang, nxt, pax, names)
            else:
                conv["step"] = "minor_check"
                q_minors(phone, lang, conv)
            at_save(phone, conv)
            return True
        if choice == "2":
            if not names:
                return False
            names.pop()
            conv["data"]["passenger_names"] = names
            conv["data"]["pax_collect_idx"] = len(names) + 1
            conv["step"] = "passenger_names"
            q_passenger_name(phone, lang, len(names) + 1, pax, names)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 10 : MINEURS ───────────────────────────────────────────
    if step == "minor_check":
        if choice == "1":
            conv["data"]["has_minors"] = False
            conv["step"] = "summary"
            show_summary(phone, conv)
            return True
        if choice == "2":
            conv["data"]["has_minors"] = True
            if (conv["data"].get("passengers") or 1) == 1:
                send(phone, f"👶 Mineur seul : un parent doit signer.\n📱 Climbie : {CLIMBIE_TEL}")
            else:
                send(phone, f"👶 Noté ! Un représentant légal devra co-signer le mandat.\n\nOn continue 👇" if lang=="fr" else f"👶 Noted! A legal guardian will need to co-sign.\n\nLet's continue 👇")
            conv["step"] = "summary"
            show_summary(phone, conv)
            return True
        return False

    return False

# ===== WEBHOOK =====

TRIGGER_WORDS = [
    "vol", "retard", "annul", "indemn", "flight", "delay", "cancel",
    "compensation", "claim", "bonjour", "hello", "salut", "hi",
    "start", "commencer", "menu", "aide", "help", "dossier", "mandat",
]

@app.route("/mandat_signed", methods=["POST"])
def mandat_signed_webhook():
    """
    Appelé par le site / Make / Zapier quand le mandat est signé.
    Body JSON : {"ref":"RDA-YYYYMMDD-XXXX","secret":"…"} — secret = MANDAT_SIGNED_WEBHOOK_SECRET.
    Optionnel : "waId" ou "phone" pour désambigüiser si plusieurs sessions (rare).
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
        if c.get("ref") != ref:
            continue
        ps = (c.get("data") or {}).get("_post_submit")
        if not isinstance(ps, dict) or not ps.get("active"):
            continue
        if h_norm:
            p_norm = _digits(str(p))
            if p_norm != h_norm and not (p_norm.endswith(h_norm) or h_norm.endswith(p_norm)):
                continue
        ps["mandate_signed_server"] = True
        updated += 1
        if post_submit_fully_done(c):
            ps["active"] = False
    return jsonify({"ok": True, "updated": updated}), 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "service": "robin-des-airs-bot", "webhook": "ready"}), 200

        raw_preview = (request.get_data(as_text=True) or "")[:800]
        print(f"[webhook] IN POST preview={raw_preview!r}", flush=True)

        data = request.get_json(silent=True) or {}
        if not data and raw_preview.strip().startswith("{"):
            try:
                data = json.loads(raw_preview)
            except (json.JSONDecodeError, TypeError):
                data = {}
        if not data:
            print("[webhook] no JSON body", flush=True)
            return jsonify({"status": "no data"}), 200

        print(
            f"[webhook] type={data.get('type')!r} event={data.get('eventType')!r} "
            f"waId={data.get('waId')!r} text={(data.get('text') or '')[:80]!r} "
            f"listReply={bool(data.get('listReply'))}",
            flush=True,
        )

        phone = normalize_phone_key(
            data.get("waId") or data.get("from") or data.get("phone")
        )
        if not phone:
            return jsonify({"status": "no phone"}), 200
        if data.get("owner") is True or str(data.get("owner", "")).lower() in ("true", "1", "yes"):
            print("[webhook] ignored owner message", flush=True)
            return jsonify({"status": "ignored own"}), 200

        conv = get_conv(phone)

        message_text, image_b64 = parse_wati_inbound_message(data)
        message_text = enrich_message_from_wati_interactive(data, message_text)
        poll_choice = resolve_tunnel_choice(conv, data, message_text)
        if poll_choice and not looks_like_full_date_input(message_text):
            message_text = poll_choice
        message_text = (message_text or "").strip()

        has_interactive = _has_wati_interactive(data)

        if not message_text and not image_b64 and not has_interactive:
            print(f"[webhook] ignored empty keys={list(data.keys())[:12]}")
            return jsonify({"status": "ignored empty"}), 200

        if not message_text and has_interactive and not poll_choice:
            print(
                f"[webhook] interactive sans choix résolu type={data.get('type')} "
                f"listReply={data.get('listReply')} btn={data.get('interactiveButtonReply')}",
                flush=True,
            )
            lang = (conv.get("data") or {}).get("lang", "fr")
            hint = (
                "👆 Choix non reçu. Ouvrez *Choisir* à nouveau ou tapez *1*, *2*, *3*… *6*."
                if lang == "fr"
                else "👆 Choice not received. Open *Choose* again or type *1*…*6*."
            )
            send(phone, hint)
            return jsonify({"status": "ok", "note": "interactive_unparsed"}), 200

        sig  = f"{message_text.lower()}|img:{bool(image_b64)}|poll:{poll_choice or ''}"
        step = conv.get("step")
        if is_dup(phone, data, sig, step):
            return jsonify({"status": "duplicate"}), 200

        touch_last_user_inbound(conv)

        print(
            f"[MSG] from={phone} step={step} text='{message_text[:50]}' "
            f"poll={poll_choice!r} type={data.get('type')}"
        )

        # Détection langue
        if message_text:
            conv["data"]["lang"] = detect_lang(message_text)
        lang = conv["data"].get("lang", "fr")

        # ── Flux en cours ──
        if step and step not in (None, "completed"):
            handled = handle_reply(phone, message_text, conv, image_b64)
            if not handled:
                fallback = (
                    "👆 *Appuyez sur un bouton* ou *ouvrez la liste* du dernier message.\n\n"
                    "💡 *menu* ou *recommencer* = tout relancer depuis le début."
                    if lang == "fr"
                    else "👆 *Tap a button* or *open the list* on the last message.\n\n"
                    "💡 *menu* or *restart* = start over from the beginning."
                )
                rep_ai = None
                if message_text and len(message_text.strip()) >= 3:
                    rep_ai = gpt_tunnel_assist(phone, message_text, step, lang)
                send(phone, rep_ai or fallback)
            return jsonify({"status": "ok"}), 200

        is_trigger = any(w in (message_text or "").lower() for w in TRIGGER_WORDS)

        # ── Dossier déjà terminé : ne pas relancer le tunnel sur chaque message court ──
        if step == "completed":
            update_post_submit_inbound(phone, conv, message_text, image_b64)
            if image_b64:
                ps0 = conv.get("data", {}).get("_post_submit") or {}
                if ps0.get("post_submit_has_boarding_image"):
                    at_save(phone, conv)
                    at_boarding_attach_to_indices(phone, conv, image_b64, [0], lang)
                    refresh_post_submit_airtable_flags(conv)
                ack = post_submit_after_image_ack(conv, lang)
                if ack:
                    send(phone, ack)
                    if post_submit_fully_done(conv):
                        send(
                            phone,
                            (
                                "✅ *Dossier complet*\n\n"
                                "• ⚙️ Nous traitons votre dossier\n"
                                "• 💬 Questions : écrivez ici"
                            )
                            if lang == "fr"
                            else (
                                "✅ *File complete*\n\n"
                                "• ⚙️ We’re processing your claim\n"
                                "• 💬 Questions: reply here"
                            ),
                        )
                    return jsonify({"status": "ok", "post_submit_image": True}), 200
            if is_trigger or (message_text and user_wants_fresh_start(message_text)):
                conv["data"] = fresh_data(lang)
                conv["step"] = "passengers"
                conv["ref"] = make_ref(phone)
                q_passengers(phone, lang, conv)
                return jsonify({"status": "flow started"}), 200
            rep = gpt_free_reply(phone, message_text, conv, dossier_done=True)
            fallback_done = completed_phase_fallback_reply(conv, lang)
            send(phone, ((rep or "").strip() or fallback_done) + site_mandat_links_footer(lang))
            return jsonify({"status": "ok"}), 200

        # ── Nouveau contact (step None) : démarrage si mot-clé ou message très court ──
        if step is None:
            if is_trigger or len((message_text or "").strip()) < 18:
                conv["data"] = fresh_data(lang)
                conv["step"] = "passengers"
                conv["ref"] = make_ref(phone)
                q_passengers(phone, lang, conv)
                return jsonify({"status": "flow started"}), 200
            rep = gpt_free_reply(phone, message_text, conv, dossier_done=False)
            nudge = (
                "\n• 💡 *menu* = ouvrir le formulaire (liste de choix)"
                if lang == "fr"
                else "\n• 💡 *menu* = open the form (pick from the list)"
            )
            fallback_new = (
                "👋 *Bonjour*\n\n"
                "• 💬 Réponse courte à votre message ci-dessous\n"
                "• 📋 Dossier : tapez *menu* puis choisissez dans la liste"
                if lang == "fr"
                else (
                    "👋 *Hello*\n\n"
                    "• 💬 Short answer to your message below\n"
                    "• 📋 Open a claim: type *menu* then pick from the list"
                )
            )
            body = ((rep or "").strip() + nudge) if rep else (fallback_new + nudge)
            send(phone, body + site_mandat_links_footer(lang))
            return jsonify({"status": "ok"}), 200

        # Sécurité (étape inconnue)
        conv["data"] = fresh_data(lang)
        conv["step"] = "passengers"
        conv["ref"] = make_ref(phone)
        q_passengers(phone, lang, conv)
        return jsonify({"status": "flow started"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

# ===== ROUTES UTILITAIRES =====

@app.route("/test", methods=["GET"])
def test():
    return jsonify({
        "status":  "running",
        "version": "v11 — récap dossier + lien /sign + tunnel langue / parcours",
        "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
        "openai":   "OK" if OPENAI_API_KEY else "MISSING",
        "wati":     "OK" if WATI_API_TOKEN else "MISSING",
        "wati_post_submit_template": WATI_POST_SUBMIT_TEMPLATE_NAME or None,
        "wati_template_channel_configured": bool(WATI_TEMPLATE_CHANNEL_NUMBER),
        "airtable_f_identite": F_PIECE_IDENTITE or None,
        "airtable_f_carte_embarquement": F_CARTE_EMBARQUEMENT or None,
        "airtable_f_mandat_signe": F_MANDAT_SIGNE or None,
        "airtable_f_stop_relance": F_STOP_RELANCE or None,
        "airtable_f_sequence_active": F_SEQUENCE_ACTIVE or None,
        "mandat_signed_webhook": "on" if MANDAT_SIGNED_WEBHOOK_SECRET else "off",
        "convs":    len(conversations),
        "terms_url": TERMS_URL,
        "privacy_url": PRIVACY_POLICY_URL,
        "stop_relances": "GET/POST /post_submit/cancel/<waId> (optionnel ?secret= si POST_SUBMIT_CANCEL_SECRET)",
        "reset_conversation": "GET /reset/<waId>",
    }), 200

@app.route("/reset/<phone>", methods=["GET"])
def reset(phone):
    conversations.pop(phone, None)
    return jsonify({"status": "reset", "phone": phone}), 200

@app.route("/post_submit/cancel/<phone>", methods=["GET", "POST"])
def post_submit_cancel(phone):
    """
    Arrête uniquement la séquence de relances (mandat + pièces) pour ce numéro WhatsApp.
    Utile si la cliente a écrit par erreur après le message « dossier prêt » — sans effacer tout l’historique.
    Optionnel : POST_SUBMIT_CANCEL_SECRET dans l’env + ?secret=… pour limiter l’accès.
    """
    want = (os.environ.get("POST_SUBMIT_CANCEL_SECRET") or "").strip()
    if want:
        got = (request.args.get("secret") or request.headers.get("X-Cancel-Secret") or "").strip()
        if got != want:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    conv = conversations.get(phone)
    if not conv:
        return jsonify({"ok": False, "reason": "no_conversation", "phone": phone}), 404
    d = conv.get("data") or {}
    ps = d.get("_post_submit")
    if not isinstance(ps, dict):
        return jsonify({"ok": False, "reason": "no_post_submit", "phone": phone}), 200
    ps["active"] = False
    ps["cancelled_at"] = time.time()
    ps["cancel_reason"] = "operator"
    return jsonify({"ok": True, "phone": phone, "ref": conv.get("ref")}), 200

@app.route("/conversations", methods=["GET"])
def list_convs():
    return jsonify({p: {"step": c["step"], "ref": c["ref"], "data": c["data"]} for p, c in conversations.items()}), 200

@app.route("/test_flow/<phone>", methods=["GET"])
def test_flow(phone):
    c = get_conv(phone)
    c["data"] = fresh_data("fr")
    c["step"] = "passengers"
    c["ref"]  = make_ref(phone)
    q_passengers(phone, "fr")
    return jsonify({"status": "started", "phone": phone}), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v11 + récap dossier + /sign", 200

@app.route("/sign/<token>", methods=["GET"])
def mandate_sign_landing(token):
    """Page intermédiaire rassurante avant redirection vers l’URL mandat réelle."""
    _sign_redirect_cleanup()
    tok = (token or "").strip()
    ent = SIGN_REDIRECTS.get(tok) if tok else None
    if not ent or float(ent.get("exp") or 0) < time.time():
        return (
            "<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\"><title>Robin des Airs</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;padding:2rem;\"><p>Lien expiré ou invalide.</p>"
            f"<p><a href=\"{escape(RDA_DOMAIN, quote=True)}\">robindesairs.eu</a></p></body></html>",
            410,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    target = (ent.get("url") or "").strip()
    if not target:
        return redirect(RDA_DOMAIN, code=302)
    href = escape(target, quote=True)
    body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robin des Airs — Signature sécurisée</title>
</head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:520px;margin:48px auto;padding:0 16px;line-height:1.5;">
  <p style="font-size:1.5rem;margin:0 0 8px;">🏹 Robin des Airs</p>
  <h1 style="font-size:1.15rem;font-weight:600;margin:0 0 16px;">Signature sécurisée</h1>
  <p>Vous allez ouvrir la page officielle de signature du mandat (même site sécurisé que robindesairs.eu).</p>
  <p><a href="{href}" style="display:inline-block;padding:12px 20px;background:#0b5ed7;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;">Continuer vers la signature</a></p>
  <p style="color:#555;font-size:0.9rem;">En cas de problème, copiez-collez ce lien dans votre navigateur :<br><span style="word-break:break-all;">{href}</span></p>
</body>
</html>"""
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/m", methods=["GET"])
def mandat_compressed_redirect():
    """Décompresse ?c=… et redirige vers mandat.html avec les mêmes paramètres qu'avant."""
    c = (request.args.get("c") or "").strip().replace(" ", "+")
    if not c:
        return redirect(MANDAT_URL, code=302)
    pad = "=" * ((4 - len(c) % 4) % 4)
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(c.encode("ascii") + pad.encode("ascii")))
        loaded = json.loads(raw.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("payload not an object")
        query = urlencode({k: str(v) for k, v in loaded.items() if v not in (None, "")})
        target = f"{MANDAT_URL}?{query}" if query else MANDAT_URL
        return redirect(target, code=302)
    except Exception as ex:
        print(f"mandat /m decode error: {ex}")
        return redirect(MANDAT_URL, code=302)

start_post_submit_reminder_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
