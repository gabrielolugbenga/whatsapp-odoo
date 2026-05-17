"""
WhatsApp → Claude → Odoo 19
Phase 1 - Version complète avec:
- Messages en anglais
- Unités affichées correctement
- Mapping produits intelligent
- Correction de commande
- Message client après validation avec prix et paiement
- Gestion messages non-commandes
- Frais de livraison IDF/hors IDF via Odoo GLS
- Adresse de livraison collectée automatiquement
"""

import os
import json
import logging
import xmlrpc.client
import ssl
import uuid
from dotenv import load_dotenv

ssl._create_default_https_context = ssl._create_unverified_context

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

# IDF postal code prefixes
IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")
IDF_FREE_DELIVERY_THRESHOLD = 100.0
IDF_DELIVERY_FEE = 3.0

# GLS shipping: 9€ + 0.65€ * weight_kg
GLS_BASE = 9.0
GLS_PER_KG = 0.65
GLS_MAX_WEIGHT = 30.0

log.info("=== SERVER START ===")
log.info("ADMIN_PHONE: %s", ADMIN_PHONE)

# ── State ──────────────────────────────────────────────────────────────────────

# Pending orders waiting for admin validation
# { token: { "order_data": ..., "phone": ..., "contact": ..., "address": ... } }
pending_orders: dict = {}

# Conversations waiting for address
# { phone: { "order_data": ..., "contact": ... } }
waiting_for_address: dict = {}

# Conversations waiting for payment choice
# { phone: { "order_name": ..., "total": ..., "order_data": ... } }
waiting_for_payment: dict = {}

# Pending corrections
# { token: { "order_data": ..., "phone": ..., "contact": ..., "address": ... } }
pending_corrections: dict = {}

# Waiting for clarification on ambiguous products
# { phone: { "order_data": ..., "contact": ..., "address": ..., "ambiguous": [...], "resolved_items": [...] } }
waiting_for_clarification: dict = {}


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

    try:
        changes = body["entry"][0]["changes"][0]["value"]
        message = changes["messages"][0]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    phone    = message["from"]
    contact  = changes.get("contacts", [{}])[0].get("profile", {}).get("name", phone)
    msg_type = message.get("type")

    log.info("Message from: %s (%s), type: %s", contact, phone, msg_type)

    # ── Admin validation / correction ──────────────────────────────────────────
    if phone == ADMIN_PHONE and msg_type == "text":
        txt = message["text"]["body"].strip()
        txt_upper = txt.upper()

        if txt_upper.startswith("OUI") or txt_upper.startswith("YES"):
            await handle_admin_validation("OUI")
            return {"status": "admin_yes"}

        if txt_upper.startswith("NON") or txt_upper.startswith("NO"):
            await handle_admin_validation("NON")
            return {"status": "admin_no"}

        # Check if it's a correction
        if pending_orders:
            await handle_admin_correction(txt, contact)
            return {"status": "admin_correction"}

    # ── Catalog order ──────────────────────────────────────────────────────────
    if msg_type == "order":
        await process_catalog_order(phone, contact, message["order"])
        return {"status": "catalog"}

    # ── Text message ───────────────────────────────────────────────────────────
    if msg_type == "text":
        text = message["text"]["body"]

        # Check if waiting for address
        if phone in waiting_for_address:
            await handle_address_response(phone, contact, text)
            return {"status": "address"}

        # Check if waiting for clarification on ambiguous products
        if phone in waiting_for_clarification:
            await handle_clarification_response(phone, contact, text)
            return {"status": "clarification"}

        # Check if waiting for payment
        if phone in waiting_for_payment:
            await handle_payment_response(phone, text)
            return {"status": "payment"}

        # Normal message - analyze
        await process_text_message(phone, contact, text)
        return {"status": "text"}

    return {"status": "ignored"}


# ── Message analysis ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an assistant that analyzes WhatsApp messages for a food delivery business.

Respond ONLY with valid JSON, no text around it:

{
  "type": "order",
  "confidence": 0.0,
  "customer_name": "",
  "notes": "",
  "items": [
    {
      "product_name": "",
      "quantity": 1,
      "unit": "",
      "unit_price": null
    }
  ]
}

OR if it's not an order:

