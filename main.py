from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from groq import Groq
from typing import List, Optional
import json
import shutil
import os
import uuid
import base64
import qrcode
import io
import time
import cloudinary
import cloudinary.uploader
from collections import defaultdict
from fastapi.responses import StreamingResponse
from datetime import datetime

app = FastAPI()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CATALOGUE_FILE = "catalogue.json"
ORDERS_FILE = "orders.json"
CUSTOMERS_FILE = "customers.json"
INTEREST_LOG_FILE = "interest_log.json"
OFFERS_FILE = "offers.json"
CALLBACKS_FILE = "callbacks.json"
UPLOAD_DIR = "static/uploads"
OWNER_PASSWORD = "royal123"

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Cloudinary (persistent image storage) ────────────────────────
# Render's free tier wipes the local filesystem on every restart/redeploy,
# so images saved to disk disappear. Cloudinary gives every uploaded photo
# a permanent CDN URL that survives restarts. If these env vars aren't set
# (e.g. running locally without a Cloudinary account yet), save_upload()
# falls back to local disk — fine for quick local testing, NOT for Render.
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.getenv("CLOUDINARY_API_KEY", ""),
    api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
    secure=True
)
CLOUDINARY_ENABLED = bool(os.getenv("CLOUDINARY_CLOUD_NAME"))

client = Groq(api_key=GROQ_API_KEY)

def load_catalogue():
    if not os.path.exists(CATALOGUE_FILE):
        return []
    with open(CATALOGUE_FILE, "r") as f:
        return json.load(f)

