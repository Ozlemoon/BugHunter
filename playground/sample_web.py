"""
Sample Web Application Target with Deliberate Real-World Flaws.

Documented deliberate flaws:
1. Form crash: POST /submit-contact returns 500 when the email has no '@'.
2. Broken link: the home page links to /legacy-api-docs, which is a 404.
3. Broken asset: the home page loads /static/missing_logo_icon.png (404).
4. Reflected XSS: GET /search echoes the query into HTML without escaping.
5. Missing hardening headers (X-Content-Type-Options, X-Frame-Options, CSP).
"""

import html
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(
    title="BugShop Web App Target",
    version="1.0.0",
    description="Sample web frontend target for BugHunter AI testing."
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory storage for feedback submissions
CONTACT_SUBMISSIONS = []
CATALOG = ["Wireless Mouse", "Mechanical Keyboard", "USB-C Monitor"]


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the homepage containing a broken link and broken image asset."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    """Render the contact feedback form with weak HTML validation."""
    return templates.TemplateResponse(request=request, name="contact.html")


# WEB BUG 1: Unhandled Server Crash on Invalid/Empty Email in Form
@app.post("/submit-contact")
async def submit_contact(
    name: str = Form(""),
    email: str = Form(""),
    message: str = Form("")
):
    """
    Handle contact form submissions.
    FLAW: Crashes with IndexError / 500 Internal Server Error when email is empty or contains no '@'
    instead of returning clean validation errors (422 or 400).
    """
    # Deliberate bug: assumes '@' is always present!
    # If email is empty ("") or has no '@', split produces 1 element -> IndexError -> 500 error!
    domain = email.split("@")[1]

    submission = {
        "id": len(CONTACT_SUBMISSIONS) + 1,
        "name": name,
        "email": email,
        "domain": domain,
        "message": message
    }
    CONTACT_SUBMISSIONS.append(submission)
    return JSONResponse(
        status_code=200,
        content={"status": "success", "message": f"Thank you {name}, we received your inquiry."}
    )


# Note: Notice that /legacy-api-docs and /static/missing_logo_icon.png are NOT defined,
# so requests to them will naturally return 404 Not Found, testing the crawler/link checker.


# WEB BUG 4: Reflected XSS
@app.get("/search", response_class=HTMLResponse)
async def search(q: str = ""):
    """
    Search the catalog.
    FLAW: product names are escaped, but the query itself is echoed raw into the page.
    """
    matches = [name for name in CATALOG if q.lower() in name.lower()]
    items = "".join(f"<li>{html.escape(name)}</li>" for name in matches)
    heading = f"<h1>Results for {q}</h1>"
    return HTMLResponse(f"<!DOCTYPE html><html><head><title>Search</title></head>"
                        f"<body>{heading}<ul>{items}</ul><a href=\"/\">Home</a></body></html>")
