"""
The Founder's File — Business Startup Helper
Flask backend. Serves the frontend and proxies three AI-assisted endpoints
to the Anthropic API: /api/analyze, /api/model, /api/tax.

Also has a small SQLite database (via SQLAlchemy) for user accounts and
saved submissions (idea + results), so a signed-in founder can come back
to their past runs. Sign-in is required for the whole app; Google OAuth is
supported alongside email/password (see .env.example for setup).

Setup:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...      (or put it in a .env file)
    python app.py
Then open http://localhost:5000
"""

import json
import os
import secrets
import logging
import traceback
from functools import wraps
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic

from models import db, User, Submission, migrate_schema

# Shared Anthropic client + model config (also used by visualization.py, so
# that module never imports from this one — see config.py's docstring for
# why that matters under `python app.py`).
from config import client as ff_client, logger as ff_logger, MODEL as FF_MODEL
from scoring import compute_final_score, score_breakdown, score_to_verdict, SCORE_WEIGHTS
from llm_json import extract_json, as_number, clamp_score

# Load a .env file if python-dotenv is installed (optional convenience)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# Business > Visualization lives in its own file (visualization.py) and is
# registered here as a Blueprint so its routes/prompts don't clutter this
# file. visualization.py imports its Anthropic client from config.py, never
# from this module, to avoid a double Flask app / double Anthropic client
# under `python app.py` (see config.py's docstring).
from visualization import viz_bp  # noqa: E402
app.register_blueprint(viz_bp)

# Secrets: require SECRET_KEY in production, but auto-generate a per-process
# dev key with a loud warning instead of hard-crashing when it's unset — a
# founder running this locally for the first time shouldn't hit a stack
# trace before they've even created a .env file. In production (FLASK_ENV
# or a real deploy), set SECRET_KEY explicitly so sessions survive restarts.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY not set in the environment — using a random key "
        "for this run only. Sessions will not survive a restart. Add "
        "SECRET_KEY=<random hex> to your .env file to fix this."
    )
app.secret_key = SECRET_KEY

# Whether this process is running as a local dev server. Controls whether we
# force HTTPS and mark cookies Secure — both of which would otherwise break
# a plain http://localhost dev setup.
IS_PRODUCTION = os.environ.get("FLASK_ENV") == "production" or os.environ.get("RENDER") or os.environ.get("DYNO")

# No CSRF token middleware: every write route here only accepts
# application/json bodies via fetch() from our own frontend. A cross-site
# <form> can't set a JSON content-type, and SESSION_COOKIE_SAMESITE="Lax"
# below already stops the session cookie from riding along on a cross-site
# POST. Adding flask-wtf's CSRFProtect on top would require issuing and
# threading a token through every postJSON/deleteJSON call in app.js for a
# same-origin JSON API that isn't exposed to browser form submission — not
# worth the complexity/breakage risk here.

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# Talisman's DEFAULT Content-Security-Policy is "default-src 'self'", which
# blocks every inline style="..." attribute on the page — including the
# style="display:none" on our modal overlays. With that blocked, modals fall
# back to their CSS (flex/visible) and render open and stacked on page load,
# eating every click on the page. We need a CSP that actually allows what
# this app does: inline styles (used throughout index.html) and Google
# Fonts' stylesheet + font files.
CSP = {
    "default-src": "'self'",
    "style-src": ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
    "font-src": ["'self'", "https://fonts.gstatic.com"],
    # Visualization (Round 2) compiles GraphViz.jsx in-browser using
    # React/ReactDOM/Babel-standalone, vendored locally under
    # static/vendor/ (self-hosted rather than loaded from unpkg.com) so it
    # works regardless of outbound network access — and so script-src can
    # stay strictly 'self' with no third-party script origin allowed.
    "script-src": ["'self'"],
    "img-src": ["'self'", "data:"],
}

if IS_PRODUCTION:
    # Force HTTPS and set HSTS only when actually deployed — never in local dev.
    Talisman(
        app,
        force_https=True,
        strict_transport_security=True,
        strict_transport_security_max_age=31536000,
        content_security_policy=CSP,
    )
    app.config["SESSION_COOKIE_SECURE"] = True
else:
    # Local dev: keep Talisman's other safe-by-default headers (X-Frame-Options,
    # etc.) without forcing HTTPS, which would otherwise make
    # http://localhost:5000 redirect-loop. Talisman also defaults to marking
    # the session cookie Secure regardless of force_https, which silently
    # breaks every session over plain http://localhost — every login/signup
    # would 401 immediately after, since the browser refuses to send a
    # Secure cookie back over HTTP. Must explicitly turn it off here.
    Talisman(app, force_https=False, session_cookie_secure=False, content_security_policy=CSP)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Request size limits
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB

basedir = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "founders_file.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# BUG FIX #8: Enable Database Pooling
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}

db.init_app(app)
with app.app_context():
    db.create_all()
    migrate_schema(db.engine)
    from models import start_backup_scheduler
    start_backup_scheduler(app)