def save_catalogue(data):
    with open(CATALOGUE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_orders():
    if not os.path.exists(ORDERS_FILE):
        return []
    with open(ORDERS_FILE, "r") as f:
        return json.load(f)

def save_orders(data):
    with open(ORDERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_customers():
    if not os.path.exists(CUSTOMERS_FILE):
        return {}
    with open(CUSTOMERS_FILE, "r") as f:
        return json.load(f)

def save_customers(data):
    with open(CUSTOMERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_interest_log():
    if not os.path.exists(INTEREST_LOG_FILE):
        return []
    with open(INTEREST_LOG_FILE, "r") as f:
        return json.load(f)

def save_interest_log(data):
    # Cap the log so it doesn't grow unbounded on a long-running shop.
    with open(INTEREST_LOG_FILE, "w") as f:
        json.dump(data[-5000:], f, indent=2)

def load_offers():
    if not os.path.exists(OFFERS_FILE):
        return []
    with open(OFFERS_FILE, "r") as f:
        return json.load(f)

def save_offers(data):
    with open(OFFERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def compute_active_offers():
    """Shared by the public /offers endpoint and the chat system prompt."""
    offers = load_offers()
    catalogue = load_catalogue()
    cat_map = {p["name"]: p for p in catalogue}
    result = []
    for o in offers:
        if not o.get("active", True):
            continue
        product = cat_map.get(o["product_name"])
        if not product or product.get("out_of_stock"):
            continue
        original = product["price"]
        offer_price = o["offer_price"]
        discount_pct = round((1 - offer_price / original) * 100) if original else 0
        result.append({
            "id": o["id"],
            "product_name": o["product_name"],
            "original_price": original,
            "offer_price": offer_price,
            "discount_pct": discount_pct,
            "image": product.get("image")
        })
    return result

def load_callbacks():
    if not os.path.exists(CALLBACKS_FILE):
        return []
    with open(CALLBACKS_FILE, "r") as f:
        return json.load(f)

def save_callbacks(data):
    with open(CALLBACKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── Cost / token control ──────────────────────────────────────
# Simple in-memory sliding-window rate limiter, keyed by customer phone.
# Prevents rapid repeated messages from burning Groq tokens unnecessarily.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_MESSAGES = 8
_rate_limit_store = defaultdict(list)

def is_rate_limited(key: str) -> bool:
    if not key:
        return False
    now = time.time()
    timestamps = _rate_limit_store[key]
    timestamps[:] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= RATE_LIMIT_MAX_MESSAGES:
        return True
    timestamps.append(now)
    return False

# Only send the model the last N messages of history — keeps input tokens
# bounded even in very long conversations.
MAX_HISTORY_MESSAGES = 12

def save_upload(file) -> str:
    """Returns a persistent URL for the uploaded image.
    Uses Cloudinary when configured (required on Render — local disk doesn't survive
    restarts there). Falls back to local disk only if Cloudinary env vars are missing,
    which is fine for quick local testing but will NOT persist on Render."""
    if CLOUDINARY_ENABLED:
        result = cloudinary.uploader.upload(
            file.file,
            folder="royal_enterprises",
            resource_type="image"
        )
        return result["secure_url"]
    else:
        ext = file.filename.split(".")[-1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return f"/static/uploads/{filename}"

def delete_upload(url: str):
    """Best-effort cleanup for an image, whether it's on Cloudinary or local disk.
    Never raises — a failed cleanup shouldn't block the actual catalogue edit."""
    if not url:
        return
    try:
        if "cloudinary.com" in url:
            # Extract the public_id from a Cloudinary URL, e.g.
            # https://res.cloudinary.com/<cloud>/image/upload/v169.../royal_enterprises/abc123.jpg
            # -> public_id = royal_enterprises/abc123
            after_upload = url.split("/upload/")[-1]
            parts = after_upload.split("/", 1)
            path_part = parts[1] if len(parts) > 1 else parts[0]
            public_id = path_part.rsplit(".", 1)[0]
            cloudinary.uploader.destroy(public_id)
        else:
            path = url.lstrip("/")
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        pass  # non-critical — orphaned image, not worth failing the request over

def strip_thinking(raw: str) -> str:
    """Remove Qwen's <think>...</think> block from a raw model response."""
    raw = raw.strip()
    if "<think>" in raw and "</think>" in raw:
        raw = raw.split("</think>", 1)[1].strip()
    elif "<think>" in raw:
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
        raw = json_match.group(0) if json_match else raw
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

def strip_markdown(text: str) -> str:
    """Safety net: remove markdown bold/italic/header syntax the chat UI can't render,
    in case the model adds it despite the system prompt telling it not to."""
    import re
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)   # **bold**
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)  # *italic*
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)  # # headers
    return text

def days_since(timestamp_str: str) -> int:
    """Parse a '%d %b %Y, %I:%M %p' timestamp and return days elapsed since then."""
    try:
        then = datetime.strptime(timestamp_str, "%d %b %Y, %I:%M %p")
        return (datetime.now() - then).days
    except Exception:
        return -1

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None

class LoginRequest(BaseModel):
    password: str

class OrderUpdate(BaseModel):
    status: str

class StockConfirmRequest(BaseModel):
    items: list

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/customer.html")

@app.get("/owner")
def owner_page():
    return FileResponse("static/owner.html")

@app.get("/quick-add")
def quick_add_page():
    return FileResponse("static/quick-add.html")

# ── Auth ──────────────────────────────────────────────────────
@app.post("/auth/login")
def login(req: LoginRequest):
    if req.password == OWNER_PASSWORD:
        return {"status": "success"}
    return JSONResponse(status_code=401, content={"status": "error", "message": "Wrong password"})

# ── Catalogue ─────────────────────────────────────────────────
@app.get("/catalogue")
def get_catalogue():
    return load_catalogue()

@app.get("/catalogue/public")
def get_catalogue_public():
    """Customer-safe catalogue — strips cost_price so margins never reach shoppers.
    The customer-facing site/chat should call THIS endpoint, not /catalogue."""
    catalogue = load_catalogue()
    return [{k: v for k, v in p.items() if k != "cost_price"} for p in catalogue]

@app.post("/catalogue")
async def add_product(
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    size: str = Form(""),
    color: str = Form(""),
    cost_price: float = Form(0),
    stock_qty: int = Form(0),
    images: List[UploadFile] = File(default=[])
):
    catalogue = load_catalogue()
    image_urls = []
    for img in images:
        if img and img.filename:
            image_urls.append(save_upload(img))

    product = {
        "name": name,
        "price": price,
        "category": category,
        "description": description,
        "size": size,
        "color": color,
        "cost_price": cost_price,  # owner-only; never exposed to customers
        "stock_qty": stock_qty,     # owner-only; units currently on hand
        "image": image_urls[0] if image_urls else None,   # primary image (backwards compat)
        "images": image_urls,                              # all images
        "out_of_stock": False
    }
    catalogue.append(product)
    save_catalogue(catalogue)
    return {"status": "added", "product": product}

@app.delete("/catalogue/{index}")
def delete_product(index: int):
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        product = catalogue[index]
        for url in product.get("images", []):
            delete_upload(url)
        if product.get("image") and product["image"] not in product.get("images", []):
            delete_upload(product["image"])
        catalogue.pop(index)
        save_catalogue(catalogue)
        return {"status": "deleted"}
    return {"status": "error"}

@app.patch("/catalogue/{index}/stock")
def toggle_stock(index: int):
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        catalogue[index]["out_of_stock"] = not catalogue[index].get("out_of_stock", False)
        save_catalogue(catalogue)
        return {"status": "updated", "out_of_stock": catalogue[index]["out_of_stock"]}
    return {"status": "error"}

class StockQtyUpdate(BaseModel):
    stock_qty: int

@app.patch("/catalogue/{index}/stock-qty")
def set_stock_qty(index: int, body: StockQtyUpdate):
    """Quick restock/adjust — separate from the full edit form."""
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        catalogue[index]["stock_qty"] = max(0, body.stock_qty)
        save_catalogue(catalogue)
        return {"status": "updated", "stock_qty": catalogue[index]["stock_qty"]}
    return JSONResponse(status_code=404, content={"status": "error"})

@app.put("/catalogue/{index}")
async def edit_product(
    index: int,
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    size: str = Form(""),
    color: str = Form(""),
    cost_price: float = Form(0),
    stock_qty: int = Form(0),
    images: List[UploadFile] = File(default=[])
):
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        existing = catalogue[index]
        new_images = []
        for img in images:
            if img and img.filename:
                new_images.append(save_upload(img))

        if new_images:
            # Delete old images
            for url in existing.get("images", []):
                delete_upload(url)
            existing["images"] = new_images
            existing["image"] = new_images[0]

        existing["name"] = name
        existing["price"] = price
        existing["category"] = category
        existing["description"] = description
        existing["size"] = size
        existing["color"] = color
        existing["cost_price"] = cost_price
        existing["stock_qty"] = stock_qty
        catalogue[index] = existing
        save_catalogue(catalogue)
        return {"status": "updated", "product": existing}
    return {"status": "error"}

@app.post("/catalogue/{index}/add-images")
async def add_images_to_product(
    index: int,
    images: List[UploadFile] = File(...)
):
    """Add more images to an existing product without replacing existing ones"""
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        existing_images = catalogue[index].get("images", [])
        if catalogue[index].get("image") and not existing_images:
            existing_images = [catalogue[index]["image"]]

        for img in images:
            if img and img.filename:
                existing_images.append(save_upload(img))

        catalogue[index]["images"] = existing_images
        catalogue[index]["image"] = existing_images[0] if existing_images else None
        save_catalogue(catalogue)
        return {"status": "updated", "images": existing_images}
    return {"status": "error"}

@app.delete("/catalogue/{index}/image/{img_index}")
def delete_product_image(index: int, img_index: int):
    """Delete a specific image from a product"""
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        images = catalogue[index].get("images", [])
        if catalogue[index].get("image") and not images:
            images = [catalogue[index]["image"]]
        if 0 <= img_index < len(images):
            delete_upload(images[img_index])
            images.pop(img_index)
            catalogue[index]["images"] = images
            catalogue[index]["image"] = images[0] if images else None
            save_catalogue(catalogue)
            return {"status": "deleted", "images": images}
    return {"status": "error"}

# ── Orders ────────────────────────────────────────────────────
@app.get("/orders")
def get_orders():
    return load_orders()

@app.post("/orders")
def create_order(order: dict):
    orders = load_orders()
    order["id"] = str(uuid.uuid4())[:8].upper()
    order["timestamp"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    order["status"] = "pending"
    orders.append(order)
    save_orders(orders)
    return {"status": "created", "order": order}

@app.patch("/orders/{order_id}")
def update_order(order_id: str, update: OrderUpdate):
    orders = load_orders()
    for order in orders:
        if order["id"] == order_id:
            order["status"] = update.status
            save_orders(orders)
            return {"status": "updated"}
    return {"status": "error"}

@app.delete("/orders/{order_id}")
def delete_order(order_id: str):
    orders = load_orders()
    orders = [o for o in orders if o["id"] != order_id]
    save_orders(orders)
    return {"status": "deleted"}

# ── Chat ──────────────────────────────────────────────────────
@app.post("/chat")
def chat(req: ChatRequest):
    # Rate limit BEFORE touching Groq at all — this is where token savings actually happen.
    rate_key = req.customer_phone or "anonymous"
    if is_rate_limited(rate_key):
        return {
            "reply": "You're sending messages really quickly! Please wait a few seconds and try again 😊",
            "image": None, "order": None
        }

    catalogue = load_catalogue()
    if not catalogue:
        return {"reply": "No products available yet.", "image": None, "order": None}

    catalogue_text = "\n".join([
        f"- {p['name']} | ₹{p['price']} | {p['category']} | {p['description']} | out_of_stock: {p.get('out_of_stock', False)}"
        for p in catalogue
    ])

    customer_info = ""
    if req.customer_name:
        customer_info = f"Customer name: {req.customer_name}"
    if req.customer_phone:
        customer_info += f" | Phone: {req.customer_phone}"

    # ── Customer memory: recognize returning customers ──────────
    customers = load_customers()
    customer_record = customers.get(req.customer_phone) if req.customer_phone else None
    is_new_session = len(req.history) == 0

    # ── Active offers (owner-controlled) ─────────────────────────
    active_offers = compute_active_offers()
    if active_offers:
        offers_text = "\n".join([
            f"- {o['product_name']}: was ₹{o['original_price']}, now ₹{o['offer_price']} ({o['discount_pct']}% off)"
            for o in active_offers
        ])
    else:
        offers_text = "No special offers running right now."

    returning_customer_context = ""
    if customer_record and is_new_session:
        last_seen_days = days_since(customer_record.get("last_seen", ""))
        past_interests = customer_record.get("interests", [])
        interests_text = ", ".join(past_interests[-3:]) if past_interests else "browsing our catalogue"
        when_text = "earlier today" if last_seen_days == 0 else f"{last_seen_days} day{'s' if last_seen_days != 1 else ''} ago" if last_seen_days >= 0 else "before"
        offer_hint = f" We currently have an offer running: {active_offers[0]['product_name']} at ₹{active_offers[0]['offer_price']} (was ₹{active_offers[0]['original_price']}) — mention it if relevant to what they liked before." if active_offers else ""
        returning_customer_context = f"""
RETURNING CUSTOMER — THIS IS IMPORTANT:
This customer ({customer_record.get('name', req.customer_name or 'them')}) has messaged us before, {when_text}. Last time they showed interest in: {interests_text}.
In your FIRST reply of this conversation, warmly welcome them back by name (e.g. "Welcome back, {{name}}!"), naturally reference what they were interested in last time.{offer_hint} Keep it brief and natural — don't make it feel like a script. After this first reply, continue the conversation normally."""

    system_prompt = f"""You are ShopBot, a friendly AI sales assistant for Royal Enterprises — a ready-made furniture retail shop in Dommasandra, Sarjapur Road, Bangalore.

SHOP CONTACT DETAILS (use these when customer asks how to contact, never use customer's own number):
- Phone: +91 8553537786
- Address: Royal Enterprises, Dommasandra, Sarjapur Road, Bangalore
- Timings: 10 AM to 9 PM, all days

You help customers find products, answer questions about prices, and confirm orders.
Always respond in a helpful, warm, conversational tone. Use ₹ for prices.
{f"You are talking to: {customer_info}" if customer_info else ""}
{returning_customer_context}

FORMATTING — CRITICAL:
This chat does NOT render markdown. Never use **bold**, *italic*, # headers, or markdown tables — the raw symbols will show up as literal asterisks/hashes to the customer, which looks broken.
Write in plain text only. Emojis are fine and encouraged for warmth. Use line breaks and simple dashes ( - ) for lists, never markdown syntax.

KEEP IT SHORT:
Keep replies brief and to the point — a few sentences or a short list, not a wall of text. Only list the FULL catalogue if the customer explicitly asks to see everything. For a specific product question, show just that product's details. This keeps the chat fast and easy to read on a phone.

CRITICAL: When a customer asks how to contact or asks for shop number — always give the SHOP contact details above, NEVER the customer's own phone number.

IMPORTANT: Only recommend products that exist in the catalogue below. Never mention products not in the catalogue.
If a product has out_of_stock: True, tell the customer it is currently unavailable and suggest the closest alternative.
Remember the full conversation context.

DISCOUNT REQUESTS:
Check the conversation history AND the ONGOING OFFERS list below before responding to a discount request.
- If the product the customer wants ALREADY has an active offer (listed below), that offer price IS the discount — do not stack an additional 5% on top of it. If they ask for a discount on a product with an active offer, just confirm the offer price warmly.
- If the product has NO active offer, and the customer asks for a discount for the FIRST time in this conversation: offer a flat 5% discount. Clearly show the original price, the 5% discount amount, and the final discounted price, all in ₹.
- If the customer pushes for MORE after either of the above (an existing offer, or your 5%): do NOT offer any further discount yourself, and do NOT place an order. Instead, warmly say our team will personally connect with them to work out the best possible price. Ask if they'd like the team to call now, or at a specific time that works for them — then follow the SCHEDULED CALLBACKS instructions below based on their answer.
- Never offer more than 5% automatically (on top of a product with no existing offer), under any circumstances.
- Asking for a bigger discount is NOT a purchase confirmation — never treat it as one.

ONGOING OFFERS (set by the shop owner):
{offers_text}
If a customer asks about offers, deals, or discounts currently running, mention these specific active offers by name and price. If none are active, say so honestly and offer to check if a discount can still be arranged for what they want — don't invent an offer that isn't listed above.

SCHEDULED CALLBACKS — CRITICAL, READ CAREFULLY:
When a customer asks our team to call them back — whether because they want a better price, have a specific question, or just prefer to talk to a person — and they give ANY indication of timing (a specific time like "7 PM", a relative time like "in an hour", "tomorrow morning", or even just confirms "yes, call me"):
1. Confirm warmly that you've noted it, restating the time back to them if they gave one.
2. Add this exact line at the end of your reply: SCHEDULE_CALLBACK:[time as the customer described it, or "as soon as possible" if no time given]|[product name if relevant, else leave blank]
This actually creates a task for the shop owner — it is not just a conversational promise. Only add this line once per callback request (don't repeat it on every subsequent message in the same conversation unless the customer asks to reschedule to a different time).
Never claim the callback is confirmed or scheduled unless you actually include this SCHEDULE_CALLBACK line — the line IS what notifies the owner.

BUDGET RECOMMENDATIONS:
If a customer mentions a budget (e.g. "I have ₹8000" or "under ₹15000" or "what can I get for ₹10000"):
1. Look through the catalogue for products within that budget
2. Suggest the best single item OR a smart combo (e.g. Dressing Table + Shoe Box) that fits the budget
3. Show total cost of the combo
4. Explain why this combo is a good choice
5. Ask if they want to order any of it

MULTILINGUAL:
Detect the language the customer is writing in. If they write in Hindi, reply in Hindi. If Kannada, reply in Kannada. If English, reply in English. If mixed, reply in English.

PRODUCT COMPARISON:
If customer asks to compare two products (e.g. "compare wardrobe and cupboard"):
- DO NOT use markdown tables, | symbols, or ** bold — they don't render in chat
- Format comparison like this:

🪑 PRODUCT A
- Price: ₹X
- Size: X feet
- Best for: X

🪑 PRODUCT B
- Price: ₹X
- Size: X feet
- Best for: X

✅ Our recommendation: [give honest recommendation based on their needs]

ORDER CONFIRMATION — CRITICAL, READ CAREFULLY:
Only add the PLACE_ORDER line when the customer's CURRENT message is an explicit, unambiguous confirmation to buy right now — e.g. "yes", "confirm", "book it", "place the order", "I'll take it".
Do NOT add PLACE_ORDER if:
- The customer is asking a question
- The customer is asking for a bigger/further discount
- The customer already has a pending order for this exact product in this conversation (check the conversation history — if you already confirmed an order earlier, do not place a duplicate)
- The customer's message is ambiguous or you are not sure they mean "buy now"
When in doubt, ask a clarifying question instead of placing an order.

When a customer confirms a purchase, respond with a clear order summary and add this exact line at the end:
PLACE_ORDER:[product name]|[price]

CRITICAL RULE: Whenever you mention or recommend any specific product, add this at the very end:
SHOW_IMAGE:[exact product name from catalogue]

SHOP CATALOGUE:
{catalogue_text}"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg in req.history[-MAX_HISTORY_MESSAGES:]:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": req.message})

    response = client.chat.completions.create(
        model="qwen/qwen3.6-27b",
        messages=messages,
        max_tokens=550,
        reasoning_effort="none"
    )

    reply = strip_thinking(response.choices[0].message.content)
    reply = strip_markdown(reply)
    image_url = None
    order_data = None
    callback_data = None
    mentioned_product = None

    lines = reply.split("\n")
    clean_lines = []
    for line in lines:
        if "SHOW_IMAGE:" in line:
            product_name = line.split("SHOW_IMAGE:")[-1].strip().lower()
            for p in catalogue:
                if p["name"].lower() == product_name and p.get("image"):
                    image_url = p["image"]
                    mentioned_product = p["name"]
                    break
        elif line.strip().startswith("PLACE_ORDER:"):
            parts = line.replace("PLACE_ORDER:", "").strip().split("|")
            if len(parts) >= 2:
                order_data = {
                    "product": parts[0].strip(),
                    "price": parts[1].strip(),
                    "customer_name": req.customer_name or "Unknown",
                    "customer_phone": req.customer_phone or "Not provided"
                }
        elif line.strip().startswith("SCHEDULE_CALLBACK:"):
            parts = line.replace("SCHEDULE_CALLBACK:", "").strip().split("|")
            if len(parts) >= 1:
                callback_data = {
                    "requested_time": parts[0].strip(),
                    "product": parts[1].strip() if len(parts) >= 2 else (mentioned_product or ""),
                    "customer_name": req.customer_name or "Unknown",
                    "customer_phone": req.customer_phone or "Not provided"
                }
        else:
            clean_lines.append(line)

    reply = "\n".join(clean_lines).strip()

    if order_data:
        orders = load_orders()
        order_data["id"] = str(uuid.uuid4())[:8].upper()
        order_data["timestamp"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        order_data["status"] = "pending"

        # Snapshot cost price NOW, at time of sale — so editing cost_price later
        # never corrupts historical P&L for orders already placed.
        for i, p in enumerate(catalogue):
            if p["name"].lower() == order_data["product"].lower():
                order_data["cost_price_at_sale"] = p.get("cost_price", 0) or 0
                # Decrement stock on hand, if the owner is tracking it (won't go below 0).
                if "stock_qty" in p and p["stock_qty"] is not None:
                    catalogue[i]["stock_qty"] = max(0, p["stock_qty"] - 1)
                    save_catalogue(catalogue)
                break

        orders.append(order_data)
        save_orders(orders)

    if callback_data:
        callbacks = load_callbacks()
        callback_data["id"] = str(uuid.uuid4())[:8].upper()
        callback_data["timestamp"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        callback_data["status"] = "pending"
        callbacks.append(callback_data)
        save_callbacks(callbacks)

    # ── Update customer memory ───────────────────────────────────
    if req.customer_phone:
        record = customers.get(req.customer_phone, {
            "name": req.customer_name or "",
            "first_seen": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "visit_count": 0,
            "interests": []
        })
        if req.customer_name:
            record["name"] = req.customer_name
        if is_new_session:
            record["visit_count"] = record.get("visit_count", 0) + 1
        if mentioned_product:
            interests = record.get("interests", [])
            if mentioned_product in interests:
                interests.remove(mentioned_product)
            interests.append(mentioned_product)
            record["interests"] = interests[-5:]  # keep last 5, most recent last
        record["last_seen"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        customers[req.customer_phone] = record
        save_customers(customers)

    # ── Log product interest for "most enquired" analytics ────────
    if mentioned_product:
        log = load_interest_log()
        log.append({
            "product": mentioned_product,
            "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p")
        })
        save_interest_log(log)

    return {"reply": reply, "image": image_url, "order": order_data, "callback": callback_data}

# ── Visual Search ──────────────────────────────────────────────
@app.post("/visual-search")
async def visual_search(image: UploadFile = File(...)):
    catalogue = load_catalogue()
    if not catalogue:
        return {"status": "error", "message": "No products in catalogue yet."}

    catalogue_context = "\n".join([
        f"- {p['name']} | ₹{p['price']} | {p['description']} | out_of_stock: {p.get('out_of_stock', False)}"
        for p in catalogue
    ])

    image_bytes = await image.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = image.content_type or "image/jpeg"

    vision_prompt = f"""You are a furniture matching assistant for Royal Enterprises, a furniture shop in Bangalore.

A customer uploaded a photo of furniture they like. Your job:
1. Analyse the furniture in the photo — type, style, color, size, material
2. Find the BEST matching products from our catalogue
3. Return top 1-3 closest matches with a brief reason

CATALOGUE:
{catalogue_context}

RULES:
- Only match to products in the catalogue
- Prefer in-stock items (out_of_stock: False)
- match_reason = 1 short sentence explaining similarity
- If photo is not furniture or unclear, return empty matches array

Respond ONLY with valid JSON, no markdown:
{{
  "detected": "brief description of furniture in photo",
  "matches": [
    {{
      "name": "exact product name from catalogue",
      "match_reason": "one sentence why this matches"
    }}
  ]
}}"""

    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                ]
            }],
            max_tokens=600,
            temperature=0.1,
            reasoning_effort="none"
        )

        raw = strip_thinking(response.choices[0].message.content)
        result = json.loads(raw)
        enriched = []
        for match in result.get("matches", []):
            for p in catalogue:
                if p["name"].lower() == match["name"].lower():
                    enriched.append({
                        "name": p["name"],
                        "price": p["price"],
                        "image": p.get("image"),
                        "images": p.get("images", [p["image"]] if p.get("image") else []),
                        "description": p.get("description", ""),
                        "match_reason": match.get("match_reason", "Similar style"),
                        "out_of_stock": p.get("out_of_stock", False)
                    })
                    break

        return {"status": "success", "detected": result.get("detected", ""), "matches": enriched}

    except json.JSONDecodeError:
        return {"status": "error", "message": "Could not parse AI response. Try a clearer photo."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ── AI Inventory Scanner ───────────────────────────────────────
@app.post("/scan-inventory")
async def scan_inventory(image: UploadFile = File(...)):
    catalogue = load_catalogue()
    catalogue_context = "\n".join([
        f"- {p['name']} | ₹{p['price']} | {p['description']}"
        for p in catalogue
    ])

    image_bytes = await image.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = image.content_type or "image/jpeg"

    vision_prompt = f"""You are an AI inventory scanner for Royal Enterprises, a furniture shop in Bangalore.

Look at this warehouse photo and identify all visible furniture items.

KNOWN CATALOGUE ITEMS:
{catalogue_context}

INSTRUCTIONS:
1. Identify each distinct furniture piece visible
2. Match each to the closest catalogue item name
3. Estimate color (e.g. Jungle Wood, White, Black, Tomato, White Marble)
4. Estimate size if possible — say "Unknown" if not clear
5. Count how many units of each item are visible
6. Give a confidence score: High / Medium / Low

Only match to items in the catalogue. Do not invent product names.

Respond ONLY with valid JSON, no markdown:
{{
  "detected_items": [
    {{
      "catalogue_name": "exact name from catalogue",
      "color": "detected color",
      "size": "detected size or Unknown",
      "count": 1,
      "confidence": "High"
    }}
  ],
  "scan_notes": "any useful observation about photo quality or visibility"
}}"""

    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                ]
            }],
            max_tokens=1000,
            temperature=0.1,
            reasoning_effort="none"
        )

        raw = strip_thinking(response.choices[0].message.content)
        result = json.loads(raw)
        enriched_items = []
        for item in result.get("detected_items", []):
            matched_product = None
            for p in catalogue:
                if p["name"].lower() == item["catalogue_name"].lower():
                    matched_product = p
                    break
            enriched_items.append({
                **item,
                "price": matched_product["price"] if matched_product else None,
                "in_catalogue": matched_product is not None,
                "currently_out_of_stock": matched_product.get("out_of_stock", False) if matched_product else False
            })

        return {
            "status": "success",
            "detected_items": enriched_items,
            "scan_notes": result.get("scan_notes", ""),
            "total_detected": len(enriched_items)
        }

    except json.JSONDecodeError:
        return {
            "status": "parse_error",
            "raw_response": raw,
            "detected_items": [],
            "scan_notes": f"RAW: {raw[:500]}"
        }

    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/scan-inventory/confirm")
async def confirm_scan(req: StockConfirmRequest):
    catalogue = load_catalogue()
    updated = []
    not_found = []

    for item in req.items:
        found = False
        for i, product in enumerate(catalogue):
            if product["name"].lower() == item["catalogue_name"].lower():
                if item.get("mark_in_stock", True):
                    catalogue[i]["out_of_stock"] = False
                scanned_count = item.get("count", 1)
                catalogue[i]["last_scanned_count"] = scanned_count
                catalogue[i]["last_scanned_at"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
                # A physical scan is a real stock count — use it to set stock_qty directly.
                catalogue[i]["stock_qty"] = scanned_count
                updated.append(product["name"])
                found = True
                break
        if not found:
            not_found.append(item["catalogue_name"])

    save_catalogue(catalogue)
    return {
        "status": "success",
        "updated": updated,
        "not_found": not_found,
        "message": f"{len(updated)} item(s) updated in inventory."
    }

# ── Quick Add: AI Auto-Fill ──────────────────────────────────────
@app.post("/quick-add/analyze")
async def quick_add_analyze(image: UploadFile = File(...)):
    """Analyze a single product photo and suggest name, category, description.
    Price is intentionally NOT suggested — owner always enters it manually."""

    image_bytes = await image.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = image.content_type or "image/jpeg"

    vision_prompt = """You are a product listing assistant for Royal Enterprises, a ready-made furniture shop in Bangalore.

Look at this photo of a single furniture item and suggest catalogue listing details.

INSTRUCTIONS:
1. Suggest a short, clear product name (e.g. "3-Door Wardrobe", "Wooden Dressing Table")
2. Pick the best matching category from: Wardrobe, Bed, Sofa, Dining, Dressing Table, Shoe Rack, Cupboard, Chair, Table, Storage, Other
3. Estimate size if visible (e.g. "6x4 feet", "Standard", "Unknown" if not clear)
4. Estimate the main color/finish (e.g. "Jungle Wood", "White", "Black", "Walnut Brown")
5. Write a warm, sales-friendly 1-2 sentence description mentioning material, color, and style if visible
6. Do NOT include or guess any price

If the photo is unclear or not furniture, set "detected" to false.

Respond ONLY with valid JSON, no markdown:
{
  "detected": true,
  "suggested_name": "...",
  "suggested_category": "...",
  "suggested_size": "...",
  "suggested_color": "...",
  "suggested_description": "..."
}"""

    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                ]
            }],
            max_tokens=400,
            temperature=0.2,
            reasoning_effort="none"
        )

        raw = strip_thinking(response.choices[0].message.content)
        result = json.loads(raw)
        return {"status": "success", **result}

    except json.JSONDecodeError:
        return {"status": "error", "message": "Could not read the photo clearly. Try again or fill in manually."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ── Analytics: most enquired + profit & loss ────────────────────
@app.get("/analytics/most-asked")
def most_asked_products(limit: int = 10):
    """Ranks products by how often they came up in customer chat — separate
    from actual sales, so the owner can see interest vs. conversion."""
    log = load_interest_log()
    counts = {}
    for entry in log:
        counts[entry["product"]] = counts.get(entry["product"], 0) + 1
    sorted_products = sorted(counts.items(), key=lambda x: -x[1])[:limit]
    return [{"product": p, "count": c} for p, c in sorted_products]

@app.get("/analytics/product-performance")
def product_performance():
    """Merges enquiry counts with actual order counts per product —
    a direct 'asked about vs. actually bought' view."""
    log = load_interest_log()
    orders = load_orders()
    enquiry_counts = {}
    for entry in log:
        enquiry_counts[entry["product"]] = enquiry_counts.get(entry["product"], 0) + 1
    order_counts = {}
    for o in orders:
        name = o.get("product", "Unknown")
        order_counts[name] = order_counts.get(name, 0) + 1
    all_products = set(enquiry_counts.keys()) | set(order_counts.keys())
    result = []
    for name in all_products:
        enquiries = enquiry_counts.get(name, 0)
        sold = order_counts.get(name, 0)
        result.append({
            "product": name,
            "enquiries": enquiries,
            "orders": sold,
            "conversion_pct": round((sold / enquiries) * 100) if enquiries else 0
        })
    result.sort(key=lambda x: -x["enquiries"])
    return result

@app.get("/analytics/pnl")
def profit_and_loss():
    """Owner-only. Modeled on how real inventory P&L works (e.g. Vyapar/Zoho Inventory):

    - COGS (Cost of Goods Sold) uses the cost price AT THE TIME each unit was sold
      (snapshotted on the order itself), never today's cost — otherwise editing a
      product's cost price would silently rewrite history for past sales.
    - Orders placed before this snapshot existed fall back to today's catalogue
      cost price, clearly flagged, since no better data exists for them.
    - Current Inventory Value is reported separately from sales — it's what's
      sitting on the shelf right now (stock_qty × cost_price), not part of P&L.
    """
    orders = load_orders()
    catalogue = load_catalogue()
    cost_map = {p["name"]: p.get("cost_price", 0) or 0 for p in catalogue}

    total_revenue = 0.0
    total_cogs = 0.0
    per_product = {}
    estimated_count = 0  # orders that had to fall back to today's cost price

    for o in orders:
        product = o.get("product", "Unknown")
        raw_price = str(o.get("price", "0")).replace("₹", "").replace(",", "").strip()
        try:
            sell_price = float(raw_price)
        except ValueError:
            sell_price = 0.0

        if "cost_price_at_sale" in o:
            unit_cost = o["cost_price_at_sale"] or 0
        else:
            unit_cost = cost_map.get(product, 0)
            estimated_count += 1

        profit = sell_price - unit_cost

        total_revenue += sell_price
        total_cogs += unit_cost

        if product not in per_product:
            per_product[product] = {"units": 0, "revenue": 0.0, "cogs": 0.0, "profit": 0.0}
        per_product[product]["units"] += 1
        per_product[product]["revenue"] += sell_price
        per_product[product]["cogs"] += unit_cost
        per_product[product]["profit"] += profit

    gross_profit = total_revenue - total_cogs
    gross_margin_pct = round((gross_profit / total_revenue) * 100, 1) if total_revenue > 0 else 0

    breakdown = []
    for name, v in per_product.items():
        stock_qty = next((p.get("stock_qty", 0) or 0 for p in catalogue if p["name"] == name), 0)
        cost_now = cost_map.get(name, 0)
        breakdown.append({
            "product": name,
            "units_sold": v["units"],
            "revenue": round(v["revenue"], 2),
            "cogs": round(v["cogs"], 2),
            "profit": round(v["profit"], 2),
            "margin_pct": round((v["profit"] / v["revenue"]) * 100, 1) if v["revenue"] > 0 else 0,
            "stock_on_hand": stock_qty,
            "inventory_value": round(stock_qty * cost_now, 2)
        })
    breakdown.sort(key=lambda x: -x["profit"])

    # Current inventory value across the WHOLE catalogue (not just products that sold) —
    # this is the "what's on my shelf right now, in ₹" figure.
    total_inventory_value = sum((p.get("stock_qty", 0) or 0) * (p.get("cost_price", 0) or 0) for p in catalogue)
    total_units_in_stock = sum((p.get("stock_qty", 0) or 0) for p in catalogue)

    return {
        "total_revenue": round(total_revenue, 2),
        "total_cogs": round(total_cogs, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin_pct": gross_margin_pct,
        "order_count": len(orders),
        "estimated_cost_orders": estimated_count,  # orders lacking a cost snapshot
        "total_inventory_value": round(total_inventory_value, 2),
        "total_units_in_stock": total_units_in_stock,
        "breakdown": breakdown
    }

# ── Offers (owner-controlled, Amazon/Flipkart-style pricing) ────
class OfferCreate(BaseModel):
    product_name: str
    offer_price: float

@app.get("/offers")
def get_active_offers():
    """Public — customer-facing site/chat should call this."""
    return compute_active_offers()

@app.get("/offers/all")
def get_all_offers():
    """Owner-only — includes inactive offers for management."""
    offers = load_offers()
    catalogue = load_catalogue()
    cat_map = {p["name"]: p for p in catalogue}
    result = []
    for o in offers:
        product = cat_map.get(o["product_name"], {})
        result.append({
            **o,
            "original_price": product.get("price"),
            "image": product.get("image"),
            "out_of_stock": product.get("out_of_stock", False)
        })
    return result

@app.post("/offers")
def create_offer(offer: OfferCreate):
    catalogue = load_catalogue()
    if not any(p["name"] == offer.product_name for p in catalogue):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Product not found in catalogue"})
    offers = load_offers()
    new_offer = {
        "id": str(uuid.uuid4())[:8].upper(),
        "product_name": offer.product_name,
        "offer_price": offer.offer_price,
        "active": True,
        "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }
    offers.append(new_offer)
    save_offers(offers)
    return {"status": "created", "offer": new_offer}

@app.patch("/offers/{offer_id}/toggle")
def toggle_offer(offer_id: str):
    offers = load_offers()
    for o in offers:
        if o["id"] == offer_id:
            o["active"] = not o.get("active", True)
            save_offers(offers)
            return {"status": "updated", "active": o["active"]}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Offer not found"})

@app.delete("/offers/{offer_id}")
def delete_offer(offer_id: str):
    offers = load_offers()
    offers = [o for o in offers if o["id"] != offer_id]
    save_offers(offers)
    return {"status": "deleted"}

# ── Scheduled Callbacks (customer asked the team to call them) ──
@app.get("/callbacks")
def get_callbacks():
    return load_callbacks()

@app.patch("/callbacks/{callback_id}/complete")
def complete_callback(callback_id: str):
    callbacks = load_callbacks()
    for c in callbacks:
        if c["id"] == callback_id:
            c["status"] = "done"
            save_callbacks(callbacks)
            return {"status": "updated"}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Callback not found"})

@app.delete("/callbacks/{callback_id}")
def delete_callback(callback_id: str):
    callbacks = load_callbacks()
    callbacks = [c for c in callbacks if c["id"] != callback_id]
    save_callbacks(callbacks)
    return {"status": "deleted"}

# ── QR Code ───────────────────────────────────────────────────
@app.get("/generate-qr")
def generate_qr(url: str = "https://shopbot-ai-1009.onrender.com"):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3D2B1F", back_color="#FDF6ED")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png", headers={
        "Content-Disposition": "inline; filename=shopbot-qr.png"
    })
# ── PWA ───────────────────────────────────────────────────────
@app.get("/sw.js")
def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")

@app.get("/manifest.json")
def manifest():
    return FileResponse("static/manifest.json", media_type="application/json")

@app.get("/manifest-owner.json")
def manifest_owner():
    return FileResponse("static/manifest-owner.json", media_type="application/json")