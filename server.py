"""
WhatsApp → Claude → Odoo 19
Bot interactif avec priorité catalogue
- Client : bot aide à finaliser, client confirme avec CONFIRM
- Staff : CMD avec validation OUI/NON
"""

import os
import json
import logging
import xmlrpc.client
import ssl
import uuid
import re
import asyncio
from dotenv import load_dotenv
from datetime import datetime, date

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

# WhatsApp catalogue link — update with real link once catalogue is set up
CATALOGUE_LINK = os.environ.get("CATALOGUE_LINK", "https://wa.me/c/971523231413")

IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")
IDF_FREE_DELIVERY_THRESHOLD = 100.0
IDF_DELIVERY_FEE = 3.0
GLS_BASE = 9.0
GLS_PER_KG = 0.65
GLS_MAX_WEIGHT = 30.0

log.info("=== SERVER START === ADMIN: %s", ADMIN_PHONE)

# ── State ──────────────────────────────────────────────────────────────────────

# Client conversation states
# { phone: { "step": "clarification"|"confirm"|"payment", "order_data": ..., "contact": ..., ... } }
client_sessions: dict = {}

# Staff CMD state
cmd_pending: dict = {}

# Admin clarification for CMD orders
admin_clarification: dict = {}
admin_waiting_idf: dict = {}

# Pending CMD orders awaiting OUI/NON
pending_orders: dict = {}

# Payment state
waiting_for_payment: dict = {}

# Track first-time customers to show catalogue message
seen_customers: set = set()


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

    # ── Admin/Staff messages ───────────────────────────────────────────────────
    if phone == ADMIN_PHONE and msg_type == "text":
        txt    = message["text"]["body"].strip()
        txt_up = txt.upper()

        if txt_up.startswith("CMD"):
            await handle_cmd_start(txt)
            return {"status": "cmd"}

        if cmd_pending.get("step") == "customer":
            await handle_cmd_customer(txt)
            return {"status": "cmd_customer"}

        if cmd_pending.get("step") == "idf":
            await handle_cmd_idf(txt)
            return {"status": "cmd_idf"}

        if admin_waiting_idf:
            await handle_admin_idf(txt)
            return {"status": "admin_idf"}

        if admin_clarification:
            await handle_admin_clarification(txt)
            return {"status": "admin_clarification"}

        if txt_up.startswith("OUI") or txt_up.startswith("YES"):
            await handle_admin_validation("OUI")
            return {"status": "admin_yes"}

        if txt_up.startswith("NON") or txt_up.startswith("NO"):
            await handle_admin_validation("NON")
            return {"status": "admin_no"}

        if txt_up.startswith("HISTORY"):
            await handle_history()
            return {"status": "history"}

        if txt_up in ("YES", "OUI") and not pending_orders:
            await send_whatsapp(ADMIN_PHONE, "✅ Great! Have a good evening. 🌙")
            return {"status": "eod_yes"}

        if txt_up in ("NO", "NON") and not pending_orders:
            await send_whatsapp(ADMIN_PHONE,
                "Please log missing orders via CMD before closing.\n\nType CMD followed by the order."
            )
            return {"status": "eod_no"}

        if pending_orders:
            await handle_admin_correction(txt)
            return {"status": "admin_correction"}

    # ── Catalogue order ────────────────────────────────────────────────────────
    if msg_type == "order":
        await handle_catalog_order(phone, contact, message["order"])
        return {"status": "catalog"}

    # ── Client text messages ───────────────────────────────────────────────────
    if msg_type == "text":
        text = message["text"]["body"].strip()

        # Payment response
        if phone in waiting_for_payment:
            await handle_payment_response(phone, text)
            return {"status": "payment"}

        # Client in active session
        if phone in client_sessions:
            await handle_client_session(phone, contact, text)
            return {"status": "session"}

        # New message — show catalogue first if first time
        await handle_new_client_message(phone, contact, text)
        return {"status": "new_message"}

    return {"status": "ignored"}


# ── New client message ─────────────────────────────────────────────────────────

async def handle_new_client_message(phone: str, contact: str, text: str):
    """First message or no active session."""
    first_time = phone not in seen_customers
    seen_customers.add(phone)

    # Analyze message
    try:
        result = analyze_message(text)
    except Exception as e:
        log.error("Analysis error: %s", e)
        await send_whatsapp(phone,
            f"Hi {contact}! 👋\n\n"
            f"The easiest way to order is through our catalogue:\n"
            f"🛒 {CATALOGUE_LINK}\n\n"
            "Or type your order and we'll help you out!"
        )
        return

    if result.get("type") != "order" or not result.get("items"):
        # Not an order — show catalogue + friendly message
        if first_time:
            await send_whatsapp(phone,
                f"Hi {contact}! 👋 Welcome to Africomfort Foods!\n\n"
                f"🛒 *Order easily via our catalogue:*\n{CATALOGUE_LINK}\n\n"
                "Browse our products, add to cart and send your order directly!\n\n"
                "Or simply type your order below and we'll take care of the rest. 😊\n\n"
                "For questions, contact us on +33 6 60 56 51 29."
            )
        else:
            await send_whatsapp(ADMIN_PHONE,
                f"💬 Message from {contact} ({phone}):\n\n{text}\n\nPlease reply directly on WhatsApp."
            )
            await send_whatsapp(phone,
                "Thank you for your message! Our team will get back to you shortly. 😊\n\n"
                f"🛒 In the meantime, browse our catalogue: {CATALOGUE_LINK}"
            )
        return

    # It's an order — start session
    await start_order_session(phone, contact, result)


async def start_order_session(phone: str, contact: str, order_data: dict):
    """Begin interactive order session with client."""
    # Check IDF status
    idf = get_customer_idf(phone)

    client_sessions[phone] = {
        "step": "resolving",
        "contact": contact,
        "order_data": order_data,
        "resolved_items": [],
        "unresolved": [],
        "total_amount": 0.0,
        "total_weight": 0.0,
        "idf": idf,
        "idf_known": idf is not None,
    }

    await resolve_client_items(phone, contact)


# ── Client session handler ─────────────────────────────────────────────────────

async def handle_client_session(phone: str, contact: str, text: str):
    """Handle messages from clients with an active session."""
    session = client_sessions.get(phone)
    if not session:
        return

    step = session.get("step")
    txt_up = text.strip().upper()

    if step == "clarification":
        await handle_client_clarification(phone, contact, text)

    elif step == "idf":
        await handle_client_idf(phone, contact, text)

    elif step == "confirm":
        if txt_up in ("CONFIRM", "YES", "OUI", "OK", "✓", "✅"):
            await finalize_client_order(phone, contact)
        elif txt_up in ("CANCEL", "NON", "NO"):
            client_sessions.pop(phone, None)
            await send_whatsapp(phone,
                "Order cancelled. Feel free to start a new order anytime! 😊\n\n"
                f"🛒 Browse our catalogue: {CATALOGUE_LINK}"
            )
        else:
            # Client wants to modify — re-analyze their message
            try:
                result = analyze_message(text)
                if result.get("type") == "order" and result.get("items"):
                    # Reset session with new order
                    idf = session.get("idf")
                    client_sessions[phone] = {
                        "step": "resolving",
                        "contact": contact,
                        "order_data": result,
                        "resolved_items": [],
                        "unresolved": [],
                        "total_amount": 0.0,
                        "total_weight": 0.0,
                        "idf": idf,
                        "idf_known": idf is not None,
                    }
                    await resolve_client_items(phone, contact)
                else:
                    await send_whatsapp(phone,
                        "Reply *CONFIRM* to place your order, or tell me what you'd like to change."
                    )
            except Exception:
                await send_whatsapp(phone,
                    "Reply *CONFIRM* to place your order, or tell me what you'd like to change."
                )

    elif step == "payment":
        await handle_payment_response(phone, text)


# ── Resolve items (client side) ────────────────────────────────────────────────

async def resolve_client_items(phone: str, contact: str):
    """Search Odoo for all items, ask client to clarify ambiguous ones."""
    session = client_sessions.get(phone)
    if not session:
        return

    models, uid = odoo_login()
    order_data  = session["order_data"]
    ambiguous   = []

    for item in order_data.get("items", []):
        product_name = item.get("product_name", "")
        size         = item.get("size")
        bags         = item.get("bags", 1)

        matches = find_products(models, uid, product_name, size)
        for m in matches:
            m["price_ttc"] = get_price_ttc(models, uid, m["id"], m["list_price"])

        if not matches:
            session["unresolved"].append({
                "product_name": f"{product_name} {size or ''}".strip(),
                "quantity": bags,
            })
        elif len(matches) == 1:
            p          = matches[0]
            line_total = p["price_ttc"] * bags
            session["resolved_items"].append({
                "product_id": p["id"], "product_name": p["name"],
                "quantity": bags, "unit_price": p["price_ttc"],
                "line_total": line_total, "weight": p.get("weight", 0),
            })
            session["total_amount"] += line_total
            session["total_weight"] += p.get("weight", 0) * bags
        else:
            ambiguous.append({
                "query": product_name, "size": size, "bags": bags, "matches": matches
            })

    if ambiguous:
        session["step"]      = "clarification"
        session["ambiguous"] = ambiguous
        session["amb_idx"]   = 0
        first = ambiguous[0]
        await send_whatsapp(phone, client_clarification_msg(first))
    elif not session.get("idf_known"):
        # Need IDF info — ask client for postal code
        session["step"] = "idf"
        await send_whatsapp(phone,
            "Almost there! 📦\n\nWhat is your delivery postal code? (e.g. 75001, 92100...)"
        )
    else:
        await show_order_recap(phone, contact)


async def handle_client_clarification(phone: str, contact: str, text: str):
    """Client responds to product clarification."""
    session   = client_sessions.get(phone)
    ambiguous = session.get("ambiguous", [])
    idx       = session.get("amb_idx", 0)

    if idx >= len(ambiguous):
        return

    current = ambiguous[idx]
    matches = current["matches"]
    bags    = current["bags"]
    t       = text.strip()

    # Try to match
    chosen = None
    if t == "0":
        session["unresolved"].append({
            "product_name": f"{current['query']} {current.get('size') or ''}".strip(),
            "quantity": bags,
        })
        session["amb_idx"] += 1
    else:
        if t.isdigit():
            n = int(t)
            if 1 <= n <= len(matches):
                chosen = matches[n - 1]
        if not chosen:
            tl = t.lower()
            for m in matches:
                if tl in m["name"].lower() or m["name"].lower() in tl:
                    chosen = m
                    break
        if not chosen:
            words = [w for w in t.lower().split() if len(w) > 2]
            for m in matches:
                if any(w in m["name"].lower() for w in words):
                    chosen = m
                    break

        if not chosen:
            await send_whatsapp(phone, client_clarification_msg(current))
            return

        line_total = chosen["price_ttc"] * bags
        session["resolved_items"].append({
            "product_id": chosen["id"], "product_name": chosen["name"],
            "quantity": bags, "unit_price": chosen["price_ttc"],
            "line_total": line_total, "weight": chosen.get("weight", 0),
        })
        session["total_amount"] += line_total
        session["total_weight"] += chosen.get("weight", 0) * bags
        session["amb_idx"] += 1

    next_idx = session["amb_idx"]
    if next_idx < len(ambiguous):
        await send_whatsapp(phone, client_clarification_msg(ambiguous[next_idx]))
    elif not session.get("idf_known"):
        session["step"] = "idf"
        await send_whatsapp(phone,
            "Almost there! 📦\n\nWhat is your delivery postal code? (e.g. 75001, 92100...)"
        )
    else:
        await show_order_recap(phone, contact)


async def handle_client_idf(phone: str, contact: str, text: str):
    """Client provides postal code."""
    session = client_sessions.get(phone)
    postal  = re.sub(r"[^\d]", "", text)[:5]
    idf     = postal[:2] in IDF_PREFIXES if len(postal) >= 2 else False
    session["idf"]       = idf
    session["idf_known"] = True
    session["postal"]    = postal
    save_customer_idf(phone, idf)
    await show_order_recap(phone, contact)


async def show_order_recap(phone: str, contact: str):
    """Show order summary and ask client to confirm."""
    session      = client_sessions.get(phone)
    resolved     = session.get("resolved_items", [])
    unresolved   = session.get("unresolved", [])
    total_amount = session.get("total_amount", 0)
    total_weight = session.get("total_weight", 0)
    idf          = session.get("idf", False)

    shipping_cost, shipping_note = calculate_shipping(idf, total_amount, total_weight)
    grand_total = total_amount + shipping_cost
    session["shipping_cost"] = shipping_cost
    session["grand_total"]   = grand_total
    session["step"]          = "confirm"

    lines = "\n".join(
        f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
        for it in resolved
    )

    msg = f"🛒 *Your order summary:*\n\n{lines}"

    if unresolved:
        msg += "\n\n⚠️ *Not found — our team will contact you:*\n"
        msg += "\n".join(f"  • {u['product_name']} × {u['quantity']}" for u in unresolved)

    msg += f"\n\n🚚 {shipping_note}"
    msg += f"\n💳 *Total: €{grand_total:.2f}*\n\n"
    msg += "Reply *CONFIRM* to place your order, or tell me what you'd like to change."

    await send_whatsapp(phone, msg)


async def finalize_client_order(phone: str, contact: str):
    """Client confirmed — create in Odoo and notify."""
    session    = client_sessions.pop(phone, {})
    order_data = {
        "items_info":    session.get("resolved_items", []),
        "unresolved":    session.get("unresolved", []),
        "shipping_cost": session.get("shipping_cost", 0),
        "grand_total":   session.get("grand_total", 0),
        "total_amount":  session.get("total_amount", 0),
        "total_weight":  session.get("total_weight", 0),
        "idf":           session.get("idf", False),
    }

    try:
        result = create_sale_order(order_data, phone, contact)
        if result is None:
            await send_whatsapp(phone,
                "Sorry, we couldn't process your order. Our team will contact you shortly."
            )
            await send_whatsapp(ADMIN_PHONE,
                f"⚠️ Order from {contact} ({phone}) could not be created in Odoo."
            )
            return

        order_name, _ = result
        grand_total   = order_data["grand_total"]
        items_info    = order_data["items_info"]

        # Notify admin
        items_txt = "\n".join(
            f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
            for it in items_info
        )
        await send_whatsapp(ADMIN_PHONE,
            f"✅ *New order — {order_name}*\n"
            f"Customer: {contact} ({phone})\n\n"
            f"{items_txt}\n\n"
            f"🚚 Delivery: €{order_data['shipping_cost']:.2f}\n"
            f"💳 Total: €{grand_total:.2f}"
        )

        # Ask client for payment
        waiting_for_payment[phone] = {
            "order_name": order_name, "total": grand_total, "order_data": order_data
        }

        await send_whatsapp(phone,
            f"✅ *Order confirmed — {order_name}*\n\n{items_txt}\n\n"
            f"🚚 Delivery: €{order_data['shipping_cost']:.2f}\n"
            f"💳 *Total: €{grand_total:.2f}*\n\n"
            "How would you like to pay?\n"
            "1️⃣ Card on delivery\n"
            "2️⃣ Cash on delivery\n"
            "3️⃣ Payment link (pay now)\n\n"
            "Reply with 1, 2 or 3."
        )

    except Exception as e:
        log.error("Order creation error: %s", e)
        await send_whatsapp(phone, "Sorry, an error occurred. Our team will contact you shortly.")
        await send_whatsapp(ADMIN_PHONE, f"❌ Order error for {contact} ({phone}): {str(e)}")


# ── Catalogue order ────────────────────────────────────────────────────────────

async def handle_catalog_order(phone: str, contact: str, order: dict):
    """Handle order placed via WhatsApp catalogue."""
    models, uid    = odoo_login()
    resolved_items = []
    total_amount   = 0.0
    total_weight   = 0.0

    for p in order.get("product_items", []):
        retailer_id = p.get("product_retailer_id", "")
        bags        = p.get("quantity", 1)
        price       = p.get("item_price", 0)

        # Search by retailer ID or name
        matches = find_products(models, uid, retailer_id, None)
        if matches:
            prod       = matches[0]
            price_ttc  = get_price_ttc(models, uid, prod["id"], prod["list_price"])
            line_total = price_ttc * bags
            total_amount += line_total
            total_weight += prod.get("weight", 0) * bags
            resolved_items.append({
                "product_id": prod["id"], "product_name": prod["name"],
                "quantity": bags, "unit_price": price_ttc,
                "line_total": line_total, "weight": prod.get("weight", 0),
            })

    idf = get_customer_idf(phone)

    if idf is None:
        # Ask postal code
        client_sessions[phone] = {
            "step": "idf",
            "contact": contact,
            "order_data": {"items": []},
            "resolved_items": resolved_items,
            "unresolved": [],
            "total_amount": total_amount,
            "total_weight": total_weight,
            "idf": None,
            "idf_known": False,
        }
        await send_whatsapp(phone,
            "Thank you for your order! 🛒\n\nWhat is your delivery postal code? (e.g. 75001, 92100...)"
        )
    else:
        shipping_cost, shipping_note = calculate_shipping(idf, total_amount, total_weight)
        grand_total = total_amount + shipping_cost

        client_sessions[phone] = {
            "step": "confirm",
            "contact": contact,
            "order_data": {"items": []},
            "resolved_items": resolved_items,
            "unresolved": [],
            "total_amount": total_amount,
            "total_weight": total_weight,
            "shipping_cost": shipping_cost,
            "grand_total": grand_total,
            "idf": idf,
            "idf_known": True,
        }
        await show_order_recap(phone, contact)


