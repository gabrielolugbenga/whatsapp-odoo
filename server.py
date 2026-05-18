"""
WhatsApp → Claude → Odoo 19
Nouvelle architecture : bot silencieux + timer 5min + tout côté admin
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
TIMER_SECONDS = int(os.environ.get("TIMER_SECONDS", 300))  # 5 minutes

IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")
IDF_FREE_DELIVERY_THRESHOLD = 100.0
IDF_DELIVERY_FEE = 3.0
GLS_BASE = 9.0
GLS_PER_KG = 0.65
GLS_MAX_WEIGHT = 30.0

log.info("=== SERVER START === ADMIN: %s TIMER: %ss", ADMIN_PHONE, TIMER_SECONDS)

# ── State ──────────────────────────────────────────────────────────────────────

# { phone: { "messages": [...], "contact": str, "timer_task": asyncio.Task } }
client_buffers: dict = {}

# { token: { order_data, phone, contact } }
pending_orders: dict = {}

# { phone: { order_name, total, order_data } }
waiting_for_payment: dict = {}

# Admin clarification states
# { "order_data": ..., "phone": ..., "contact": ..., "ambiguous": [...], ... }
admin_clarification: dict = {}

# Admin IDF question
# { "order_data": ..., "phone": ..., "contact": ... }
admin_waiting_idf: dict = {}

# CMD flow
cmd_pending: dict = {}  # { "order_data": ..., "step": "customer"|"address" }


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

    # ── Admin messages ─────────────────────────────────────────────────────────
    if phone == ADMIN_PHONE and msg_type == "text":
        txt = message["text"]["body"].strip()
        txt_up = txt.upper()

        # HISTORY command
        if txt_up.startswith("HISTORY"):
            await handle_history()
            return {"status": "history"}

        # EOD response
        if txt_up in ("YES", "OUI") and not pending_orders and not admin_waiting_idf and not admin_clarification:
            await send_whatsapp(ADMIN_PHONE, "✅ Great! Have a good evening. 🌙")
            return {"status": "eod_yes"}

        if txt_up in ("NO", "NON") and not pending_orders and not admin_waiting_idf and not admin_clarification:
            await send_whatsapp(ADMIN_PHONE,
                "Please log missing orders via CMD before closing.\n\n"
                "Type CMD followed by the order to get started."
            )
            return {"status": "eod_no"}

        # CMD flow
        if txt_up.startswith("CMD"):
            await handle_cmd_start(txt)
            return {"status": "cmd"}

        if cmd_pending.get("step") == "customer":
            await handle_cmd_customer(txt)
            return {"status": "cmd_customer"}

        if cmd_pending.get("step") == "idf":
            await handle_cmd_idf(txt)
            return {"status": "cmd_idf"}

        # IDF question for new customer
        if admin_waiting_idf:
            await handle_admin_idf(txt)
            return {"status": "admin_idf"}

        # Product clarification
        if admin_clarification:
            await handle_admin_clarification(txt)
            return {"status": "admin_clarification"}

        # Validation
        if txt_up.startswith("OUI") or txt_up.startswith("YES"):
            await handle_admin_validation("OUI")
            return {"status": "admin_yes"}
        if txt_up.startswith("NON") or txt_up.startswith("NO"):
            await handle_admin_validation("NON")
            return {"status": "admin_no"}
        if pending_orders:
            await handle_admin_correction(txt)
            return {"status": "admin_correction"}

    # ── Catalog order ──────────────────────────────────────────────────────────
    if msg_type == "order":
        await buffer_message(phone, contact, f"[CATALOG ORDER: {json.dumps(message['order'])}]")
        return {"status": "catalog"}

    # ── Client text message ────────────────────────────────────────────────────
    if msg_type == "text":
        text = message["text"]["body"]

        # Payment response
        if phone in waiting_for_payment:
            await handle_payment_response(phone, text)
            return {"status": "payment"}

        # Buffer message and start/reset timer
        await buffer_message(phone, contact, text)
        return {"status": "buffered"}

    return {"status": "ignored"}


# ── Message buffer & timer ─────────────────────────────────────────────────────

async def buffer_message(phone: str, contact: str, text: str):
    """Add message to buffer and reset 5-minute timer."""
    if phone not in client_buffers:
        client_buffers[phone] = {"messages": [], "contact": contact, "timer_task": None}

    client_buffers[phone]["messages"].append(text)
    client_buffers[phone]["contact"] = contact

    # Cancel existing timer
    existing = client_buffers[phone].get("timer_task")
    if existing and not existing.done():
        existing.cancel()

    # Start new timer
    task = asyncio.create_task(timer_expired(phone))
    client_buffers[phone]["timer_task"] = task
    log.info("Timer started for %s (%d messages)", phone, len(client_buffers[phone]["messages"]))


async def timer_expired(phone: str):
    """Called after TIMER_SECONDS of silence — synthesize and notify admin."""
    await asyncio.sleep(TIMER_SECONDS)

    buffer = client_buffers.pop(phone, None)
    if not buffer:
        return

    messages = buffer["messages"]
    contact  = buffer["contact"]

    if not messages:
        return

    log.info("Timer expired for %s — synthesizing %d messages", phone, len(messages))
    full_text = "\n".join(messages)

    try:
        result = analyze_message(full_text)
    except Exception as e:
        log.error("Synthesis error: %s", e)
        await send_whatsapp(ADMIN_PHONE,
            f"⚠️ Could not analyze message from {contact} ({phone}):\n\n{full_text}"
        )
        return

    if result.get("type") != "order" or not result.get("items"):
        # Not an order — forward to admin as regular message
        await send_whatsapp(ADMIN_PHONE,
            f"💬 Message from {contact} ({phone}):\n\n{full_text}\n\n"
            "Please reply to them directly on WhatsApp."
        )
        return

    result["customer_phone"] = phone
    result["customer_name"]  = contact

    # Check if we know IDF status for this customer
    idf_status = get_customer_idf(phone)
    if idf_status is None:
        # Ask admin
        admin_waiting_idf["order_data"] = result
        admin_waiting_idf["phone"] = phone
        admin_waiting_idf["contact"] = contact

        items_txt = format_items_preview(result)
        await send_whatsapp(ADMIN_PHONE,
            f"🛒 *New order detected*\n"
            f"Customer: {contact} ({phone})\n\n"
            f"{items_txt}\n\n"
            f"Is this customer in *IDF*? Reply *YES* or *NO*"
        )
    else:
        await process_order_with_idf(result, phone, contact, idf_status)


# ── IDF handling ───────────────────────────────────────────────────────────────

async def handle_admin_idf(txt: str):
    """Admin answers YES/NO to IDF question."""
    order_data = admin_waiting_idf.pop("order_data", None)
    phone      = admin_waiting_idf.pop("phone", None)
    contact    = admin_waiting_idf.pop("contact", None)

    if not order_data:
        return

    txt_up = txt.strip().upper()
    if txt_up in ("YES", "OUI", "Y"):
        idf = True
    elif txt_up in ("NO", "NON", "N"):
        idf = False
    else:
        await send_whatsapp(ADMIN_PHONE, "Please reply *YES* or *NO*")
        admin_waiting_idf["order_data"] = order_data
        admin_waiting_idf["phone"] = phone
        admin_waiting_idf["contact"] = contact
        return

    # Save IDF status on partner
    save_customer_idf(phone, idf)
    await process_order_with_idf(order_data, phone, contact, idf)


async def process_order_with_idf(order_data: dict, phone: str, contact: str, idf: bool):
    """Resolve products and calculate shipping, then notify admin."""
    models, uid = odoo_login()
    resolved = []
    ambiguous_list = []
    missing = []
    total_amount = 0.0
    total_weight = 0.0

    for item in order_data.get("items", []):
        product_name = item.get("product_name", "")
        size = item.get("size")
        bags = item.get("bags", 1)

        matches = find_products(models, uid, product_name, size)
        for m in matches:
            m["price_ttc"] = get_price_ttc(models, uid, m["id"], m["list_price"])

        if not matches:
            missing.append(f"{product_name} {size or ''}".strip())
        elif len(matches) == 1:
            p = matches[0]
            line_total = p["price_ttc"] * bags
            total_amount += line_total
            total_weight += p.get("weight", 0) * bags
            resolved.append({
                "product_id": p["id"], "product_name": p["name"],
                "quantity": bags, "unit_price": p["price_ttc"],
                "line_total": line_total, "weight": p.get("weight", 0),
            })
        else:
            ambiguous_list.append({
                "query": product_name, "size": size,
                "bags": bags, "matches": matches,
            })

    order_data["items_info"] = resolved
    order_data["missing"] = missing
    order_data["total_amount"] = total_amount
    order_data["total_weight"] = total_weight
    order_data["idf"] = idf

    if ambiguous_list:
        # Ask admin to clarify first ambiguous
        admin_clarification["ambiguous"] = ambiguous_list
        admin_clarification["current_idx"] = 0
        admin_clarification["order_data"] = order_data
        admin_clarification["phone"] = phone
        admin_clarification["contact"] = contact

        first = ambiguous_list[0]
        await send_whatsapp(ADMIN_PHONE, clarification_msg(first))
    else:
        await finalize_and_notify(order_data, phone, contact)


async def finalize_and_notify(order_data: dict, phone: str, contact: str):
    """Calculate shipping and send recap to admin."""
    idf = order_data.get("idf", False)
    total_amount = order_data.get("total_amount", 0)
    total_weight = order_data.get("total_weight", 0)
    shipping_cost, shipping_note = calculate_shipping(idf, total_amount, total_weight)
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"] = total_amount + shipping_cost

    token = str(uuid.uuid4())[:8]
    pending_orders[token] = {
        "order_data": order_data, "phone": phone, "contact": contact
    }

    items_info = order_data.get("items_info", [])
    missing = order_data.get("missing", [])

    lines = "\n".join(
        f"  • {it['product_name']} × {it['quantity']} — €{it['line_total']:.2f}"
        for it in items_info
    )
    msg = (
        f"🛒 *New order — {contact} ({phone})*\n\n"
        f"{lines}"
    )
    if missing:
        msg += f"\n\n⚠️ Not found: {', '.join(missing)}"
    msg += f"\n\n🚚 {shipping_note}"
    msg += f"\n💰 Subtotal: €{total_amount:.2f}"
    msg += f"\n🚚 Delivery: €{shipping_cost:.2f}"
    msg += f"\n💳 *Total: €{order_data['grand_total']:.2f}*"
    msg += "\n\nReply *OUI* to confirm, *NON* to cancel, or send a correction."

    await send_whatsapp(ADMIN_PHONE, msg)


# ── Admin clarification (product ambiguity) ────────────────────────────────────

def clarification_msg(amb: dict) -> str:
    options = "\n".join(
        f"  {i+1}. {m['name']} — €{m.get('price_ttc', m['list_price']):.2f}"
        for i, m in enumerate(amb["matches"])
    )
    size_hint = f" ({amb['size']})" if amb.get("size") else ""
    return (
        f"Which *{amb['query']}{size_hint}*?\n\n"
        f"{options}\n"
        f"  0. Not in catalogue — skip\n\n"
        "Reply with number or name."
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
        # Skip this product
        admin_clarification["current_idx"] += 1
    else:
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
            await send_whatsapp(ADMIN_PHONE, clarification_msg(current))
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

    # Next ambiguous or finish
    next_idx = admin_clarification["current_idx"]
    if next_idx < len(ambiguous):
        await send_whatsapp(ADMIN_PHONE, clarification_msg(ambiguous[next_idx]))
    else:
        admin_clarification.clear()
        await finalize_and_notify(order_data, phone, contact)


# ── Admin validation ───────────────────────────────────────────────────────────

async def handle_admin_validation(decision: str):
    if not pending_orders:
        await send_whatsapp(ADMIN_PHONE, "No pending orders.")
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
                "1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link\n\n"
                "Reply with 1, 2 or 3."
            )
        except Exception as e:
            log.error("Order error: %s", e)
            await send_whatsapp(ADMIN_PHONE, f"❌ Odoo error: {str(e)}")
    else:
        await send_whatsapp(ADMIN_PHONE, "Order cancelled.")
        await send_whatsapp(phone, "Sorry, we could not process your order. We will contact you shortly.")


# ── Admin correction ───────────────────────────────────────────────────────────

CORRECTION_PROMPT = """
You are an assistant that parses order corrections from a store operator.
Respond ONLY with valid JSON, no markdown:

{
  "actions": [
    { "type": "add", "product_name": "poundo yam eagle", "size": "10kg", "bags": 1 },
    { "type": "remove", "product_name": "olaola pounded yam" },
    { "type": "replace", "old_product": "olaola pounded yam", "new_product_name": "poundo yam eagle", "size": "10kg", "bags": 1 },
    { "type": "change_qty", "product_name": "merluza", "bags": 2 }
  ]
}

- "instead of", "replace" → replace action
- "remove", "enlever" → remove action
- "add", "ajouter" → add action
- quantity change → change_qty
- no keyword → add
"""

async def handle_admin_correction(correction_text: str):
    if not pending_orders:
        return

    token   = next(iter(pending_orders))
    pending = pending_orders.pop(token)
    order_data = pending["order_data"]
    phone      = pending["phone"]
    contact    = pending["contact"]

    try:
        correction = analyze_correction(correction_text)
        actions    = correction.get("actions", [])
    except Exception as e:
        log.error("Correction error: %s", e)
        await send_whatsapp(ADMIN_PHONE, "Could not understand. Please try again or reply OUI/NON.")
        pending_orders[token] = pending
        return

    models, uid  = odoo_login()
    items_info   = list(order_data.get("items_info", []))

    for action in actions:
        atype = action.get("type")

        if atype == "remove":
            target = action.get("product_name", "").lower()
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
                p          = matches[0]
                price_ttc  = get_price_ttc(models, uid, p["id"], p["list_price"])
                new_item   = {
                    "product_id": p["id"], "product_name": p["name"],
                    "quantity": bags, "unit_price": price_ttc,
                    "line_total": price_ttc * bags, "weight": p.get("weight", 0),
                }
                if atype == "replace":
                    old = action.get("old_product", "").lower()
                    if old:
                        items_info = [it for it in items_info if old not in it["product_name"].lower()]
                items_info.append(new_item)
            else:
                await send_whatsapp(ADMIN_PHONE, f"⚠️ Product not found: *{pname} {size or ''}*")
                pending_orders[token] = pending
                return

    total_amount = sum(it["line_total"] for it in items_info)
    total_weight = sum(it.get("weight", 0) * it["quantity"] for it in items_info)
    idf          = order_data.get("idf", False)
    shipping_cost, shipping_note = calculate_shipping(idf, total_amount, total_weight)

    order_data["items_info"]   = items_info
    order_data["total_amount"] = total_amount
    order_data["shipping_cost"] = shipping_cost
    order_data["grand_total"]  = total_amount + shipping_cost
    order_data["total_weight"] = total_weight

    await finalize_and_notify(order_data, phone, contact)


# ── CMD flow ───────────────────────────────────────────────────────────────────

async def handle_history():
    """Show today's orders created via WhatsApp."""
    try:
        models, uid = odoo_login()
        import datetime
        today = datetime.date.today().strftime("%Y-%m-%d")
        ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "search",
            [[["client_order_ref", "like", "WA-"],
              ["date_order", ">=", f"{today} 00:00:00"]]],
        )
        if not ids:
            await send_whatsapp(ADMIN_PHONE, "📋 No orders logged today via WhatsApp.")
            return

        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
            [ids], {"fields": ["name", "partner_id", "amount_total", "state"]}
        )
        status_map = {"draft": "⏳", "sent": "📤", "sale": "✅", "cancel": "❌"}
        lines = "\n".join(
            f"{status_map.get(o['state'], '?')} {o['name']} — "
            f"{o['partner_id'][1]} — €{o['amount_total']:.2f}"
            for o in orders
        )
        total = sum(o["amount_total"] for o in orders)
        await send_whatsapp(ADMIN_PHONE,
            f"📋 *Today's orders ({len(orders)})*\n\n{lines}\n\n"
            f"💳 *Total: €{total:.2f}*"
        )
    except Exception as e:
        log.error("History error: %s", e)
        await send_whatsapp(ADMIN_PHONE, f"Could not fetch history: {str(e)}")