{
  "type": "question",
  "message": "original message text"
}

Rules:
- type "order": customer is ordering products. confidence between 0 and 1.
- type "question": customer is asking something, chatting, or the message is unclear.
- Always include unit (kg, g, l, pcs, carton...) when mentioned.
- Never invent information not present in the message.
- If confidence < 0.6, use type "question" instead.
"""

def analyze_message(text: str) -> dict:
    log.info("=== CLAUDE ANALYSIS ===")
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    log.info("Claude response: %s", raw)
    return json.loads(raw)


async def process_text_message(phone: str, contact: str, text: str):
    log.info("=== PROCESS TEXT MESSAGE ===")
    try:
        result = analyze_message(text)
    except Exception as e:
        log.error("Analysis error: %s", e)
        await send_whatsapp(phone, "Sorry, an error occurred. Please try again.")
        return

    msg_type = result.get("type", "question")

    if msg_type == "order" and result.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
        # It's an order — ask for address
        waiting_for_address[phone] = {
            "order_data": result,
            "contact": contact
        }
        await send_whatsapp(phone,
            "Thank you for your order! 🛒\n\n"
            "Please provide your delivery address (including postal code):"
        )
    else:
        # Not an order — forward to admin
        log.info("Not an order, forwarding to admin")
        await send_whatsapp(ADMIN_PHONE,
            f"💬 Message from {contact} ({phone}):\n\n{text}\n\n"
            f"Please reply to them directly on WhatsApp."
        )
        await send_whatsapp(phone,
            "Thank you for your message! Our team will get back to you shortly. 😊"
        )


# ── Address handling ───────────────────────────────────────────────────────────

def is_idf(address: str) -> bool:
    """Detect if address is in Île-de-France based on postal code."""
    import re
    postal_codes = re.findall(r'\b(\d{5})\b', address)
    for pc in postal_codes:
        if pc[:2] in IDF_PREFIXES:
            return True
    return False


def calculate_shipping(address: str, total_amount: float, total_weight_kg: float) -> tuple[float, str]:
    """Returns (shipping_cost, shipping_note)."""
    if is_idf(address):
        if total_amount >= IDF_FREE_DELIVERY_THRESHOLD:
            return IDF_DELIVERY_FEE, f"IDF delivery fee: €{IDF_DELIVERY_FEE:.2f}"
        else:
            needed = IDF_FREE_DELIVERY_THRESHOLD - total_amount
            shipping = GLS_BASE + GLS_PER_KG * total_weight_kg
            note = (
                f"💡 Add €{needed:.2f} more to get free IDF delivery (over €{IDF_FREE_DELIVERY_THRESHOLD:.0f})!\n"
                f"Current delivery fee: €{shipping:.2f}"
            )
            return shipping, note
    else:
        shipping = GLS_BASE + GLS_PER_KG * min(total_weight_kg, GLS_MAX_WEIGHT)
        return shipping, f"GLS delivery: €{shipping:.2f} (9€ + 0.65€/kg)"


async def handle_address_response(phone: str, contact: str, address: str):
    """Handle address provided by customer."""
    log.info("=== HANDLE ADDRESS ===")
    pending = waiting_for_address.pop(phone, None)
    if not pending:
        return

    order_data = pending["order_data"]
    order_data["delivery_address"] = address

    # Check products in Odoo and calculate totals
    models, uid = odoo_login()
    items_info = []
    total_weight = 0.0
    total_amount = 0.0
    missing = []
    ambiguous = []

    for item in order_data.get("items", []):
        product_name = item["product_name"]
        qty = item.get("quantity", 1)
        unit = item.get("unit", "")

        # Search products
        matches = find_products(models, uid, product_name)

        if len(matches) == 0:
            missing.append(f"{product_name} × {qty}{unit}")
        elif len(matches) > 1:
            ambiguous.append({
                "query": product_name,
                "matches": matches,
                "quantity": qty,
                "unit": unit
            })
        else:
            product = matches[0]
            price = product.get("list_price", 0)
            weight = product.get("weight", 0)
            line_total = price * qty
            total_amount += line_total
            total_weight += weight * qty
            items_info.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "quantity": qty,
                "unit": unit,
                "unit_price": price,
                "line_total": line_total,
                "weight": weight
            })

    # Handle ambiguous products — ask one by one
    if ambiguous:
        first_amb = ambiguous[0]
        options = "\n".join([f"  {i+1}. {m['name']} — €{m['list_price']:.2f}"
                             for i, m in enumerate(first_amb["matches"])])
        clarification = (
            f"Which *{first_amb['query']}* did you mean?\n\n"
            f"{options}\n\n"
            f"Reply with the number or the product name."
        )
        await send_whatsapp(phone, clarification)

        # Save state for clarification
        waiting_for_clarification[phone] = {
            "order_data": order_data,
            "contact": contact,
            "address": address,
            "ambiguous": ambiguous,
            "current_amb_index": 0,
            "resolved_items": items_info,
            "missing": missing,
            "total_weight": total_weight,
            "total_amount": total_amount,
        }
        return

    # Calculate shipping
    shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)
    grand_total = total_amount + shipping_cost

    order_data["items_info"] = items_info
    order_data["missing"] = missing
    order_data["total_amount"] = total_amount
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"] = grand_total
    order_data["total_weight"] = total_weight

    await notify_admin(order_data, phone, contact, address, shipping_note)


# ── Clarification handling ─────────────────────────────────────────────────────

async def handle_clarification_response(phone: str, contact: str, text: str):
    """Handle customer response to ambiguous product clarification."""
    log.info("=== HANDLE CLARIFICATION ===")
    state = waiting_for_clarification.get(phone)
    if not state:
        return

    ambiguous = state["ambiguous"]
    idx = state["current_amb_index"]
    current_amb = ambiguous[idx]
    matches = current_amb["matches"]
    qty = current_amb["quantity"]
    unit = current_amb["unit"]

    # Try to match by number or by name
    chosen = None
    text_stripped = text.strip()

    # Check if it's a number
    if text_stripped.isdigit():
        num = int(text_stripped)
        if 1 <= num <= len(matches):
            chosen = matches[num - 1]

    # Check if it matches a product name (flexible)
    if not chosen:
        text_lower = text_stripped.lower()
        for m in matches:
            if text_lower in m["name"].lower() or m["name"].lower() in text_lower:
                chosen = m
                break

    # Still not found — try partial word match
    if not chosen:
        words = text_lower.split()
        for m in matches:
            name_lower = m["name"].lower()
            if any(word in name_lower for word in words if len(word) > 2):
                chosen = m
                break

    if not chosen:
        options = "\n".join([f"  {i+1}. {m['name']} — €{m['list_price']:.2f}"
                             for i, m in enumerate(matches)])
        await send_whatsapp(phone,
            f"Sorry, I didn't understand. Please choose:\n\n{options}\n\n"
            f"Reply with the number or product name."
        )
        return

    # Add chosen product to resolved items
    price = chosen.get("list_price", 0)
    weight = chosen.get("weight", 0)
    line_total = price * qty
    state["resolved_items"].append({
        "product_id": chosen["id"],
        "product_name": chosen["name"],
        "quantity": qty,
        "unit": unit,
        "unit_price": price,
        "line_total": line_total,
        "weight": weight
    })
    state["total_amount"] += line_total
    state["total_weight"] += weight * qty
    state["current_amb_index"] += 1

    # Check if more ambiguous products
    if state["current_amb_index"] < len(ambiguous):
        next_amb = ambiguous[state["current_amb_index"]]
        options = "\n".join([f"  {i+1}. {m['name']} — €{m['list_price']:.2f}"
                             for i, m in enumerate(next_amb["matches"])])
        await send_whatsapp(phone,
            f"Which *{next_amb['query']}* did you mean?\n\n{options}\n\n"
            f"Reply with the number or product name."
        )
        waiting_for_clarification[phone] = state
    else:
        # All resolved — proceed with order
        waiting_for_clarification.pop(phone, None)

        address = state["address"]
        order_data = state["order_data"]
        items_info = state["resolved_items"]
        missing = state["missing"]
        total_amount = state["total_amount"]
        total_weight = state["total_weight"]

        shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)
        grand_total = total_amount + shipping_cost

        order_data["items_info"] = items_info
        order_data["missing"] = missing
        order_data["total_amount"] = total_amount
        order_data["shipping_cost"] = shipping_cost
        order_data["grand_total"] = grand_total
        order_data["total_weight"] = total_weight

        await notify_admin(order_data, phone, contact, address, shipping_note)

async def notify_admin(order_data: dict, phone: str, contact: str, address: str, shipping_note: str = ""):
    log.info("=== NOTIFY ADMIN ===")
    token = str(uuid.uuid4())[:8]
    pending_orders[token] = {
        "order_data": order_data,
        "phone": phone,
        "contact": contact,
        "address": address
    }

    items_info = order_data.get("items_info", [])
    missing = order_data.get("missing", [])

    lines = []
    for item in items_info:
        unit = f" {item['unit']}" if item.get("unit") else ""
        lines.append(
            f"  • {item['product_name']} × {item['quantity']}{unit} — €{item['line_total']:.2f}"
        )

    msg = (
        f"🛒 *New Order* (Text)\n"
        f"Customer: {contact} ({phone})\n"
        f"Address: {address}\n\n"
        + "\n".join(lines)
    )

    if missing:
        msg += f"\n\n⚠️ Products not found: {', '.join(missing)}"

    if shipping_note:
        msg += f"\n\n🚚 {shipping_note}"

    msg += f"\n\n💰 Subtotal: €{order_data.get('total_amount', 0):.2f}"
    msg += f"\n🚚 Delivery: €{order_data.get('shipping_cost', 0):.2f}"
    msg += f"\n💳 *Total: €{order_data.get('grand_total', 0):.2f}*"
    msg += "\n\nReply *OUI* to confirm, *NON* to cancel, or send a correction."

    log.info("Sending admin notification to: %s", ADMIN_PHONE)
    await send_whatsapp(ADMIN_PHONE, msg)
    await send_whatsapp(phone,
        "Your order has been received! ✅\n"
        "We are processing it and will confirm shortly."
    )


# ── Admin validation ───────────────────────────────────────────────────────────

async def handle_admin_validation(decision: str):
    log.info("=== ADMIN VALIDATION: %s ===", decision)
    if not pending_orders:
        await send_whatsapp(ADMIN_PHONE, "No pending orders.")
        return

    token = next(iter(pending_orders))
    pending = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone = pending["phone"]
    contact = pending["contact"]
    address = pending["address"]

    if decision == "OUI":
        try:
            result = create_sale_order(order_data, phone, contact, address)
            if result is None:
                await send_whatsapp(ADMIN_PHONE, "❌ Error: no products found in Odoo.")
                await send_whatsapp(phone, "Sorry, we could not process your order. We will contact you shortly.")
                return

            order_name, missing = result
            grand_total = order_data.get("grand_total", 0)

            await send_whatsapp(ADMIN_PHONE, f"✅ Order {order_name} created in Odoo.")

            # Ask customer for payment
            waiting_for_payment[phone] = {
                "order_name": order_name,
                "total": grand_total,
                "order_data": order_data
            }

            items_info = order_data.get("items_info", [])
            items_txt = "\n".join(
                f"  • {it['product_name']} × {it['quantity']}{' ' + it['unit'] if it.get('unit') else ''} — €{it['line_total']:.2f}"
                for it in items_info
            )

            await send_whatsapp(phone,
                f"✅ *Order Confirmed — {order_name}*\n\n"
                f"{items_txt}\n\n"
                f"🚚 Delivery: €{order_data.get('shipping_cost', 0):.2f}\n"
                f"💳 *Total: €{grand_total:.2f}*\n\n"
                f"How would you like to pay?\n"
                f"1️⃣ Card on delivery\n"
                f"2️⃣ Cash on delivery\n"
                f"3️⃣ Payment link (now)\n\n"
                f"Reply with 1, 2 or 3."
            )

        except Exception as e:
            log.error("Order creation error: %s", e)
            await send_whatsapp(ADMIN_PHONE, f"❌ Odoo error: {str(e)}")

    else:
        await send_whatsapp(ADMIN_PHONE, "Order cancelled.")
        await send_whatsapp(phone, "Your order has been cancelled. Feel free to place a new order anytime! 😊")


async def handle_admin_correction(correction_text: str, contact: str):
    """Admin sends a correction — re-analyze and send new recap."""
    log.info("=== ADMIN CORRECTION ===")
    if not pending_orders:
        return

    token = next(iter(pending_orders))
    pending = pending_orders[token]
    phone = pending["phone"]
    address = pending["address"]

    # Re-analyze the correction
    try:
        result = analyze_message(correction_text)
        if result.get("type") != "order":
            await send_whatsapp(ADMIN_PHONE, "Could not understand the correction. Please try again or reply OUI/NON.")
            return

        result["delivery_address"] = address
        pending_orders.pop(token)

        # Keep already confirmed items, only replace missing ones
        existing_order = pending["order_data"]
        confirmed_items = existing_order.get("items_info", [])
        confirmed_amount = sum(i["line_total"] for i in confirmed_items)
        confirmed_weight = sum(i.get("weight", 0) * i["quantity"] for i in confirmed_items)

        # Search only the corrected/new items
        models, uid = odoo_login()
        new_items_info = list(confirmed_items)  # start with confirmed items
        total_weight = confirmed_weight
        total_amount = confirmed_amount
        missing = []

        for item in result.get("items", []):
            matches = find_products(models, uid, item["product_name"])
            if len(matches) == 0:
                missing.append(item["product_name"])
            else:
                product = matches[0]
                qty = item.get("quantity", 1)
                unit = item.get("unit", "")
                price = product.get("list_price", 0)
                weight = product.get("weight", 0)
                line_total = price * qty
                total_amount += line_total
                total_weight += weight * qty
                new_items_info.append({
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "quantity": qty,
                    "unit": unit,
                    "unit_price": price,
                    "line_total": line_total,
                    "weight": weight
                })

        shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)
        grand_total = total_amount + shipping_cost

        result["items_info"] = new_items_info
        result["missing"] = missing
        result["total_amount"] = total_amount
        result["shipping_cost"] = shipping_cost
        result["grand_total"] = grand_total
        result["total_weight"] = total_weight

        await notify_admin(result, phone, pending["contact"], address, shipping_note)

    except Exception as e:
        log.error("Correction error: %s", e)
        await send_whatsapp(ADMIN_PHONE, f"Error processing correction: {str(e)}")


# ── Payment handling ───────────────────────────────────────────────────────────

async def handle_payment_response(phone: str, text: str):
    """Handle payment choice from customer."""
    pending = waiting_for_payment.pop(phone, None)
    if not pending:
        return

    order_name = pending["order_name"]
    total = pending["total"]
    choice = text.strip()

    if choice == "1":
        await send_whatsapp(phone,
            f"✅ Great! Your order *{order_name}* will be paid by *card on delivery*.\n"
            f"Total to pay: €{total:.2f}\n\n"
            f"Thank you for ordering with us! 🙏"
        )
        await send_whatsapp(ADMIN_PHONE,
            f"💳 {order_name}: Customer chose *Card on delivery* (€{total:.2f})"
        )

    elif choice == "2":
        await send_whatsapp(phone,
            f"✅ Great! Your order *{order_name}* will be paid by *cash on delivery*.\n"
            f"Total to pay: €{total:.2f}\n\n"
            f"Thank you for ordering with us! 🙏"
        )
        await send_whatsapp(ADMIN_PHONE,
            f"💵 {order_name}: Customer chose *Cash on delivery* (€{total:.2f})"
        )

    elif choice == "3":
        await send_whatsapp(phone,
            f"✅ Payment link requested for *{order_name}* (€{total:.2f}).\n"
            f"We will send you the payment link shortly! ⏳"
        )
        await send_whatsapp(ADMIN_PHONE,
            f"🔗 {order_name}: Customer wants *Payment link* (€{total:.2f})\n"
            f"Please send them a payment link on WhatsApp."
        )
    else:
        await send_whatsapp(phone,
            "Please reply with:\n1️⃣ for Card on delivery\n2️⃣ for Cash on delivery\n3️⃣ for Payment link"
        )
        waiting_for_payment[phone] = pending


# ── Catalog order ──────────────────────────────────────────────────────────────

async def process_catalog_order(phone: str, contact: str, order: dict):
    items = [
        {
            "product_name": p.get("product_retailer_id", ""),
            "quantity": p.get("quantity", 1),
            "unit_price": p.get("item_price"),
            "unit": "",
        }
        for p in order.get("product_items", [])
    ]
    order_data = {
        "type": "order",
        "confidence": 1.0,
        "customer_name": contact,
        "notes": "Order via WhatsApp catalog",
        "items": items,
    }
    waiting_for_address[phone] = {"order_data": order_data, "contact": contact}
    await send_whatsapp(phone,
        "Thank you for your order! 🛒\n\n"
        "Please provide your delivery address (including postal code):"
    )


# ── Odoo ───────────────────────────────────────────────────────────────────────

def odoo_login():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError(f"Odoo authentication failed for {ODOO_USER}")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def find_products(models, uid, name: str) -> list:
    """Search products by name with flexible word matching."""
    def search(domain):
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search",
                                [domain], {"limit": 5})
        if not ids:
            return []
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [ids], {"fields": ["id", "name", "list_price", "weight"]})

    # 1. Try exact phrase first
    results = search([["name", "ilike", name], ["sale_ok", "=", True]])
    if results:
        return results

    # 2. Try each word individually and intersect
    words = [w for w in name.split() if len(w) > 2]
    if not words:
        return []

    all_ids = None
    for word in words:
        ids = set(p["id"] for p in search([["name", "ilike", word], ["sale_ok", "=", True]]))
        if all_ids is None:
            all_ids = ids
        else:
            all_ids = all_ids & ids

    if all_ids:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [list(all_ids)], {"fields": ["id", "name", "list_price", "weight"]})

    # 3. Try any word match (union)
    all_ids = set()
    for word in words:
        ids = set(p["id"] for p in search([["name", "ilike", word], ["sale_ok", "=", True]]))
        all_ids = all_ids | ids

    if all_ids:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [list(all_ids)[:5]], {"fields": ["id", "name", "list_price", "weight"]})

    return []


def find_or_create_customer(models, uid, name: str, phone: str) -> int:
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                            [[["phone", "=", phone]]])
    if ids:
        return ids[0]
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                             [{"name": name or phone, "phone": phone, "customer_rank": 1}])


def create_sale_order(order_data: dict, phone: str, contact: str, address: str):
    models, uid = odoo_login()
    partner_id = find_or_create_customer(
        models, uid, order_data.get("customer_name") or contact, phone
    )

    items_info = order_data.get("items_info", [])
    if not items_info:
        return None

    lines = []
    missing = []

    for item in items_info:
        # Get product.product id from product.template id
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["product_tmpl_id", "=", item["product_id"]], ["active", "=", True]]],
            {"limit": 1}
        )
        if not product_ids:
            missing.append(item["product_name"])
            continue

        line = {
            "product_id": product_ids[0],
            "product_uom_qty": item.get("quantity", 1),
            "price_unit": item.get("unit_price", 0),
        }
        lines.append((0, 0, line))

    if not lines:
        return None

    note = order_data.get("notes") or ""
    if address:
        note = (note + f"\nDelivery address: {address}").strip()

    # Add shipping line if needed
    shipping_cost = order_data.get("shipping_cost", 0)
    if shipping_cost > 0:
        # Find delivery product
        delivery_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["name", "ilike", "Livraison"], ["sale_ok", "=", True]]],
            {"limit": 1}
        )
        if delivery_ids:
            lines.append((0, 0, {
                "product_id": delivery_ids[0],
                "product_uom_qty": 1,
                "price_unit": shipping_cost,
                "name": "Delivery fee",
            }))

    vals = {
        "partner_id": partner_id,
        "order_line": lines,
        "note": note,
        "client_order_ref": f"WA-{phone}",
    }

    oid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [vals])
    rec = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
                            [[oid]], {"fields": ["name"]})[0]
    log.info("Order created: %s", rec["name"])
    return rec["name"], missing


# ── WhatsApp ───────────────────────────────────────────────────────────────────

async def send_whatsapp(phone: str, text: str):
    log.info("Sending WA to %s", phone)
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    async with httpx.AsyncClient() as client:
        r = await client.post(url,
            json={"messaging_product": "whatsapp", "to": phone,
                  "type": "text", "text": {"body": text}},
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
        )
        log.info("WA → %s : %s", r.status_code, r.text)


# ── Start ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
