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
from fastapi.responses import StreamingResponse
from datetime import datetime

app = FastAPI()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CATALOGUE_FILE = "catalogue.json"
ORDERS_FILE = "orders.json"
UPLOAD_DIR = "static/uploads"
OWNER_PASSWORD = "royal123"

os.makedirs(UPLOAD_DIR, exist_ok=True)

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

def save_upload(file) -> str:
    ext = file.filename.split(".")[-1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return f"/static/uploads/{filename}"

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

@app.post("/catalogue")
async def add_product(
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
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
            path = url.lstrip("/")
            if os.path.exists(path):
                os.remove(path)
        if product.get("image") and product["image"] not in product.get("images", []):
            path = product["image"].lstrip("/")
            if os.path.exists(path):
                os.remove(path)
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

@app.put("/catalogue/{index}")
async def edit_product(
    index: int,
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
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
                path = url.lstrip("/")
                if os.path.exists(path):
                    os.remove(path)
            existing["images"] = new_images
            existing["image"] = new_images[0]

        existing["name"] = name
        existing["price"] = price
        existing["category"] = category
        existing["description"] = description
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
            path = images[img_index].lstrip("/")
            if os.path.exists(path):
                os.remove(path)
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

    system_prompt = f"""You are ShopBot, a friendly AI sales assistant for Royal Enterprises — a ready-made furniture retail shop in Dommasandra, Sarjapur Road, Bangalore.

SHOP CONTACT DETAILS (use these when customer asks how to contact, never use customer's own number):
- Phone: +91 8553537786
- Address: Royal Enterprises, Dommasandra, Sarjapur Road, Bangalore
- Timings: 10 AM to 9 PM, all days

You help customers find products, answer questions about prices, and confirm orders.
Always respond in a helpful, warm, conversational tone. Use ₹ for prices.
{f"You are talking to: {customer_info}" if customer_info else ""}

CRITICAL: When a customer asks how to contact or asks for shop number — always give the SHOP contact details above, NEVER the customer's own phone number.

IMPORTANT: Only recommend products that exist in the catalogue below. Never mention products not in the catalogue.
If a product has out_of_stock: True, tell the customer it is currently unavailable and suggest the closest alternative.
Remember the full conversation context.

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
- DO NOT use markdown tables or | symbols — they don't render in chat
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

When a customer confirms a purchase, respond with a clear order summary and add this exact line at the end:
PLACE_ORDER:[product name]|[price]

CRITICAL RULE: Whenever you mention or recommend any specific product, add this at the very end:
SHOW_IMAGE:[exact product name from catalogue]

SHOP CATALOGUE:
{catalogue_text}"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg in req.history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": req.message})

    response = client.chat.completions.create(
        model="qwen/qwen3.6-27b",
        messages=messages,
        max_tokens=500
    )

    reply = response.choices[0].message.content
    # Strip thinking block from qwen model
    if "</think>" in reply:
        reply = reply.split("</think>")[-1].strip()
    image_url = None
    order_data = None

    lines = reply.split("\n")
    clean_lines = []
    for line in lines:
        if "SHOW_IMAGE:" in line:
            product_name = line.split("SHOW_IMAGE:")[-1].strip().lower()
            for p in catalogue:
                if p["name"].lower() == product_name and p.get("image"):
                    image_url = p["image"]
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
        else:
            clean_lines.append(line)

    reply = "\n".join(clean_lines).strip()

    if order_data:
        orders = load_orders()
        order_data["id"] = str(uuid.uuid4())[:8].upper()
        order_data["timestamp"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        order_data["status"] = "pending"
        orders.append(order_data)
        save_orders(orders)

    return {"reply": reply, "image": image_url, "order": order_data}

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
            temperature=0.1
        )

        raw = response.choices[0].message.content.strip()
        if "<think>" in raw and "</think>" in raw:
            raw = raw.split("</think>", 1)[1].strip()
        elif "<think>" in raw:
            # thinking block didn't close — extract JSON manually
            import re
            json_match = re.search(r'\{[\s\S]*\}', raw)
            raw = json_match.group(0) if json_match else raw
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

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
            temperature=0.1
        )

        raw = response.choices[0].message.content.strip()
        if "<think>" in raw and "</think>" in raw:
            raw = raw.split("</think>", 1)[1].strip()
        elif "<think>" in raw:
            # thinking block didn't close — extract JSON manually
            import re
            json_match = re.search(r'\{[\s\S]*\}', raw)
            raw = json_match.group(0) if json_match else raw
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

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
                catalogue[i]["last_scanned_count"] = item.get("count", 1)
                catalogue[i]["last_scanned_at"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
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