# ── Helpers ────────────────────────────────────────────────────────────────────

def client_clarification_msg(amb: dict) -> str:
    options = "\n".join(
        f"  {i+1}. {m['name']} — €{m.get('price_ttc', m['list_price']):.2f}"
        for i, m in enumerate(amb["matches"])
    )
    size_hint = f" ({amb['size']})" if amb.get("size") else ""
    return (
        f"Which *{amb['query']}{size_hint}* did you mean?\n\n"
        f"{options}\n"
        f"  0. None of the above — we'll sort it out\n\n"
        "Reply with the number or product name."
    )


def calculate_shipping(idf: bool, total_amount: float, total_weight: float) -> tuple:
    if idf:
        if total_amount >= IDF_FREE_DELIVERY_THRESHOLD:
            return IDF_DELIVERY_FEE, f"IDF delivery: €{IDF_DELIVERY_FEE:.2f}"
        else:
            needed   = IDF_FREE_DELIVERY_THRESHOLD - total_amount
            shipping = GLS_BASE + GLS_PER_KG * total_weight
            return shipping, (
                f"💡 Add €{needed:.2f} more for free IDF delivery!\n"
                f"Delivery fee: €{shipping:.2f}"
            )
    else:
        shipping = GLS_BASE + GLS_PER_KG * min(total_weight, GLS_MAX_WEIGHT)
        return shipping, f"GLS delivery: €{shipping:.2f}"


# ── Payment ────────────────────────────────────────────────────────────────────

async def handle_payment_response(phone: str, text: str):
    pending    = waiting_for_payment.pop(phone, None)
    if not pending:
        return
    order_name = pending["order_name"]
    total      = pending["total"]
    choice     = text.strip()

    if choice == "1":
        await send_whatsapp(phone, f"✅ *Card on delivery* for {order_name}.\nTotal: €{total:.2f}\n\nThank you! 🙏")
        await send_whatsapp(ADMIN_PHONE, f"💳 {order_name}: *Card on delivery* — €{total:.2f}")
    elif choice == "2":
        await send_whatsapp(phone, f"✅ *Cash on delivery* for {order_name}.\nTotal: €{total:.2f}\n\nThank you! 🙏")
        await send_whatsapp(ADMIN_PHONE, f"💵 {order_name}: *Cash on delivery* — €{total:.2f}")
    elif choice == "3":
        await send_whatsapp(phone, "⏳ Generating your payment link...")
        payment_url = await create_mollie_payment(order_name, total, phone)
        if payment_url:
            await send_whatsapp(phone,
                f"💳 *Payment link for {order_name}*\n\nTotal: €{total:.2f}\n\n"
                f"👉 {payment_url}\n\nThis link expires in 24 hours."
            )
            await send_whatsapp(ADMIN_PHONE, f"🔗 {order_name}: Payment link sent — €{total:.2f}")
        else:
            await send_whatsapp(phone, "Sorry, we could not generate the payment link. Our team will send it shortly.")
            await send_whatsapp(ADMIN_PHONE, f"⚠️ {order_name}: Could not generate Mollie link — €{total:.2f}\nPlease send manually.")
    else:
        await send_whatsapp(phone, "Please reply:\n1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link")
        waiting_for_payment[phone] = pending


async def create_mollie_payment(order_name: str, amount: float, customer_phone: str):
    mollie_key  = os.environ.get("MOLLIE_API_KEY", "")
    webhook_url = os.environ.get("MOLLIE_WEBHOOK_URL", "")
    if not mollie_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.mollie.com/v2/payments",
                json={
                    "amount": {"currency": "EUR", "value": f"{amount:.2f}"},
                    "description": f"Africomfort Foods — {order_name}",
                    "redirectUrl": "https://africomfort-foods.odoo.com",
                    "webhookUrl": webhook_url,
                    "metadata": {"order_name": order_name, "customer_phone": customer_phone}
                },
                headers={"Authorization": f"Bearer {mollie_key}"}
            )
            if r.status_code == 201:
                return r.json()["_links"]["checkout"]["href"]
    except Exception as e:
        log.error("Mollie error: %s", e)
    return None


@app.post("/mollie-webhook")
async def mollie_webhook(request: Request):
    body       = await request.form()
    payment_id = body.get("id")
    if not payment_id:
        return {"status": "ignored"}
    mollie_key = os.environ.get("MOLLIE_API_KEY", "")
    try:
        async with httpx.AsyncClient() as client:
            r       = await client.get(f"https://api.mollie.com/v2/payments/{payment_id}",
                                       headers={"Authorization": f"Bearer {mollie_key}"})
            payment = r.json()
        status         = payment.get("status")
        metadata       = payment.get("metadata", {})
        order_name     = metadata.get("order_name", "")
        customer_phone = metadata.get("customer_phone", "")
        amount         = payment.get("amount", {}).get("value", "0")
        if status == "paid":
            await send_whatsapp(customer_phone,
                f"✅ *Payment received — {order_name}*\nAmount: €{amount}\n\nThank you! 🙏")
            await send_whatsapp(ADMIN_PHONE,
                f"💰 *Payment received* — {order_name}\nCustomer: {customer_phone}\nAmount: €{amount}")
        elif status in ("failed", "expired", "canceled"):
            await send_whatsapp(customer_phone,
                f"❌ Payment {status} for {order_name}. Please try again or contact us.")
            await send_whatsapp(ADMIN_PHONE,
                f"⚠️ Payment {status} — {order_name} (€{amount}) — Customer: {customer_phone}")
    except Exception as e:
        log.error("Mollie webhook error: %s", e)
    return {"status": "ok"}


# ── CMD flow (staff) ───────────────────────────────────────────────────────────

async def handle_cmd_start(txt: str):
    order_text = txt[3:].strip().lstrip(":").strip()
    if not order_text:
        await send_whatsapp(ADMIN_PHONE, "Type CMD followed by the order:\nExample: CMD 5kg president rice, merluza")
        return
    try:
        result = analyze_message(order_text)
    except Exception as e:
        await send_whatsapp(ADMIN_PHONE, f"Could not parse: {str(e)}")
        return
    if not result.get("items"):
        await send_whatsapp(ADMIN_PHONE, "No products found. Please try again.")
        return
    cmd_pending["order_data"] = result
    cmd_pending["step"]       = "customer"
    items_txt = "\n".join(
        f"  • {it.get('product_name')} {it.get('size') or ''} × {it.get('bags', 1)}"
        for it in result.get("items", [])
    )
    await send_whatsapp(ADMIN_PHONE, f"Order noted:\n{items_txt}\n\nCustomer name or phone number?")


async def handle_cmd_customer(txt: str):
    t      = txt.strip()
    digits = re.sub(r"[^\d]", "", t)

    if t.isdigit() and "cmd_customer_results" in cmd_pending:
        results = cmd_pending.pop("cmd_customer_results")
        n = int(t)
        if 1 <= n <= len(results):
            chosen = results[n - 1]
            await _process_cmd_with_customer(chosen["phone"], chosen["name"])
        else:
            await send_whatsapp(ADMIN_PHONE, f"Please reply 1-{len(results)}.")
            cmd_pending["cmd_customer_results"] = results
        return

    if len(digits) >= 8:
        try:
            models, uid = odoo_login()
            ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                    [[["phone", "like", digits[-8:]]]])
            if ids:
                partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                                             [ids[:3]], {"fields": ["name", "phone"]})
                if len(partners) == 1:
                    await _process_cmd_with_customer(digits, partners[0]["name"])
                else:
                    options = "\n".join(f"  {i+1}. {p['name']} — {p['phone']}"
                                        for i, p in enumerate(partners))
                    cmd_pending["cmd_customer_results"] = [
                        {"name": p["name"], "phone": re.sub(r"[^\d]", "", p["phone"] or digits)}
                        for p in partners
                    ]
                    await send_whatsapp(ADMIN_PHONE, f"Found:\n{options}\n\nReply with number.")
            else:
                await _process_cmd_with_customer(digits, digits)
        except Exception:
            await _process_cmd_with_customer(digits, digits)
    else:
        try:
            models, uid = odoo_login()
            ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                    [[["name", "ilike", t], ["customer_rank", ">", 0]]], {"limit": 5})
            if ids:
                partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                                             [ids], {"fields": ["name", "phone"]})
                partners = [p for p in partners if p.get("phone")]
                if not partners:
                    await send_whatsapp(ADMIN_PHONE, f"No customer found with name *{t}*. Please provide their phone number.")
                    return
                if len(partners) == 1:
                    phone = re.sub(r"[^\d]", "", partners[0]["phone"])
                    await _process_cmd_with_customer(phone, partners[0]["name"])
                else:
                    options = "\n".join(f"  {i+1}. {p['name']} — {p['phone']}"
                                        for i, p in enumerate(partners))
                    cmd_pending["cmd_customer_results"] = [
                        {"name": p["name"], "phone": re.sub(r"[^\d]", "", p["phone"])}
                        for p in partners
                    ]
                    await send_whatsapp(ADMIN_PHONE, f"Found:\n{options}\n\nReply with number or type their phone.")
            else:
                await send_whatsapp(ADMIN_PHONE, f"No customer found with name *{t}*. Please provide their phone number.")
        except Exception as e:
            await send_whatsapp(ADMIN_PHONE, f"Search error: {str(e)}")


async def _process_cmd_with_customer(customer_phone: str, customer_name: str):
    order_data                    = cmd_pending.get("order_data", {})
    order_data["customer_phone"]  = customer_phone
    order_data["customer_name"]   = customer_name
    idf = get_customer_idf(customer_phone)
    if idf is None:
        cmd_pending["step"] = "idf"
        await send_whatsapp(ADMIN_PHONE,
            f"Customer: *{customer_name}* ({customer_phone})\n\nIs this customer in *IDF*? Reply *YES* or *NO*")
    else:
        idf_label = "IDF ✓" if idf else "Outside IDF ✓"
        await send_whatsapp(ADMIN_PHONE, f"Customer: *{customer_name}* ({customer_phone}) — {idf_label}\n\nProcessing...")
        cmd_pending.clear()
        await process_cmd_order(order_data, customer_phone, customer_name, idf)


async def handle_cmd_idf(txt: str):
    txt_up     = txt.strip().upper()
    order_data = cmd_pending.pop("order_data", {})
    phone      = order_data.get("customer_phone", "")
    name       = order_data.get("customer_name", phone)
    cmd_pending.clear()
    idf = txt_up in ("YES", "OUI", "Y")
    save_customer_idf(phone, idf)
    await process_cmd_order(order_data, phone, name, idf)


async def process_cmd_order(order_data: dict, phone: str, name: str, idf: bool):
    """Resolve products for CMD order and send recap to admin for validation."""
    models, uid    = odoo_login()
    resolved_items = []
    ambiguous_list = []
    missing        = []
    total_amount   = 0.0
    total_weight   = 0.0

    for item in order_data.get("items", []):
        product_name = item.get("product_name", "")
        size         = item.get("size")
        bags         = item.get("bags", 1)
        matches      = find_products(models, uid, product_name, size)
        for m in matches:
            m["price_ttc"] = get_price_ttc(models, uid, m["id"], m["list_price"])
        if not matches:
            missing.append(f"{product_name} {size or ''}".strip())
        elif len(matches) == 1:
            p          = matches[0]
            line_total = p["price_ttc"] * bags
            total_amount += line_total
            total_weight += p.get("weight", 0) * bags
            resolved_items.append({
                "product_id": p["id"], "product_name": p["name"],
                "quantity": bags, "unit_price": p["price_ttc"],
                "line_total": line_total, "weight": p.get("weight", 0),
            })
        else:
            ambiguous_list.append({"query": product_name, "size": size, "bags": bags, "matches": matches})

    order_data["items_info"]   = resolved_items
    order_data["missing"]      = missing
    order_data["total_amount"] = total_amount
    order_data["total_weight"] = total_weight
    order_data["idf"]          = idf

    if ambiguous_list:
        admin_clarification["ambiguous"]   = ambiguous_list
        admin_clarification["current_idx"] = 0
        admin_clarification["order_data"]  = order_data
        admin_clarification["phone"]       = phone
        admin_clarification["contact"]     = name
        await send_whatsapp(ADMIN_PHONE, admin_clarification_msg(ambiguous_list[0]))
    else:
        await send_cmd_recap_to_admin(order_data, phone, name)


async def send_cmd_recap_to_admin(order_data: dict, phone: str, name: str):
    idf          = order_data.get("idf", False)
    total_amount = order_data.get("total_amount", 0)
    total_weight = order_data.get("total_weight", 0)
    shipping_cost, shipping_note = calculate_shipping(idf, total_amount, total_weight)
    grand_total = total_amount + shipping_cost
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"]   = grand_total

    token = str(uuid.uuid4())[:8]
    pending_orders[token] = {"order_data": order_data, "phone": phone, "contact": name}

    items_info = order_data.get("items_info", [])
    missing    = order_data.get("missing", [])
    lines      = "\n".join(
        f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
        for it in items_info
    )
    msg = f"🛒 *CMD Order — {name} ({phone})*\n\n{lines}"
    if missing:
        msg += f"\n\n⚠️ Not found: {', '.join(missing)}"
    msg += f"\n\n🚚 {shipping_note}"
    msg += f"\n💰 Subtotal: €{total_amount:.2f}"
    msg += f"\n💳 *Total: €{grand_total:.2f}*"
    msg += "\n\nReply *OUI* to confirm, *NON* to cancel, or send a correction."
    await send_whatsapp(ADMIN_PHONE, msg)


def admin_clarification_msg(amb: dict) -> str:
    options = "\n".join(
        f"  {i+1}. {m['name']} — €{m.get('price_ttc', m['list_price']):.2f}"
        for i, m in enumerate(amb["matches"])
    )
    size_hint = f" ({amb['size']})" if amb.get("size") else ""
    return (
        f"Which *{amb['query']}{size_hint}*?\n\n{options}\n  0. Skip\n\nReply with number or name."
    )


async def handle_admin_clarification(txt: str):
    ambiguous  = admin_clarification.get("ambiguous", [])
    idx        = admin_clarification.get("current_idx", 0)
    order_data = admin_clarification.get("order_data")
    phone      = admin_clarification.get("phone")
    contact    = admin_clarification.get("contact")

    if not ambiguous or idx >= len(ambiguous):
        return

    current = ambiguous[idx]
    matches = current["matches"]
    bags    = current["bags"]
    t       = txt.strip()

    if t == "0":
        admin_clarification["current_idx"] += 1
    else:
        chosen = None
        if t.isdigit():
            n = int(t)
            if 1 <= n <= len(matches):
                chosen = matches[n - 1]
        if not chosen:
            tl = t.lower()
            for m in matches:
                if tl in m["name"].lower():
                    chosen = m
                    break
        if not chosen:
            await send_whatsapp(ADMIN_PHONE, admin_clarification_msg(current))
            return

        price_ttc  = chosen.get("price_ttc", chosen["list_price"])
        line_total = price_ttc * bags
        order_data["items_info"].append({
            "product_id": chosen["id"], "product_name": chosen["name"],
            "quantity": bags, "unit_price": price_ttc,
            "line_total": line_total, "weight": chosen.get("weight", 0),
        })
        order_data["total_amount"] += line_total
        order_data["total_weight"] += chosen.get("weight", 0) * bags
        admin_clarification["current_idx"] += 1

    next_idx = admin_clarification["current_idx"]
    if next_idx < len(ambiguous):
        await send_whatsapp(ADMIN_PHONE, admin_clarification_msg(ambiguous[next_idx]))
    else:
        admin_clarification.clear()
        await send_cmd_recap_to_admin(order_data, phone, contact)


