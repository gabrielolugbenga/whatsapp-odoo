"""
WhatsApp → Claude → Odoo 19
Avec validation manuelle OUI/NON et support catalogue WhatsApp

pip install fastapi uvicorn httpx anthropic python-dotenv
"""

import os
import json
import logging
import xmlrpc.client
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import uuid
from dotenv import load_dotenv

import httpx
import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()

# ── Config ─────────────────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ODOO_URL      = os.environ["ODOO_URL"]
ODOO_DB       = os.environ["ODOO_DB"]
ODOO_USER     = os.environ["ODOO_USER"]
ODOO_PASSWORD = os.environ["ODOO_PASSWORD"]

WA_TOKEN     = os.environ["WHATSAPP_TOKEN"]
WA_PHONE_ID  = os.environ["WHATSAPP_PHONE_ID"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
ADMIN_PHONE  = os.environ.get("ADMIN_PHONE", "")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", 0.75))

# Commandes en attente de validation admin
# { token: { "order_data": ..., "phone": ..., "contact": ... } }
pending_orders: dict = {}


# ── Webhook ────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN and params.get("hub.mode") == "subscribe":
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="Token invalide")


@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()
    log.info("Webhook: %s", json.dumps(body, ensure_ascii=False))

    try:
        changes = body["entry"][0]["changes"][0]["value"]
        message = changes["messages"][0]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    phone    = message["from"]
    contact  = changes.get("contacts", [{}])[0].get("profile", {}).get("name", phone)
    msg_type = message.get("type")

    # Réponse OUI/NON de l'admin
    if phone == ADMIN_PHONE and msg_type == "text":
        txt = message["text"]["body"].strip().upper()
        if txt.startswith("OUI") or txt.startswith("NON"):
            await handle_admin_validation(txt)
            return {"status": "admin"}

    # Commande via catalogue WhatsApp
    if msg_type == "order":
        await process_catalog_order(phone, contact, message["order"])
        return {"status": "catalog"}

    # Commande texte libre
    if msg_type == "text":
        await process_text_order(phone, contact, message["text"]["body"])
        return {"status": "text"}

    return {"status": "ignored"}


# ── Validation admin ───────────────────────────────────────────────────────────

async def handle_admin_validation(text: str):
    if not pending_orders:
        await send_whatsapp(ADMIN_PHONE, "Aucune commande en attente.")
        return

    token   = next(iter(pending_orders))
    pending = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone      = pending["phone"]
    contact    = pending["contact"]

    if text.startswith("OUI"):
        result = create_sale_order(order_data, phone, contact)
        if result is None:
            await send_whatsapp(ADMIN_PHONE, "Erreur : aucun produit reconnu dans Odoo.")
            await send_whatsapp(phone, "Désolé, nous n'avons pas pu traiter votre commande. Nous vous recontactons.")
            return
        order_name, missing = result
        await send_whatsapp(phone, format_client_confirmation(order_name, order_data, missing))
        await send_whatsapp(ADMIN_PHONE, f"Commande {order_name} créée dans Odoo.")
    else:
        await send_whatsapp(ADMIN_PHONE, "Commande ignorée.")
        await send_whatsapp(phone, "Votre commande n'a pas pu être traitée. Nous vous recontactons.")


# ── Texte libre ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
Tu es un assistant qui extrait des commandes WhatsApp.
Réponds UNIQUEMENT en JSON valide, sans texte autour :

{
  "confidence": 0.0,
  "customer_name": "",
  "delivery_address": "",
  "notes": "",
  "items": [
    { "product_name": "", "quantity": 1, "unit": "", "unit_price": null }
  ]
}

