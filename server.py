"""
WhatsApp → Claude → Odoo 19
Phase 1 - Version complète avec prix TTC et process complet
"""

import os
import json
import logging
import xmlrpc.client
import ssl
import uuid
import re
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

IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")
IDF_FREE_DELIVERY_THRESHOLD = 100.0
IDF_DELIVERY_FEE = 3.0
GLS_BASE = 9.0
GLS_PER_KG = 0.65
GLS_MAX_WEIGHT = 30.0

log.info("=== SERVER START === ADMIN: %s", ADMIN_PHONE)

# ── State ──────────────────────────────────────────────────────────────────────

pending_orders: dict = {}
waiting_for_address: dict = {}
waiting_for_payment: dict = {}
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
    log.info("From: %s (%s) type: %s", contact, phone, msg_type)

    if phone == ADMIN_PHONE and msg_type == "text":
        txt = message["text"]["body"].strip()
        txt_up = txt.upper()
        if txt_up.startswith("OUI") or txt_up.startswith("YES"):
            await handle_admin_validation("OUI")
            return {"status": "admin_yes"}
        if txt_up.startswith("NON") or txt_up.startswith("NO"):
            await handle_admin_validation("NON")
            return {"status": "admin_no"}
        if pending_orders:
            await handle_admin_correction(txt)
            return {"status": "admin_correction"}

    if msg_type == "order":
        await process_catalog_order(phone, contact, message["order"])
        return {"status": "catalog"}

    if msg_type == "text":
        text = message["text"]["body"]
        if phone in waiting_for_address:
            # Handle YES confirmation of saved address
            if text.strip().upper() in ("YES", "OUI", "Y"):
                saved = get_saved_address(phone)
                if saved:
                    await handle_address_response(phone, contact, saved)
                    return {"status": "address_confirmed"}
            await handle_address_response(phone, contact, text)
            return {"status": "address"}
        if phone in waiting_for_clarification:
            await handle_clarification_response(phone, contact, text)
            return {"status": "clarification"}
        if phone in waiting_for_payment:
            await handle_payment_response(phone, text)
            return {"status": "payment"}
        await process_text_message(phone, contact, text)
        return {"status": "text"}

    return {"status": "ignored"}


# ── Prompts ────────────────────────────────────────────────────────────────────

ORDER_PROMPT = """
You are an assistant for an African food delivery business.
Analyze the WhatsApp message and respond ONLY with valid JSON, no markdown.

If it's an order:
{
  "type": "order",
  "confidence": 0.9,
  "notes": "",
  "items": [
    {
      "product_name": "pounded yam",
      "size": "5kg",
      "bags": 1
    }
  ]
}

If it's NOT an order (question, greeting, chat):
{
  "type": "question",
  "message": "original text"
}

Rules:
- Extract product name WITHOUT size/weight (put size separately)
- "5kg pounded yam" → product_name: "pounded yam", size: "5kg", bags: 1
- "2 bags of rice 10kg" → product_name: "rice", size: "10kg", bags: 2
- "some rice" → product_name: "rice", size: null, bags: 1
- If confidence < 0.65, use type "question"
- Never invent information
"""

CORRECTION_PROMPT = """
You are an assistant that parses order corrections from a store operator.
Respond ONLY with valid JSON, no markdown:

{
  "actions": [
    {
      "type": "add",
      "product_name": "poundo yam eagle",
      "size": "10kg",
      "bags": 1
    }
  ]
}

Action types:
- "replace": "X instead of Y", "remplacer X par Y", "X à la place de Y"
  → { "type": "replace", "old_product": "Y", "new_product_name": "X", "size": "...", "bags": 1 }
- "remove": "remove X", "enlever X", "supprimer X"
  → { "type": "remove", "product_name": "X" }
- "change_qty": "2 bags of X instead of 1", "changer quantité X à 2"
  → { "type": "change_qty", "product_name": "X", "bags": 2 }
- "add": anything else (adding missing product)
  → { "type": "add", "product_name": "X", "size": "...", "bags": 1 }

Always extract size separately from product name.
"""


