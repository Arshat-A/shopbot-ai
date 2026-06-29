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
from datetime import datetime

app = FastAPI()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
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
    items: list  # list of confirmed items from scanner

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/customer.html")

@app.get("/owner")
def owner_page():
    return FileResponse("static/owner.html")

# --- Auth ---
@app.post("/auth/login")
def login(req: LoginRequest):
    if req.password == OWNER_PASSWORD:
        return {"status": "success"}
    return JSONResponse(status_code=401, content={"status": "error", "message": "Wrong password"})

# --- Catalogue ---
@app.get("/catalogue")
def get_catalogue():
    return load_catalogue()

@app.post("/catalogue")
async def add_product(
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    image: Optional[UploadFile] = File(None)
):
    catalogue = load_catalogue()
    image_url = None
    if image and image.filename:
        ext = image.filename.split(".")[-1]
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            shutil.copyfileobj(image.file, f)
        image_url = f"/static/uploads/{filename}"

    product = {
        "name": name,
        "price": price,
        "category": category,
        "description": description,
        "image": image_url,
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
        if product.get("image"):
            filepath = product["image"].lstrip("/")
            if os.path.exists(filepath):
                os.remove(filepath)
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
    image: Optional[UploadFile] = File(None)
):
    catalogue = load_catalogue()
    if 0 <= index < len(catalogue):
        existing = catalogue[index]
        if image and image.filename:
            if existing.get("image"):
                old_path = existing["image"].lstrip("/")
                if os.path.exists(old_path):
                    os.remove(old_path)
            ext = image.filename.split(".")[-1]
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            with open(filepath, "wb") as f:
                shutil.copyfileobj(image.file, f)
            existing["image"] = f"/static/uploads/{filename}"
        existing["name"] = name
        existing["price"] = price
        existing["category"] = category
        existing["description"] = description
        catalogue[index] = existing
        save_catalogue(catalogue)
        return {"status": "updated", "product": existing}
    return {"status": "error"}

# --- Orders ---
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

# --- Chat ---
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

You help customers find products, answer questions about prices, and confirm orders.
Always respond in a helpful, warm, conversational tone. Use ₹ for prices.
{f"You are talking to: {customer_info}" if customer_info else ""}
IMPORTANT: Only recommend products that exist in the catalogue below. Never mention products not in the catalogue.
If a product has out_of_stock: True, tell the customer it is currently unavailable and suggest the closest alternative.
Remember the full conversation context.
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
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=500
    )

    reply = response.choices[0].message.content
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


# ─────────────────────────────────────────────────────────────
# AI VISION INVENTORY SCANNER
# ─────────────────────────────────────────────────────────────

@app.post("/scan-inventory")
async def scan_inventory(image: UploadFile = File(...)):
    """
    Takes a warehouse photo, uses Groq Llama 4 Scout vision to detect
    furniture items and match them against the Royal Enterprises catalogue.
    Returns detected items with suggested name, color, size, count.
    Human confirmation required before stock is updated.
    """
    catalogue = load_catalogue()

    # Build catalogue context for the AI
    catalogue_names = list(set([p["name"] for p in catalogue]))
    catalogue_context = "\n".join([
        f"- {p['name']} | ₹{p['price']} | {p['description']}"
        for p in catalogue
    ])

    # Read and encode image as base64
    image_bytes = await image.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = image.content_type or "image/jpeg"

    vision_prompt = f"""You are an AI inventory scanner for Royal Enterprises, a furniture shop in Bangalore.

Your job is to look at this warehouse photo and identify all visible furniture items.

KNOWN CATALOGUE ITEMS (match only to these):
{catalogue_context}

INSTRUCTIONS:
1. Identify each distinct furniture piece visible in the image
2. Match each piece to the closest catalogue item name
3. Estimate color (e.g. Jungle Wood, White, Black, Tomato, White Marble)
4. Estimate size if possible (e.g. 3 feet, 6 feet, 4 feet) — say "Unknown" if not clear
5. Count how many units of each item are visible
6. Give a confidence score: High / Medium / Low

IMPORTANT: Only match to items in the catalogue above. Do not invent product names.

Respond ONLY with valid JSON in this exact format, no explanation, no markdown:
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
  "scan_notes": "any useful observation about the photo quality or visibility"
}}"""

    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": vision_prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1000,
            temperature=0.1
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        # Enrich with catalogue match info (price, existing stock)
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
        # If AI didn't return clean JSON, return raw text for debugging
        return {
            "status": "parse_error",
            "raw_response": raw,
            "detected_items": [],
            "scan_notes": "AI response could not be parsed. Try a clearer photo."
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )


@app.post("/scan-inventory/confirm")
async def confirm_scan(req: StockConfirmRequest):
    """
    After human reviews detected items, confirm updates stock.
    Each item in req.items: { catalogue_name, count, mark_in_stock: true/false }
    """
    catalogue = load_catalogue()
    updated = []
    not_found = []

    for item in req.items:
        found = False
        for i, product in enumerate(catalogue):
            if product["name"].lower() == item["catalogue_name"].lower():
                # Mark as in stock if it was out of stock
                if item.get("mark_in_stock", True):
                    catalogue[i]["out_of_stock"] = False
                # Store scanned count as metadata
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