async def handle_admin_validation(decision: str):
    if not pending_orders:
        await send_whatsapp(ADMIN_PHONE, "No pending CMD orders.")
        return
    token   = next(iter(pending_orders))
    pending = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone      = pending["phone"]
    contact    = pending["contact"]

    if decision == "OUI":
        try:
            result = create_sale_order(order_data, phone, contact)
            if result is None:
                await send_whatsapp(ADMIN_PHONE, "❌ No products found in Odoo.")
                return
            order_name, _ = result
            grand_total   = order_data.get("grand_total", 0)
            items_info    = order_data.get("items_info", [])
            items_txt = "\n".join(
                f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
                for it in items_info
            )
            await send_whatsapp(ADMIN_PHONE, f"✅ Order {order_name} created in Odoo.")
            waiting_for_payment[phone] = {"order_name": order_name, "total": grand_total, "order_data": order_data}
            await send_whatsapp(phone,
                f"✅ *Order Confirmed — {order_name}*\n\n{items_txt}\n\n"
                f"🚚 Delivery: €{order_data.get('shipping_cost', 0):.2f}\n"
                f"💳 *Total: €{grand_total:.2f}*\n\n"
                "How would you like to pay?\n"
                "1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link\n\nReply 1, 2 or 3."
            )
        except Exception as e:
            await send_whatsapp(ADMIN_PHONE, f"❌ Odoo error: {str(e)}")
    else:
        await send_whatsapp(ADMIN_PHONE, "Order cancelled.")
        await send_whatsapp(phone, "Your order has been cancelled. Feel free to order again anytime! 😊")


async def handle_admin_idf(txt: str):
    order_data = admin_waiting_idf.pop("order_data", None)
    phone      = admin_waiting_idf.pop("phone", None)
    contact    = admin_waiting_idf.pop("contact", None)
    if not order_data:
        return
    idf = txt.strip().upper() in ("YES", "OUI", "Y")
    save_customer_idf(phone, idf)
    await process_cmd_order(order_data, phone, contact, idf)


CORRECTION_PROMPT = """
Parse order corrections. Respond ONLY with valid JSON:
{
  "actions": [
    { "type": "add", "product_name": "...", "size": "...", "bags": 1 },
    { "type": "remove", "product_name": "..." },
    { "type": "replace", "old_product": "...", "new_product_name": "...", "size": "...", "bags": 1 },
    { "type": "change_qty", "product_name": "...", "bags": 2 }
  ]
}
"""

async def handle_admin_correction(correction_text: str):
    if not pending_orders:
        return
    token      = next(iter(pending_orders))
    pending    = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone      = pending["phone"]
    contact    = pending["contact"]

    try:
        correction = analyze_correction(correction_text)
        actions    = correction.get("actions", [])
    except Exception:
        await send_whatsapp(ADMIN_PHONE, "Could not understand. Try again or reply OUI/NON.")
        pending_orders[token] = pending
        return

    models, uid = odoo_login()
    items_info  = list(order_data.get("items_info", []))

    for action in actions:
        atype = action.get("type")
        if atype == "remove":
            target     = action.get("product_name", "").lower()
            items_info = [it for it in items_info if target not in it["product_name"].lower()]
        elif atype == "change_qty":
            target  = action.get("product_name", "").lower()
            new_qty = action.get("bags", 1)
            for it in items_info:
                if target in it["product_name"].lower():
                    it["quantity"]   = new_qty
                    it["line_total"] = it["unit_price"] * new_qty
                    break
        elif atype in ("add", "replace"):
            pname   = action.get("product_name") or action.get("new_product_name", "")
            size    = action.get("size")
            bags    = action.get("bags", 1)
            matches = find_products(models, uid, pname, size)
            if matches:
                p         = matches[0]
                price_ttc = get_price_ttc(models, uid, p["id"], p["list_price"])
                new_item  = {
                    "product_id": p["id"], "product_name": p["name"],
                    "quantity": bags, "unit_price": price_ttc,
                    "line_total": price_ttc * bags, "weight": p.get("weight", 0),
                }
                if atype == "replace":
                    old        = action.get("old_product", "").lower()
                    items_info = [it for it in items_info if old not in it["product_name"].lower()]
                items_info.append(new_item)
            else:
                await send_whatsapp(ADMIN_PHONE, f"⚠️ Product not found: *{pname} {size or ''}*")
                pending_orders[token] = pending
                return

    total_amount = sum(it["line_total"] for it in items_info)
    total_weight = sum(it.get("weight", 0) * it["quantity"] for it in items_info)
    idf          = order_data.get("idf", False)
    shipping_cost, _ = calculate_shipping(idf, total_amount, total_weight)

    order_data["items_info"]    = items_info
    order_data["total_amount"]  = total_amount
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"]   = total_amount + shipping_cost
    order_data["total_weight"]  = total_weight

    await send_cmd_recap_to_admin(order_data, phone, contact)


async def handle_history():
    try:
        models, uid = odoo_login()
        today = date.today().strftime("%Y-%m-%d")
        ids   = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "search",
                                  [[["client_order_ref", "like", "WA-"],
                                    ["date_order", ">=", f"{today} 00:00:00"]]])
        if not ids:
            await send_whatsapp(ADMIN_PHONE, "📋 No orders today via WhatsApp.")
            return
        orders = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
                                   [ids], {"fields": ["name", "partner_id", "amount_total", "state"]})
        status_map = {"draft": "⏳", "sent": "📤", "sale": "✅", "cancel": "❌"}
        lines = "\n".join(
            f"{status_map.get(o['state'], '?')} {o['name']} — {o['partner_id'][1]} — €{o['amount_total']:.2f}"
            for o in orders
        )
        total = sum(o["amount_total"] for o in orders)
        await send_whatsapp(ADMIN_PHONE,
            f"📋 *Today's orders ({len(orders)})*\n\n{lines}\n\n💳 *Total: €{total:.2f}*")
    except Exception as e:
        await send_whatsapp(ADMIN_PHONE, f"Could not fetch history: {str(e)}")


# ── EOD reminder ───────────────────────────────────────────────────────────────

@app.on_event("startup")
async def schedule_eod_reminder():
    asyncio.create_task(eod_reminder_loop())

async def eod_reminder_loop():
    while True:
        now    = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await send_whatsapp(ADMIN_PHONE,
            "⏰ *End of day check*\n\nHave you logged all orders today?\n\nReply *YES* or *NO*")


# ── Claude ─────────────────────────────────────────────────────────────────────

ORDER_PROMPT = """
You are an assistant for an African food delivery business.
Analyze the message and respond ONLY with valid JSON, no markdown.

If it's an order:
{
  "type": "order",
  "confidence": 0.9,
  "items": [
    { "product_name": "pounded yam", "size": "5kg", "bags": 1 }
  ]
}

If NOT an order: { "type": "question", "message": "original text" }

Rules:
- Extract product name WITHOUT size (put size separately)
- "5kg pounded yam" → product_name: "pounded yam", size: "5kg", bags: 1
- "2 bags rice 10kg" → product_name: "rice", size: "10kg", bags: 2
- confidence < 0.65 → type "question"
"""

def analyze_message(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        system=ORDER_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    log.info("Claude: %s", raw)
    return json.loads(raw)

def analyze_correction(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        system=CORRECTION_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ── Odoo ───────────────────────────────────────────────────────────────────────

def odoo_login():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("Odoo auth failed")
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object"), uid


def find_products(models, uid, name: str, size: str = None) -> list:
    def search(domain):
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search",
                                [domain], {"limit": 20})
        if not ids:
            return []
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                 [ids], {"fields": ["id", "name", "list_price", "weight"]})

    results = search([["name", "ilike", name], ["sale_ok", "=", True], ["is_published", "=", True]])
    if not results:
        words = [w for w in name.split()
                 if len(w) > 2 and not re.match(r"^[0-9]+[kgKGlLmM]+$", w)]
        if words:
            all_ids = None
            for word in words:
                ids = {p["id"] for p in search([["name", "ilike", word],
                                                ["sale_ok", "=", True], ["is_published", "=", True]])}
                all_ids = ids if all_ids is None else all_ids & ids
            if all_ids:
                results = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                            [list(all_ids)], {"fields": ["id", "name", "list_price", "weight"]})

    if size and results:
        size_clean    = size.lower().replace(" ", "")
        size_filtered = [m for m in results if size_clean in m["name"].lower()]
        if size_filtered:
            return size_filtered

    return results[:5]


def get_price_ttc(models, uid, product_id: int, price_ht: float) -> float:
    try:
        products = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
                                     [[product_id]], {"fields": ["taxes_id"]})[0]
        tax_ids  = products.get("taxes_id", [])
        if not tax_ids:
            return price_ht
        taxes    = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "read",
                                     [tax_ids], {"fields": ["amount", "amount_type"]})
        tax_rate = sum(t["amount"] / 100 for t in taxes if t.get("amount_type") == "percent")
        return round(price_ht * (1 + tax_rate), 2)
    except Exception:
        return price_ht


def get_customer_idf(phone: str):
    try:
        models, uid = odoo_login()
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                [[["phone", "=", phone]]])
        if not ids:
            return None
        partner = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                                    [ids], {"fields": ["comment"]})[0]
        comment = partner.get("comment") or ""
        if "IDF:YES" in comment:
            return True
        if "IDF:NO" in comment:
            return False
        return None
    except Exception:
        return None


def save_customer_idf(phone: str, idf: bool):
    try:
        models, uid = odoo_login()
        idf_tag = "IDF:YES" if idf else "IDF:NO"
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                [[["phone", "=", phone]]])
        if ids:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "write",
                              [ids, {"comment": idf_tag}])
        else:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                              [{"phone": phone, "name": phone, "customer_rank": 1, "comment": idf_tag}])
    except Exception as e:
        log.error("save_customer_idf error: %s", e)


def find_or_create_customer(models, uid, name: str, phone: str) -> int:
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                            [[["phone", "=", phone]]])
    if ids:
        return ids[0]
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                             [{"name": name or phone, "phone": phone, "customer_rank": 1}])