def analyze_message(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ORDER_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    log.info("Claude order: %s", raw)
    return json.loads(raw)


def analyze_correction(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CORRECTION_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    log.info("Claude correction: %s", raw)
    return json.loads(raw)


async def process_text_message(phone: str, contact: str, text: str):
    try:
        result = analyze_message(text)
    except Exception as e:
        log.error("Analysis error: %s", e)
        await send_whatsapp(phone, "Sorry, an error occurred. Please try again.")
        return

    if result.get("type") == "order" and result.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
        waiting_for_address[phone] = {"order_data": result, "contact": contact}
        await send_whatsapp(phone,
            "Thank you for your order! 🛒\n\n"
            "Please provide your delivery address (including postal code):"
        )
    else:
        await send_whatsapp(ADMIN_PHONE,
            f"💬 Message from {contact} ({phone}):\n\n{text}\n\n"
            "Please reply to them directly on WhatsApp."
        )
        await send_whatsapp(phone,
            "Thank you for your message! Our team will get back to you shortly. 😊"
        )


# ── Address & shipping ─────────────────────────────────────────────────────────

def is_idf(address: str) -> bool:
    for pc in re.findall(r'\b(\d{5})\b', address):
        if pc[:2] in IDF_PREFIXES:
            return True
    return False


def calculate_shipping(address: str, total_amount: float, total_weight_kg: float) -> tuple:
    if is_idf(address):
        if total_amount >= IDF_FREE_DELIVERY_THRESHOLD:
            return IDF_DELIVERY_FEE, f"IDF delivery fee: €{IDF_DELIVERY_FEE:.2f}"
        else:
            needed = IDF_FREE_DELIVERY_THRESHOLD - total_amount
            shipping = GLS_BASE + GLS_PER_KG * total_weight_kg
            return shipping, (
                f"💡 Add €{needed:.2f} more for free IDF delivery (over €{IDF_FREE_DELIVERY_THRESHOLD:.0f})!\n"
                f"Current delivery fee: €{shipping:.2f}"
            )
    else:
        shipping = GLS_BASE + GLS_PER_KG * min(total_weight_kg, GLS_MAX_WEIGHT)
        return shipping, f"GLS delivery: €{shipping:.2f} (€9 + €0.65/kg)"


def get_price_ttc(models, uid, product_id: int, price_ht: float) -> float:
    """Calculate TTC price by fetching tax rate from Odoo."""
    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
            [[product_id]], {"fields": ["taxes_id"]}
        )
        if not products:
            return price_ht
        tax_ids = products[0].get("taxes_id", [])
        if not tax_ids:
            return price_ht
        taxes = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "read",
            [tax_ids], {"fields": ["amount", "amount_type"]}
        )
        total_tax_rate = 0.0
        for tax in taxes:
            if tax.get("amount_type") == "percent":
                total_tax_rate += tax.get("amount", 0) / 100.0
        return round(price_ht * (1 + total_tax_rate), 2)
    except Exception as e:
        log.warning("Could not get TTC price: %s", e)
        return price_ht


async def handle_address_response(phone: str, contact: str, address: str):
    pending = waiting_for_address.pop(phone, None)
    if not pending:
        return
    order_data = pending["order_data"]
    order_data["delivery_address"] = address
    await resolve_items(phone, contact, address, order_data, [], [], [], 0.0, 0.0)


def clarification_message(query: str, size: str, matches: list) -> str:
    options = "\n".join(
        f"  {i+1}. {m['name']} — €{m['price_ttc']:.2f}"
        for i, m in enumerate(matches)
    )
    size_hint = f" ({size})" if size else ""
    return (
        f"Which *{query}{size_hint}* did you mean?\n\n"
        f"{options}\n"
        f"  0. None of the above — we'll get in touch to confirm\n\n"
        f"Reply with the number or product name."
    )


