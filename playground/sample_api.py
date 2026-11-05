"""
Sample E-Commerce REST API with Deliberate Real-World Bugs.
Designed as a live target for BugHunter AI.

Documented deliberate flaws:
1. Boundary Bug: POST /orders accepts negative quantity (e.g. quantity: -5).
2. Server Crash / 500 Error: GET /users/{user_id}/profile crashes with unhandled KeyError on non-existent users.
3. Security / Broken Authorization: DELETE /products/{product_id} lacks authorization checks.
4. Performance / SLA Violation: GET /products/heavy-search takes > 1200ms, violating sub-second SLA criteria.
"""

import asyncio
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Response
from pydantic import BaseModel, Field

app = FastAPI(
    title="ShopMaster Demo API",
    version="1.0.0",
    description="E-Commerce API target for BugHunter AI autonomous testing."
)

# In-memory database
PRODUCTS_DB: Dict[int, Dict[str, Any]] = {
    1: {"id": 1, "name": "Wireless Mouse", "price": 25.0, "stock": 100},
    2: {"id": 2, "name": "Mechanical Keyboard", "price": 85.0, "stock": 50},
    3: {"id": 3, "name": "USB-C Monitor", "price": 220.0, "stock": 20},
}

USERS_DB: Dict[int, Dict[str, Any]] = {
    101: {"id": 101, "name": "Alice Smith", "email": "alice@example.com", "role": "admin"},
    102: {"id": 102, "name": "Bob Jones", "email": "bob@example.com", "role": "customer"},
}

USER_PROFILES_DB: Dict[int, Dict[str, Any]] = {
    101: {"address": "123 Main St", "phone": "555-0101"},
    # Note: user 102 profile is intentionally missing to test edge cases!
}

ORDERS_DB: List[Dict[str, Any]] = []


class OrderCreate(BaseModel):
    product_id: int
    # Deliberate bug: No gt=0 validation on quantity!
    quantity: int
    customer_id: int


class ProductCreate(BaseModel):
    name: str
    price: float
    stock: int


@app.get("/")
def root():
    return {
        "service": "ShopMaster Demo API",
        "status": "online",
        "version": "1.0.0",
        "endpoints": ["/products", "/orders", "/users/{user_id}/profile"]
    }


@app.get("/products", response_model=List[Dict[str, Any]])
def get_products():
    """Retrieve all products in the catalog."""
    return list(PRODUCTS_DB.values())


@app.get("/products/{product_id}")
def get_product(product_id: int):
    """Retrieve product details by ID."""
    if product_id not in PRODUCTS_DB:
        raise HTTPException(status_code=404, detail="Product not found")
    return PRODUCTS_DB[product_id]


@app.post("/products", status_code=201)
def create_product(product: ProductCreate):
    """Create a new product."""
    new_id = max(PRODUCTS_DB.keys(), default=0) + 1
    new_prod = {
        "id": new_id,
        "name": product.name,
        "price": product.price,
        "stock": product.stock,
    }
    PRODUCTS_DB[new_id] = new_prod
    return new_prod


# BUG 3: Broken Access Control (BOLA / Missing Auth on Destructive Endpoint)
# Anyone can delete any product without an Authorization token or admin check!
@app.delete("/products/{product_id}")
def delete_product(product_id: int, authorization: Optional[str] = Header(None)):
    """
    Delete a product from the catalog.
    FLAW: authorization header is neither enforced nor checked for admin role.
    """
    if product_id not in PRODUCTS_DB:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Intentionally missing: if not authorization or authorization != "Bearer admin-token": raise 401/403
    deleted = PRODUCTS_DB.pop(product_id)
    return {"message": "Product deleted successfully", "deleted": deleted}


# BUG 1: Boundary & Business Logic Bug
# Accepts negative quantity (e.g. quantity: -10), corrupting inventory and balance!
@app.post("/orders", status_code=201)
def create_order(order: OrderCreate):
    """
    Create a new customer order.
    FLAW: Missing quantity <= 0 validation check.
    """
    if order.product_id not in PRODUCTS_DB:
        raise HTTPException(status_code=404, detail="Product not found")
    
    product = PRODUCTS_DB[order.product_id]
    
    # Intentionally missing:
    # if order.quantity <= 0:
    #     raise HTTPException(status_code=422, detail="Quantity must be greater than zero")

    total_price = product["price"] * order.quantity
    product["stock"] -= order.quantity  # Negative quantity actually increases stock!

    order_record = {
        "order_id": len(ORDERS_DB) + 1,
        "product_id": order.product_id,
        "product_name": product["name"],
        "quantity": order.quantity,
        "total_price": total_price,
        "customer_id": order.customer_id,
    }
    ORDERS_DB.append(order_record)
    return order_record


# BUG 2: Server Crash / HTTP 500 Unhandled Exception
# Fails with unhandled KeyError instead of returning 404 or empty profile object.
@app.get("/users/{user_id}/profile")
def get_user_profile(user_id: int):
    """
    Retrieve user extended profile.
    FLAW: Direct dictionary lookup crashes with KeyError (500) if user or profile doesn't exist.
    """
    # Direct dictionary lookup without .get() or existence check:
    # Crashes on user 102 or invalid ID with KeyError -> Starlette converts to 500 Internal Server Error
    profile_data = USER_PROFILES_DB[user_id]
    return {
        "user_id": user_id,
        "profile": profile_data
    }


# BUG 4: Performance / SLA Violation
@app.get("/products/heavy-search")
async def heavy_search(query: str = ""):
    """
    Search products with deliberate artificial delay simulating unindexed heavy query.
    FLAW: Exceeds standard <500ms SLA criteria.
    """
    await asyncio.sleep(1.3)  # Deliberately sleeps 1.3 seconds
    results = [p for p in PRODUCTS_DB.values() if query.lower() in p["name"].lower()]
    return {"query": query, "count": len(results), "results": results}