def create_sale_order(order_data: dict, phone: str, contact: str):
    models, uid = odoo_login()
    partner_id  = find_or_create_customer(models, uid, contact, phone)
    items_info  = order_data.get("items_info", [])
    if not items_info:
        return None

    lines   = []
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
            [[["name", "ilike", "Livraison"], ["sale_ok", "=", True]]], {"limit": 1}
        )
        if delivery_ids:
            lines.append((0, 0, {
                "product_id": delivery_ids[0], "product_uom_qty": 1,
                "price_unit": shipping_cost, "name": "Delivery fee",
            }))

    oid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [{
        "partner_id": partner_id, "order_line": lines,
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


# ── Extraction Factures Fournisseurs ───────────────────────────────────────────

import base64
from fastapi import UploadFile, File
from fastapi.responses import HTMLResponse

INVOICE_SYSTEM_PROMPT = """Tu es expert en extraction de factures fournisseurs.
Retourne UNIQUEMENT un objet JSON valide, sans markdown, sans texte autour.
Format exact :
{
  "fournisseur": "nom complet",
  "adresse": "adresse complete",
  "tva_fournisseur": "",
  "reference": "numero facture",
  "date": "YYYY-MM-DD",
  "echeance": "YYYY-MM-DD",
  "devise": "EUR",
  "lignes": [
    {"ref":"","designation":"nom produit","quantite":1.0,"unite":"","prix_unitaire_ht":0.0,"tva_pct":0.0,"montant_ht":0.0}
  ],
  "total_ht": 0.0,
  "total_tva": 0.0,
  "total_ttc": 0.0,
  "iban": ""
}
Extrais TOUTES les lignes produits sans exception."""

INVOICE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AfriComfort — Extraction Factures</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
:root{--bg:#0d0d0d;--sur:#161616;--sur2:#1f1f1f;--brd:#2a2a2a;--acc:#00e5a0;--acd:#00e5a015;--txt:#f0f0f0;--mut:#888;--dim:#555;--red:#ff4d4d;--orn:#f0a500;--mono:'IBM Plex Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;min-height:100vh}
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
.side{background:var(--sur);border-right:1px solid var(--brd);padding:28px 20px;display:flex;flex-direction:column;gap:20px}
.logo{font-family:var(--mono);font-size:11px;color:var(--acc);letter-spacing:.15em;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;background:var(--acc);border-radius:50%;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.slabel{font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--dim);text-transform:uppercase;margin-bottom:6px}
.info-row{font-size:11px;color:var(--mut);padding:6px 10px;background:var(--sur2);border:1px solid var(--brd);border-radius:6px;font-family:var(--mono);line-height:1.6}
.info-row span{color:var(--acc)}
.conn{display:flex;align-items:center;gap:6px;font-size:11px;font-family:var(--mono);padding:6px 10px;border-radius:4px;background:var(--sur2);border:1px solid var(--brd)}
.cdot{width:6px;height:6px;border-radius:50%;background:var(--acc);flex-shrink:0}
.main{padding:32px 36px;display:flex;flex-direction:column;gap:20px}
.ptitle{font-size:22px;font-weight:300;letter-spacing:-.02em}
.ptitle span{color:var(--acc);font-weight:600}
.zone{border:1.5px dashed var(--brd);border-radius:10px;padding:40px 24px;text-align:center;cursor:pointer;transition:all .25s;background:var(--sur);position:relative}
.zone:hover,.zone.drag{border-color:var(--acc);background:var(--acd)}
.zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.zi{width:48px;height:48px;border:1.5px solid var(--brd);border-radius:10px;display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:20px}
.sb{background:var(--sur);border:1px solid var(--brd);border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;font-family:var(--mono);font-size:12px;color:var(--mut)}
.sb.proc{border-color:var(--orn);color:var(--orn)}.sb.ok{border-color:var(--acc);color:var(--acc)}.sb.err{border-color:var(--red);color:var(--red)}
.sp{width:14px;height:14px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.card{background:var(--sur);border:1px solid var(--brd);border-radius:10px;overflow:hidden}
.ch{padding:14px 18px;border-bottom:1px solid var(--brd);display:flex;align-items:center;justify-content:space-between}
.ct{font-weight:500;font-size:14px}.cm{font-size:11px;color:var(--mut);font-family:var(--mono);margin-top:2px}
.bpush{padding:8px 18px;background:var(--acc);color:#000;border:none;border-radius:6px;font-family:var(--mono);font-size:11px;font-weight:500;cursor:pointer;transition:opacity .2s}
.bpush:hover{opacity:.85}.bpush:disabled{opacity:.4;cursor:not-allowed}
.hg{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));border-bottom:1px solid var(--brd)}
.hf{padding:12px 18px;border-right:1px solid var(--brd)}.hf:last-child{border-right:none}
.hl{font-size:10px;color:var(--dim);font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;margin-bottom:3px}
.hv{font-size:13px;font-weight:500}.hv.a{color:var(--acc)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 18px;font-size:10px;font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--brd);background:var(--sur2)}
td{padding:10px 18px;font-size:13px;border-bottom:1px solid var(--brd);vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:var(--sur2)}
.m{font-family:var(--mono);font-size:12px}.r{text-align:right}.a{color:var(--acc);font-weight:500}
.rb{background:var(--sur2);border:1px solid var(--brd);border-radius:4px;padding:2px 7px;font-family:var(--mono);font-size:10px;color:var(--mut)}
.tots{padding:14px 18px;border-top:1px solid var(--brd);display:flex;justify-content:flex-end;gap:40px;background:var(--sur2)}
.ti{text-align:right}.tl{font-size:10px;color:var(--dim);font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase}.tv{font-size:16px;font-weight:600}
.log{background:var(--sur);border:1px solid var(--brd);border-radius:8px;padding:14px 16px;font-family:var(--mono);font-size:11px;color:var(--mut);max-height:150px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.ll{display:flex;gap:10px}.lt{color:var(--dim);flex-shrink:0}
.lok{color:var(--acc)}.lerr{color:var(--red)}.lwarn{color:var(--orn)}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app">
<aside class="side">
  <div class="logo"><div class="dot"></div>AFRICOMFORT</div>
  <div>
    <div class="slabel">Statut</div>
    <div class="conn"><div class="cdot"></div><span>Serveur Railway connecté</span></div>
  </div>
  <div>
    <div class="slabel">Odoo</div>
    <div class="info-row"><span>africomfort-foods.odoo.com</span><br>Credentials Railway ✓</div>
  </div>
  <div>
    <div class="slabel">Claude AI</div>
    <div class="info-row">Anthropic API<br><span>Configuré Railway ✓</span></div>
  </div>
  <div style="margin-top:auto;font-size:11px;color:var(--dim);line-height:1.8">
    Extraction IA via Railway<br>Factures → Odoo 19 Enterprise<br>
    <span style="color:var(--mut)">v4.0 — AfriComfort Foods</span>
  </div>
</aside>
<main class="main">
  <div>
    <div class="ptitle">Extraction <span>factures</span></div>
    <div style="font-size:13px;color:var(--mut);margin-top:4px">Upload un PDF ou une photo — Claude extrait toutes les lignes et les envoie dans Odoo</div>
  </div>
  <div class="zone" id="uz">
    <input type="file" id="fi" accept="image/*,.pdf" onchange="handleFile(this.files[0])"/>
    <div class="zi">📄</div>
    <div style="font-size:15px;font-weight:500;margin-bottom:4px">Déposer la facture ici</div>
    <div style="font-size:12px;color:var(--mut)">PDF ou image (JPG, PNG, WhatsApp) · Glisser-déposer ou cliquer</div>
  </div>
  <div class="sb hidden" id="sb"><div class="sp hidden" id="sp"></div><span id="st"></span></div>
  <div class="card hidden" id="rc">
    <div class="ch">
      <div><div class="ct" id="rsup"></div><div class="cm" id="rmeta"></div></div>
      <button class="bpush" id="bp" onclick="push()">↑ Envoyer dans Odoo</button>
    </div>
    <div class="hg">
      <div class="hf"><div class="hl">Référence</div><div class="hv m" id="rref"></div></div>
      <div class="hf"><div class="hl">Date</div><div class="hv" id="rdate"></div></div>
      <div class="hf"><div class="hl">Échéance</div><div class="hv" id="rdue"></div></div>
      <div class="hf"><div class="hl">Devise</div><div class="hv" id="rcur"></div></div>
      <div class="hf"><div class="hl">Total TTC</div><div class="hv a" id="rttc"></div></div>
    </div>
    <table><thead><tr><th>Réf.</th><th>Désignation</th><th class="r">Qté</th><th class="r">Prix HT</th><th class="r">TVA</th><th class="r">Montant HT</th></tr></thead>
    <tbody id="lb"></tbody></table>
    <div class="tots">
      <div class="ti"><div class="tl">HT</div><div class="tv" id="tht"></div></div>
      <div class="ti"><div class="tl">TVA</div><div class="tv" id="ttva"></div></div>
      <div class="ti"><div class="tl">TTC</div><div class="tv a" id="tttc"></div></div>
    </div>
  </div>
  <div class="log" id="log"><div class="ll"><span class="lt">--:--:--</span><span>Prêt — en attente d'une facture</span></div></div>
</main>
</div>
<script>
let inv=null;
const $=id=>document.getElementById(id);
function log(m,t=''){const l=$('log'),n=document.createElement('div'),now=new Date().toLocaleTimeString('fr-FR');n.className='ll';n.innerHTML=`<span class="lt">${now}</span><span class="${t?'l'+t:''}">${m}</span>`;l.appendChild(n);l.scrollTop=l.scrollHeight}
function status(m,t=''){const b=$('sb');b.className='sb'+(t?' '+t:'');$('st').textContent=m;$('sp').classList.toggle('hidden',t!=='proc');b.classList.remove('hidden')}
function fmt(n){return Number(n).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' \u20ac'}
async function handleFile(f){
  if(!f)return;
  log(`Fichier : ${f.name} (${(f.size/1024).toFixed(0)} Ko)`);
  status(`Analyse de ${f.name}...`,'proc');
  $('rc').classList.add('hidden');
  try{
    const fd=new FormData();fd.append('file',f);
    log('Envoi au serveur pour extraction Claude...','warn');
    const r=await fetch('/invoice/extract',{method:'POST',body:fd});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    inv=d;
    log(`Extraction réussie — ${inv.lignes.length} ligne(s) trouvée(s)`,'ok');
    status(`${inv.lignes.length} lignes extraites avec succès`,'ok');
    render(inv);
  }catch(e){log(`Erreur : ${e.message}`,'err');status("Erreur d'extraction",'err')}
}
function render(d){
  $('rsup').textContent=d.fournisseur||'—';
  $('rmeta').textContent=(d.tva_fournisseur||'')+(d.adresse?' · '+d.adresse.replace(/\n/g,', '):'');
  $('rref').textContent=d.reference||'—';$('rdate').textContent=d.date||'—';
  $('rdue').textContent=d.echeance||'—';$('rcur').textContent=d.devise||'EUR';
  $('rttc').textContent=fmt(d.total_ttc);$('tht').textContent=fmt(d.total_ht);
  $('ttva').textContent=fmt(d.total_tva);$('tttc').textContent=fmt(d.total_ttc);
  const tb=$('lb');tb.innerHTML='';
  (d.lignes||[]).forEach(l=>{const tr=document.createElement('tr');tr.innerHTML=`<td><span class="rb">${l.ref||'—'}</span></td><td>${l.designation}</td><td class="m r">${l.quantite} ${l.unite||''}</td><td class="m r">${fmt(l.prix_unitaire_ht)}</td><td class="m r">${l.tva_pct}%</td><td class="m r a">${fmt(l.montant_ht)}</td>`;tb.appendChild(tr)});
  $('rc').classList.remove('hidden');$('bp').textContent='↑ Envoyer dans Odoo';$('bp').disabled=false;
}
async function push(){
  if(!inv)return;
  $('bp').disabled=true;log('Envoi vers Odoo...','warn');
  try{
    const r=await fetch('/invoice/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(inv)});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    log(`✓ Facture créée dans Odoo — ID: ${d.bill_id}`,'ok');
    log(`✓ ${inv.lignes.length} ligne(s) importée(s)`,'ok');
    status(`Facture ${inv.reference} envoyée dans Odoo (ID: ${d.bill_id})`,'ok');
    $('bp').textContent='✓ Envoyé dans Odoo';
  }catch(e){log(`Erreur push : ${e.message}`,'err');status('Erreur envoi Odoo','err');$('bp').disabled=false}
}
const uz=$('uz');
uz.addEventListener('dragover',e=>{e.preventDefault();uz.classList.add('drag')});
uz.addEventListener('dragleave',()=>uz.classList.remove('drag'));
uz.addEventListener('drop',e=>{e.preventDefault();uz.classList.remove('drag');const f=e.dataTransfer.files[0];if(f)handleFile(f)});
</script>
</body>
</html>"""


@app.get("/invoice", response_class=HTMLResponse)
async def invoice_ui():
    return HTMLResponse(content=INVOICE_HTML)


@app.post("/invoice/extract")
async def invoice_extract(file: UploadFile = File(...)):
    try:
        content = await file.read()
        b64 = base64.b64encode(content).decode()
        mime = file.content_type or "application/octet-stream"
        log.info("Invoice extract: %s (%d bytes)", file.filename, len(content))

        if mime.startswith("image/"):
            msg_content = [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": "Extrais toutes les données de cette facture fournisseur."}
            ]
        elif mime == "application/pdf":
            msg_content = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": "Extrais toutes les données de cette facture fournisseur."}
            ]
        else:
            return {"error": f"Format non supporté : {mime}"}

        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=INVOICE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": msg_content}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        log.info("Extracted %d lines", len(result.get("lignes", [])))
        return result

    except Exception as e:
        log.error("Invoice extract error: %s", e)
        return {"error": str(e)}


def find_partner_by_name(models, uid, supplier_name: str):
    """Check mapping first, then fallback to Odoo search."""
    if not supplier_name:
        return False
    m = load_mapping()
    # 1. Check mapping (exact key or substring match)
    for key, pid in m.get("fournisseurs", {}).items():
        if key.lower() in supplier_name.lower() or supplier_name.lower() in key.lower():
            log.info("Partner from mapping: %s -> %s", supplier_name, pid)
            return int(pid) if pid else False
    # 2. Odoo exact
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
        [[["name", "=ilike", supplier_name]]], {"limit": 1})
    if ids:
        return ids[0]
    # 3. Odoo ilike full name
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
        [[["name", "ilike", supplier_name]]], {"limit": 1})
    if ids:
        return ids[0]
    log.info("Partner not found: %s", supplier_name)
    return False


def find_product_in_mapping(designation: str):
    """Return (odoo_id, facteur, note) from mapping if found."""
    m = load_mapping()
    desig_up = designation.upper()
    for key, cfg in m.get("produits", {}).items():
        if key.upper() in desig_up or desig_up in key.upper():
            log.info("Product from mapping: %s -> id=%s x%s", designation, cfg.get("odoo_id"), cfg.get("facteur",1))
            return cfg.get("odoo_id"), cfg.get("facteur", 1), cfg.get("note", "")
    return None, 1, ""


def find_product_by_name(models, uid, designation: str):
    if not designation:
        return None, None
    # 1. Odoo exact
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
        [[["name", "=ilike", designation], ["active", "=", True]]], {"limit": 1})
    if ids:
        p = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
            [ids], {"fields": ["id", "name"]})[0]
        return ids[0], p
    # 2. Odoo ilike
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
        [[["name", "ilike", designation], ["active", "=", True]]], {"limit": 1})
    if ids:
        p = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
            [ids], {"fields": ["id", "name"]})[0]
        return ids[0], p
    log.info("Product not found in Odoo: %s", designation)
    return None, None


@app.post("/invoice/push")
async def invoice_push(request: Request):
    try:
        inv = await request.json()
        log.info("Invoice push: %s / %s", inv.get("fournisseur"), inv.get("reference"))
        models, uid = odoo_login()

        # Find partner (mapping first)
        partner_id = find_partner_by_name(models, uid, inv.get("fournisseur", ""))

        # Detect if supplier is French → apply VAT
        french_tax_id = False
        if partner_id:
            partner_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                [[partner_id]], {"fields": ["country_id"]}
            )
            country = partner_info[0].get("country_id")
            is_french = country and country[1] in ("France", "FR")
            if is_french:
                # Find 5.5% food tax in Odoo
                tax_ids_55 = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "search",
                    [[["amount", "=", 5.5], ["type_tax_use", "=", "purchase"], ["active", "=", True]]],
                    {"limit": 1}
                )
                if not tax_ids_55:
                    # Try 20%
                    tax_ids_55 = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "search",
                        [[["amount", "=", 20.0], ["type_tax_use", "=", "purchase"], ["active", "=", True]]],
                        {"limit": 1}
                    )
                french_tax_id = tax_ids_55[0] if tax_ids_55 else False
                log.info("French supplier — tax id: %s", french_tax_id)
            else:
                log.info("Non-French supplier — no VAT applied")

        def get_tax_ids_for_line(tva_pct):
            """Get tax id matching the invoice line TVA percentage."""
            if not french_tax_id:
                return []
            # Try to find exact match for the percentage on the invoice
            if tva_pct and tva_pct > 0:
                exact = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "search",
                    [[["amount", "=", tva_pct], ["type_tax_use", "=", "purchase"], ["active", "=", True]]],
                    {"limit": 1}
                )
                if exact:
                    return [exact[0]]
            return [french_tax_id]

        # Build invoice lines
        lines = []
        unmatched = []
        for l in inv.get("lignes", []):
            designation = l.get("designation", "")
            qty = l.get("quantite", 1)
            price = l.get("prix_unitaire_ht", 0)
            tva_pct = l.get("tva_pct", 0)
            tax_ids = get_tax_ids_for_line(tva_pct)

            # 1. Try mapping first
            mapped_id, facteur, note = find_product_in_mapping(designation)
            if mapped_id:
                real_qty = qty * facteur
                # Price per Odoo unit = price_per_invoice_unit / facteur
                # e.g. facteur=0.2 (5KG unit): qty=30, price=3.75/kg
                #   -> real_qty = 30*0.2 = 6 units of 5KG
                #   -> price_unit = 3.75/0.2 = 18.75 per 5KG unit
                real_price = price / facteur if facteur != 1 else price
                prod = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                    [[int(mapped_id)]], {"fields": ["id", "name"]})[0]
                line_vals = {
                    "product_id": int(mapped_id),
                    "name": prod["name"],
                    "quantity": real_qty,
                    "price_unit": real_price,
                }
                if tax_ids:
                    line_vals["tax_ids"] = [(6, 0, tax_ids)]
                lines.append([0, 0, line_vals])
                log.info("Mapped: %s -> %s qty=%s (x%s) tax=%s", designation, prod["name"], real_qty, facteur, tax_ids)
                continue

            # 2. Fallback: Odoo search
            product_id, product = find_product_by_name(models, uid, designation)
            if product_id:
                line_vals = {
                    "product_id": product_id,
                    "name": product["name"],
                    "quantity": qty,
                    "price_unit": price,
                }
                if tax_ids:
                    line_vals["tax_ids"] = [(6, 0, tax_ids)]
                lines.append([0, 0, line_vals])
            else:
                unmatched.append(designation)
                line_vals = {
                    "name": f"[A LIER] {designation}",
                    "quantity": qty,
                    "price_unit": price,
                }
                if tax_ids:
                    line_vals["tax_ids"] = [(6, 0, tax_ids)]
                lines.append([0, 0, line_vals])

        # Create vendor bill
        bill_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "create", [{
                "move_type": "in_invoice",
                "partner_id": partner_id or False,
                "ref": inv.get("reference", ""),
                "invoice_date": inv.get("date") or False,
                "invoice_date_due": inv.get("echeance") or False,
                "invoice_line_ids": lines,
            }]
        )
        log.info("Bill ID=%s — %d lines, %d unmatched", bill_id, len(lines), len(unmatched))
        return {"bill_id": bill_id, "lines": len(lines), "unmatched": unmatched, "partner_found": bool(partner_id)}

    except Exception as e:
        log.error("Invoice push error: %s", e)
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ── Mapping Tool ───────────────────────────────────────────────────────────────

MAPPING_FILE = os.path.join(os.path.dirname(__file__), "mapping.json")
MAPPING_ENV_KEY = "AFRICOMFORT_MAPPING"
DEFAULT_MAPPING = {"fournisseurs":{"BISKOLAK INTERNATIONAL SRL":435,"BISKOLAK INTERNATIONAL":435,"BISKOLAK":435,"SULTAN EXOTIC":433,"SULTAN EXOTIC FR09892180100":1072,"SULTAN EXOTIC - FR09892180100":1072,"DADA MARIAM OLABISI":1327,"DADA MARIAM":1327,"CINQ ETOILES DISTRIBUTION":434,"CINQ ETOILES":434,"SHAMA INTERNATIONAL":438,"SHAMA":438,"KOAS FOODS B.V.":440,"KOAS FOODS":440,"KOAS":440,"FOURNISSEUR SANS FACTURE MARCHE PARTICULIER":1012,"MARCHE PARTICULIER":1012,"KMI-MUSIC BANK":436,"KMI MUSIC BANK":436,"KMI":436,"ANJU ENTERPRISES":439,"ANJU":439,"LAMAISON FOODS":515,"LAMAISON":515,"BENNY FOOD PACKING":1019,"BENNY FOOD":1019,"BENNY":1019,"JMT-EURO":11,"JMT EURO":11,"JMT":11,"QUEENBEE FOOD":980,"QUEENBEE":980,"MKF GUINEA FRESH MULTISERVICES INTERNATIONAL B.V.":582,"MKF GUINEA FRESH MULTISERVICES INTERNATIONAL":582,"MKF GUINEA FRESH":582,"AFRICOMFORT FOODS INTERNATIONAL":444,"AFRICOMFORT FOODS":444,"AFRICOMFORT":444,"JABNET INVESTMENT COMPANY LTD":442,"JABNET INVESTMENT":442,"JABNET":442},"produits":{"POUNDO YAM EAGLE 20KG":{"odoo_id":4179,"odoo_name":"[FAR-032] Poundo Yam Eagle 20kg","facteur":1,"note":""},"POUNDO YAM EAGLE 5KG":{"odoo_id":4181,"odoo_name":"[FAR-033] Poundo Yam Eagle 5kg","facteur":1,"note":""},"PEAK MILK 170G":{"odoo_id":4205,"odoo_name":"[DEJ-025] Peak Milk (170g x 24)","facteur":1,"note":""},"CUBES MAGGI 60X10G":{"odoo_id":4183,"odoo_name":"[SEAS-004] Cubes- Maggi (60 x 10g)","facteur":1,"note":""},"TILAPIA FILLET 100G":{"odoo_id":4364,"odoo_name":"[VIPO-030] TILAPIA Fillet (100g)","facteur":1,"note":""},"VITAMILK BANANA 300ML":{"odoo_id":4410,"odoo_name":"[BOI-016] VITAMILK BANANA (300ML)","facteur":1,"note":""},"VITAMILK STRAWBERRY 300ML":{"odoo_id":4411,"odoo_name":"[BOI-018] VITAMILK STRAWBERRY (300ML)","facteur":1,"note":""},"INDOMIE CHICKEN":{"odoo_id":4213,"odoo_name":"[RINOU-004] Indomie (Chicken)","facteur":1,"note":""},"CHECKERS CUSTARD VANILLA 2KG":{"odoo_id":4228,"odoo_name":"[DEJ-009] Checkers - Custard Vanilla Flavour (2kg)","facteur":1,"note":""},"HOT SUYA SEASONING 100G":{"odoo_id":4407,"odoo_name":"[SEAS-007] Hot Suya Seasoning (100g)","facteur":1,"note":""},"CUSTARD POWDER LADY B 1.6KG":{"odoo_id":4273,"odoo_name":"[DEJ-011] Custard Powder Lady B (1.6Kg)","facteur":1,"note":""},"KILISHI 35G":{"odoo_id":4398,"odoo_name":"[GOU-005] KILISHI (35g)","facteur":1,"note":""},"MALTA GUINESS CAN 33CL":{"odoo_id":4442,"odoo_name":"[BOI-024] Malta Guiness Can (33cl x 24)","facteur":1,"note":""},"JOLLOF EGUSI SOUP SEASONING 100G":{"odoo_id":4408,"odoo_name":"[SEAS-008] Jollof Egusi Soup Seasoning (100g)","facteur":1,"note":""},"TRANSPORT":{"odoo_id":4306,"odoo_name":"Transport","facteur":1,"note":""},"CURRY POWDER 100G":{"odoo_id":4406,"odoo_name":"[SEAS-005] Curry Powder (100g)","facteur":1,"note":""},"PURE HEAVEN STRAWBERRY WHITE 750ML":{"odoo_id":4367,"odoo_name":"[BOI-023] Pure Heaven Drink Strawberry & White 12x750ml","facteur":1,"note":""},"VITAMILK ORIGINALE 300ML":{"odoo_id":4409,"odoo_name":"[BOI-017] VITAMILK ORIGINALE (300ML)","facteur":1,"note":""},"HYPER MALT 24 PACK":{"odoo_id":4188,"odoo_name":"[BOI-010] Hyper Malt (24 pack)","facteur":1,"note":""},"HONEY BEANS 5KG":{"odoo_id":4350,"odoo_name":"[FAR-020] Honey Beans (5kg)","facteur":1,"note":""},"POUNDO YAM EAGLE 10KG":{"odoo_id":4180,"odoo_name":"[FAR-031] Poundo Yam Eagle 10kg","facteur":1,"note":""},"EAGLE PALM JUICE 60CL":{"odoo_id":4234,"odoo_name":"[BOI-006] Eagle Palm Juice (60cl x 12)","facteur":1,"note":""},"GOLDEN MORN 300G":{"odoo_id":4167,"odoo_name":"[DEJ-016] Golden Morn 300g","facteur":1,"note":""},"GOLDEN MORN 600G":{"odoo_id":4166,"odoo_name":"[DEJ-018] Golden Morn 600g","facteur":1,"note":""},"GUINESS NIGERIA":{"odoo_id":4189,"odoo_name":"[BOI-009] Guiness (Nigeria)","facteur":1,"note":""},"LION DRIED THYMES":{"odoo_id":4187,"odoo_name":"[SEAS-015] Lion Dried Thymes x 1","facteur":1,"note":""},"PEAK MILK EVAPORATED 410G":{"odoo_id":4287,"odoo_name":"[DEJ-024] Peak Milk  Evaporated (410g x 24)","facteur":1,"note":""},"AGEGE BREAD":{"odoo_id":4264,"odoo_name":"[DEJ-001] Agege Bread","facteur":1,"note":""},"EAGLE PLANTAIN FLOUR 800G":{"odoo_id":4402,"odoo_name":"[FAR-014] Eagle Plantain Flour (800g)","facteur":1,"note":""},"INDOMIE ONION":{"odoo_id":4170,"odoo_name":"[RINOU-005] Indomie (Onion)","facteur":1,"note":""},"NIDO MILK POWDER 900G":{"odoo_id":4447,"odoo_name":"Nido Milk Powder (900G) 12PCS","facteur":1,"note":""},"FECULE POMME DE TERRE 25KG":{"odoo_id":4448,"odoo_name":"Fecule de pomme de terre (25kg)","facteur":1,"note":""},"ARACHIDE GRILLE TOGO BOUTEILLE":{"odoo_id":4449,"odoo_name":"ARACHIDE GRILLE TOGO BOUTEILLE","facteur":1,"note":""},"CHIPS PLANTAIN SALTED 85G":{"odoo_id":4450,"odoo_name":"Chips Plantain N1 Salted 85G","facteur":1,"note":""},"CHIPS PLANTAIN SPICY 85G":{"odoo_id":4405,"odoo_name":"[GOU-002] Chips Plantain N1 Spicy 85G","facteur":1,"note":""},"TOOBA GINGER GARLIC 330G":{"odoo_id":4451,"odoo_name":"TOOBA GINCER GARLIC (12X 330g)","facteur":1,"note":""},"OKRA GOMBO FRAIS HONRAS 5KG":{"odoo_id":4452,"odoo_name":"OKRA GOMBO FRAIS HONRAS 5KG","facteur":1,"note":""},"MACKEREL SMOKED":{"odoo_id":4392,"odoo_name":"[VIPO-028] mackerel smoked","facteur":1,"note":""},"MAGGI BOUILLON SHRIMP 60X10G":{"odoo_id":4453,"odoo_name":"MAGGI BOUILLON TABLEST SHRIMP (60X10G) 24PCS","facteur":1,"note":""},"PRAISE PALM PASTE BANGA":{"odoo_id":4250,"odoo_name":"[VEG-007] Praise Palm PASTE (Banga cream)","facteur":1,"note":""},"HUILE TOURNESOL 5L":{"odoo_id":4243,"odoo_name":"[HUI-005] Huile de tournesol 5L","facteur":1,"note":""},"MACKEREL TITUS NORWAY 20KG":{"odoo_id":4374,"odoo_name":"[VIPO-015] Mackerel Fish / Titus Norway (20kg)","facteur":20,"note":""},"MACKEREL TITUS IRELAND 5KG":{"odoo_id":4352,"odoo_name":"[VIPO-014] Mackerel Fish / Titus (5KG) Ireland","facteur":1,"note":""},"CATFISH 4KG":{"odoo_id":4248,"odoo_name":"[VIPO-005] Catfish 4KG","facteur":1,"note":""},"MERLUZA 5KG":{"odoo_id":4353,"odoo_name":"[VIPO-016] Merluza (5KG)","facteur":1,"note":""},"THOMSON CHILE SESE 5KG":{"odoo_id":4354,"odoo_name":"[VIPO-024] Thomson - Chile (Sese) 5Kg","facteur":1,"note":""},"BITTERLEAF NDOLE":{"odoo_id":4209,"odoo_name":"[VEG-001] Bitterleaf (NDOLE)","facteur":1,"note":""},"DINDE AILERON FRAIS RONSARD":{"odoo_id":4384,"odoo_name":"DINDE Aileron Frais Ronsard","facteur":1,"note":""},"POTATO STARCH FECULE 5KG":{"odoo_id":4176,"odoo_name":"[FAR-028] Potato Starch / Fecule de pomme de terre (5kg)","facteur":1,"note":""},"AILE DE POULET 10KG":{"odoo_id":4385,"odoo_name":"Aile De Poulet 10kg","facteur":1,"note":""},"BIRDS CUSTARD POWDER 500G":{"odoo_id":4381,"odoo_name":"[DEJ-002] Birds Custard Powder (500g)","facteur":1,"note":""},"FRESH YAMS 18KG 20KG":{"odoo_id":4211,"odoo_name":"[VEG-012] Fresh Yams 18kg/20KG","facteur":1,"note":""},"WILKI POULET HALA":{"odoo_id":4215,"odoo_name":"[VIPO-027] Wilki poulet Hala","facteur":1,"note":""},"BLACK TILAPIA FISH 4KG":{"odoo_id":4151,"odoo_name":"[VIPO-003] Black Tilapia Fish (4kg)","facteur":1,"note":""},"CAPITAINE FISH CROACKER 5KG":{"odoo_id":4231,"odoo_name":"[VIPO-004] Capitaine Fish (Croacker) (5kg)","facteur":1,"note":""},"PRESIDENT GOLDEN SELLA BASMATI 20KG":{"odoo_id":4203,"odoo_name":"[RINOU-010] President Golden Sella Parboiled BASMATI Rice 20KG","facteur":1,"note":""},"ASAFO POUNDED YAM 9KG":{"odoo_id":4156,"odoo_name":"[FAR-007] Asafo Pounded Yam (9kg)","facteur":1,"note":""},"RED TILAPIA FISH 4KG":{"odoo_id":4193,"odoo_name":"[VIPO-019] Red tilapia fish 4kg","facteur":1,"note":""},"CHECKER BANANA FLAVOUR 2KG":{"odoo_id":4241,"odoo_name":"[DEJ-007] Checker - Banana Flavour (2kg)","facteur":1,"note":""},"AKASH SELLA BASMATI 20KG":{"odoo_id":4200,"odoo_name":"[RINOU-001] Akash Sella Basmati (20KG)","facteur":1,"note":""},"AKASH SELLA BASMATI 5KG":{"odoo_id":4201,"odoo_name":"[RINOU-002] Akash Sella Basmati (5KG)","facteur":1,"note":""},"MAGGI CRAYFISH 60X10G":{"odoo_id":4375,"odoo_name":"[SEAS-018] Maggi Crayfish (60x10GM)","facteur":1,"note":""},"BOURN VITA 900G":{"odoo_id":4308,"odoo_name":"[DEJ-006] Bourn Vita (900G)","facteur":1,"note":""},"BOURN VITA 500G":{"odoo_id":4232,"odoo_name":"[DEJ-005] Bourn Vita (500g)","facteur":1,"note":""},"AFP FUFU POTATO FLAKES 10KG":{"odoo_id":4277,"odoo_name":"[FAR-001] AFP FUFU POTATO FLAKES / Puree 10kg","facteur":1,"note":""},"AFP FUFU POTATO FLAKES 5KG":{"odoo_id":4278,"odoo_name":"[FAR-002] AFP FUFU POTATO FLAKES / Puree 5kg","facteur":1,"note":""},"MACKEREL TITUS 1KG":{"odoo_id":4251,"odoo_name":"Mackerel Fish - Titus (1KG)","facteur":1,"note":""},"REMISE":{"odoo_id":4317,"odoo_name":"Remise","facteur":1,"note":""},"PUREE 5KG":{"odoo_id":4178,"odoo_name":"[FAR-035] Puree 5KG","facteur":1,"note":""},"SUNRISE GOLDEN SELLA BASMATI 5KG":{"odoo_id":4366,"odoo_name":"[RINOU-018] Sunrise Golden Selle Basmati Rice 5kg","facteur":1,"note":""},"SUNRISE GOLDEN SELLA BASMATI 10KG":{"odoo_id":4361,"odoo_name":"[RINOU-016] Sunrise Golden Sella Basmati Rice 10kg","facteur":1,"note":""},"SHAMA GOLDEN SELLA BASMATI 20KG":{"odoo_id":4195,"odoo_name":"[RINOU-014] Shama Golden Sella Basmati (20KG)","facteur":1,"note":""},"SHAMA GOLDEN SELLA BASMATI 10KG":{"odoo_id":4220,"odoo_name":"[RINOU-013] Shama Golden Sella Basmati (10KG)","facteur":1,"note":""},"SHAMA GOLDEN SELLA BASMATI 5KG":{"odoo_id":4196,"odoo_name":"[RINOU-015] Shama Golden Sella Basmati (5KG)","facteur":1,"note":""},"SHAMA MADRAS CURRY POWDER 1KG":{"odoo_id":4383,"odoo_name":"[SEAS-024] Shama Madras Curry Powder mild 1Kg","facteur":1,"note":""},"SUNRISE GOLDEN SELLA BASMATI 20KG":{"odoo_id":4365,"odoo_name":"[RINOU-017] Sunrise Golden Selle Basmati Rice 20kg","facteur":1,"note":""},"GIRA RICE FLOUR 5KG":{"odoo_id":4191,"odoo_name":"[FAR-019] Gira (5kg) / Rice flour","facteur":1,"note":""},"PALM OIL BON GUINEE 4.5L":{"odoo_id":4174,"odoo_name":"[HUI-008] Palm Oil Bon Guinee (4.5L)","facteur":1,"note":""},"KULIKULI 200G":{"odoo_id":4185,"odoo_name":"[GOU-007] Kulikuli (200G)","facteur":1,"note":""},"AFRICAN BEAUTY CHINCHIN 80G":{"odoo_id":4254,"odoo_name":"[GOU-001] African Beauty ChinChin 80 gram","facteur":1,"note":""},"STOCKFISH STEAKS 100G":{"odoo_id":4218,"odoo_name":"[VIPO-023] StockFish Steaks (100g)","facteur":1,"note":""},"POUNDED YAM 5KG":{"odoo_id":4379,"odoo_name":"[FAR-029] Pounded Yam (5KG)","facteur":1,"note":""},"PALM OIL NIGERIAN HERITAGE 2L":{"odoo_id":4369,"odoo_name":"[HUI-012] Palm Oil Nigerian Heritage 2Lt","facteur":1,"note":""},"AFRICAN BEAUTY PALM OIL 2L":{"odoo_id":4252,"odoo_name":"[HUI-001] African Beauty Palm oil 2L","facteur":1,"note":""},"BON GUINEE 2L":{"odoo_id":4240,"odoo_name":"[HUI-002] Bon Guinee 2L","facteur":1,"note":""},"GHANA HERITAGE PALM OIL 4.5L":{"odoo_id":4378,"odoo_name":"[HUI-003] Ghana Heritage Palm oil 4.5L","facteur":1,"note":""},"KNOR CHINESE CHICKEN POWDER 900G":{"odoo_id":4284,"odoo_name":"[SEAS-013] Knor Chinese Chicken Powder 900gram","facteur":1,"note":""},"OFADA RICE 900G":{"odoo_id":4302,"odoo_name":"[RINOU-024] ofada rice (900grm)","facteur":1,"note":""},"BLACK EYE BEANS 5KG":{"odoo_id":4380,"odoo_name":"[FAR-010] Black Eye Beans (5kg)","facteur":1,"note":""},"GARI WHITE 5KG":{"odoo_id":4347,"odoo_name":"[FAR-018] Gari White (5kg)","facteur":1,"note":""},"DUDU OSUN BLACK SOAP":{"odoo_id":4207,"odoo_name":"[COS-003] Dudu-Osun, Black soap","facteur":1,"note":""},"AFRICAN BEAUTY POUNDED YAM 20KG":{"odoo_id":4210,"odoo_name":"[FAR-004] African Beauty Pounded Yam (20kg)","facteur":1,"note":""},"AFRICAN BEAUTY POUNDO 4KG":{"odoo_id":4288,"odoo_name":"[FAR-005] African Beauty Poundo (4kg)","facteur":1,"note":""},"AFRICAN BEAUTY POUNDO 8KG":{"odoo_id":4239,"odoo_name":"[FAR-006] African Beauty Poundo (8kg)","facteur":1,"note":""},"TIGERNUT":{"odoo_id":4393,"odoo_name":"[GOU-008] Tigernut","facteur":1,"note":""},"FLAT FISH 100G":{"odoo_id":4221,"odoo_name":"[VIPO-011] Flat Fish (100g)","facteur":1,"note":""},"KNOR CHICKEN":{"odoo_id":4150,"odoo_name":"[SEAS-012] Knor (chicken)","facteur":1,"note":""},"OFADA RICE 5KG":{"odoo_id":4285,"odoo_name":"[RINOU-007] Ofada Rice 5kg","facteur":1,"note":""},"DRIED SMOKED CATFISH FILLETS 100G":{"odoo_id":4226,"odoo_name":"[VIPO-010] Dried Smoked Catfish Fillets (100g)","facteur":1,"note":""},"PEELED BEANS 1KG":{"odoo_id":4263,"odoo_name":"[FAR-023] Peeled beans (1kg)","facteur":1,"note":""},"COW STOMACH ESTOMAC ABODI":{"odoo_id":4233,"odoo_name":"[VIPO-001] Abodi (1kg)","facteur":12,"note":"1 carton = 12 KG"},"CHIPS PLANTAIN SWEET":{"odoo_id":4311,"odoo_name":"[GOU-003] Chips Plantain N1 Sweet 85G","facteur":1,"note":""},"MERLU HAKE":{"odoo_id":4353,"odoo_name":"[VIPO-016] Merluza (5KG)","facteur":0.2,"note":"30x1KG facture = 6x5KG Odoo"},"COW TRIPES SHAKI":{"odoo_id":4212,"odoo_name":"[VIPO-006] Cow trips - SHAKI (1KG)","facteur":12,"note":"1 carton = 12 KG"},"FANTA 50CL":{"odoo_id":4455,"odoo_name":"Fanta 50cl (x24)","facteur":1,"note":""},"FULL GOAT MEAT":{"odoo_id":4456,"odoo_name":"Full Goat Meat","facteur":1,"note":""},"PRESIDENT BASMATI 5KG":{"odoo_id":4457,"odoo_name":"President Golden Sella Parboiled BASMATI Rice 5KG","facteur":1,"note":""}}}

def save_mapping(data: dict):
    # Save to local file
    with open(MAPPING_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Also save to Odoo as a system parameter (persistent storage)
    try:
        models, uid = odoo_login()
        json_str = json.dumps(data, ensure_ascii=False)
        # Check if param exists
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "ir.config_parameter", "search",
            [[["key", "=", "africomfort.invoice.mapping"]]], {"limit": 1}
        )
        if existing:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "ir.config_parameter", "write",
                [existing, {"value": json_str}]
            )
        else:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "ir.config_parameter", "create",
                [{"key": "africomfort.invoice.mapping", "value": json_str}]
            )
        log.info("Mapping saved to Odoo system parameters")
    except Exception as e:
        log.warning("Could not save mapping to Odoo: %s", e)

def load_mapping() -> dict:
    # 1. Try Odoo system parameters (most persistent)
    try:
        models, uid = odoo_login()
        params = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "ir.config_parameter", "search_read",
            [[["key", "=", "africomfort.invoice.mapping"]]],
            {"fields": ["value"], "limit": 1}
        )
        if params and params[0].get("value"):
            data = json.loads(params[0]["value"])
            log.info("Mapping loaded from Odoo: %d suppliers, %d products",
                     len(data.get("fournisseurs",{})), len(data.get("produits",{})))
            return data
    except Exception as e:
        log.warning("Could not load mapping from Odoo: %s", e)
    # 2. Fallback to env var
    env_val = os.environ.get(MAPPING_ENV_KEY)
    if env_val:
        try:
            return json.loads(env_val)
        except Exception:
            pass
    # 3. Fallback to local file
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            return json.load(f)
    # Last resort: use hardcoded default mapping
    log.info("Using default hardcoded mapping")
    return DEFAULT_MAPPING

MAPPING_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AfriComfort — Mapping</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
:root{--bg:#0d0d0d;--sur:#161616;--sur2:#1f1f1f;--brd:#2a2a2a;--acc:#00e5a0;--acd:#00e5a015;--txt:#f0f0f0;--mut:#888;--dim:#555;--red:#ff4d4d;--orn:#f0a500;--mono:'IBM Plex Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;min-height:100vh;padding:32px}
h1{font-size:22px;font-weight:300;margin-bottom:6px}.h1 span{color:var(--acc);font-weight:600}
.sub{font-size:13px;color:var(--mut);margin-bottom:28px}
.tabs{display:flex;gap:4px;margin-bottom:24px;border-bottom:1px solid var(--brd);padding-bottom:0}
.tab{padding:10px 20px;font-size:13px;cursor:pointer;color:var(--mut);border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .2s}
.tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.section{display:none}.section.active{display:block}
.card{background:var(--sur);border:1px solid var(--brd);border-radius:10px;overflow:hidden;margin-bottom:16px}
.card-header{padding:12px 18px;background:var(--sur2);border-bottom:1px solid var(--brd);display:flex;align-items:center;justify-content:space-between}
.card-title{font-size:13px;font-weight:500}
.card-meta{font-size:11px;color:var(--mut);font-family:var(--mono)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 16px;font-size:10px;font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--brd);background:var(--sur2)}
td{padding:8px 16px;border-bottom:1px solid var(--brd);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--sur2)}
select,input[type=text],input[type=number]{background:var(--sur2);border:1px solid var(--brd);border-radius:5px;padding:5px 8px;color:var(--txt);font-size:12px;font-family:var(--mono);outline:none;transition:border-color .2s;width:100%}
select:focus,input:focus{border-color:var(--acc)}
.btn{padding:9px 20px;background:var(--acc);color:#000;border:none;border-radius:6px;font-family:var(--mono);font-size:12px;font-weight:500;cursor:pointer;transition:opacity .2s}
.btn:hover{opacity:.85}
.btn-sm{padding:5px 12px;background:transparent;border:1px solid var(--acc);color:var(--acc);border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer}
.btn-sm:hover{background:var(--acd)}
.btn-del{padding:4px 10px;background:transparent;border:1px solid var(--red);color:var(--red);border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-family:var(--mono);font-weight:500}
.badge-ok{background:#00e5a020;color:var(--acc)}
.badge-warn{background:#f0a50020;color:var(--orn)}
.badge-err{background:#ff4d4d20;color:var(--red)}
.log{background:var(--sur);border:1px solid var(--brd);border-radius:8px;padding:12px 16px;font-family:var(--mono);font-size:11px;color:var(--mut);max-height:80px;overflow-y:auto;margin-top:16px}
.ll{display:flex;gap:8px}.lt{color:var(--dim);flex-shrink:0}
.lok{color:var(--acc)}.lerr{color:var(--red)}.lwarn{color:var(--orn)}
.actions{display:flex;gap:8px;margin-bottom:20px;align-items:center}
.search{background:var(--sur2);border:1px solid var(--brd);border-radius:6px;padding:8px 12px;color:var(--txt);font-size:13px;outline:none;width:280px}
.search:focus{border-color:var(--acc)}
.hidden{display:none!important}
.facteur-wrap{display:flex;gap:6px;align-items:center}
.facteur-wrap input{width:60px}
.facteur-wrap span{font-size:11px;color:var(--mut);white-space:nowrap}
</style>
</head>
<body>
<h1><span>AfriComfort</span> — Mapping fournisseurs & produits</h1>
<div class="sub">Associe chaque nom de facture à un fournisseur/produit Odoo. Définis le conditionnement une fois pour toutes.</div>

<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <div class="tabs" style="margin-bottom:0;border-bottom:none">
    <div class="tab active" onclick="showTab('suppliers')">Fournisseurs</div>
    <div class="tab" onclick="showTab('products')">Produits & conditionnement</div>
  </div>
  <button class="btn" onclick="learnFromOdoo()" id="btn-learn">🧠 Apprendre depuis Odoo</button>
</div>
<div style="border-bottom:1px solid var(--brd);margin-bottom:24px"></div>

<!-- SUPPLIERS -->
<div class="section active" id="tab-suppliers">
  <div class="actions">
    <input class="search" id="s-search" placeholder="Filtrer fournisseurs..." oninput="filterTable('s-table',this.value)"/>
    <button class="btn-sm" onclick="addSupplierRow()">+ Ajouter</button>
    <button class="btn" onclick="saveAll()">↑ Sauvegarder tout</button>
  </div>
  <div class="card">
    <div class="card-header">
      <div class="card-title">Noms sur factures → Fournisseur Odoo</div>
      <div class="card-meta" id="s-count"></div>
    </div>
    <table>
      <thead><tr><th>Nom sur la facture</th><th>Fournisseur Odoo</th><th>Statut</th><th></th></tr></thead>
      <tbody id="s-table"></tbody>
    </table>
  </div>
</div>

<!-- PRODUCTS -->
<div class="section" id="tab-products">
  <div class="actions">
    <input class="search" id="p-search" placeholder="Filtrer produits..." oninput="filterTable('p-table',this.value)"/>
    <button class="btn-sm" onclick="addProductRow()">+ Ajouter</button>
    <button class="btn" onclick="saveAll()">↑ Sauvegarder tout</button>
  </div>
  <div class="card">
    <div class="card-header">
      <div class="card-title">Noms sur factures → Produit Odoo + conditionnement</div>
      <div class="card-meta" id="p-count"></div>
    </div>
    <table>
      <thead><tr><th>Nom sur la facture</th><th>Produit Odoo</th><th>Conditionnement (×)</th><th>Note</th><th></th></tr></thead>
      <tbody id="p-table"></tbody>
    </table>
  </div>
</div>

<div class="log" id="log"><div class="ll"><span class="lt">--:--:--</span><span>Chargement des données Odoo...</span></div></div>

<script>
let odooPartners = [];
let odooProducts = [];
let mapping = {fournisseurs:{}, produits:{}};

const $=id=>document.getElementById(id);
function log(m,t=''){const l=$('log'),n=document.createElement('div'),now=new Date().toLocaleTimeString('fr-FR');n.className='ll';n.innerHTML=`<span class="lt">${now}</span><span class="${t?'l'+t:''}">${m}</span>`;l.appendChild(n);l.scrollTop=l.scrollHeight}

function showTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',['suppliers','products'][i]===t));
  document.querySelectorAll('.section').forEach(el=>el.classList.remove('active'));
  $(`tab-${t}`).classList.add('active');
}

function filterTable(tableId, q){
  const rows = document.querySelectorAll(`#${tableId} tr`);
  rows.forEach(r=>{r.style.display=r.textContent.toLowerCase().includes(q.toLowerCase())?'':'none'});
}

async function init(){
  try{
    const [pd, md] = await Promise.all([
      fetch('/mapping/odoo-data').then(r=>r.json()),
      fetch('/mapping/load').then(r=>r.json())
    ]);
    odooPartners = pd.partners || [];
    odooProducts = pd.products || [];
    mapping = md;
    log(`Chargé : ${odooPartners.length} fournisseurs, ${odooProducts.length} produits Odoo`,'ok');
    renderSuppliers();
    renderProducts();
  }catch(e){log(`Erreur chargement : ${e.message}`,'err')}
}

// ── SUPPLIERS ──────────────────────────────────────────────────────────────

function partnerOptions(selectedId){
  return `<option value="">— Sélectionner —</option>` +
    odooPartners.map(p=>`<option value="${p.id}" ${p.id==selectedId?'selected':''}>${p.name}</option>`).join('');
}

function renderSuppliers(){
  const tb = $('s-table'); tb.innerHTML='';
  const entries = Object.entries(mapping.fournisseurs);
  $('s-count').textContent = `${entries.length} entrée(s)`;
  entries.forEach(([factureName, partnerId])=>{
    const partner = odooPartners.find(p=>p.id==partnerId);
    const tr = document.createElement('tr');
    tr.dataset.key = factureName;
    tr.innerHTML = `
      <td><input type="text" value="${factureName}" onchange="renameSupplier(this,'${factureName}')" style="width:220px"/></td>
      <td><select onchange="updateSupplier('${factureName}',this.value)">${partnerOptions(partnerId)}</select></td>
      <td>${partner?`<span class="badge badge-ok">✓ ${partner.name}</span>`:`<span class="badge badge-err">Non trouvé (id:${partnerId})</span>`}</td>
      <td><button class="btn-del" onclick="deleteSupplier('${factureName}')">✕</button></td>`;
    tb.appendChild(tr);
  });
}

function addSupplierRow(){
  const key = prompt('Nom exact sur la facture (ex: SAS SULTAN EXOTIC):');
  if(!key||!key.trim())return;
  mapping.fournisseurs[key.trim()]='';
  renderSuppliers();
}

function renameSupplier(input, oldKey){
  const newKey = input.value.trim();
  if(!newKey||newKey===oldKey)return;
  const val = mapping.fournisseurs[oldKey];
  delete mapping.fournisseurs[oldKey];
  mapping.fournisseurs[newKey] = val;
  renderSuppliers();
}

function updateSupplier(key, val){
  mapping.fournisseurs[key] = val ? parseInt(val) : '';
  renderSuppliers();
}

function deleteSupplier(key){
  if(!confirm(`Supprimer "${key}" ?`))return;
  delete mapping.fournisseurs[key];
  renderSuppliers();
}

// ── PRODUCTS ───────────────────────────────────────────────────────────────

function productOptions(selectedId){
  return `<option value="">— Sélectionner —</option>` +
    odooProducts.map(p=>`<option value="${p.id}" ${p.id==selectedId?'selected':''}>${p.name}</option>`).join('');
}

function renderProducts(){
  const tb = $('p-table'); tb.innerHTML='';
  const entries = Object.entries(mapping.produits);
  $('p-count').textContent = `${entries.length} entrée(s)`;
  entries.forEach(([factureName, cfg])=>{
    const product = odooProducts.find(p=>p.id==cfg.odoo_id);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="text" value="${factureName}" onchange="renameProduct(this,'${factureName}')" style="width:200px"/></td>
      <td><select onchange="updateProduct('${factureName}','odoo_id',parseInt(this.value)||'')">${productOptions(cfg.odoo_id)}</select></td>
      <td>
        <div class="facteur-wrap">
          <input type="number" min="1" value="${cfg.facteur||1}" onchange="updateProduct('${factureName}','facteur',parseFloat(this.value)||1)"/>
          <span>× qté facture = qté Odoo</span>
        </div>
      </td>
      <td><input type="text" value="${cfg.note||''}" placeholder="ex: 1 carton = 12 KG" onchange="updateProduct('${factureName}','note',this.value)" style="width:200px"/></td>
      <td><button class="btn-del" onclick="deleteProduct('${factureName}')">✕</button></td>`;
    tb.appendChild(tr);
  });
}

function addProductRow(){
  const key = prompt('Mot-clé sur la facture (ex: COW STOMACH):');
  if(!key||!key.trim())return;
  mapping.produits[key.trim()]={odoo_id:'',facteur:1,note:''};
  renderProducts();
}

function renameProduct(input, oldKey){
  const newKey = input.value.trim();
  if(!newKey||newKey===oldKey)return;
  const val = mapping.produits[oldKey];
  delete mapping.produits[oldKey];
  mapping.produits[newKey] = val;
  renderProducts();
}

function updateProduct(key, field, val){
  if(!mapping.produits[key]) mapping.produits[key]={odoo_id:'',facteur:1,note:''};
  mapping.produits[key][field] = val;
}

function deleteProduct(key){
  if(!confirm(`Supprimer "${key}" ?`))return;
  delete mapping.produits[key];
  renderProducts();
}

// ── SAVE ───────────────────────────────────────────────────────────────────

async function saveAll(){
  try{
    const r = await fetch('/mapping/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(mapping)});
    const d = await r.json();
    if(d.error)throw new Error(d.error);
    log(`Mapping sauvegardé — ${Object.keys(mapping.fournisseurs).length} fournisseurs, ${Object.keys(mapping.produits).length} produits`,'ok');
  }catch(e){log(`Erreur sauvegarde : ${e.message}`,'err')}
}

async function learnFromOdoo(){
  const btn = $('btn-learn');
  btn.disabled=true;
  btn.textContent='⏳ Analyse en cours...';
  log('Lecture des factures Odoo — cela peut prendre 30 secondes...','warn');
  try{
    const r = await fetch('/mapping/learn');
    const d = await r.json();
    if(d.error)throw new Error(d.error);
    log(`✓ ${d.bills_analyzed} factures analysées — ${d.suppliers_found} fournisseurs, ${d.mapping_products} produits mappés`,'ok');
    log('Rechargement du mapping...','warn');
    const md = await fetch('/mapping/load').then(r=>r.json());
    mapping = md;
    renderSuppliers();
    renderProducts();
    log('Mapping mis à jour — vérifie et ajuste les conditionnements','ok');
  }catch(e){
    log(`Erreur : ${e.message}`,'err');
  }finally{
    btn.disabled=false;
    btn.textContent='🧠 Apprendre depuis Odoo';
  }
}

init();
</script>
</body>
</html>"""


@app.get("/mapping", response_class=HTMLResponse)
async def mapping_ui():
    return HTMLResponse(content=MAPPING_HTML)


@app.get("/mapping/odoo-data")
async def mapping_odoo_data():
    """Return all suppliers (is_company or supplier_rank>0) and products from Odoo."""
    try:
        models, uid = odoo_login()

        # All partners with supplier rank > 0
        partner_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
            [[["supplier_rank", ">", 0]]], {"limit": 500}
        )
        partners = []
        if partner_ids:
            partners = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                [partner_ids], {"fields": ["id", "name"]}
            )
            partners = sorted(partners, key=lambda p: p["name"])

        # All active products (purchasable)
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["active", "=", True], ["purchase_ok", "=", True]]], {"limit": 1000}
        )
        products = []
        if product_ids:
            products = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [product_ids], {"fields": ["id", "name"]}
            )
            products = sorted(products, key=lambda p: p["name"])

        log.info("Mapping data: %d partners, %d products", len(partners), len(products))
        return {"partners": partners, "products": products}

    except Exception as e:
        log.error("mapping_odoo_data error: %s", e)
        return {"error": str(e), "partners": [], "products": []}