async def handle_cmd_start(txt: str):
    order_text = txt[3:].strip().lstrip(":").strip()
    if not order_text:
        await send_whatsapp(ADMIN_PHONE,
            "Type CMD followed by the order:\nExample: CMD 5kg president rice, merluza"
        )
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
    items_txt = format_items_preview(result)
    await send_whatsapp(ADMIN_PHONE,
        f"Order noted:\n{items_txt}\n\n"
        "Customer name or phone number?"
    )


async def handle_cmd_customer(txt: str):
    """Handle customer search by name or phone number."""
    t = txt.strip()
    # Check if it's a number selection from previous search
    if t.isdigit() and "cmd_customer_results" in cmd_pending:
        results = cmd_pending.pop("cmd_customer_results")
        n = int(t)
        if 1 <= n <= len(results):
            chosen = results[n - 1]
            await _process_cmd_with_customer(chosen["phone"], chosen["name"])
        else:
            await send_whatsapp(ADMIN_PHONE, f"Please reply with a number between 1 and {len(results)}.")
            cmd_pending["cmd_customer_results"] = results
        return

    # Check if it's a phone number
    digits_only = re.sub(r"[^\d]", "", t)
    if len(digits_only) >= 8:
        # Search by phone
        try:
            models, uid = odoo_login()
            ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                    [[["phone", "like", digits_only[-8:]]]])
            if ids:
                partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                                             [ids[:3]], {"fields": ["name", "phone"]})
                if len(partners) == 1:
                    await _process_cmd_with_customer(digits_only, partners[0]["name"])
                else:
                    options = "\n".join(f"  {i+1}. {p['name']} — {p['phone']}"
                                        for i, p in enumerate(partners))
                    cmd_pending["cmd_customer_results"] = [
                        {"name": p["name"], "phone": re.sub(r"[^\d]", "", p["phone"] or digits_only)}
                        for p in partners
                    ]
                    await send_whatsapp(ADMIN_PHONE,
                        f"Found {len(partners)} customers:\n\n{options}\n\nReply with number."
                    )
            else:
                # New customer
                await _process_cmd_with_customer(digits_only, digits_only)
        except Exception as e:
            log.error("CMD customer search error: %s", e)
            await _process_cmd_with_customer(digits_only, digits_only)
    else:
        # Search by name
        try:
            models, uid = odoo_login()
            ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                    [[["name", "ilike", t], ["customer_rank", ">", 0]]], {"limit": 5})
            if ids:
                partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                                             [ids], {"fields": ["name", "phone"]})
                # Filter out partners without phone
                partners = [p for p in partners if p.get("phone")]
                if not partners:
                    await send_whatsapp(ADMIN_PHONE,
                        f"No customer found with name *{t}* and a phone number.\n"
                        "Please provide their phone number directly."
                    )
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
                    await send_whatsapp(ADMIN_PHONE,
                        f"Found {len(partners)} customers named *{t}*:\n\n{options}\n\n"
                        "Reply with number, or type their phone if not listed."
                    )
            else:
                await send_whatsapp(ADMIN_PHONE,
                    f"No customer found with name *{t}*.\n"
                    "Please provide their phone number directly."
                )
        except Exception as e:
            log.error("CMD name search error: %s", e)
            await send_whatsapp(ADMIN_PHONE, "Search error. Please provide their phone number directly.")