async def resolve_items(phone, contact, address, order_data, resolved_items, unresolved, missing, total_amount, total_weight):
    """Search Odoo for all items. Handle ambiguous and missing ones."""
    models, uid = odoo_login()
    ambiguous = []

    for item in order_data.get("items", []):
        product_name = item.get("product_name", "")
        size = item.get("size")
        bags = item.get("bags", 1)
        # Always search by product name only, then filter by size
        matches = find_products(models, uid, product_name)
        # Enrich with TTC price before size filtering
        for m in matches:
            m["price_ttc"] = get_price_ttc(models, uid, m["id"], m["list_price"])
        # If size specified, filter matches by size
        if size and matches:
            size_clean = size.lower().replace(" ", "")
            size_filtered = [m for m in matches if size_clean in m["name"].lower()]
            if size_filtered:
                matches = size_filtered

        # Enrich with TTC price
        for m in matches:
            m["price_ttc"] = get_price_ttc(models, uid, m["id"], m["list_price"])

        if not matches:
            # Not found — add to unresolved, process will continue
            unresolved.append({
                "product_name": f"{product_name} {size or ''}".strip(),
                "quantity": bags,
                "original_query": query,
            })
        elif len(matches) == 1:
            # Check if size matches — if not, ask for clarification
            p = matches[0]
            if size and size.lower().replace(" ", "") not in p["name"].lower():
                # Size doesn't match — show as ambiguous so admin can confirm
                ambiguous.append({
                    "query": product_name, "size": size,
                    "bags": bags, "matches": matches,
                })
            else:
                price_ttc = p["price_ttc"]
                line_total = price_ttc * bags
                total_amount += line_total
                total_weight += p.get("weight", 0) * bags
                resolved_items.append({
                    "product_id": p["id"],
                    "product_name": p["name"],
                    "quantity": bags,
                    "unit_price": price_ttc,
                    "line_total": line_total,
                    "weight": p.get("weight", 0),
                })
        else:
            # Multiple matches — if size specified, filter by size first
            if size:
                size_clean = size.lower().replace(" ", "")
                size_matches = [m for m in matches if size_clean in m["name"].lower()]
                if len(size_matches) == 1:
                    p = size_matches[0]
                    price_ttc = p["price_ttc"]
                    line_total = price_ttc * bags
                    total_amount += line_total
                    total_weight += p.get("weight", 0) * bags
                    resolved_items.append({
                        "product_id": p["id"],
                        "product_name": p["name"],
                        "quantity": bags,
                        "unit_price": price_ttc,
                        "line_total": line_total,
                        "weight": p.get("weight", 0),
                    })
                else:
                    # Still ambiguous even with size filter
                    ambiguous.append({
                        "query": product_name, "size": size,
                        "bags": bags, "matches": size_matches if size_matches else matches,
                    })
            else:
                ambiguous.append({
                    "query": product_name, "size": size,
                    "bags": bags, "matches": matches,
                })

    if ambiguous:
        first = ambiguous[0]
        await send_whatsapp(phone, clarification_message(
            first["query"], first.get("size"), first["matches"]
        ))
        waiting_for_clarification[phone] = {
            "order_data": order_data, "contact": contact, "address": address,
            "ambiguous": ambiguous, "current_idx": 0,
            "resolved_items": resolved_items, "unresolved": unresolved,
            "missing": missing, "total_amount": total_amount, "total_weight": total_weight,
        }
    else:
        # All done — notify admin
        order_data["items_info"] = resolved_items
        order_data["unresolved"] = unresolved
        order_data["missing"] = missing
        shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)
        order_data["total_amount"] = total_amount
        order_data["shipping_cost"] = shipping_cost
        order_data["grand_total"] = total_amount + shipping_cost
        order_data["total_weight"] = total_weight
        await notify_admin(order_data, phone, contact, address, shipping_note)


# ── Clarification ──────────────────────────────────────────────────────────────

async def handle_clarification_response(phone: str, contact: str, text: str):
    state = waiting_for_clarification.get(phone)
    if not state:
        return

    ambiguous = state["ambiguous"]
    idx = state["current_idx"]
    current = ambiguous[idx]
    matches = current["matches"]
    bags = current["bags"]
    t = text.strip()

    # Customer chose "none of the above"
    if t == "0":
        state["unresolved"].append({
            "product_name": f"{current['query']} {current.get('size') or ''}".strip(),
            "quantity": bags,
            "original_query": current["query"],
        })
        state["current_idx"] += 1
        await _next_clarification_or_finish(phone, contact, state)
        return

    # Try to match choice
    chosen = None
    if t.isdigit():
        n = int(t)
        if 1 <= n <= len(matches):
            chosen = matches[n - 1]
    if not chosen:
        t_lower = t.lower()
        for m in matches:
            if t_lower in m["name"].lower() or m["name"].lower() in t_lower:
                chosen = m
                break
    if not chosen:
        words = [w for w in t.lower().split() if len(w) > 2]
        for m in matches:
            if any(w in m["name"].lower() for w in words):
                chosen = m
                break

    if not chosen:
        await send_whatsapp(phone,
            clarification_message(current["query"], current.get("size"), matches)
        )
        return

    # Add chosen product
    price_ttc = chosen.get("price_ttc", chosen["list_price"])
    line_total = price_ttc * bags
    state["resolved_items"].append({
        "product_id": chosen["id"], "product_name": chosen["name"],
        "quantity": bags, "unit_price": price_ttc,
        "line_total": line_total, "weight": chosen.get("weight", 0),
    })
    state["total_amount"] += line_total
    state["total_weight"] += chosen.get("weight", 0) * bags
    state["current_idx"] += 1
    await _next_clarification_or_finish(phone, contact, state)