@app.get("/mapping/load")
async def mapping_load():
    """Return current mapping file."""
    return load_mapping()


@app.post("/mapping/save")
async def mapping_save(request: Request):
    """Save mapping file."""
    try:
        data = await request.json()
        save_mapping(data)
        log.info("Mapping saved: %d suppliers, %d products",
                  len(data.get("fournisseurs", {})), len(data.get("produits", {})))
        return {"ok": True}
    except Exception as e:
        log.error("mapping_save error: %s", e)
        return {"error": str(e)}

@app.get("/mapping/learn")
async def mapping_learn():
    """
    Read all validated vendor bills from Odoo and build a mapping
    by analyzing supplier → product associations and quantities.
    Then use Claude to suggest invoice name → Odoo product mappings.
    """
    try:
        models, uid = odoo_login()
        log.info("Learning from Odoo vendor bills...")

        # 1. Fetch all validated vendor bills (last 500)
        bill_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "search",
            [[["move_type", "=", "in_invoice"], ["state", "=", "posted"]]], 
            {"limit": 500, "order": "invoice_date desc"}
        )
        if not bill_ids:
            return {"error": "Aucune facture validée trouvée dans Odoo"}

        bills = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "read",
            [bill_ids], {"fields": ["id", "ref", "partner_id", "invoice_line_ids", "invoice_date"]}
        )
        log.info("Found %d validated bills", len(bills))

        # 2. Collect all line IDs and fetch them
        all_line_ids = []
        for b in bills:
            all_line_ids.extend(b.get("invoice_line_ids", []))

        lines = []
        if all_line_ids:
            lines = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "account.move.line", "read",
                [all_line_ids],
                {"fields": ["id", "move_id", "product_id", "name", "quantity", "price_unit"]}
            )

        # 3. Build bill_id → partner map
        bill_partner = {b["id"]: b["partner_id"] for b in bills}

        # 4. Aggregate: partner → products with avg qty
        from collections import defaultdict
        partner_products = defaultdict(lambda: defaultdict(list))
        product_map = {}  # product_id -> name

        for line in lines:
            if not line.get("product_id"):
                continue
            pid = line["product_id"][0]
            pname = line["product_id"][1]
            product_map[pid] = pname
            move_id = line["move_id"][0]
            partner = bill_partner.get(move_id)
            if partner:
                partner_id = partner[0]
                partner_name = partner[1]
                partner_products[(partner_id, partner_name)][pid].append(line["quantity"])

        # 5. Build summary for Claude — process in batches to avoid token limits
        summary_lines = []
        for (partner_id, partner_name), products in partner_products.items():
            prod_list = []
            for pid, qtys in list(products.items())[:30]:  # max 30 products per supplier
                avg_qty = round(sum(qtys) / len(qtys), 1)
                prod_list.append(f"{product_map[pid]} (id:{pid}, qty:{avg_qty})")
            summary_lines.append(f"{partner_name} (id:{partner_id}): " + " | ".join(prod_list))

        summary = "\n".join(summary_lines)
        log.info("Built summary for %d suppliers, summary length: %d chars", len(partner_products), len(summary))

        # 6. Ask Claude to generate the mapping JSON
        prompt = f"""Tu es un assistant qui génère un fichier de mapping JSON.

Voici les fournisseurs et produits réels dans Odoo (extraits de factures validées) :
{summary}

Génère UNIQUEMENT un objet JSON valide, sans markdown, sans texte avant ou après.
Format exact :
{{
  "fournisseurs": {{
    "NOM FACTURE VARIANTE 1": <partner_id as integer>,
    "NOM FACTURE VARIANTE 2": <partner_id as integer>
  }},
  "produits": {{
    "MOT CLE FACTURE": {{
      "odoo_id": <product_id as integer>,
      "odoo_name": "nom exact dans Odoo",
      "facteur": 1,
      "note": ""
    }}
  }}
}}

Règles strictes :
- Les IDs doivent être des entiers, pas des strings
- Pour chaque fournisseur, génère 2-3 variantes du nom (avec/sans SAS/SARL/SRL/INTERNATIONAL)
- Pour chaque produit, le mot-clé doit être le mot distinctif en MAJUSCULES tel qu'il apparaît sur une facture
- facteur = 1 par défaut
- Retourne un JSON complet et valide"""

        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        # Ensure JSON is complete
        if not raw.endswith("}"):
            # Find last complete entry and close properly
            last_brace = raw.rfind("}")
            if last_brace > 0:
                raw = raw[:last_brace+1]
                # Count braces to close
                opens = raw.count("{") - raw.count("}")
                raw += "}" * opens
        suggested_mapping = json.loads(raw)

        # 7. Merge with existing mapping (don't overwrite manual entries)
        existing = load_mapping()
        for k, v in suggested_mapping.get("fournisseurs", {}).items():
            if k not in existing["fournisseurs"]:
                existing["fournisseurs"][k] = v
        for k, v in suggested_mapping.get("produits", {}).items():
            if k not in existing["produits"]:
                existing["produits"][k] = v

        save_mapping(existing)
        log.info("Mapping learned: %d suppliers, %d products",
                 len(existing["fournisseurs"]), len(existing["produits"]))

        return {
            "ok": True,
            "bills_analyzed": len(bills),
            "lines_analyzed": len(lines),
            "suppliers_found": len(partner_products),
            "mapping_suppliers": len(existing["fournisseurs"]),
            "mapping_products": len(existing["produits"])
        }

    except Exception as e:
        log.error("mapping_learn error: %s", e)
        return {"error": str(e)}


