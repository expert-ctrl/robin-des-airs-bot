from flask import Flask, request, jsonify
import requests
import os
import json
import base64
import re
import hashlib
from datetime import datetime, timedelta

app = Flask(__name__)

# ===== CONFIG =====
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN   = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL    = os.environ.get("WATI_BASE_URL", "")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appv72lKbQtjt7EIP")

RDA_DOMAIN = os.environ.get("RDA_SITE", os.environ.get("RDA_DOMAIN", "https://robindesairs.eu"))
# Mandat HTML integre (copie public/mandat.html du site) — un seul fichier a deployer.
_MANDAT_ZLIB_B64 = (
    "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"
    "Hvw/CBEh/A=="
)

def _decode_mandat_html():
    import zlib as _z, base64 as _b
    return _z.decompress(_b.b64decode("".join(_MANDAT_ZLIB_B64))).decode("utf-8")


_MANDAT_HTML_BODY = _decode_mandat_html()
MANDAT_URL = os.environ.get("MANDAT_URL", f"{RDA_DOMAIN.rstrip('/')}/mandat.html")
DEPOT_URL   = f"{RDA_DOMAIN.rstrip('/')}/depot-express"
SUIVI_URL   = f"{RDA_DOMAIN.rstrip('/')}/suivi-dossier"
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
    "denied": "Refus d'embarquement",
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


def split_itinerary_for_mandat(itin):
    """Découpe une ligne d'itinéraire pour préremplir départ / arrivée / escale(s)."""
    itin = (itin or "").strip()
    if not itin:
        return "", "", ""
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


# ===== FLUX EN 8 ÉTAPES =====
# 1. passengers        → nombre de passagers + montant affiché immédiatement
# 2. incident_type     → retard / annulation / refus
# 3. airline           → compagnie aérienne
# 4. pnr_input         → PNR (code réservation 6 car)
# 5. flight_number     → numéro de vol (ou photo)
# 6. flight_date       → année → mois → jour
# 7. itinerary         → tracé du vol (mandat : départ / arrivée / escales)
# 8. passenger_names   → noms un par un
# 9. minor_check       → mineurs oui/non → récap + lien mandat

STEPS = [
    "passengers", "incident_type", "airline", "airline_other",
    "pnr_input", "flight_number",
    "flight_date", "flight_month", "flight_day",
    "itinerary",
    "passenger_names", "minor_check",
    "summary", "completed",
]

# ===== MEMOIRE =====
conversations    = {}
recent_event_ids = {}
MEMORY_HOURS     = 24

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
    }