- Pas de commande : confidence=0, items=[].
- Incertain : confidence < 0.6.
- Ne complète jamais des infos absentes.
"""

def extract_order(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return json.loads(resp.content[0].text.strip())

async def process_text_order(phone: str, contact: str, text: str):
    try:
        order_data = extract_order(text)
    except json.JSONDecodeError:
        await send_whatsapp(phone, "Une erreur est survenue, veuillez réessayer.")
        return

    if not order_data.get("items") or order_data.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        await send_whatsapp(phone,
            "Je n'ai pas bien compris votre commande.\n"
            "Pouvez-vous préciser les produits et quantités ?\n\n"
            "Exemple : '3 cartons d'eau et 2 packs de jus d'orange'"
        )
        return

    await notify_admin(order_data, phone, contact, source="texte")


# ── Catalogue WhatsApp ─────────────────────────────────────────────────────────

async def process_catalog_order(phone: str, contact: str, order: dict):
    items = [
        {
            "product_name": p.get("product_retailer_id", ""),
            "quantity": p.get("quantity", 1),
            "unit_price": p.get("item_price"),
            "unit": None,
        }
        for p in order.get("product_items", [])
    ]
    order_data = {
        "confidence": 1.0,
        "customer_name": contact,
        "delivery_address": None,
        "notes": "Commande via catalogue WhatsApp",
        "items": items,
    }
    await notify_admin(order_data, phone, contact, source="catalogue")


# ── Notification admin ─────────────────────────────────────────────────────────

async def notify_admin(order_data: dict, phone: str, contact: str, source: str):
    token = str(uuid.uuid4())[:8]
    pending_orders[token] = {"order_data": order_data, "phone": phone, "contact": contact}

    models, uid = odoo_login()
    lines = []
    missing = []
    for item in order_data.get("items", []):
        found = find_product(models, uid, item["product_name"])
        icon  = "✓" if found else "⚠️"
        if not found:
            missing.append(item["product_name"])
        lines.append(f"  {icon} {item['product_name']} × {item.get('quantity', 1)}")

    source_label = "Catalogue" if source == "catalogue" else "Texte libre"
    msg = (
        f"Nouvelle commande ({source_label})\n"
        f"Client : {contact} ({phone})\n\n"
        + "\n".join(lines)
    )
    if order_data.get("delivery_address"):
        msg += f"\nAdresse : {order_data['delivery_address']}"
    if order_data.get("notes"):
        msg += f"\nNote : {order_data['notes']}"
    if missing:
        msg += f"\n\nProduits non trouvés dans Odoo : {', '.join(missing)}"
    msg += "\n\nRépondre OUI pour valider ou NON pour ignorer."

    await send_whatsapp(ADMIN_PHONE, msg)
    await send_whatsapp(phone, "Commande reçue. Nous la traitons et vous confirmons rapidement.")


# ── Odoo ───────────────────────────────────────────────────────────────────────

def odoo_login():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("Authentification Odoo échouée")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid

def find_or_create_customer(models, uid, name: str, phone: str) -> int:
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                            [[["phone", "=", phone]]])
    if ids:
        return ids[0]
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                             [{"name": name or phone, "phone": phone, "customer_rank": 1}])

def find_product(models, uid, name: str):
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
                            [[["name", "ilike", name], ["sale_ok", "=", True]]], {"limit": 1})
    return ids[0] if ids else None

def create_sale_order(order_data: dict, phone: str, contact: str):
    models, uid = odoo_login()
    partner_id  = find_or_create_customer(models, uid,
                                          order_data.get("customer_name") or contact, phone)
    lines, missing = [], []
    for item in order_data.get("items", []):
        pid = find_product(models, uid, item["product_name"])
        if not pid:
            missing.append(item["product_name"])
            continue
        line = {"product_id": pid, "product_uom_qty": item.get("quantity", 1)}
        if item.get("unit_price"):
            line["price_unit"] = item["unit_price"]
        lines.append((0, 0, line))

    if not lines:
        return None

    vals = {
        "partner_id": partner_id,
        "order_line": lines,
        "note": order_data.get("notes") or "",
        "client_order_ref": f"WA-{phone}",
    }
    if order_data.get("delivery_address"):
        vals["note"] = (vals["note"] + f"\nAdresse : {order_data['delivery_address']}").strip()

    oid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [vals])
    rec = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
                            [[oid]], {"fields": ["name"]})[0]
    return rec["name"], missing


# ── Messages ───────────────────────────────────────────────────────────────────

def format_client_confirmation(order_name: str, order_data: dict, missing: list) -> str:
    lines = "\n".join(
        f"  • {it['product_name']} × {it.get('quantity', 1)}"
        for it in order_data.get("items", [])
        if it["product_name"] not in missing
    )
    msg = f"Commande confirmée — {order_name}\n\n{lines}"
    if missing:
        msg += f"\n\nProduits non disponibles : {', '.join(missing)}\nNous vous recontactons."
    return msg

async def send_whatsapp(phone: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    async with httpx.AsyncClient() as client:
        r = await client.post(url,
            json={"messaging_product": "whatsapp", "to": phone,
                  "type": "text", "text": {"body": text}},
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
        )
        log.info("WA → %s : %s", r.status_code, r.text)


# ── Démarrage ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