# ── Catalogue PDF ──────────────────────────────────────────────────────────────

from fastapi.responses import StreamingResponse
import io
import base64
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, PageBreak, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import HRFlowable
from collections import defaultdict

BRAND_DARK  = colors.HexColor("#1A1A1A")
BRAND_GREEN = colors.HexColor("#C9A84C")
BRAND_LIGHT = colors.HexColor("#FAF7F2")
BRAND_GRAY  = colors.HexColor("#F5EDE6")
TEXT_DARK   = colors.HexColor("#2A1A0A")
TEXT_MUTED  = colors.HexColor("#8B6B4A")
BRAND_BROWN = colors.HexColor("#5C2E0A")
LOGO_DARK_B64 = "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"
LOGO_LIGHT_B64 = "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"


CATEGORY_LABELS = {
    "FAR": ("Farines & Féculents", "Flours & Starches"),
    "VIPO": ("Viandes & Poissons", "Meat & Fish"),
    "BOI": ("Boissons", "Beverages"),
    "SEAS": ("Épices & Assaisonnements", "Spices & Seasonings"),
    "GOU": ("Snacks & Gourmandises", "Snacks & Treats"),
    "DEJ": ("Petit-Déjeuner", "Breakfast"),
    "VEG": ("Légumes & Frais", "Vegetables & Fresh"),
    "HUI": ("Huiles", "Oils"),
    "COS": ("Cosmétiques", "Cosmetics"),
    "RINOU": ("Riz & Nouilles", "Rice & Noodles"),
    "AUTRE": ("Autres Produits", "Other Products"),
}