async def _next_clarification_or_finish(phone, contact, state):
    ambiguous = state["ambiguous"]
    if state["current_idx"] < len(ambiguous):
        nxt = ambiguous[state["current_idx"]]
        waiting_for_clarification[phone] = state
        await send_whatsapp(phone, clarification_message(
            nxt["query"], nxt.get("size"), nxt["matches"]
        ))
    else:
        waiting_for_clarification.pop(phone, None)
        order_data = state["order_data"]
        address = state["address"]
        total_amount = state["total_amount"]
        total_weight = state["total_weight"]
        order_data["items_info"] = state["resolved_items"]
        order_data["unresolved"] = state["unresolved"]
        order_data["missing"] = state["missing"]
        shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)
        order_data["total_amount"] = total_amount
        order_data["shipping_cost"] = shipping_cost
        order_data["grand_total"] = total_amount + shipping_cost
        order_data["total_weight"] = total_weight
        await notify_admin(order_data, phone, contact, address, shipping_note)


# ── Admin notification ─────────────────────────────────────────────────────────

async def notify_admin(order_data, phone, contact, address, shipping_note=""):
    token = str(uuid.uuid4())[:8]
    pending_orders[token] = {
        "order_data": order_data, "phone": phone,
        "contact": contact, "address": address
    }
    items_info = order_data.get("items_info", [])
    unresolved = order_data.get("unresolved", [])

    lines = "\n".join(
        f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
        for it in items_info
    )
    msg = (
        f"🛒 *New Order*\n"
        f"Customer: {contact} ({phone})\n"
        f"Address: {address}\n\n{lines}"
    )
    if unresolved:
        unresolved_txt = "\n".join(
            f"  ❓ {u['product_name']} × {u['quantity']} — to confirm"
            for u in unresolved
        )
        msg += f"\n\n⚠️ *Items to confirm:*\n{unresolved_txt}"

    if shipping_note:
        msg += f"\n\n🚚 {shipping_note}"

    msg += f"\n\n💰 Subtotal: €{order_data.get('total_amount', 0):.2f}"
    msg += f"\n🚚 Delivery: €{order_data.get('shipping_cost', 0):.2f}"
    msg += f"\n💳 *Total: €{order_data.get('grand_total', 0):.2f}*"
    msg += "\n\nReply *OUI* to confirm, *NON* to cancel, or send a correction."

    await send_whatsapp(ADMIN_PHONE, msg)
    await send_whatsapp(phone,
        "Your order has been received! ✅\n"
        "We are processing it and will confirm shortly."
    )


# ── Admin validation ───────────────────────────────────────────────────────────

async def handle_admin_validation(decision: str):
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
                await send_whatsapp(ADMIN_PHONE, "❌ No products found in Odoo.")
                await send_whatsapp(phone, "Sorry, we could not process your order. We will contact you shortly.")
                return
            order_name, _ = result
            grand_total = order_data.get("grand_total", 0)
            items_info = order_data.get("items_info", [])
            await send_whatsapp(ADMIN_PHONE, f"✅ Order {order_name} created in Odoo.")
            items_txt = "\n".join(
                f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
                for it in items_info
            )
            waiting_for_payment[phone] = {
                "order_name": order_name, "total": grand_total, "order_data": order_data
            }
            await send_whatsapp(phone,
                f"✅ *Order Confirmed — {order_name}*\n\n{items_txt}\n\n"
                f"🚚 Delivery: €{order_data.get('shipping_cost', 0):.2f}\n"
                f"💳 *Total: €{grand_total:.2f}*\n\n"
                "How would you like to pay?\n"
                "1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link (now)\n\n"
                "Reply with 1, 2 or 3."
            )
        except Exception as e:
            log.error("Order error: %s", e)
            await send_whatsapp(ADMIN_PHONE, f"❌ Odoo error: {str(e)}")
    else:
        await send_whatsapp(ADMIN_PHONE, "Order cancelled.")
        await send_whatsapp(phone, "Your order has been cancelled. Feel free to place a new order anytime! 😊")