async def _process_cmd_with_customer(customer_phone: str, customer_name: str):
    """Continue CMD flow once customer is identified."""
    order_data = cmd_pending.get("order_data", {})
    order_data["customer_phone"] = customer_phone
    order_data["customer_name"]  = customer_name

    idf = get_customer_idf(customer_phone)
    if idf is None:
        cmd_pending["step"] = "idf"
        await send_whatsapp(ADMIN_PHONE,
            f"Customer: *{customer_name}* ({customer_phone})\n\n"
            "Is this customer in *IDF*? Reply *YES* or *NO*"
        )
    else:
        idf_label = "IDF ✓" if idf else "Outside IDF ✓"
        await send_whatsapp(ADMIN_PHONE,
            f"Customer: *{customer_name}* ({customer_phone}) — {idf_label}\n\nProcessing..."
        )
        cmd_pending.clear()
        await process_order_with_idf(order_data, customer_phone, customer_name, idf)


async def handle_cmd_idf(txt: str):
    txt_up = txt.strip().upper()
    order_data     = cmd_pending.pop("order_data", {})
    customer_phone = order_data.get("customer_phone", "")

    if txt_up in ("YES", "OUI", "Y"):
        idf = True
    elif txt_up in ("NO", "NON", "N"):
        idf = False
    else:
        await send_whatsapp(ADMIN_PHONE, "Please reply *YES* or *NO*")
        cmd_pending["order_data"] = order_data
        return

    cmd_pending.clear()
    save_customer_idf(customer_phone, idf)
    await process_order_with_idf(order_data, customer_phone, customer_phone, idf)


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
        await send_whatsapp(phone, f"✅ Payment link requested for {order_name} (€{total:.2f}).\nWe will send it shortly! ⏳")
        await send_whatsapp(ADMIN_PHONE, f"🔗 {order_name}: *Payment link* — €{total:.2f}\nPlease send the payment link.")
    else:
        await send_whatsapp(phone, "Please reply:\n1️⃣ Card on delivery\n2️⃣ Cash on delivery\n3️⃣ Payment link")
        waiting_for_payment[phone] = pending