# Audit log: who did what, when — signups, logins, saves, deletes. Written
# next to the app so it's easy to find; if the file can't be opened for some
# reason (read-only filesystem, permissions), fall back to console logging
# instead of crashing the whole app on startup.
audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)
try:
    audit_path = os.path.join(basedir, "audit.log")
    audit_handler = logging.FileHandler(audit_path)
    audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    audit_logger.addHandler(audit_handler)
except OSError as exc:
    print(f"Could not open audit.log ({exc}) — audit events will go to the console instead.")
    audit_logger.addHandler(logging.StreamHandler())

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5-20250929"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}


def extract_json_object(text: str) -> str:
    """Pull a JSON object out of a model response that may have surrounding
    prose or a markdown fence around it (common when the model does visible
    reasoning or web-search narration before its final structured answer)."""
    text = text.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def ask_claude_json(system_prompt: str, user_content: str, max_tokens: int = 1200, use_search: bool = False) -> dict:
    """Call the Anthropic API and parse a strict-JSON response.

    The system prompt is marked cache_control="ephemeral" — since these
    prompts are large, static, and reused across every request to the same
    endpoint, Anthropic's prompt caching means repeat calls (within the
    cache TTL) are billed at a fraction of the input-token cost instead of
    re-processing the whole instruction block every time. This is a free
    cost/latency win with zero quality tradeoff.

    use_search=True gives Claude Anthropic's native web_search tool so it can
    check real, current facts and return real source URLs instead of
    inventing plausible-sounding ones. When enabled, the response may contain
    several text blocks (search narration, then a final structured answer),
    so we take the LAST text block and extract the JSON object from it rather
    than concatenating everything.
    """
    kwargs = {}
    if use_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
        **kwargs,
    )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        raise RuntimeError("The response didn't contain any text to parse.")
    candidate = extract_json_object(text_blocks[-1])

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "The response was cut off before it finished (ran out of output budget). "
                "Try again, or shorten the input — this endpoint's token limit may need raising."
            ) from exc
        raise RuntimeError(f"Got a response back that wasn't valid JSON ({exc}).") from exc


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def login_required(view):
    """Block an endpoint entirely unless a signed-in session is present.

    The whole app is gated behind an account, so every route that does
    real work (running the AI rounds, reading/writing saved submissions)
    uses this instead of trusting the frontend to hide things.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "Sign in required"}), 401
        return view(*args, **kwargs)
    return wrapped


def log_change(user_id, action, item_id):
    """BUG FIX #17: Log all data changes for audit trail."""
    audit_logger.info(f"{user_id} | {action} | {item_id}")


# BUG FIX #7: Add Error Handlers
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Invalid request"}), 400


@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "Forbidden"}), 403


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    app.logger.error(f'Error: {e}')
    return jsonify({"error": "Internal server error"}), 500


@app.route("/")
@limiter.limit("100 per hour")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Auth — email/password
# ---------------------------------------------------------------------------

@app.route("/api/signup", methods=["POST"])
@limiter.limit("5 per hour")
def signup():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    # BUG FIX #3: Input Validation
    if not email or "@" not in email or len(email) > 255:
        return jsonify({"error": "A valid email is required"}), 400
    if len(password) < 8 or len(password) > 128:
        return jsonify({"error": "Password must be 8-128 characters"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists"}), 409

    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        auth_provider="password",
        has_onboarded=False,
    )
    db.session.add(user)
    db.session.commit()

    session["user_id"] = user.id
    log_change(user.id, "SIGNUP", user.id)
    return jsonify(user.to_public_dict()), 201


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per hour")
def login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    # BUG FIX #3: Input Validation
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email or password"}), 401

    user = User.query.filter_by(email=email).first()
    if not user or user.auth_provider != "password" or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user.id
    log_change(user.id, "LOGIN", user.id)
    return jsonify(user.to_public_dict())


@app.route("/api/logout", methods=["POST"])
@limiter.limit("100 per hour")
def logout():
    user = current_user()
    if user:
        log_change(user.id, "LOGOUT", user.id)
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/me")
@limiter.limit("100 per hour")
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user.to_public_dict()})