# ── Admin correction ───────────────────────────────────────────────────────────

async def handle_admin_correction(correction_text: str):
    if not pending_orders:
        return

    token = next(iter(pending_orders))
    pending = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone = pending["phone"]
    contact = pending["contact"]
    address = pending["address"]

    try:
        correction = analyze_correction(correction_text)
        actions = correction.get("actions", [])
    except Exception as e:
        log.error("Correction parse error: %s", e)
        await send_whatsapp(ADMIN_PHONE,
            "Could not understand the correction. Please try again or reply OUI/NON."
        )
        pending_orders[token] = pending
        return

    models, uid = odoo_login()
    items_info = list(order_data.get("items_info", []))

    for action in actions:
        action_type = action.get("type")

        if action_type == "remove":
            target = action.get("product_name", "").lower()
            items_info = [it for it in items_info if target not in it["product_name"].lower()]

        elif action_type == "change_qty":
            target = action.get("product_name", "").lower()
            new_qty = action.get("bags", 1)
            for it in items_info:
                if target in it["product_name"].lower():
                    it["quantity"] = new_qty
                    it["line_total"] = it["unit_price"] * new_qty
                    break

        elif action_type in ("add", "replace"):
            product_name = action.get("product_name") or action.get("new_product_name", "")
            size = action.get("size")
            bags = action.get("bags", 1)
            query = f"{product_name} {size}" if size else product_name
            matches = find_products(models, uid, query)
            if not matches:
                matches = find_products(models, uid, product_name)

            if matches:
                p = matches[0]
                price_ttc = get_price_ttc(models, uid, p["id"], p["list_price"])
                new_item = {
                    "product_id": p["id"], "product_name": p["name"],
                    "quantity": bags, "unit_price": price_ttc,
                    "line_total": price_ttc * bags, "weight": p.get("weight", 0),
                }
                if action_type == "replace":
                    old_target = action.get("old_product", "").lower()
                    if old_target:
                        items_info = [it for it in items_info if old_target not in it["product_name"].lower()]
                items_info.append(new_item)
            else:
                await send_whatsapp(ADMIN_PHONE,
                    f"⚠️ Product not found: *{product_name} {size or ''}*\nPlease try a different name."
                )
                pending_orders[token] = pending
                return

    # Recalculate totals
    total_amount = sum(it["line_total"] for it in items_info)
    total_weight = sum(it.get("weight", 0) * it["quantity"] for it in items_info)
    shipping_cost, shipping_note = calculate_shipping(address, total_amount, total_weight)

    order_data["items_info"] = items_info
    order_data["unresolved"] = []
    order_data["total_amount"] = total_amount
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"] = total_amount + shipping_cost
    order_data["total_weight"] = total_weight

    await notify_admin(order_data, phone, contact, address, shipping_note)


# ── Payment ────────────────────────────────────────────────────────────────────

async def handle_payment_response(phone: str, text: str):
    pending = waiting_for_payment.pop(phone, None)
    if not pending:
        return
    order_name = pending["order_name"]
    total = pending["total"]
    choice = text.strip()

    if choice == "1":
        await send_whatsapp(phone, f"✅ *Card on delivery* for {order_name}.\nTotal: €{total:.2f}\n\nThank you! 🙏")
        await send_whatsapp(ADMIN_PHONE, f"💳 {order_name}: *Card on delivery* — €{total:.2f}")
    elif choice == "2":
        await send_whatsapp(phone, f"✅ *Cash on delivery* for {order_name}.\nTotal: €{total:.2f}\n\nThank you! 🙏")
        await send_whatsapp(ADMIN_PHONE, f"💵 {order_name}: *Cash on delivery* — €{total:.2f}")
    elif choice == "3":
        await send_whatsapp(phone, f"✅ Payment link requested for {order_name} (€{total:.2f}).\nWe will send it shortly! ⏳")
        await send_whatsapp(ADMIN_PHONE, f"🔗 {order_name}: *Payment link* — €{total:.2f}\nPlease send the payment link.")
    else:
        await send_whatsapp(phone, "Please reply with:\n1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link")
        waiting_for_payment[phone] = pending


