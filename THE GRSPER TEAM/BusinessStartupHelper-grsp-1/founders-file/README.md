# The Founder's File — Business Startup Helper

A three-round tool for turning an idea into a business:

1. **Sentiment read** — describe an idea, get a 0-100 score and a verdict on whether it's worth pursuing, with supporting evidence and risks.
2. **Business model** — pulls context from Round 1 and drafts a one-page model: customer, revenue model, and a visual breakdown of startup costs vs. revenue.
3. **Tax/finance organizer** — maps the business to Schedule C deduction categories, covers quarterly/self-employment tax basics, and points to small-business accounting firms.

## Stack

- **Backend:** Python (Flask) — proxies three endpoints to the Anthropic API so your key never reaches the browser.
- **Frontend:** plain HTML/CSS/JS, no build step.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste in your Anthropic API key (https://console.anthropic.com/settings/keys)

python app.py
```

Then open **http://localhost:5000**.

## Project structure

```
founders-file/
├── app.py                 # Flask app + /api/analyze, /api/model, /api/tax
├── requirements.txt
├── .env.example
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── app.js
```

## Database

Uses **SQLite** (via SQLAlchemy) for user accounts and saved submissions — no separate database server needed. The file `founders_file.db` is created automatically on first run inside `founders-file/` and is git-ignored.

Add a `SECRET_KEY` to your `.env` (used to sign session cookies) — see `.env.example`.

### Endpoints

- `POST /api/signup` — `{ email, password }` — creates an account and signs you in
- `POST /api/login` — `{ email, password }`
- `POST /api/logout`
- `GET  /api/me` — current signed-in user, or `{ "user": null }`
- `GET  /api/submissions` — list your saved idea + results (auth required)
- `POST /api/submissions` — `{ id?, idea, analyzeResult?, modelResult?, taxResult? }` — create or update a saved run (auth required)
- `DELETE /api/submissions/<id>` — remove a saved run (auth required)

## Notes on the "sentiment" round

This uses Claude's general knowledge to reason about the idea (market size, competition, timing), not a live news/forum scraper. If you want it to genuinely pull fresh articles and forum posts (closer to your original "Xtract" concept), the cleanest way to extend `/api/analyze` in `app.py` is either:

- Add the Anthropic **web search tool** to the `messages.create()` call (a few lines — happy to add this if you want it), or
- Plug in a real news/search API (e.g. NewsAPI, Bing News Search) inside `/api/analyze`, fetch a handful of relevant articles/snippets yourself, and pass them into the prompt as context before asking Claude to score it.

## Deploying

This is a standard Flask app — deploy it anywhere that runs Python (Render, Railway, Fly.io, a VPS with gunicorn, etc.). Set `ANTHROPIC_API_KEY` as an environment variable on the host; don't commit your `.env` file.

## Disclaimer

This tool gives general, educational output — not licensed financial, legal, or tax advice. Verify anything specific with a CPA before acting on it.