# ── Helpers ────────────────────────────────────────────────────────────────────

def format_items_preview(result: dict) -> str:
    return "\n".join(
        f"  • {it.get('product_name')} {it.get('size') or ''} × {it.get('bags', 1)}"
        for it in result.get("items", [])
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
                f"Current delivery: €{shipping:.2f}"
            )
    else:
        shipping = GLS_BASE + GLS_PER_KG * min(total_weight, GLS_MAX_WEIGHT)
        return shipping, f"GLS delivery: €{shipping:.2f}"


# ── Claude ─────────────────────────────────────────────────────────────────────

ORDER_PROMPT = """
You are an assistant for an African food delivery business.
Analyze the message(s) and respond ONLY with valid JSON, no markdown.

If it contains an order:
{
  "type": "order",
  "confidence": 0.9,
  "items": [
    { "product_name": "pounded yam", "size": "5kg", "bags": 1 }
  ]
}

If NOT an order:
{ "type": "question", "message": "original text" }

Rules:
- Synthesize ALL messages together into one order
- Extract product name WITHOUT size (put size separately)
- "5kg pounded yam" → product_name: "pounded yam", size: "5kg", bags: 1
- "2 bags rice 10kg" → product_name: "rice", size: "10kg", bags: 2
- "some rice" → product_name: "rice", size: null, bags: 1
- confidence < 0.65 → use type "question"
"""