def get_conv(phone):
    now = datetime.now()
    if phone in conversations:
        if (now - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
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
        return
    url     = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    try:
        r = requests.post(url, headers=headers, params={"messageText": msg}, timeout=30)
        print(f"Wati {r.status_code}")
    except Exception as e:
        print(f"Wati error: {e}")

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

def at_save(phone, conv):
    """Sauvegarde progressive — crée ou met à jour les records Airtable."""
    if not AIRTABLE_API_KEY:
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

        existing = at_find(ref)

        extra_bits = []
        if d.get("itinerary"):
            extra_bits.append(f"Itinéraire: {d['itinerary']}")
        if d.get("has_minors"):
            extra_bits.append("Mineur(s): oui")
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

# ===== MESSAGES DU FLUX =====

def q_passengers(phone, lang):
    """Étape 1 — Passagers + montant visible immédiatement"""
    rows = []
    for n in range(1, 6):
        brut, net, _, _ = calc_amounts(n)
        rows.append(f"{n}️⃣  {n} passager{'s' if n>1 else ''} — 💶 jusqu'à *{net}€* net")
    rows.append(f"6️⃣  6 ou plus — 📱 Climbie vous appelle")
    bloc = "\n".join(rows)

    if lang == "en":
        msg = (
            "👋 Welcome to *Robin des Airs* ✈️\n\n"
            "Flight delayed or cancelled? You may be owed *up to 600€* per passenger.\n\n"
            "👥 *How many passengers?*\n\n"
            + bloc.replace("passager", "passenger").replace("ou plus", "or more")
            .replace("jusqu'à", "up to").replace("vous appelle", "will call you")
            + "\n\nReply *1–6*"
        )
    else:
        msg = (
            "👋 Bienvenue chez *Robin des Airs* ✈️\n\n"
            "Vol retardé ou annulé ? Vous avez peut-être droit à *600€ par passager*.\n\n"
            "👥 *Combien de passagers ?*\n\n"
            + bloc
            + "\n\nRépondez *1 à 6*"
        )
    send(phone, msg)

def q_incident(phone, lang, pax):
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
    if lang == "en":
        msg = (
            f"✅ *{pax} passenger(s) noted!*\n\n"
            f"{box}\n\n"
            "✈️ *What happened?*\n\n"
            "1️⃣  Delay of 3+ hours ⏱️\n"
            "2️⃣  Flight cancelled ❌\n"
            "3️⃣  Denied boarding 🚫\n\n"
            "Reply *1, 2 or 3*"
        )
    else:
        msg = (
            f"✅ *{pax} passager(s) noté(s) !*\n\n"
            f"{box}\n\n"
            "✈️ *Que s'est-il passé ?*\n\n"
            "1️⃣  Retard de +3 heures ⏱️\n"
            "2️⃣  Vol annulé ❌\n"
            "3️⃣  Refus d'embarquement 🚫\n\n"
            "Répondez *1, 2 ou 3*"
        )
    send(phone, msg)

def q_airline(phone, lang):
    lines = [f"{k}️⃣  {v}" for k, v in AIRLINES_MAP.items()]
    lines.append("9️⃣  Autre — tapez le nom")
    bloc = "\n".join(lines)
    if lang == "en":
        msg = f"🛫 *Which airline?*\n\n{bloc}\n\nReply *1–9* or type the name"
    else:
        msg = f"🛫 *Quelle compagnie aérienne ?*\n\n{bloc}\n\nRépondez *1 à 9* ou tapez le nom"
    send(phone, msg)

def q_pnr(phone, lang, airline):
    if lang == "en":
        msg = (
            f"✅ *{airline}* noted!\n\n"
            "📋 *PNR / Booking reference*\n"
            "(6-character code on your confirmation email)\n\n"
            "Example: *ABC123*\n\n"
            "_(Don't have it? Reply *SKIP*)_"
        )
    else:
        msg = (
            f"✅ *{airline}* noté !\n\n"
            "📋 *PNR / Code de réservation*\n"
            "(6 caractères sur votre email de confirmation)\n\n"
            "Exemple : *ABC123*\n\n"
            "_(Pas le code ? Répondez *SKIP*)_"
        )
    send(phone, msg)

def q_flight_number(phone, lang):
    if lang == "en":
        msg = (
            "✈️ *Flight number?*\n\n"
            "Example: *AF718 · SN271 · KL563*\n\n"
            "📸 Or send a photo of your boarding pass"
        )
    else:
        msg = (
            "✈️ *Numéro de vol ?*\n\n"
            "Exemple : *AF718 · SN271 · KL563*\n\n"
            "📸 Ou envoyez une photo de votre carte d'embarquement"
        )
    send(phone, msg)

def q_flight_date(phone, lang, conv):
    cy = datetime.now().year
    conv["data"]["temp_years"] = [cy, cy-1, cy-2, cy-3, cy-4]
    if lang == "en":
        msg = (
            "📅 *Year of the flight?*\n\n"
            f"1️⃣  {cy}\n2️⃣  {cy-1}\n3️⃣  {cy-2}\n4️⃣  {cy-3}\n5️⃣  {cy-4}\n"
            f"6️⃣  Before {cy-4} _(outside 5-year limit)_"
        )
    else:
        msg = (
            "📅 *Année du vol ?*\n\n"
            f"1️⃣  {cy}\n2️⃣  {cy-1}\n3️⃣  {cy-2}\n4️⃣  {cy-3}\n5️⃣  {cy-4}\n"
            f"6️⃣  Avant {cy-4} _(hors rétroactivité 5 ans)_"
        )
    send(phone, msg)

def q_flight_month(phone, lang, year):
    y = (year or "").strip() or "?"
    if lang == "en":
        msg = (
            f"📌 *Year selected: {y}*\n\n"
            "📅 *Month of the flight?*\n\n"
            "1️⃣ Jan  2️⃣ Feb  3️⃣ Mar  4️⃣ Apr\n"
            "5️⃣ May  6️⃣ Jun  7️⃣ Jul  8️⃣ Aug\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Dec"
        )
    else:
        msg = (
            f"📌 *Année retenue : {y}*\n\n"
            "📅 *Mois du vol ?*\n\n"
            "1️⃣ Jan  2️⃣ Fév  3️⃣ Mar  4️⃣ Avr\n"
            "5️⃣ Mai  6️⃣ Juin  7️⃣ Juil  8️⃣ Août\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Déc"
        )
    send(phone, msg)

def q_flight_day(phone, lang, year, month_mm):
    y = (year or "").strip() or "?"
    mw = month_word(month_mm, lang)
    if lang == "en":
        msg = (
            f"📌 *So far: {mw} {y}*\n\n"
            "📅 *Exact day of the flight?* _(1–31)_"
        )
    else:
        msg = (
            f"📌 *Déjà choisi : {mw} {y}*\n\n"
            "📅 *Jour du vol ?* _(1–31)_"
        )
    send(phone, msg)


def q_itinerary(phone, lang, conv):
    """Demande le tracé du vol (préremplit départ / arrivée / escales sur le mandat)."""
    fd = conv["data"].get("flight_date") or "?"
    fn = conv["data"].get("flight_number") or "?"
    if lang == "en":
        msg = (
            f"🛤️ *Flight route* (vol *{fn}* · {fd})\n\n"
            "Send **departure → final arrival** (cities or airport codes).\n"
            "If you had stopovers: **A → B → C** (in order).\n\n"
            "Examples: *Brussels BRU → Abidjan ABJ*\n"
            "*Paris CDG → Casablanca CMN → Dakar DSS*"
        )
    else:
        msg = (
            f"🛤️ *Itinéraire du vol* (vol *{fn}* · {fd})\n\n"
            "Indiquez **départ → arrivée finale** (villes ou codes aéroports).\n"
            "Avec escales, dans l'ordre : **A → B → C**.\n\n"
            "Ex. : *Bruxelles BRU → Abidjan ABJ*\n"
            "*Paris CDG → Casablanca CMN → Dakar DSS*"
        )
    send(phone, msg)


def q_passenger_name(phone, lang, idx, pax, names_so_far):
    already = ""
    if names_so_far:
        already = "\n".join([f"✅ {i+1}. {n}" for i, n in enumerate(names_so_far)]) + "\n\n"
    if lang == "en":
        msg = (
            f"{already}"
            f"👤 *Passenger {idx} of {pax}*\n\n"
            "Send *First LAST* (last name in caps)\n"
            "Example: *John DOE*"
        )
    else:
        msg = (
            f"{already}"
            f"👤 *Passager {idx} sur {pax}*\n\n"
            "Envoyez *Prénom NOM* (nom en majuscules)\n"
            "Exemple : *Jean DUPONT*"
        )
    send(phone, msg)

def q_minors(phone, lang, pax):
    if lang == "en":
        msg = (
            "👶 *Any minors (under 18) among the passengers?*\n\n"
            "1️⃣  No — all adults\n"
            "2️⃣  Yes — at least one minor"
        )
    else:
        msg = (
            "👶 *Y a-t-il des mineurs (moins de 18 ans) parmi les passagers ?*\n\n"
            "1️⃣  Non — tous majeurs\n"
            "2️⃣  Oui — au moins un mineur"
        )
    send(phone, msg)

def show_summary(phone, conv):
    d    = conv["data"]
    lang = d.get("lang", "fr")
    pax  = d.get("passengers") or 1
    ref  = conv.get("ref") or make_ref(phone)
    conv["ref"] = ref

    brut, net, com, per_pax = calc_amounts(pax)
    incident = INCIDENT_LABELS.get(d.get("incident_type", ""), "?")
    names    = d.get("passenger_names") or []
    names_str = "\n".join([f"  • {n}" for n in names]) if names else "  • À compléter"
    pnr_line  = f"\n📋 PNR : *{d['pnr']}*" if d.get("pnr") else ""
    route_line = f"\n🛤️ *{d['itinerary']}*" if d.get("itinerary") else ""

    # Lien mandat pré-rempli
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
    query       = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items() if v)
    mandat_link = f"{MANDAT_URL}?{query}"

    box = (
        f"╔══════════════════════╗\n"
        f"║  💶 MONTANT ESTIMÉ     ║\n"
        f"║  {per_pax}€ × {pax} passager(s) ║\n"
        f"║  = *{brut} EUR* brut    ║\n"
        f"║                        ║\n"
        f"║  ✅ NET POUR VOUS :    ║\n"
        f"║   *{net} EUR* (75%)    ║\n"
        f"╚══════════════════════╝"
    )

    if lang == "en":
        msg = (
            f"🎉 *File created!*\n\n"
            f"📁 Ref: *{ref}*\n"
            f"✈️ Flight: *{d.get('flight_number','?')}* ({d.get('airline','?')}){pnr_line}{route_line}\n"
            f"📅 Date: *{d.get('flight_date','?')}*\n"
            f"⚠️ Incident: *{incident}*\n"
            f"👥 Passengers ({pax}):\n{names_str}\n\n"
            f"{box}\n\n"
            f"👇 *Sign your mandate (2 min):*\n{mandat_link}"
        )
    else:
        msg = (
            f"🎉 *Dossier créé !*\n\n"
            f"📁 Réf : *{ref}*\n"
            f"✈️ Vol : *{d.get('flight_number','?')}* ({d.get('airline','?')}){pnr_line}{route_line}\n"
            f"📅 Date : *{d.get('flight_date','?')}*\n"
            f"⚠️ Incident : *{incident}*\n"
            f"👥 Passagers ({pax}) :\n{names_str}\n\n"
            f"{box}\n\n"
            f"👇 *Signez votre mandat (2 min) :*\n{mandat_link}"
        )
    send(phone, msg)
    at_save(phone, conv)
    conv["step"] = "completed"

