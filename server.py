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

BRAND_DARK  = colors.HexColor("#1A3C6E")
BRAND_GREEN = colors.HexColor("#00A878")
BRAND_LIGHT = colors.HexColor("#F0F7F4")
BRAND_GRAY  = colors.HexColor("#F5F5F5")
TEXT_DARK   = colors.HexColor("#222222")
TEXT_MUTED  = colors.HexColor("#888888")

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


def make_cover(styles):
    """Generate cover page elements."""
    elems = []
    elems.append(Spacer(1, 40*mm))
    elems.append(Paragraph(
        '<font color="#1A3C6E"><b>AFRICOMFORT FOODS</b></font>',
        ParagraphStyle("cover_title", fontSize=36, alignment=TA_CENTER,
                       fontName="Helvetica-Bold", leading=44)
    ))
    elems.append(Spacer(1, 6*mm))
    elems.append(Paragraph(
        '<font color="#00A878">Catalogue Produits / Product Catalogue</font>',
        ParagraphStyle("cover_sub", fontSize=18, alignment=TA_CENTER,
                       fontName="Helvetica", leading=24)
    ))
    elems.append(Spacer(1, 4*mm))
    elems.append(HRFlowable(width="60%", thickness=2, color=BRAND_GREEN,
                             spaceAfter=6*mm, hAlign="CENTER"))
    elems.append(Paragraph(
        "Épicerie africaine & caribéenne / African & Caribbean Grocery",
        ParagraphStyle("cover_desc", fontSize=12, alignment=TA_CENTER,
                       fontName="Helvetica", textColor=TEXT_MUTED, leading=18)
    ))
    elems.append(Spacer(1, 8*mm))
    elems.append(Paragraph(
        "2026",
        ParagraphStyle("cover_year", fontSize=14, alignment=TA_CENTER,
                       fontName="Helvetica", textColor=TEXT_MUTED)
    ))
    elems.append(Spacer(1, 40*mm))

    # Contact block
    contact_data = [
        ["AfriComfort Foods International"],
        ["www.africomfortfoods.com"],
        ["africomfortfoods@gmail.com"],
        ["Paris, France"],
    ]
    contact_table = Table(contact_data, colWidths=[120*mm])
    contact_table.setStyle(TableStyle([
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("FONTNAME", (0,0), (0,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (0,0), 13),
        ("FONTSIZE", (0,1), (-1,-1), 11),
        ("TEXTCOLOR", (0,0), (0,0), BRAND_DARK),
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
        # Header bar
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, PAGE_H - 18*mm, PAGE_W, 18*mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(15*mm, PAGE_H - 11*mm, "AFRICOMFORT FOODS")
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(PAGE_W - 15*mm, PAGE_H - 11*mm,
                               "Catalogue 2026 — Épicerie africaine & caribéenne")
        # Green accent line
        canvas.setFillColor(BRAND_GREEN)
        canvas.rect(0, PAGE_H - 19.5*mm, PAGE_W, 1.5*mm, fill=1, stroke=0)

        # Footer
        canvas.setFillColor(BRAND_LIGHT)
        canvas.rect(0, 0, PAGE_W, 10*mm, fill=1, stroke=0)
        canvas.setFillColor(TEXT_MUTED)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(15*mm, 3.5*mm, "www.africomfortfoods.com — africomfortfoods@gmail.com")
        canvas.drawRightString(PAGE_W - 15*mm, 3.5*mm, f"Page {doc.page}")
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
            ("BACKGROUND", (0,0), (-1,-1), BRAND_DARK),
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