CAT_ORDER = ["RINOU", "FAR", "VIPO", "BOI", "SEAS", "GOU", "DEJ", "VEG", "HUI", "COS", "AUTRE"]


def get_cat_prefix(ref):
    if not ref or not isinstance(ref, str):
        return "AUTRE"
    ref = ref.strip("[]")
    for prefix in CAT_ORDER:
        if ref.upper().startswith(prefix):
            return prefix
    return "AUTRE"


# Products to exclude from catalogue
CATALOGUE_EXCLUDED = [
    "beans powder",
    "smoked catfish",
    "smocked catfish",
    "dried snail",
    "jekomo",
    "orijin",
]

def fetch_products_with_images(models, uid):
    """Fetch all active products with images from Odoo, excluding no-photo and blacklisted."""
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search",
        [[["active", "=", True], ["sale_ok", "=", True]]],
        {"limit": 500}
    )
    if not ids:
        return []
    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read",
        [ids],
        {"fields": ["id", "name", "default_code", "image_128", "categ_id"]}
    )
    filtered = []
    for p in products:
        # Exclude products without photo
        img = p.get("image_128")
        if not img or not isinstance(img, str) or len(img) < 100:
            log.info("Catalogue: excluded (no photo) — %s", p.get("name"))
            continue
        # Exclude blacklisted products
        name_lower = (p.get("name") or "").lower()
        excluded = False
        for excl in CATALOGUE_EXCLUDED:
            if excl in name_lower:
                log.info("Catalogue: excluded (blacklist) — %s", p.get("name"))
                excluded = True
                break
        if not excluded:
            filtered.append(p)
    log.info("Catalogue: %d products after filtering (from %d)", len(filtered), len(products))
    return filtered


def logo_image(b64_str, width_mm, height_mm):
    """Create a ReportLab Image from base64 string."""
    img_bytes = base64.b64decode(b64_str)
    buf = io.BytesIO(img_bytes)
    return RLImage(buf, width=width_mm*mm, height=height_mm*mm)