# ===== OPENAI (photo carte d'embarquement) =====

def gpt_read_boarding_pass(image_b64):
    if not OPENAI_API_KEY:
        return {}
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": 'Extract from this boarding pass. Reply ONLY JSON: {"flight_number":"","date":"DD/MM/YYYY","airline":""}'},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]
                }],
                "max_tokens": 150,
            },
            timeout=45,
        )
        txt = r.json()["choices"][0]["message"]["content"]
        m   = re.search(r"\{[^}]+\}", txt)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        print(f"GPT vision error: {e}")
        return {}

def gpt_free_reply(phone, text, conv):
    """Réponse libre OpenAI pour les messages hors flux."""
    if not OPENAI_API_KEY:
        return None
    lang = conv["data"].get("lang", "fr")
    system = (
        f"Tu es l'assistant de Robin des Airs (EU261). "
        f"Réponds en {'français' if lang=='fr' else 'anglais'}, "
        f"max 5 lignes, 3+ emojis. "
        f"Renvoie toujours vers {MANDAT_URL} pour déposer un dossier."
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 300,
                "temperature": 0.7,
            },
            timeout=30,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"GPT error: {e}")
        return None

# ===== TRAITEMENT RÉPONSES =====

def handle_reply(phone, text, conv, image_b64=None):
    step = conv.get("step")
    lang = conv["data"].get("lang", "fr")
    t    = text.strip()
    low  = t.lower()

    # Extraction du premier chiffre
    m      = re.search(r"^(\d+)", t)
    choice = m.group(1) if m else None

    print(f"[STEP={step}] text='{t[:30]}' choice={choice}")

    # ── ÉTAPE 1 : PASSAGERS ──────────────────────────────────────────
    if step == "passengers":
        if choice in ["1","2","3","4","5"]:
            pax = int(choice)
            conv["data"]["passengers"] = pax
            conv["step"] = "incident_type"
            q_incident(phone, lang, pax)
            at_save(phone, conv)
            return True
        if choice == "6":
            send(phone, f"🙏 Pour 6+ passagers, *Climbie* vous contacte personnellement.\n\n📱 {CLIMBIE_TEL}\n\n👉 {DEPOT_URL}")
            return True
        return False

    # ── ÉTAPE 2 : INCIDENT ───────────────────────────────────────────
    if step == "incident_type":
        mapping = {"1": "delay", "2": "cancel", "3": "denied"}
        if choice in mapping:
            conv["data"]["incident_type"] = mapping[choice]
            conv["step"] = "airline"
            q_airline(phone, lang)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 3 : COMPAGNIE ──────────────────────────────────────────
    if step == "airline":
        u = t.strip()
        # Touche numérique seule (WhatsApp peut envoyer 1 + variation + keycap)
        m_key = re.match(r"^\s*([1-9])(?:\uFE0F\u20E3)?\s*$", u)
        d1    = m_key.group(1) if m_key else None
        if d1 and d1 in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[d1]
            conv["step"] = "pnr_input"
            q_pnr(phone, lang, conv["data"]["airline"])
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
                "⚠️ Répondez *1 à 8* pour la liste, *9* pour « autre », ou *tapez le nom* de la compagnie (ex. *EasyJet*)."
                if lang == "fr"
                else "⚠️ Reply *1–8* from the list, *9* for *other*, or *type the airline name* (e.g. *EasyJet*).",
            )
            return True
        # Nom tapé (lettres : évite interpréter "12" comme compagnie)
        has_alpha = bool(re.search(r"[A-Za-zÀ-ÿ]", u))
        if has_alpha and len(u) >= 2:
            conv["data"]["airline"] = u
            conv["step"] = "pnr_input"
            q_pnr(phone, lang, u)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 3b : AUTRE COMPAGNIE ───────────────────────────────────
    if step == "airline_other":
        conv["data"]["airline"] = t
        conv["step"] = "pnr_input"
        q_pnr(phone, lang, t)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 4 : PNR ────────────────────────────────────────────────
    if step == "pnr_input":
        pnr_clean = re.sub(r"[^A-Z0-9]", "", t.upper())
        if low in ("skip", "passer", "aucun", "non", "no") or not pnr_clean:
            conv["data"]["pnr"] = None
        else:
            conv["data"]["pnr"] = pnr_clean[:8]
        conv["step"] = "flight_number"
        q_flight_number(phone, lang)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 5 : NUMÉRO DE VOL ──────────────────────────────────────
    if step == "flight_number":
        # Photo carte d'embarquement
        if image_b64:
            info = gpt_read_boarding_pass(image_b64)
            if info.get("flight_number"):
                conv["data"]["flight_number"] = info["flight_number"]
                if info.get("airline") and not conv["data"].get("airline"):
                    conv["data"]["airline"] = info["airline"]
                if info.get("date"):
                    conv["data"]["flight_date"] = info["date"]
                    conv["step"] = "itinerary"
                    send(phone, f"📸 Carte lue !\n✈️ *{info['flight_number']}* · {info.get('airline','')}\n📅 {info.get('date','')}")
                    q_itinerary(phone, lang, conv)
                    at_save(phone, conv)
                    return True
                else:
                    conv["step"] = "flight_date"
                    send(phone, f"📸 Vol *{info['flight_number']}* lu !")
                    q_flight_date(phone, lang, conv)
                    at_save(phone, conv)
                    return True
        # Texte
        m2 = re.search(r"\b([A-Z]{1,2}\d{1,4})\b", t.upper())
        conv["data"]["flight_number"] = m2.group(1) if m2 else t.upper()[:10]
        conv["step"] = "flight_date"
        q_flight_date(phone, lang, conv)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 6a : ANNÉE ─────────────────────────────────────────────
    if step == "flight_date":
        years = conv["data"].get("temp_years", [])
        if choice == "6":
            send(phone, f"😔 Rétroactivité 5 ans max. Votre vol est trop ancien.\n\n👉 {RDA_DOMAIN}")
            return True
        idx = int(choice) - 1 if choice and choice.isdigit() else -1
        if 0 <= idx < len(years):
            conv["data"]["temp_year"] = str(years[idx])
            conv["step"] = "flight_month"
            q_flight_month(phone, lang, conv["data"]["temp_year"])
            return True
        return False

    # ── ÉTAPE 6b : MOIS ──────────────────────────────────────────────
    if step == "flight_month":
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            conv["step"] = "flight_day"
            q_flight_day(phone, lang, conv["data"].get("temp_year", ""), conv["data"]["temp_month"])
            return True
        return False

    # ── ÉTAPE 6c : JOUR ──────────────────────────────────────────────
    if step == "flight_day":
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day   = f"{int(choice):02d}"
            year  = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            conv["step"] = "itinerary"
            q_itinerary(phone, lang, conv)
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 7 : ITINÉRAIRE (mandat) ───────────────────────────────
    if step == "itinerary":
        if len(t) >= 5:
            conv["data"]["itinerary"] = t
            conv["step"] = "passenger_names"
            q_passenger_name(phone, lang, 1, conv["data"]["passengers"] or 1, [])
            at_save(phone, conv)
            return True
        return False

    # ── ÉTAPE 8 : NOMS PASSAGERS (un par un) ─────────────────────────
    if step == "passenger_names":
        pax = conv["data"].get("passengers") or 1
        idx = conv["data"].get("pax_collect_idx") or 1

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
            send(phone, f"👤 Envoyez *Prénom NOM* (2 mots minimum)\nEx : *Jean DUPONT*" if lang=="fr" else f"👤 Send *First LAST* (2 words min)\nEx: *John DOE*")
            return True

        names = list(conv["data"].get("passenger_names") or [])
        names.append(formatted)
        conv["data"]["passenger_names"] = names

        if len(names) >= pax:
            # Tous les noms collectés → mineurs
            conv["step"] = "minor_check"
            q_minors(phone, lang, pax)
        else:
            conv["data"]["pax_collect_idx"] = idx + 1
            q_passenger_name(phone, lang, idx + 1, pax, names)
        at_save(phone, conv)
        return True

    # ── ÉTAPE 9 : MINEURS ────────────────────────────────────────────
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

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        phone = data.get("waId") or data.get("from") or data.get("phone")
        if not phone:
            return jsonify({"status": "no phone"}), 200
        if data.get("owner") is True:
            return jsonify({"status": "ignored own"}), 200

        conv = get_conv(phone)

        # Extraction message / image
        image_b64    = None
        message_text = ""

        if data.get("type") == "image" or "image" in data:
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    r = requests.get(media_url, headers={"Authorization": f"Bearer {WATI_API_TOKEN}"}, timeout=30)
                    if r.status_code == 200:
                        image_b64 = base64.b64encode(r.content).decode()
                except Exception:
                    pass
            message_text = data.get("caption", "") or ""
        else:
            if isinstance(data.get("text"), dict):
                message_text = data["text"].get("body", "")
            elif isinstance(data.get("text"), str):
                message_text = data["text"]
            elif data.get("body"):
                message_text = data["body"]

        if not message_text and not image_b64:
            return jsonify({"status": "ignored empty"}), 200

        sig  = f"{message_text.strip().lower()}|img:{bool(image_b64)}"
        step = conv.get("step")
        if is_dup(phone, data, sig, step):
            return jsonify({"status": "duplicate"}), 200

        print(f"[MSG] from={phone} step={step} text='{message_text[:50]}'")

        # Détection langue
        if message_text:
            conv["data"]["lang"] = detect_lang(message_text)
        lang = conv["data"].get("lang", "fr")

        # ── Flux en cours ──
        if step and step not in (None, "completed"):
            handled = handle_reply(phone, message_text, conv, image_b64)
            if not handled:
                send(phone,
                     "👆 Répondez avec le numéro proposé (ex : *1*, *2*, *3*…)"
                     if lang == "fr" else
                     "👆 Reply with the number shown (e.g. *1*, *2*, *3*…)")
            return jsonify({"status": "ok"}), 200

        # ── Démarrage ──
        is_trigger = any(w in message_text.lower() for w in TRIGGER_WORDS)
        if step is None or step == "completed" or is_trigger or len(message_text) < 60:
            # Reset propre + démarrage
            ref_saved = conv.get("ref")  # garde la ref si dossier existant
            conv["data"] = fresh_data(lang)
            conv["step"] = "passengers"
            conv["ref"]  = make_ref(phone)
            q_passengers(phone, lang)
            return jsonify({"status": "flow started"}), 200

        # ── Réponse libre ──
        rep = gpt_free_reply(phone, message_text, conv)
        if not rep:
            rep = f"Bonjour ! 😊 Je suis Robin des Airs.\n\nTapez *menu* pour vérifier votre droit à indemnisation ✈️\n\n👉 {MANDAT_URL}"
        send(phone, rep)
        return jsonify({"status": "ok"}), 200

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
        "version": "v9 — tunnel 9 etapes (itineraire) + mandat mineurs/itineraire",
        "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
        "openai":   "OK" if OPENAI_API_KEY else "MISSING",
        "wati":     "OK" if WATI_API_TOKEN else "MISSING",
        "convs":    len(conversations),
    }), 200

@app.route("/reset/<phone>", methods=["GET"])
def reset(phone):
    conversations.pop(phone, None)
    return jsonify({"status": "reset", "phone": phone}), 200

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
    return "Robin des Airs Bot v9 + mandat integre", 200


@app.route("/mandat.html", methods=["GET"])
def serve_mandat_html():
    """Sert le mandat HTML integre (meme contenu que public/mandat.html)."""
    return _MANDAT_HTML_BODY, 200, {"Content-Type": "text/html; charset=utf-8"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False) 