# ── Catalog ────────────────────────────────────────────────────────────────────

async def process_catalog_order(phone: str, contact: str, order: dict):
    items = [
        {"product_name": p.get("product_retailer_id", ""), "size": None, "bags": p.get("quantity", 1)}
        for p in order.get("product_items", [])
    ]
    order_data = {"type": "order", "confidence": 1.0, "customer_name": contact, "items": items}
    waiting_for_address[phone] = {"order_data": order_data, "contact": contact}
    await send_whatsapp(phone,
        "Thank you for your order! 🛒\n\nPlease provide your delivery address (including postal code):"
    )


# ── Odoo ───────────────────────────────────────────────────────────────────────

def get_saved_address(phone: str) -> str | None:
    """Look up saved delivery address for a customer by phone."""
    try:
        models, uid = odoo_login()
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search',
                                [[['phone', '=', phone]]])
        if not ids:
            return None
        partner = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'read',
                                    [ids], {'fields': ['street']})[0]
        return partner.get('street') or None
    except Exception as e:
        log.warning('Could not get saved address: %s', e)
        return None


def odoo_login():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("Odoo auth failed")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def find_products(models, uid, name: str) -> list:
    def search(domain):
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search",
                                [domain], {"limit": 20})
        if not ids:
            return []
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [ids], {"fields": ["id", "name", "list_price", "weight", "taxes_id"]})

    results = search([["name", "ilike", name], ["sale_ok", "=", True], ["is_published", "=", True]])
    if results:
        return results

    words = [w for w in name.split() if len(w) > 2 and not re.match(r"^[0-9]+[kgKGlLmM]+$", w)]
    if not words:
        return []

    all_ids = None
    for word in words:
        ids = {p["id"] for p in search([["name", "ilike", word], ["sale_ok", "=", True], ["is_published", "=", True]])}
        all_ids = ids if all_ids is None else all_ids & ids
    if all_ids:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [list(all_ids)], {"fields": ["id", "name", "list_price", "weight", "taxes_id"]})

    all_ids = set()
    for word in words:
        all_ids |= {p["id"] for p in search([["name", "ilike", word], ["sale_ok", "=", True], ["is_published", "=", True]])}
    if all_ids:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [list(all_ids)[:5]], {"fields": ["id", "name", "list_price", "weight", "taxes_id"]})
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
    partner_id = find_or_create_customer(models, uid, order_data.get("customer_name") or contact, phone)
    items_info = order_data.get("items_info", [])
    if not items_info:
        return None

    lines = []
    missing = []
    for item in items_info:
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["product_tmpl_id", "=", item["product_id"]], ["active", "=", True]]], {"limit": 1}
        )
        if not product_ids:
            missing.append(item["product_name"])
            continue
        lines.append((0, 0, {
            "product_id": product_ids[0],
            "product_uom_qty": item["quantity"],
            "price_unit": item["unit_price"],
        }))

    if not lines:
        return None

    shipping_cost = order_data.get("shipping_cost", 0)
    if shipping_cost > 0:
        delivery_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["name", "ilike", "Livraison"], ["sale_ok", "=", True], ["is_published", "=", True]]], {"limit": 1}
        )
        if delivery_ids:
            lines.append((0, 0, {
                "product_id": delivery_ids[0],
                "product_uom_qty": 1, "price_unit": shipping_cost, "name": "Delivery fee",
            }))

    # Save delivery address on partner
    if address:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "write",
                          [[partner_id], {"street": address}])

    oid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [{
        "partner_id": partner_id, "order_line": lines,
        "note": f"Delivery: {address}" if address else "",
        "client_order_ref": f"WA-{phone}",
    }])
    rec = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
                            [[oid]], {"fields": ["name"]})[0]
    return rec["name"], missing


# ── WhatsApp ───────────────────────────────────────────────────────────────────

async def send_whatsapp(phone: str, text: str):
    log.info("WA → %s", phone)
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    async with httpx.AsyncClient() as client:
        r = await client.post(url,
            json={"messaging_product": "whatsapp", "to": phone,
                  "type": "text", "text": {"body": text}},
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
        )
        log.info("WA %s → %s", r.status_code, phone)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