def analyze_message(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ORDER_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    log.info("Claude: %s", raw)
    return json.loads(raw)


def analyze_correction(text: str) -> dict:
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
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

    # Search by name only
    results = search([["name", "ilike", name], ["sale_ok", "=", True], ["is_published", "=", True]])

    # If no results, try word by word
    if not results:
        words = [w for w in name.split()
                 if len(w) > 2 and not re.match(r"^[0-9]+[kgKGlLmM]+$", w)]
        if words:
            all_ids = None
            for word in words:
                ids = {p["id"] for p in search([["name", "ilike", word],
                                                ["sale_ok", "=", True],
                                                ["is_published", "=", True]])}
                all_ids = ids if all_ids is None else all_ids & ids
            if all_ids:
                results = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                            "product.template", "read",
                                            [list(all_ids)],
                                            {"fields": ["id", "name", "list_price", "weight"]})

    # Filter by size if specified
    if size and results:
        size_clean    = size.lower().replace(" ", "")
        size_filtered = [m for m in results if size_clean in m["name"].lower()]
        if size_filtered:
            return size_filtered

    return results[:5]


def get_price_ttc(models, uid, product_id: int, price_ht: float) -> float:
    try:
        products  = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                      "product.template", "read",
                                      [[product_id]], {"fields": ["taxes_id"]})[0]
        tax_ids   = products.get("taxes_id", [])
        if not tax_ids:
            return price_ht
        taxes     = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                      "account.tax", "read",
                                      [tax_ids], {"fields": ["amount", "amount_type"]})
        tax_rate  = sum(t["amount"] / 100 for t in taxes if t.get("amount_type") == "percent")
        return round(price_ht * (1 + tax_rate), 2)
    except Exception:
        return price_ht


def get_customer_idf(phone: str):
    """Returns True/False if known, None if unknown."""
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
    """Save IDF status in partner comment field."""
    try:
        models, uid = odoo_login()
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
                                [[["phone", "=", phone]]])
        idf_tag = "IDF:YES" if idf else "IDF:NO"
        if ids:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "write",
                              [ids, {"comment": idf_tag}])
        else:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                              [{"phone": phone, "name": phone,
                                "customer_rank": 1, "comment": idf_tag}])
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
                "product_id": delivery_ids[0],
                "product_uom_qty": 1,
                "price_unit": shipping_cost,
                "name": "Delivery fee",
            }))

    oid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": lines,
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

# ── End of day reminder ────────────────────────────────────────────────────────

@app.on_event("startup")
async def schedule_eod_reminder():
    asyncio.create_task(eod_reminder_loop())

async def eod_reminder_loop():
    """Send end of day reminder every day at 19h CET."""
    import datetime
    while True:
        now = datetime.datetime.now()
        # Target 19:00 CET (UTC+1 or UTC+2 in summer)
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)  # 18 UTC = 19 CET
        if now >= target:
            target += datetime.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        log.info("EOD reminder scheduled in %.0f seconds", wait_seconds)
        await asyncio.sleep(wait_seconds)
        await send_whatsapp(ADMIN_PHONE,
            "⏰ *End of day check*\n\n"
            "Have you logged all orders today?\n\n"
            "Reply *YES* or *NO*"
        )
