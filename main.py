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

RDA_DOMAIN  = "https://robindesairs.eu"
MANDAT_URL  = f"{RDA_DOMAIN}/mandat-representation"
DEPOT_URL   = f"{RDA_DOMAIN}/depot-express"
SUIVI_URL   = f"{RDA_DOMAIN}/suivi-dossier"
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

# ===== FLUX EN 8 ÉTAPES =====
# 1. passengers        → nombre de passagers + montant affiché immédiatement
# 2. incident_type     → retard / annulation / refus
# 3. airline           → compagnie aérienne
# 4. pnr_input         → PNR (code réservation 6 car)
# 5. flight_number     → numéro de vol (ou photo)
# 6. flight_date       → année → mois → jour
# 7. passenger_names   → noms un par un
# 8. minor_check       → mineurs oui/non → récap + lien mandat

STEPS = [
    "passengers", "incident_type", "airline", "airline_other",
    "pnr_input", "flight_number",
    "flight_date", "flight_month", "flight_day",
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

        if not existing:
            # CRÉATION — 1 ligne par passager
            records = []
            for i in range(pax):
                f = dict(common)
                f[F_NOM_PASSAGER] = names[i] if i < len(names) else f"Passager {i+1}"
                f[F_REMARQUES]    = f"Ref: {ref} | Passager {i+1}/{pax} | Bot WhatsApp"
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


def q_flight_month(phone, lang):
    if lang == "en":
        msg = (
            "📅 *Month?*\n\n"
            "1️⃣ Jan  2️⃣ Feb  3️⃣ Mar  4️⃣ Apr\n"
            "5️⃣ May  6️⃣ Jun  7️⃣ Jul  8️⃣ Aug\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Dec"
        )
    else:
        msg = (
            "📅 *Mois ?*\n\n"
            "1️⃣ Jan  2️⃣ Fév  3️⃣ Mar  4️⃣ Avr\n"
            "5️⃣ Mai  6️⃣ Juin  7️⃣ Juil  8️⃣ Août\n"
            "9️⃣ Sep  *10* Oct  *11* Nov  *12* Déc"
        )
    send(phone, msg)


def q_flight_day(phone, lang):
    msg = "📅 *Jour exact ?* (1–31)" if lang == "fr" else "📅 *Exact day?* (1–31)"
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
            f"✈️ Flight: *{d.get('flight_number','?')}* ({d.get('airline','?')}){pnr_line}\n"
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
            f"✈️ Vol : *{d.get('flight_number','?')}* ({d.get('airline','?')}){pnr_line}\n"
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
            at_save(phone, conv)
            q_incident(phone, lang, pax)
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
            at_save(phone, conv)
            q_airline(phone, lang)
            return True
        return False

    # ── ÉTAPE 3 : COMPAGNIE ──────────────────────────────────────────
    if step == "airline":
        if choice and choice in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[choice]
            conv["step"] = "pnr_input"
            at_save(phone, conv)
            q_pnr(phone, lang, conv["data"]["airline"])
            return True
        if choice == "9":
            conv["step"] = "airline_other"
            send(phone, "✍️ Tapez le nom de votre compagnie :" if lang=="fr" else "✍️ Type your airline name:")
            return True
        # Nom tapé directement
        if not choice and len(t) >= 3:
            conv["data"]["airline"] = t
            conv["step"] = "pnr_input"
            at_save(phone, conv)
            q_pnr(phone, lang, t)
            return True
        return False

    # ── ÉTAPE 3b : AUTRE COMPAGNIE ───────────────────────────────────
    if step == "airline_other":
        conv["data"]["airline"] = t
        conv["step"] = "pnr_input"
        at_save(phone, conv)
        q_pnr(phone, lang, t)
        return True

    # ── ÉTAPE 4 : PNR ────────────────────────────────────────────────
    if step == "pnr_input":
        pnr_clean = re.sub(r"[^A-Z0-9]", "", t.upper())
        if low in ("skip", "passer", "aucun", "non", "no") or not pnr_clean:
            conv["data"]["pnr"] = None
        else:
            conv["data"]["pnr"] = pnr_clean[:8]
        conv["step"] = "flight_number"
        at_save(phone, conv)
        q_flight_number(phone, lang)
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
                    conv["step"] = "passenger_names"
                    at_save(phone, conv)
                    send(phone, f"📸 Carte lue !\n✈️ *{info['flight_number']}* · {info.get('airline','')}\n📅 {info.get('date','')}")
                    q_passenger_name(phone, lang, 1, conv["data"]["passengers"] or 1, [])
                    return True
                else:
                    conv["step"] = "flight_date"
                    at_save(phone, conv)
                    send(phone, f"📸 Vol *{info['flight_number']}* lu !")
                    q_flight_date(phone, lang, conv)
                    return True
        # Texte
        m2 = re.search(r"\b([A-Z]{1,2}\d{1,4})\b", t.upper())
        conv["data"]["flight_number"] = m2.group(1) if m2 else t.upper()[:10]
        conv["step"] = "flight_date"
        at_save(phone, conv)
        q_flight_date(phone, lang, conv)
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
            q_flight_month(phone, lang)
            return True
        return False

    # ── ÉTAPE 6b : MOIS ──────────────────────────────────────────────
    if step == "flight_month":
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            conv["step"] = "flight_day"
            q_flight_day(phone, lang)
            return True
        return False

    # ── ÉTAPE 6c : JOUR ──────────────────────────────────────────────
    if step == "flight_day":
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day   = f"{int(choice):02d}"
            year  = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            conv["step"] = "passenger_names"
            at_save(phone, conv)
            q_passenger_name(phone, lang, 1, conv["data"]["passengers"] or 1, [])
            return True
        return False

    # ── ÉTAPE 7 : NOMS PASSAGERS (un par un) ─────────────────────────
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
        at_save(phone, conv)

        if len(names) >= pax:
            # Tous les noms collectés → mineurs
            conv["step"] = "minor_check"
            q_minors(phone, lang, pax)
        else:
            conv["data"]["pax_collect_idx"] = idx + 1
            q_passenger_name(phone, lang, idx + 1, pax, names)
        return True

    # ── ÉTAPE 8 : MINEURS ────────────────────────────────────────────
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
        "version": "v9 — tunnel 8 étapes optimisé",
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
    return "Robin des Airs Bot v9", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