@app.route("/api/onboarding-complete", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def onboarding_complete():
    user = current_user()
    user.has_onboarded = True
    db.session.commit()
    log_change(user.id, "ONBOARDING_COMPLETE", user.id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Auth — Google OAuth (optional; disabled until GOOGLE_CLIENT_ID/SECRET are set)
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

oauth = None
if GOOGLE_ENABLED:
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@app.route("/api/auth-providers")
@limiter.limit("100 per hour")
def auth_providers():
    """Tells the frontend which social login buttons to actually enable."""
    return jsonify({"google": GOOGLE_ENABLED})


@app.route("/auth/google/login")
@limiter.limit("50 per hour")
def google_login():
    if not GOOGLE_ENABLED:
        return jsonify({"error": "Google sign-in isn't configured on this server yet."}), 503
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
@limiter.limit("50 per hour")
def google_callback():
    if not GOOGLE_ENABLED:
        return jsonify({"error": "Google sign-in isn't configured on this server yet."}), 503

    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.parse_id_token(token)
    email = (userinfo.get("email") or "").strip().lower()
    sub = userinfo.get("sub")

    # BUG FIX #3: Input Validation
    if not email or "@" not in email or not sub or len(email) > 255:
        return redirect("/?auth_error=google_invalid")

    user = User.query.filter_by(google_sub=sub).first()
    if user is None:
        user = User.query.filter_by(email=email).first()
        if user is None:
            # Brand new account. password_hash stays NOT NULL-satisfied with
            # an unusable random hash — this account can only ever sign in
            # via Google, never with a password.
            user = User(
                email=email,
                password_hash=generate_password_hash(secrets.token_urlsafe(32)),
                auth_provider="google",
                google_sub=sub,
                has_onboarded=False,
            )
            db.session.add(user)
        else:
            # An email/password account with a matching email — link Google
            # as an additional way in, rather than creating a duplicate.
            user.google_sub = sub
        db.session.commit()

    session["user_id"] = user.id
    log_change(user.id, "GOOGLE_LOGIN", user.id)
    return redirect("/")


# ---------------------------------------------------------------------------
# Saved submissions (idea + final results), tied to the signed-in user
# ---------------------------------------------------------------------------

@app.route("/api/submissions", methods=["GET"])
@login_required
@limiter.limit("50 per hour")
def list_submissions():
    user = current_user()
    # BUG FIX #14: Add Pagination
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    pagination = Submission.query.filter_by(user_id=user.id) \
        .order_by(Submission.created_at.desc()) \
        .paginate(page=page, per_page=per_page)

    return jsonify({
        'items': [s.to_dict() for s in pagination.items],
        'total': pagination.total,
        'page': page,
        'pages': pagination.pages
    })


@app.route("/api/submissions", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def save_submission():
    """Create or update a saved submission for the signed-in user.

    Body: { "id": <optional, updates existing>, "idea": str,
            "analyzeResult": obj|null, "modelResult": obj|null, "taxResult": obj|null }
    """
    user = current_user()

    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()

    # BUG FIX #3: Input Validation
    if not idea or len(idea) > 5000:
        return jsonify({"error": "Missing or invalid idea"}), 400

    submission_id = data.get("id")
    submission = None
    if submission_id:
        # BUG FIX #4: Authentication Checks — Verify user owns submission
        submission = Submission.query.filter_by(id=submission_id, user_id=user.id).first()

    if submission is None:
        submission = Submission(user_id=user.id, idea=idea)
        db.session.add(submission)

    submission.idea = idea
    if "caseName" in data:
        case_name = (data.get("caseName") or "").strip() or None
        if case_name and len(case_name) > 255:
            return jsonify({"error": "Case name too long"}), 400
        submission.case_name = case_name
    if "budget" in data:
        budget = (data.get("budget") or "").strip() or None
        if budget and len(budget) > 120:
            return jsonify({"error": "Budget too long"}), 400
        submission.budget = budget
    if "location" in data:
        location = (data.get("location") or "").strip() or None
        if location and len(location) > 255:
            return jsonify({"error": "Location too long"}), 400
        submission.location = location
    if "analyzeResult" in data:
        submission.analyze_result = data.get("analyzeResult")
    if "modelResult" in data:
        submission.model_result = data.get("modelResult")
    if "taxResult" in data:
        submission.tax_result = data.get("taxResult")

    db.session.commit()
    log_change(user.id, "SAVE_SUBMISSION", submission.id)
    return jsonify(submission.to_dict())


@app.route("/api/submissions/<int:submission_id>", methods=["DELETE"])
@login_required
@limiter.limit("20 per hour")
def delete_submission(submission_id):
    user = current_user()
    # BUG FIX #4: Authentication Checks — Verify user owns submission
    submission = Submission.query.filter_by(id=submission_id, user_id=user.id).first()
    if not submission:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(submission)
    db.session.commit()
    log_change(user.id, "DELETE_SUBMISSION", submission_id)
    return jsonify({"ok": True})


@app.route("/submissions/<int:submission_id>/view")
@login_required
@limiter.limit("100 per hour")
def view_submission(submission_id):
    """A standalone, print-friendly read-only page for one saved idea —
    opened in a new tab from 'My saved ideas'. Browser print-to-PDF gives
    the user a PDF without us needing a PDF-generation dependency."""
    user = current_user()
    # BUG FIX #4: Authentication Checks — Verify user owns submission
    submission = Submission.query.filter_by(id=submission_id, user_id=user.id).first()
    if not submission:
        return "Not found", 404
    return render_template("view_submission.html", sub=submission.to_dict())


# ---------------------------------------------------------------------------
# AI-assisted rounds
# ---------------------------------------------------------------------------

def ask_claude_json_ff(system_prompt: str, user_content: str, max_tokens: int = 1500) -> dict:
    """FEATURE's plain (non-search, non-cached) JSON-call helper — used only
    by the protected Sentiment Score sub-calls below (fetch_all_parallel),
    which are scoped to a single narrow question each and don't need web
    search or prompt caching. Kept separate from the app's other
    ask_claude_json so neither implementation has to compromise for the
    other's needs."""
    response = ff_client.messages.create(
        model=FF_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return extract_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"Response was truncated at max_tokens={max_tokens} before it finished "
                f"valid JSON ({len(text)} chars received). Raise max_tokens for this call."
            ) from exc
        raise RuntimeError(f"Model did not return usable JSON: {text[:300]!r}") from exc


# ---- Fully parallel prompt batch (PROTECTED — FEATURE's exact prompts/budgets) ----
# Each entry: (system_prompt, max_tokens). All fire concurrently in ONE round —
# no sequential synthesis step. Verdict is computed deterministically in Python
# from the score (score_to_verdict), not asked of the model at all.
#
# BUG FIX: these budgets were originally sized assuming the model's reply is
# just the bare JSON payload — but they were tight enough that a slightly
# longer-than-average response (e.g. a 4-item risks list with fuller
# sentences) hit max_tokens mid-string, producing invalid JSON ("Unterminated
# string...") that looked like a parsing bug but was actually a truncation
# bug. Raised every budget with real headroom instead of the minimum that
# happens to work most of the time.
PARALLEL_PROMPTS = {
    "demandScore": (
        "You judge ONE thing: how much real, current demand exists for a business idea. "
        'Respond ONLY with strict JSON: {"demandScore": <integer 0-100>, "reason": "<one short sentence>"}. '
        "0 = no evidence of demand, 100 = clearly strong proven demand. Be decisive, don't cluster near 50.",
        150,
    ),
    "competitionIntensity": (
        "You judge ONE thing: how saturated/competitive a business idea's space is. "
        'Respond ONLY with strict JSON: {"competitionIntensity": <integer 0-100>, "reason": "<one short sentence>"}. '
        "0 = wide open, 100 = brutally saturated with entrenched players. Be decisive, don't cluster near 50.",
        150,
    ),
    "timingScore": (
        "You judge ONE thing: how good the timing is right now for a business idea. "
        'Respond ONLY with strict JSON: {"timingScore": <integer 0-100>, "reason": "<one short sentence>"}. '
        "0 = bad timing (declining/dying category), 100 = excellent timing (rising trend, low friction to start now). Be decisive.",
        150,
    ),
    "moatScore": (
        "You judge ONE thing: how defensible a business idea could be once running. "
        'Respond ONLY with strict JSON: {"moatScore": <integer 0-100>, "reason": "<one short sentence>"}. '
        "0 = trivially copyable, 100 = hard to replicate (network effects, brand, supply lock-in). Be decisive.",
        150,
    ),
    "positives": (
        "You judge ONE thing: what's genuinely in favor of a business idea. "
        'Respond ONLY with strict JSON: {"positives": ["<short factual-sounding point>", "... 2-4 items"]}. '
        "Be specific to this idea, not generic startup positives.",
        400,
    ),
    "risks": (
        "You judge ONE thing: what's genuinely risky or an open question about a business idea. "
        'Respond ONLY with strict JSON: {"risks": ["<short factual-sounding risk or open question>", "... 2-4 items"]}. '
        "Be specific to this idea, not generic startup risks.",
        400,
    ),
    "summary": (
        "You judge ONE thing: write a grounded 2-3 sentence read on a business idea's overall viability — "
        "weighing demand, competition, timing, and defensibility from general market knowledge. "
        'Respond ONLY with strict JSON: {"summary": "<2-3 sentences, specific to this idea, not generic>"}. '
        "Do not hedge excessively; give an actual read.",
        350,
    ),
    "customer": (
        "You judge ONE thing: who the likely first customer is for a business idea. "
        'Respond ONLY with strict JSON: {"customer": "<one sentence, specific>"}.',
        150,
    ),
    "revenueIdea": (
        "You judge ONE thing: the most plausible way a business idea makes money. "
        'Respond ONLY with strict JSON: {"revenueIdea": "<one sentence, specific>"}.',
        150,
    ),
    "startupCost": (
        "You judge ONE thing: a rough plain-language startup cost band for a business idea. "
        'Respond ONLY with strict JSON: {"startupCost": "<e.g. \'under $2,000\' or \'$10,000-$30,000\'>"}.',
        100,
    ),
}

SUBSCORE_KEYS = {"demandScore", "competitionIntensity", "timingScore", "moatScore"}
LIST_KEYS = {"positives", "risks"}


def fetch_all_parallel(idea: str) -> dict:
    """Fire every prompt in PARALLEL_PROMPTS concurrently in one round. No sequential step."""
    user_content = f"Business idea: {idea}"
    results = {}
    errors = {}  # BUG FIX: previously every failure was caught and silently
    # replaced with a neutral default (score 50 / empty list / empty string)
    # with NOTHING logged anywhere — if the API key was missing/invalid, if
    # billing/credits ran out, if a rate limit hit, or if the model's JSON
    # didn't parse, every single one of the 9 sub-calls would fail the exact
    # same way and you'd just see "everything is 50" with zero indication of
    # why. Now each failure is logged with its real exception, and the
    # collected errors are surfaced back to the caller so the API response
    # (and the UI) can show that this was a failure, not a genuine neutral
    # read.
    with ThreadPoolExecutor(max_workers=len(PARALLEL_PROMPTS)) as pool:
        futures = {
            pool.submit(ask_claude_json_ff, prompt, user_content, max_tok): key
            for key, (prompt, max_tok) in PARALLEL_PROMPTS.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                data = future.result()
                if key in LIST_KEYS:
                    results[key] = data.get(key, [])
                elif key in SUBSCORE_KEYS:
                    results[key] = data.get(key, 50)
                    results[f"{key}Reason"] = data.get("reason", "")
                else:
                    results[key] = data.get(key, "")
            except Exception as exc:  # noqa: BLE001
                errors[key] = f"{type(exc).__name__}: {exc}"
                ff_logger.error(
                    "Sentiment sub-call '%s' failed — falling back to a default value.\n%s",
                    key, traceback.format_exc(),
                )
                if key in LIST_KEYS:
                    results[key] = []
                elif key in SUBSCORE_KEYS:
                    results[key] = 50
                    results[f"{key}Reason"] = ""
                else:
                    results[key] = ""
    if errors:
        results["_errors"] = errors
    return results


@app.route("/api/analyze", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def analyze():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()

    if not idea or len(idea) > 5000:
        return jsonify({"error": "Missing or invalid idea"}), 400

    try:
        # --- Single parallel round: every field, all at once, no sequential call ---
        fields = fetch_all_parallel(idea)

        # Only score on sub-calls that genuinely came back. A failed call's
        # placeholder 50 no longer gets a vote, and the score regresses
        # toward the midpoint as data drops out.
        failed = set((fields.get("_errors") or {}).keys())
        measured = set(PARALLEL_PROMPTS) - failed

        breakdown = score_breakdown(fields, measured=measured)
        final_score = breakdown["score"]

        result = {
            "summary": fields.get("summary", ""),
            "positives": fields.get("positives", []),
            "risks": fields.get("risks", []),
            "customer": fields.get("customer", ""),
            "revenueIdea": fields.get("revenueIdea", ""),
            "startupCost": fields.get("startupCost", ""),
            "score": final_score,
            "verdict": score_to_verdict(final_score),  # deterministic, zero-cost, always consistent with score
            "scoreBreakdown": {
                "demandScore": fields.get("demandScore"),
                "demandScoreReason": fields.get("demandScoreReason", ""),
                "competitionIntensity": fields.get("competitionIntensity"),
                "competitionIntensityReason": fields.get("competitionIntensityReason", ""),
                "timingScore": fields.get("timingScore"),
                "timingScoreReason": fields.get("timingScoreReason", ""),
                "moatScore": fields.get("moatScore"),
                "moatScoreReason": fields.get("moatScoreReason", ""),
                "contestability": breakdown["contestability"],
                "factors": breakdown["factors"],
                "compositeBeforeSharpening": breakdown["compositeBeforeSharpening"],
                "confidence": breakdown["confidence"],
                "weights": SCORE_WEIGHTS,
            },
        }

        # BUG FIX: surface sub-call failures instead of quietly presenting
        # defaulted values as if they were a real analysis. If EVERY key
        # failed (see _errors length vs. PARALLEL_PROMPTS length), that's a
        # strong signal of a systemic problem (bad/missing API key, no
        # credits, rate limit) rather than 9 independent flukes.
        sub_errors = fields.get("_errors") or {}
        if sub_errors:
            total = len(PARALLEL_PROMPTS)
            failed_count = len(sub_errors)
            if failed_count == total:
                cause_hint = (
                    "All analysis calls failed the same way — check that ANTHROPIC_API_KEY is set "
                    "correctly and the account has available credits. See the terminal running this "
                    "server for the exact error."
                )
            else:
                cause_hint = (
                    f"{failed_count} of {total} analysis calls failed and fell back to neutral defaults. "
                    "See the terminal running this server for the exact error."
                )
            result["_warning"] = cause_hint
            result["_errorDetail"] = sub_errors

        log_change(current_user().id, "ANALYZE", "api_call")
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        app.logger.error("analyze() failed outright:\n%s", traceback.format_exc())
        return jsonify({"error": str(exc)}), 500


@app.route("/api/model", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def model():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    round1 = data.get("round1")
    notes = (data.get("notes") or "").strip()

    if len(idea) > 5000 or len(notes) > 5000:
        return jsonify({"error": "Input too long"}), 400

    if round1:
        context = (
            f"Idea: {idea}\n"
            f"Prior read: score {round1.get('score')}/100 ({round1.get('verdict')}). "
            f"Likely customer: {round1.get('customer')}. "
            f"Revenue idea: {round1.get('revenueIdea')}. "
            f"Rough startup cost: {round1.get('startupCost')}."
        )
    else:
        context = f"Idea: {idea or 'not specified — infer a plausible small business from the notes below'}."

    system_prompt = """You help first-time founders sketch a one-page business model. Respond ONLY with strict JSON, no markdown fences, matching exactly this shape:
{
  "customer": "<2-3 sentences on who the customer is, specifically>",
  "revenueModel": "<2-3 sentences on how money is made — pricing structure, e.g. subscription, one-time, commission>",
  "valueProp": "<1-2 sentences on why this customer chooses this over the alternative>",
  "keyCosts": ["<short cost line item>", "... 3-5 items covering both one-time startup and recurring"],
  "costBreakdown": [ {"label": "<short cost category, <=18 chars>", "amount": <integer dollars, one-time startup investment for this category>} , ... 3-5 items ],
  "revenueBreakdown": [ {"label": "<short revenue stream name, <=18 chars>", "amount": <integer, relative weight or estimated monthly dollars>} , ... 1-4 items ],
  "totalStartupInvestment": <integer dollars, sum-ish of costBreakdown>,
  "firstMilestone": "<one sentence describing the first concrete proof-of-concept milestone to hit>"
}
Ground every number in realistic ranges for a small first-time business of this kind — don't inflate. Keep cost/revenue labels short since they render as bar chart labels."""

    try:
        result = ask_claude_json_ff(
            system_prompt,
            f"{context}{(' Founder notes: ' + notes) if notes else ''}",
        )
        log_change(current_user().id, "MODEL", "api_call")
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        app.logger.error(f'Model error: {exc}')
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tax", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def tax():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    budget = (data.get("budget") or "").strip()
    location = (data.get("location") or "").strip()
    round2 = data.get("round2")
    expenses = (data.get("expenses") or "").strip()
    tax_year = data.get("taxYear") or None
    hiring = data.get("hiring") or {}
    will_hire = bool(hiring.get("willHire"))
    hire_count = hiring.get("employeeCount")
    hire_avg_salary = hiring.get("avgSalary")

    # BUG FIX #3: Input Validation
    if len(idea) > 5000 or len(budget) > 500 or len(location) > 500 or len(expenses) > 10000:
        return jsonify({"error": "Input too long"}), 400

    parts = []
    if idea:
        parts.append(f"Idea: {idea}")
    if budget:
        parts.append(f"Starting budget: {budget}")
    if location:
        parts.append(f"Business location (state/country): {location}")
    if round2:
        parts.append(
            f"Revenue model: {round2.get('revenueModel')}\n"
            f"Key costs: {'; '.join(round2.get('keyCosts') or [])}\n"
            f"Startup investment: ${round2.get('totalStartupInvestment')}"
        )
        rev_breakdown = round2.get("revenueBreakdown") or []
        if rev_breakdown:
            parts.append(
                "Revenue streams (for projecting growth across quarters): "
                + "; ".join(f"{r.get('label')}: ${r.get('amount')}" for r in rev_breakdown)
            )
    if expenses:
        parts.append(f"Raw Expenses Provided by User:\n{expenses}")
    if tax_year:
        parts.append(f"Tax year to plan for: {tax_year}")
    if will_hire:
        hire_desc = "The founder plans to hire employees this quarter."
        if hire_count:
            hire_desc += f" Planned headcount: {hire_count}."
        if hire_avg_salary:
            hire_desc += f" Average salary/role: ${hire_avg_salary}."
        parts.append(hire_desc)
    else:
        parts.append("The founder does not plan to hire employees this quarter (solo operation for now).")

    context = "\n".join(parts) or "General first-time small business, no specifics given."

    system_prompt = """You help a new sole proprietor understand Schedule C (Form 1040) and basic tax strategy for a small business in the US. The user may provide a list of raw expenses, whether they plan to hire employees this quarter, and which tax year to plan for. You have a web_search tool — use it to confirm current-year Schedule C line numbers, mileage rates, deduction thresholds, and state LLC formation/tax facts against IRS.gov, a state Secretary of State site, or another authoritative source rather than relying on memory, since these figures change year to year.

A single point estimate presented as fact is misleading — a first-time founder can't know their exact income yet. Instead of one number, give a LOW / MID / HIGH band for income, expenses, and net profit, and project that band across 4 quarters (adjusting for the revenue growth implied by the business model, or flat if no growth signal is given). If the founder plans to hire employees this quarter, fold estimated payroll and employer payroll taxes into the expense band and quarterly figures starting the quarter they said they'd hire, and mention it explicitly in taxStrategies or quarterlyPlanner.hiringImpact.

After researching, your FINAL message must be ONLY a single strict JSON object — no markdown fences, no commentary before or after it — matching exactly this shape:
{
  "projectedScheduleC": {
    "estimatedIncome": {"low": <integer>, "mid": <integer>, "high": <integer>},
    "totalExpenses": {"low": <integer>, "mid": <integer>, "high": <integer>},
    "netProfit": {"low": <integer>, "mid": <integer>, "high": <integer>},
    "categorizedExpenses": [
      {
        "item": "<short description of the expense item>",
        "amount": <integer dollars, mid-case>,
        "scheduleCLine": "<Schedule C line reference, e.g. 'Line 18'>",
        "category": "<deduction category name>"
      }
    ]
  },
  "quarterlyPlanner": {
    "taxYear": "<the tax year these quarters are for, e.g. '2026'>",
    "quarters": [
      {
        "label": "<e.g. 'Q1'>",
        "estimatedIncome": {"low": <integer>, "mid": <integer>, "high": <integer>},
        "estimatedExpenses": {"low": <integer>, "mid": <integer>, "high": <integer>},
        "estimatedQuarterlyTaxDue": {"low": <integer>, "mid": <integer>, "high": <integer>},
        "note": "<one short sentence specific to this quarter, e.g. ramp-up, seasonality, or hiring kicking in>"
      }
    ],
    "hiringImpact": "<1-2 sentences on how the hiring plan changes quarterly tax obligations (employer payroll tax, withholding, EIN/state registration needs), or null if not hiring>"
  },
  "taxStrategies": [
    { "claim": "<a tailored, strategic tax tip based on their business model and provided expenses, e.g. Section 179 for equipment, Home Office Deduction>", "source": "<a real URL you retrieved via web_search backing this rule (ideally IRS.gov), or null if it's general judgment rather than a specific citable rule>" }
  ],
  "structureNote": "<2-3 sentences on sole proprietor vs LLC vs S-corp considerations for a business at this stage, general and cautious>",
  "recommendedStructure": {
    "entity": "<the single entity type you'd lean toward for THIS specific business given its idea, budget, and risk profile, e.g. 'Sole Proprietorship', 'Single-Member LLC', 'LLC (multi-member)', 'S-Corporation'>",
    "reasoning": "<2-3 sentences on why this fits this specific business right now — liability exposure, budget for formation/maintenance costs, growth plans>",
    "whenToRevisit": "<one sentence on what would change this recommendation later, e.g. hiring employees, revenue crossing a threshold>"
  },
  "stateRecommendation": {
    "state": "<the single state you'd recommend forming the LLC/entity in for THIS business>",
    "reasoning": "<2-3 sentences explaining why — for almost all small local businesses this should be the founder's home/operating state, since forming out-of-state (e.g. Delaware/Wyoming/Nevada) usually just adds foreign-qualification paperwork and fees without the tax benefits people assume; only recommend an out-of-state entity if there's a specific, concrete reason tied to this business, and say what that reason is>",
    "source": "<a real URL you retrieved via web_search backing the state's franchise tax / formation fee facts, or null if general judgment>"
  },
  "quarterlyNote": "<2-3 sentences on estimated quarterly taxes and self-employment tax basics, general guidance>",
  "recordkeeping": ["<short concrete recordkeeping habit>", "... 3-4 items"],
  "whenToHireAccountant": "<2-3 sentences on the signals that mean this founder should stop DIYing taxes and hire a professional>"
}
Only put a URL in "source" if it is a real URL you retrieved via web_search — never invent or guess one. Keep it educational and general — never claim to replace a CPA, and say so implicitly through cautious, non-definitive phrasing. If raw expenses are provided, parse and categorize them accurately into Schedule C lines using the mid-case amount. Ensure there are 3-5 tailored tax strategies. Always fill in "recommendedStructure" with a real, specific lean based on the actual business described — never leave it generic or say "it depends" without still naming one entity type as the current best fit. Always fill in "stateRecommendation" — default to the founder's stated business location/home state unless there's a genuine, business-specific reason not to; never suggest a tax-haven state just because it's commonly mentioned online. quarterlyPlanner.quarters must always contain exactly 4 items (Q1-Q4), each internally consistent with the low/mid/high bands in projectedScheduleC."""

    try:
        result = ask_claude_json(system_prompt, context, max_tokens=4500, use_search=True)
        log_change(current_user().id, "TAX", "api_call")
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        app.logger.error(f'Tax error: {exc}')
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cushion-assessment", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def cushion_assessment():
    """Cushion Fund sub-tab 'Get Claude's read on this' button. The runway
    number itself is pure client-side math (see app.js computeCushion); this
    endpoint only judges realism and gives advice, grounded in real sources.
    """
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    savings = data.get("savings")
    loan = data.get("loan")
    monthly_expenses = data.get("monthlyExpenses")
    runway_months = data.get("runwayMonths")
    hours_per_week = data.get("hoursPerWeek")

    if len(idea) > 5000:
        return jsonify({"error": "Idea description too long"}), 400
    try:
        savings = float(savings)
        loan = float(loan)
        monthly_expenses = float(monthly_expenses)
        runway_months = float(runway_months)
        hours_per_week = float(hours_per_week)
    except (TypeError, ValueError):
        return jsonify({"error": "Cushion fund inputs must be numbers"}), 400
    if not (0 <= savings <= 100_000_000 and 0 <= loan <= 100_000_000 and 0 <= monthly_expenses <= 10_000_000
            and 0 <= runway_months <= 1000 and 0 <= hours_per_week <= 168):
        return jsonify({"error": "One or more values is out of a realistic range"}), 400

    system_prompt = """You help a first-time founder judge whether their cash cushion, loan plans, and time commitment are realistic given their idea. You have a web_search tool — use it to check real, current guidance (e.g. typical recommended emergency-fund months for a small business, realistic ramp-up timelines for a business like theirs) rather than relying on memory.

After researching, your FINAL message must be ONLY a single strict JSON object — no markdown fences, no commentary before or after it — matching exactly this shape:
{
  "assessment": "<3-4 sentences giving an honest, specific read on whether this founder's runway (savings + loan vs. monthly burn) is realistic for their idea — not generic advice, grounded in their actual numbers>",
  "timeCommitmentNote": "<1-3 sentences on how their stated hours/week affects how fast this specific business can realistically ramp up revenue, or null if hours/week wasn't meaningfully low or high enough to flag>",
  "advice": [ {"claim": "<one specific, actionable piece of advice>", "source": "<a real URL you retrieved via web_search that backs this specific claim, or null if it's general judgment rather than a checkable fact>"} , ... 2-4 items ]
}
Only put a URL in "source" if it is a real URL you retrieved via web_search — never invent or guess one. Be honest, even if the honest answer is that the runway is too thin or the time commitment is unrealistic for the stated idea — do not soften a real risk to be encouraging."""

    user_content = (
        f"Business idea: {idea or 'not specified'}\n"
        f"Current savings/cushion: ${savings:,.0f}\n"
        f"Loan/outside capital being considered: ${loan:,.0f}\n"
        f"Monthly personal expenses: ${monthly_expenses:,.0f}\n"
        f"Computed runway: {runway_months} months\n"
        f"Hours per week the founder can commit: {hours_per_week}"
    )

    try:
        result = ask_claude_json(system_prompt, user_content, max_tokens=1200, use_search=True)
        log_change(current_user().id, "CUSHION_ASSESSMENT", "api_call")
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        app.logger.error(f'Cushion assessment error: {exc}')
        return jsonify({"error": str(exc)}), 500


# Learn tab: FAQ content itself lives client-side in app.js (written once,
# with real sources baked in — see claude/Learn_Tab_FAQ_Sources.md for the
# research trail). This endpoint only covers the "Ask Claude" free-text box
# for questions not covered by the FAQ.
ASK_FOUNDER_MAX_LEN = 400


@app.route("/api/ask-founder", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def ask_founder():
    """Learn tab's 'Ask Claude' box. Per an explicit hard requirement: every
    answer must cite a real, retrieved source AND show the actual excerpt of
    text used to infer the answer — never a bare, uncited claim presented as
    fact. Length-capped per question (not count-capped) both here and in
    app.js's maxlength/counter.
    """
    data = request.get_json(force=True)
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"error": "Ask something first"}), 400
    if len(question) > ASK_FOUNDER_MAX_LEN:
        return jsonify({"error": f"Keep it under {ASK_FOUNDER_MAX_LEN} characters"}), 400

    system_prompt = f"""You are answering a first-time founder's question inside a small business help tool's "Learn" section. They may ask about legal structure, financing, taxes, margins, or any other basic small-business concept. You have a web_search tool — use it to find a real, authoritative source for your answer (prefer IRS.gov, SBA.gov, USA.gov, or another authoritative source) and quote the actual text you used.

After researching, your FINAL message must be ONLY a single strict JSON object — no markdown fences, no commentary before or after it — matching exactly this shape:
{{
  "answer": "<2-4 sentences answering their question in plain language a total beginner can follow>",
  "sourceUrl": "<a real URL you retrieved via web_search that backs this answer, or null if you genuinely could not find one and the answer is general reasoning>",
  "sourceName": "<the publisher/site name, or null if sourceUrl is null>",
  "excerpt": "<a real, short (under 30 words) quote taken directly from the source page's actual text that supports your answer, or null if sourceUrl is null>"
}}
CRITICAL: never invent, guess, or paraphrase a URL — only use one you actually retrieved via web_search. Never fabricate an excerpt — it must be text that genuinely appears on the source page you're citing. If you cannot find a real source, set sourceUrl, sourceName, and excerpt all to null rather than inventing any of them — a null source is honest; a fake one is not acceptable under any circumstance."""

    try:
        result = ask_claude_json(system_prompt, f"Question: {question}", max_tokens=1000, use_search=True)
        log_change(current_user().id, "ASK_FOUNDER", "api_call")
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        app.logger.error(f'Ask founder error: {exc}')
        return jsonify({"error": str(exc)}), 500


# Asset caching: static files (CSS/JS/images) get a long cache lifetime in
# production, where a filename-based cache-buster would normally handle
# invalidation. In local dev this actively breaks development — the browser
# would keep serving a year-old cached app.js/style.css after every edit,
# which looks exactly like "my fix isn't working" even when the file on disk
# is correct. Never cache static assets outside of a real deployment.
if IS_PRODUCTION:
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # 1 year

    @app.after_request
    def cache_static(response):
        if any(ct in (response.content_type or "") for ct in ["text/css", "application/javascript", "image/"]):
            response.cache_control.max_age = 31536000
        return response
else:
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def no_cache_static(response):
        if any(ct in (response.content_type or "") for ct in ["text/css", "application/javascript", "image/", "text/html"]):
            response.cache_control.no_cache = True
            response.cache_control.no_store = True
            response.cache_control.must_revalidate = True
        return response


if __name__ == "__main__":
    # threaded=True lets the three AI calls (and any concurrent visitors during
    # a demo) run without blocking each other on Flask's dev server.
    app.run(debug=False, port=5000, threaded=True)