def make_cover(styles):
    """Generate branded cover page with AfriComfort logo."""
    elems = []
    elems.append(Spacer(1, 20*mm))

    # Logo on cover — use light JPG (white background, clean)
    try:
        logo = logo_image(LOGO_LIGHT_B64, 90, 75)
        logo.hAlign = "CENTER"
        elems.append(logo)
    except Exception:
        elems.append(Paragraph(
            '<b>AFRICOMFORT FOODS</b>',
            ParagraphStyle("cover_fallback", fontSize=36, alignment=TA_CENTER,
                           fontName="Helvetica-Bold")
        ))

    elems.append(Spacer(1, 8*mm))

    # Gold divider
    elems.append(HRFlowable(width="50%", thickness=2,
                             color=BRAND_GREEN, spaceAfter=4*mm, hAlign="CENTER"))

    elems.append(Paragraph(
        '<font color="#5C2E0A">Catalogue Produits / Product Catalogue</font>',
        ParagraphStyle("cover_sub", fontSize=20, alignment=TA_CENTER,
                       fontName="Helvetica-Bold", leading=26)
    ))
    elems.append(Spacer(1, 3*mm))
    elems.append(Paragraph(
        "Épicerie africaine & caribéenne / African & Caribbean Grocery",
        ParagraphStyle("cover_desc", fontSize=12, alignment=TA_CENTER,
                       fontName="Helvetica", textColor=TEXT_MUTED, leading=18)
    ))
    elems.append(Spacer(1, 3*mm))
    elems.append(Paragraph(
        "2026",
        ParagraphStyle("cover_year", fontSize=14, alignment=TA_CENTER,
                       fontName="Helvetica", textColor=TEXT_MUTED)
    ))

    elems.append(HRFlowable(width="50%", thickness=1,
                             color=BRAND_GREEN, spaceBefore=4*mm, spaceAfter=10*mm, hAlign="CENTER"))

    # Contact block with brand styling
    contact_data = [
        ["AfriComfort Foods International"],
        ["www.africomfortfoods.com"],
        ["africomfortfoods@gmail.com"],
        ["+33 6 60 56 51 29"],
        ["Athis-Mons, France"],
    ]
    contact_table = Table(contact_data, colWidths=[120*mm])
    contact_table.setStyle(TableStyle([
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("FONTNAME", (0,0), (0,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (0,0), 13),
        ("FONTSIZE", (0,1), (-1,-1), 11),
        ("TEXTCOLOR", (0,0), (0,0), BRAND_BROWN),
        ("TEXTCOLOR", (0,1), (-1,-1), TEXT_MUTED),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    elems.append(contact_table)
    elems.append(PageBreak())
    return elems


def make_conditions(styles):
    """Generate commercial conditions page."""
    elems = []
    elems.append(Paragraph(
        "Conditions Commerciales / Commercial Terms",
        ParagraphStyle("sec_title", fontSize=18, fontName="Helvetica-Bold",
                       textColor=BRAND_DARK, spaceAfter=6*mm)
    ))
    elems.append(HRFlowable(width="100%", thickness=1.5, color=BRAND_GREEN,
                             spaceAfter=8*mm))

    terms = [
        ("🕐  Délais de livraison / Delivery", 
         "Paris & Île-de-France : 24-48h\nFrance : 48-72h\nSur devis pour commandes Europe / Europe on request"),
        ("📦  Commande minimum / Minimum order",
         "Commande minimum : 2 000€ HT\nMinimum order: €2,000 excl. VAT"),
        ("💶  Paiement / Payment",
         "Nouveaux clients : paiement à la commande\nClients établis : conditions sur accord commercial\nNew clients: payment on order\nEstablished clients: terms by agreement"),
        ("🔄  Retours / Returns",
         "Produits non conformes uniquement, sous 48h après livraison\nNon-conforming products only, within 48h of delivery"),
        ("📋  Tarifs / Pricing",
         "Tarifs sur demande — contact commercial\nPrices on request — contact our sales team\nafriacomfortfoods@gmail.com"),
    ]

    for title, body in terms:
        elems.append(Paragraph(
            f"<b>{title}</b>",
            ParagraphStyle("term_title", fontSize=12, fontName="Helvetica-Bold",
                           textColor=BRAND_DARK, spaceBefore=6*mm, spaceAfter=2*mm)
        ))
        elems.append(Paragraph(
            body.replace("\n", "<br/>"),
            ParagraphStyle("term_body", fontSize=10, fontName="Helvetica",
                           textColor=TEXT_DARK, leading=16, leftIndent=10)
        ))

    elems.append(PageBreak())
    return elems


def build_catalogue_pdf(products):
    """Build the full catalogue PDF and return bytes."""
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4

    def header_footer(canvas, doc):
        canvas.saveState()

        # Header — cream background with brown accent
        canvas.setFillColor(BRAND_LIGHT)
        canvas.rect(0, PAGE_H - 20*mm, PAGE_W, 20*mm, fill=1, stroke=0)

        # Gold top line
        canvas.setFillColor(BRAND_GREEN)
        canvas.rect(0, PAGE_H - 1.5*mm, PAGE_W, 1.5*mm, fill=1, stroke=0)

        # Brown bottom line
        canvas.setFillColor(BRAND_BROWN)
        canvas.rect(0, PAGE_H - 20*mm, PAGE_W, 1*mm, fill=1, stroke=0)

        # Logo in header — use light JPG (white background)
        try:
            img_bytes = base64.b64decode(LOGO_LIGHT_B64)
            img_buf = io.BytesIO(img_bytes)
            canvas.drawImage(img_buf, 8*mm, PAGE_H - 19*mm,
                           width=22*mm, height=18*mm,
                           preserveAspectRatio=True, mask=None)
        except Exception:
            canvas.setFillColor(BRAND_BROWN)
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawString(15*mm, PAGE_H - 12*mm, "Africomfort Foods")

        # Header right text only — no duplicate name
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(TEXT_MUTED)
        canvas.drawRightString(PAGE_W - 12*mm, PAGE_H - 11*mm,
                               "Catalogue 2026 — Épicerie africaine & caribéenne")
        canvas.drawRightString(PAGE_W - 12*mm, PAGE_H - 16*mm,
                               "African & Caribbean Grocery")

        # Footer — brown bar
        canvas.setFillColor(BRAND_BROWN)
        canvas.rect(0, 0, PAGE_W, 10*mm, fill=1, stroke=0)
        # Gold top line on footer
        canvas.setFillColor(BRAND_GREEN)
        canvas.rect(0, 10*mm, PAGE_W, 1*mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(12*mm, 3.5*mm, "www.africomfortfoods.com  |  africomfortfoods@gmail.com  |  +33 6 60 56 51 29")
        canvas.drawRightString(PAGE_W - 12*mm, 3.5*mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=24*mm, bottomMargin=16*mm
    )
    styles = getSampleStyleSheet()
    story = []

    # Cover
    story += make_cover(styles)

    # Conditions
    story += make_conditions(styles)

    # Group by category
    by_cat = defaultdict(list)
    for p in products:
        prefix = get_cat_prefix(p.get("default_code", ""))
        by_cat[prefix].append(p)

    cat_style = ParagraphStyle(
        "cat_header", fontSize=16, fontName="Helvetica-Bold",
        textColor=colors.white, alignment=TA_LEFT, leading=20
    )
    ref_style = ParagraphStyle(
        "ref", fontSize=7.5, fontName="Helvetica",
        textColor=TEXT_MUTED, leading=10
    )
    name_style = ParagraphStyle(
        "name", fontSize=9.5, fontName="Helvetica-Bold",
        textColor=TEXT_DARK, leading=13
    )
    name_en_style = ParagraphStyle(
        "name_en", fontSize=8.5, fontName="Helvetica",
        textColor=TEXT_MUTED, leading=12
    )

    IMG_SIZE = 28*mm
    COL_W = [IMG_SIZE + 2*mm, 0]  # will be computed below
    COLS = 4
    CELL_W = (PAGE_W - 24*mm) / COLS

    for prefix in CAT_ORDER:
        if prefix == "AUTRE":
            continue  # Never show "Autres Produits" in catalogue
        prods = by_cat.get(prefix, [])
        if not prods:
            continue

        labels = CATEGORY_LABELS.get(prefix, (prefix, prefix))
        fr_label, en_label = labels

        # Category header
        cat_table = Table(
            [[Paragraph(f"{fr_label}  /  {en_label}", cat_style)]],
            colWidths=[PAGE_W - 24*mm]
        )
        cat_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), BRAND_BROWN),
            ("TOPPADDING", (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ]))
        story.append(Spacer(1, 4*mm))
        story.append(cat_table)
        story.append(Spacer(1, 4*mm))

        # Products grid — 4 per row
        prods_sorted = sorted(prods, key=lambda x: (x.get("default_code") or x.get("name") or ""))
        row_data = []
        current_row = []

        for prod in prods_sorted:
            ref = prod.get("default_code") or ""
            name = prod.get("name") or ""
            img_b64 = prod.get("image_128") or ""
            if not isinstance(ref, str): ref = ""
            if not isinstance(name, str): name = ""
            if not isinstance(img_b64, str): img_b64 = ""

            # Build image
            if img_b64:
                try:
                    img_bytes = base64.b64decode(img_b64)
                    img_buf = io.BytesIO(img_bytes)
                    img = RLImage(img_buf, width=IMG_SIZE, height=IMG_SIZE)
                except Exception:
                    img = Paragraph("", name_style)
            else:
                img = Paragraph("", name_style)

            cell = Table(
                [[img], [Paragraph(ref, ref_style)], [Paragraph(name, name_style)]],
                colWidths=[CELL_W - 4*mm]
            )
            cell.setStyle(TableStyle([
                ("ALIGN", (0,0), (-1,-1), "CENTER"),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("TOPPADDING", (0,0), (-1,-1), 3),
                ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                ("LEFTPADDING", (0,0), (-1,-1), 2),
                ("RIGHTPADDING", (0,0), (-1,-1), 2),
                ("BACKGROUND", (0,0), (-1,-1), colors.white),
                ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#EEEEEE")),
                ("ROUNDEDCORNERS", [4]),
            ]))
            current_row.append(cell)

            if len(current_row) == COLS:
                row_data.append(current_row)
                current_row = []

        # Pad last row
        while len(current_row) > 0 and len(current_row) < COLS:
            current_row.append(Paragraph("", name_style))
        if current_row:
            row_data.append(current_row)

        if row_data:
            grid = Table(row_data, colWidths=[CELL_W] * COLS,
                         repeatRows=0)
            grid.setStyle(TableStyle([
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("TOPPADDING", (0,0), (-1,-1), 3),
                ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                ("LEFTPADDING", (0,0), (-1,-1), 2),
                ("RIGHTPADDING", (0,0), (-1,-1), 2),
            ]))
            story.append(grid)
            story.append(Spacer(1, 4*mm))

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    buf.seek(0)
    return buf.read()


@app.get("/catalogue/pdf")
async def catalogue_pdf():
    """Generate and return the product catalogue as PDF."""
    try:
        models, uid = odoo_login()
        log.info("Fetching products for catalogue...")
        products = fetch_products_with_images(models, uid)
        log.info("Generating PDF for %d products...", len(products))
        pdf_bytes = build_catalogue_pdf(products)
        log.info("Catalogue PDF generated: %d bytes", len(pdf_bytes))
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=AfriComfort_Catalogue_2026.pdf"}
        )
    except Exception as e:
        log.error("catalogue_pdf error: %s", e)
        return {"error": str(e)}

# ── Créer réceptions depuis factures fournisseurs ──────────────────────────────

RECEPTION_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AfriComfort — Réceptions depuis factures</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
:root{--bg:#0d0d0d;--sur:#161616;--sur2:#1f1f1f;--brd:#2a2a2a;--acc:#00e5a0;--acd:#00e5a015;--txt:#f0f0f0;--mut:#888;--dim:#555;--red:#ff4d4d;--orn:#f0a500;--mono:'IBM Plex Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;min-height:100vh;padding:32px}
h1{font-size:22px;font-weight:300;margin-bottom:6px}
h1 span{color:var(--acc);font-weight:600}
.sub{font-size:13px;color:var(--mut);margin-bottom:24px}
.controls{display:flex;gap:12px;align-items:flex-end;margin-bottom:24px;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12px;color:var(--mut);font-family:var(--mono)}
.field input{background:var(--sur2);border:1px solid var(--brd);border-radius:6px;padding:8px 12px;color:var(--txt);font-family:var(--mono);font-size:13px;outline:none;transition:border-color .2s}
.field input:focus{border-color:var(--acc)}
.btn{padding:10px 20px;background:var(--acc);color:#000;border:none;border-radius:6px;font-family:var(--mono);font-size:12px;font-weight:500;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-sec{background:transparent;border:1px solid var(--acc);color:var(--acc)}
.btn-sec:hover{background:var(--acd)}
.card{background:var(--sur);border:1px solid var(--brd);border-radius:10px;overflow:hidden;margin-bottom:16px}
.card-header{padding:12px 18px;background:var(--sur2);border-bottom:1px solid var(--brd);display:flex;align-items:center;justify-content:space-between}
.card-title{font-size:13px;font-weight:500}
.card-meta{font-size:11px;color:var(--mut);font-family:var(--mono)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 16px;font-size:10px;font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--brd);background:var(--sur2)}
td{padding:10px 16px;font-size:13px;border-bottom:1px solid var(--brd);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--sur2)}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-family:var(--mono);font-weight:500}
.badge-ok{background:#00e5a020;color:var(--acc)}
.badge-warn{background:#f0a50020;color:var(--orn)}
.badge-err{background:#ff4d4d20;color:var(--red)}
.badge-info{background:#1a3c6e30;color:#6699ff}
.check{cursor:pointer}
.log{background:var(--sur);border:1px solid var(--brd);border-radius:8px;padding:14px 16px;font-family:var(--mono);font-size:11px;color:var(--mut);max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;margin-top:16px}
.ll{display:flex;gap:10px}.lt{color:var(--dim);flex-shrink:0}
.lok{color:var(--acc)}.lerr{color:var(--red)}.lwarn{color:var(--orn)}
.hidden{display:none!important}
.summary{background:var(--sur);border:1px solid var(--acc);border-radius:8px;padding:14px 18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:16px}
.sum-item{text-align:center}
.sum-val{font-size:24px;font-weight:600;color:var(--acc)}
.sum-label{font-size:11px;color:var(--mut);font-family:var(--mono)}
</style>
</head>
<body>
<h1>Réceptions depuis <span>factures</span></h1>
<div class="sub">Crée automatiquement les réceptions de stock depuis les factures fournisseurs confirmées</div>

<div class="controls">
  <div class="field">
    <label>Date comptable FROM</label>
    <input type="date" id="date-from" value="2026-05-20"/>
  </div>
  <div class="field">
    <label>Date comptable TO</label>
    <input type="date" id="date-to" value="2026-05-21"/>
  </div>
  <button class="btn" onclick="loadInvoices()">🔍 Charger les factures</button>
</div>

<div class="summary hidden" id="summary">
  <div class="sum-item"><div class="sum-val" id="s-total">0</div><div class="sum-label">Factures trouvées</div></div>
  <div class="sum-item"><div class="sum-val" id="s-selected">0</div><div class="sum-label">Sélectionnées</div></div>
  <div class="sum-item"><div class="sum-val" id="s-lines">0</div><div class="sum-label">Lignes produits</div></div>
  <div class="sum-item"><div class="sum-val" id="s-done">0</div><div class="sum-label">Réceptions créées</div></div>
</div>

<div class="card hidden" id="invoices-card">
  <div class="card-header">
    <div class="card-title">Factures fournisseurs confirmées</div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-sec" onclick="selectAll(true)">Tout sélectionner</button>
      <button class="btn btn-sec" onclick="selectAll(false)">Tout désélectionner</button>
      <button class="btn" id="btn-create" onclick="createReceptions()">✓ Créer les réceptions</button>
    </div>
  </div>
  <table>
    <thead><tr>
      <th style="width:40px"><input type="checkbox" id="chk-all" onchange="selectAll(this.checked)"/></th>
      <th>Numéro</th>
      <th>Fournisseur</th>
      <th>Référence</th>
      <th>Date comptable</th>
      <th>Lignes</th>
      <th>Statut</th>
    </tr></thead>
    <tbody id="invoices-body"></tbody>
  </table>
</div>

<div class="log" id="log">
  <div class="ll"><span class="lt">--:--:--</span><span>Prêt — sélectionne une période et charge les factures</span></div>
</div>

<script>
let invoices = [];
const $=id=>document.getElementById(id);
function log(m,t=''){const l=$('log'),n=document.createElement('div'),now=new Date().toLocaleTimeString('fr-FR');n.className='ll';n.innerHTML=`<span class="lt">${now}</span><span class="${t?'l'+t:''}">${m}</span>`;l.appendChild(n);l.scrollTop=l.scrollHeight}

function updateSummary(){
  const selected = invoices.filter((_,i)=>$(`chk-${i}`)?.checked);
  const lines = selected.reduce((s,inv)=>s+(inv.lines||0),0);
  $('s-total').textContent = invoices.length;
  $('s-selected').textContent = selected.length;
  $('s-lines').textContent = lines;
}

function selectAll(v){
  invoices.forEach((_,i)=>{const c=$(`chk-${i}`);if(c)c.checked=v});
  $('chk-all').checked=v;
  updateSummary();
}

async function loadInvoices(){
  const from=$('date-from').value, to=$('date-to').value;
  if(!from||!to){log('Sélectionne une période','err');return}
  log(`Chargement factures du ${from} au ${to}...`,'warn');
  try{
    const r=await fetch(`/receptions/invoices?date_from=${from}&date_to=${to}`);
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    invoices=d.invoices||[];
    log(`${invoices.length} facture(s) trouvée(s)`,'ok');
    renderInvoices();
    $('invoices-card').classList.remove('hidden');
    $('summary').classList.remove('hidden');
    $('s-done').textContent='0';
    updateSummary();
  }catch(e){log(`Erreur : ${e.message}`,'err')}
}

function renderInvoices(){
  const tb=$('invoices-body');tb.innerHTML='';
  invoices.forEach((inv,i)=>{
    const tr=document.createElement('tr');
    const hasProducts = inv.lines > 0;
    tr.innerHTML=`
      <td><input type="checkbox" id="chk-${i}" class="check" ${hasProducts?'checked':''} onchange="updateSummary()" ${hasProducts?'':'disabled'}></td>
      <td style="font-family:var(--mono);font-size:12px">${inv.name}</td>
      <td>${inv.partner}</td>
      <td style="font-family:var(--mono);font-size:12px">${inv.ref||'—'}</td>
      <td style="font-family:var(--mono);font-size:12px">${inv.date_accounting}</td>
      <td><span class="badge ${inv.lines>0?'badge-ok':'badge-err'}">${inv.lines} ligne(s)</span></td>
      <td><span class="badge badge-info">${inv.state}</span></td>`;
    tb.appendChild(tr);
  });
}

async function createReceptions(){
  const selected=invoices.filter((_,i)=>$(`chk-${i}`)?.checked);
  if(!selected.length){log('Aucune facture sélectionnée','err');return}
  $('btn-create').disabled=true;
  log(`Création de ${selected.length} réception(s)...`,'warn');
  let done=0;
  for(const inv of selected){
    try{
      const r=await fetch('/receptions/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({invoice_id:inv.id})});
      const d=await r.json();
      if(d.error)throw new Error(d.error);
      log(`✓ ${inv.name} (${inv.partner}) → Réception ${d.reception_name} — ${d.lines} ligne(s)`,'ok');
      done++;
      $('s-done').textContent=done;
    }catch(e){
      log(`✗ ${inv.name} — ${e.message}`,'err');
    }
  }
  log(`Terminé — ${done}/${selected.length} réception(s) créée(s)`,'ok');
  $('btn-create').disabled=false;
}
</script>
</body>
</html>"""


@app.get("/receptions", response_class=HTMLResponse)
async def receptions_ui():
    return HTMLResponse(content=RECEPTION_HTML)


@app.get("/receptions/invoices")
async def receptions_get_invoices(date_from: str, date_to: str):
    """Fetch confirmed vendor bills in accounting date range."""
    try:
        models, uid = odoo_login()

        bill_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "search",
            [[
                ["move_type", "=", "in_invoice"],
                ["state", "in", ["posted"]],
                ["date", ">=", date_from],
                ["date", "<=", date_to],
            ]],
            {"order": "date asc"}
        )

        if not bill_ids:
            return {"invoices": []}

        bills = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "read",
            [bill_ids],
            {"fields": ["id", "name", "ref", "partner_id", "date",
                        "invoice_line_ids", "state"]}
        )

        result = []
        for b in bills:
            # Count lines with products
            line_ids = b.get("invoice_line_ids", [])
            lines_with_products = 0
            if line_ids:
                lines = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, "account.move.line", "read",
                    [line_ids], {"fields": ["product_id", "quantity"]}
                )
                lines_with_products = sum(
                    1 for l in lines
                    if l.get("product_id") and l.get("quantity", 0) > 0
                )

            result.append({
                "id": b["id"],
                "name": b["name"],
                "ref": b.get("ref") or "",
                "partner": b["partner_id"][1] if b.get("partner_id") else "—",
                "date_accounting": str(b.get("date") or ""),
                "state": b["state"],
                "lines": lines_with_products,
            })

        log.info("Found %d invoices between %s and %s", len(result), date_from, date_to)
        return {"invoices": result}

    except Exception as e:
        log.error("receptions_get_invoices error: %s", e)
        return {"error": str(e)}


@app.post("/receptions/create")
async def receptions_create(request: Request):
    """Create a stock reception from a vendor bill."""
    try:
        data = await request.json()
        invoice_id = data["invoice_id"]
        models, uid = odoo_login()

        # Read invoice lines
        bill = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move", "read",
            [[invoice_id]],
            {"fields": ["id", "name", "ref", "partner_id", "invoice_line_ids"]}
        )[0]

        line_ids = bill.get("invoice_line_ids", [])
        if not line_ids:
            return {"error": "Aucune ligne sur cette facture"}

        lines = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "account.move.line", "read",
            [line_ids],
            {"fields": ["product_id", "name", "quantity", "product_uom_id"]}
        )

        # Filter lines with products and positive qty
        stock_lines = [
            l for l in lines
            if l.get("product_id") and l.get("quantity", 0) > 0
        ]

        if not stock_lines:
            return {"error": "Aucune ligne avec produit trouvée"}

        # Find WH/IN picking type
        picking_types = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.picking.type", "search_read",
            [[["code", "=", "incoming"], ["warehouse_id.active", "=", True]]],
            {"fields": ["id", "name", "default_location_dest_id"], "limit": 1}
        )
        if not picking_types:
            return {"error": "Aucun type de réception trouvé (WH/IN)"}

        picking_type = picking_types[0]
        dest_location_id = picking_type["default_location_dest_id"][0]

        # Find source location (supplier)
        supplier_locations = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.location", "search",
            [[["usage", "=", "supplier"]]],
            {"limit": 1}
        )
        src_location_id = supplier_locations[0] if supplier_locations else False

        # Build move lines
        move_lines = []
        for l in stock_lines:
            product_id = l["product_id"][0]

            # Get product's UOM
            product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[product_id]], {"fields": ["uom_id", "uom_po_id"]}
            )[0]
            uom_id = product_info["uom_id"][0] if product_info.get("uom_id") else False

            move_lines.append((0, 0, {
                "name": l.get("name") or l["product_id"][1],
                "product_id": product_id,
                "product_uom_qty": l["quantity"],
                "product_uom": uom_id,
                "location_id": src_location_id,
                "location_dest_id": dest_location_id,
            }))

        # Create picking (reception)
        partner_id = bill["partner_id"][0] if bill.get("partner_id") else False
        origin = f"{bill['name']} / {bill.get('ref') or ''}"

        picking_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "create",
            [{
                "picking_type_id": picking_type["id"],
                "partner_id": partner_id,
                "origin": origin.strip(" /"),
                "location_id": src_location_id,
                "location_dest_id": dest_location_id,
                "move_ids": move_lines,
            }]
        )

        # Validate immediately (mark as done)
        picking = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "read",
            [[picking_id]], {"fields": ["name", "state"]}
        )[0]

        # Set qty_done on move lines then validate
        moves = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.move", "search_read",
            [[["picking_id", "=", picking_id]]],
            {"fields": ["id", "product_uom_qty"]}
        )
        for move in moves:
            # Create move line done
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "stock.move", "write",
                [[move["id"]], {"quantity_done": move["product_uom_qty"]}]
            )

        # Validate the picking
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "button_validate",
            [[picking_id]]
        )

        log.info("Reception created: %s for invoice %s (%d lines)",
                 picking["name"], bill["name"], len(stock_lines))

        return {
            "ok": True,
            "reception_id": picking_id,
            "reception_name": picking["name"],
            "lines": len(stock_lines),
            "invoice": bill["name"]
        }

    except Exception as e:
        log.error("receptions_create error: %s", e)
        return {"error": str(e)}
