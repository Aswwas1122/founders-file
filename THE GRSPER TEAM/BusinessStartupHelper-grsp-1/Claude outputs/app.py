"""
The Founder's File — Business Startup Helper
Flask backend. Serves the frontend and proxies three AI-assisted endpoints
to the Anthropic API: /api/analyze, /api/model, /api/tax.

Also has a small SQLite database (via SQLAlchemy) for user accounts and
saved submissions (idea + results), so a signed-in founder can come back
to their past runs.

Setup:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...      (or put it in a .env file)
    python app.py
Then open http://localhost:5000
"""

import json
import os

from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic

from models import db, User, Submission

# Load a .env file if python-dotenv is installed (optional convenience)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-.env")

basedir = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "founders_file.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
with app.app_context():
    db.create_all()

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5-20250929"


def ask_claude_json(system_prompt: str, user_content: str, max_tokens: int = 1200) -> dict:
    """Call the Anthropic API and parse a strict-JSON response.

    The system prompt is marked cache_control="ephemeral" — since these
    prompts are large, static, and reused across every request to the same
    endpoint, Anthropic's prompt caching means repeat calls (within the
    cache TTL) are billed at a fraction of the input-token cost instead of
    re-processing the whole instruction block every time. This is a free
    cost/latency win with zero quality tradeoff.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    text = text.strip()
    # Strip accidental markdown fences, just in case
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists"}), 409

    user = User(email=email, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()

    session["user_id"] = user.id
    return jsonify(user.to_public_dict()), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user.id
    return jsonify(user.to_public_dict())


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user.to_public_dict()})


# ---------------------------------------------------------------------------
# Saved submissions (idea + final results), tied to the signed-in user
# ---------------------------------------------------------------------------

@app.route("/api/submissions", methods=["GET"])
def list_submissions():
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to view saved submissions"}), 401
    subs = Submission.query.filter_by(user_id=user.id).order_by(Submission.created_at.desc()).all()
    return jsonify([s.to_dict() for s in subs])


@app.route("/api/submissions", methods=["POST"])
def save_submission():
    """Create or update a saved submission for the signed-in user.

    Body: { "id": <optional, updates existing>, "idea": str,
            "analyzeResult": obj|null, "modelResult": obj|null, "taxResult": obj|null }
    """
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to save your results"}), 401

    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    if not idea:
        return jsonify({"error": "Missing idea"}), 400

    submission_id = data.get("id")
    submission = None
    if submission_id:
        submission = Submission.query.filter_by(id=submission_id, user_id=user.id).first()

    if submission is None:
        submission = Submission(user_id=user.id, idea=idea)
        db.session.add(submission)

    submission.idea = idea
    if "analyzeResult" in data:
        submission.analyze_result = data.get("analyzeResult")
    if "modelResult" in data:
        submission.model_result = data.get("modelResult")
    if "taxResult" in data:
        submission.tax_result = data.get("taxResult")

    db.session.commit()
    return jsonify(submission.to_dict())


@app.route("/api/submissions/<int:submission_id>", methods=["DELETE"])
def delete_submission(submission_id):
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in required"}), 401
    submission = Submission.query.filter_by(id=submission_id, user_id=user.id).first()
    if not submission:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(submission)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# AI-assisted rounds
# ---------------------------------------------------------------------------

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    if not idea:
        return jsonify({"error": "Missing idea"}), 400

    system_prompt = """You are a sober, evidence-minded market analyst helping someone decide whether a business idea is worth pursuing. Respond ONLY with strict JSON, no markdown fences, no preamble, matching exactly this shape:
{
  "score": <integer 0-100, where 0 is do not pursue and 100 is very strong signal>,
  "verdict": "<one short punchy phrase, e.g. 'Worth a pilot' or 'Wait and validate more' or 'Hard pass for now'>",
  "summary": "<2-3 sentences giving the overall read, grounded and specific to this idea, not generic>",
  "positives": ["<short factual-sounding point in favor>", "... 2-4 items"],
  "risks": ["<short factual-sounding risk or open question>", "... 2-4 items"],
  "customer": "<one sentence describing the likely first customer>",
  "revenueIdea": "<one sentence on the most plausible way this makes money>",
  "startupCost": "<a rough plain-language cost band to get this running, e.g. 'under $2,000' or '$10,000-$30,000'>"
}
Base the read on general, well-established market knowledge and reasonable judgment about the category — be specific to the idea given, not generic startup advice. Do not hedge excessively; give an actual call."""

    try:
        result = ask_claude_json(system_prompt, f"Business idea: {idea}", max_tokens=700)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/model", methods=["POST"])
def model():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    round1 = data.get("round1")
    notes = (data.get("notes") or "").strip()

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
        result = ask_claude_json(
            system_prompt,
            f"{context}{(' Founder notes: ' + notes) if notes else ''}",
            max_tokens=900,
        )
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tax", methods=["POST"])
def tax():
    data = request.get_json(force=True)
    idea = (data.get("idea") or "").strip()
    round2 = data.get("round2")
    expenses = (data.get("expenses") or "").strip()

    parts = []
    if idea:
        parts.append(f"Idea: {idea}")
    if round2:
        parts.append(
            f"Revenue model: {round2.get('revenueModel')}\n"
            f"Key costs: {'; '.join(round2.get('keyCosts') or [])}\n"
            f"Startup investment: ${round2.get('totalStartupInvestment')}"
        )
    if expenses:
        parts.append(f"Raw Expenses Provided by User:\n{expenses}")

    context = "\n".join(parts) or "General first-time small business, no specifics given."

    system_prompt = """You help a new sole proprietor understand Schedule C (Form 1040) and basic tax strategy for a small business in the US. The user may provide a list of raw expenses.
Respond ONLY with strict JSON, no markdown fences, matching exactly this shape:
{
  "projectedScheduleC": {
    "estimatedIncome": <integer dollars, estimate a reasonable first-year income based on the business model. Default to 50000 if unsure>,
    "totalExpenses": <integer dollars, sum of the categorized expenses below>,
    "netProfit": <integer dollars, estimatedIncome - totalExpenses>,
    "categorizedExpenses": [
      {
        "item": "<short description of the expense item>",
        "amount": <integer dollars>,
        "scheduleCLine": "<Schedule C line reference, e.g. 'Line 18'>",
        "category": "<deduction category name>"
      }
    ]
  },
  "taxStrategies": [
    "<a tailored, strategic tax tip based on their business model and provided expenses (e.g. Section 179 for equipment, Home Office Deduction)>"
  ],
  "structureNote": "<2-3 sentences on sole proprietor vs LLC vs S-corp considerations for a business at this stage, general and cautious>",
  "quarterlyNote": "<2-3 sentences on estimated quarterly taxes and self-employment tax basics, general guidance>",
  "recordkeeping": ["<short concrete recordkeeping habit>", "... 3-4 items"],
  "whenToHireAccountant": "<2-3 sentences on the signals that mean this founder should stop DIYing taxes and hire a professional>"
}
Keep it educational and general — never claim to replace a CPA, and say so implicitly through cautious, non-definitive phrasing. If raw expenses are provided, parse and categorize them accurately into Schedule C lines. Ensure there are 3-5 tailored tax strategies."""

    try:
        result = ask_claude_json(system_prompt, context, max_tokens=1600)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    # threaded=True lets the three AI calls (and any concurrent visitors during
    # a demo) run without blocking each other on Flask's dev server.
    app.run(debug=True, port=5000, threaded=True)
