import os
import json
import requests
from flask import Flask, redirect, request, session, render_template_string
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime

TOKENS_FILE = "tokens.json"

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return []

def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

CLIENT_ID = os.getenv("TRUELAYER_CLIENT_ID")
CLIENT_SECRET = os.getenv("TRUELAYER_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TRUELAYER_REDIRECT_URI")

AUTH_URL = "https://auth.truelayer.com"
API_URL = "https://api.truelayer.com"

SCOPES = "info accounts balance transactions cards"


@app.route("/")
def index():
    tokens = load_tokens()
    return render_template_string(HOME_HTML, connected=len(tokens) > 0, bank_count=len(tokens))


@app.route("/connect")
def connect():
    url = (
        f"{AUTH_URL}/?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&scope={SCOPES.replace(' ', '%20')}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&providers=uk-ob-all%20uk-oauth-all"
    )
    return redirect(url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: no code returned", 400

    resp = requests.post(f"{AUTH_URL}/connect/token", data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    })
    if not resp.ok:
        return f"Token exchange failed: {resp.status_code} — {resp.text}", 400
    data = resp.json()
    if "access_token" not in data:
        return f"No access token in response: {data}", 400
    tokens = load_tokens()
    tokens.append(data["access_token"])
    save_tokens(tokens)
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    tokens = load_tokens()
    if not tokens:
        return redirect("/")

    accounts = []
    cards = []
    all_transactions = []

    for token in tokens:
        headers = {"Authorization": f"Bearer {token}"}
        token_accounts = _get_json(f"{API_URL}/data/v1/accounts", headers).get("results", [])
        token_cards = _get_json(f"{API_URL}/data/v1/cards", headers).get("results", [])
        accounts += token_accounts
        cards += token_cards
        for acc in token_accounts:
            label = acc.get("display_name") or acc.get("account_id")
            txns = _get_json(f"{API_URL}/data/v1/accounts/{acc['account_id']}/transactions", headers).get("results", [])
            for t in txns:
                t["_account_label"] = label
            all_transactions.extend(txns)
        for card in token_cards:
            label = card.get("display_name") or card.get("account_id")
            txns = _get_json(f"{API_URL}/data/v1/cards/{card['account_id']}/transactions", headers).get("results", [])
            for t in txns:
                t["_account_label"] = label
            all_transactions.extend(txns)

    subscriptions = detect_subscriptions(all_transactions)

    monthly_total = sum(s["monthly_estimate"] for s in subscriptions)
    annual_total = round(monthly_total * 12, 2)
    by_frequency = defaultdict(list)
    for s in subscriptions:
        by_frequency[s["frequency"]].append(s)

    return render_template_string(
        DASHBOARD_HTML,
        accounts=accounts,
        cards=cards,
        subscriptions=subscriptions,
        total_monthly=monthly_total,
        total_annual=annual_total,
        by_frequency=dict(by_frequency),
    )


@app.route("/disconnect")
def disconnect():
    save_tokens([])
    session.clear()
    return redirect("/")


def _get_json(url, headers):
    try:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def detect_subscriptions(transactions):
    """Group transactions by merchant and flag those that recur monthly."""
    by_merchant = defaultdict(list)
    for t in transactions:
        name = t.get("merchant_name") or t.get("description", "Unknown")
        amount = abs(t.get("amount", 0))
        date_str = t.get("timestamp", t.get("date", ""))[:10]
        label = t.get("_account_label", "Unknown account")
        if amount > 0 and date_str:
            by_merchant[name].append({"amount": amount, "date": date_str, "account": label})

    subscriptions = []
    for merchant, charges in by_merchant.items():
        if len(charges) < 2:
            continue
        dates = sorted(datetime.strptime(c["date"], "%Y-%m-%d") for c in charges)
        gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
        avg_gap = sum(gaps) / len(gaps)
        # Recurring = charges roughly weekly (5-9d), monthly (25-35d), or yearly (360-370d)
        if any(lo <= avg_gap <= hi for lo, hi in [(5, 9), (25, 35), (360, 370)]):
            avg_amount = sum(c["amount"] for c in charges) / len(charges)
            monthly = avg_amount if 25 <= avg_gap <= 35 else (
                avg_amount * 4.33 if avg_gap <= 9 else avg_amount / 12
            )
            accounts_seen = sorted(set(c["account"] for c in charges))
            subscriptions.append({
                "merchant": merchant,
                "frequency": "Weekly" if avg_gap <= 9 else ("Monthly" if avg_gap <= 35 else "Annual"),
                "avg_amount": round(avg_amount, 2),
                "monthly_estimate": round(monthly, 2),
                "occurrences": len(charges),
                "accounts": ", ".join(accounts_seen),
            })

    return sorted(subscriptions, key=lambda s: s["monthly_estimate"], reverse=True)


HOME_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Spending Tracker</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 700px; margin: 60px auto; padding: 0 20px; color: #1a1a1a; }
    h1 { font-size: 2rem; margin-bottom: 0.25rem; }
    p { color: #555; }
    a.btn { display: inline-block; margin-top: 1.5rem; padding: 12px 28px; background: #2563eb; color: white;
            border-radius: 8px; text-decoration: none; font-weight: 600; }
    a.btn:hover { background: #1d4ed8; }
    .connected { color: #16a34a; font-weight: 600; }
  </style>
</head>
<body>
  <h1>Spending Tracker</h1>
  <p>Connect your UK bank accounts to find subscriptions and recurring charges.</p>
  {% if connected %}
    <p class="connected">&#10003; {{ bank_count }} bank connection{{ 's' if bank_count != 1 else '' }}</p>
    <a class="btn" href="/dashboard">View Dashboard</a>
    <a class="btn" style="background:#16a34a;margin-left:8px" href="/connect">Add another bank</a>
    <a class="btn" style="background:#dc2626;margin-left:8px" href="/disconnect">Disconnect all</a>
  {% else %}
    <a class="btn" href="/connect">Connect your bank</a>
  {% endif %}
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Dashboard – Spending Tracker</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
    h1 { font-size: 1.8rem; }
    h2 { font-size: 1.2rem; color: #374151; margin-top: 2rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
    th { text-align: left; padding: 8px 12px; background: #f3f4f6; font-size: 0.85rem; color: #6b7280; }
    th.sortable { cursor: pointer; user-select: none; }
    th.sortable:hover { background: #e5e7eb; }
    td { padding: 10px 12px; border-bottom: 1px solid #e5e7eb; }
    tr:hover td { background: #f9fafb; }
    .amount { font-weight: 600; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
    .Monthly { background: #dbeafe; color: #1d4ed8; }
    .Weekly  { background: #fef9c3; color: #854d0e; }
    .Annual  { background: #dcfce7; color: #166534; }
    .total { font-size: 1.1rem; font-weight: 700; margin-top: 1rem; }
    a.back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  </style>
</head>
<body>
  <a class="back" href="/">&larr; Home</a>
  <h1>Your Subscriptions</h1>

  {% if subscriptions %}
  <div style="display:flex;gap:16px;margin:1.5rem 0;flex-wrap:wrap">
    <div style="flex:1;min-width:160px;background:#eff6ff;border-radius:10px;padding:16px 20px">
      <div style="font-size:0.8rem;color:#3b82f6;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Monthly total</div>
      <div style="font-size:2rem;font-weight:700;margin-top:4px">&pound;{{ "%.2f"|format(total_monthly) }}</div>
    </div>
    <div style="flex:1;min-width:160px;background:#f0fdf4;border-radius:10px;padding:16px 20px">
      <div style="font-size:0.8rem;color:#16a34a;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Annual total</div>
      <div style="font-size:2rem;font-weight:700;margin-top:4px">&pound;{{ "%.2f"|format(total_annual) }}</div>
    </div>
    <div style="flex:1;min-width:160px;background:#faf5ff;border-radius:10px;padding:16px 20px">
      <div style="font-size:0.8rem;color:#7c3aed;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Subscriptions found</div>
      <div style="font-size:2rem;font-weight:700;margin-top:4px">{{ subscriptions|length }}</div>
    </div>
  </div>
  <table>
  <div style="display:flex;gap:12px;margin-bottom:1rem;flex-wrap:wrap;align-items:center">
    <input id="search" placeholder="Search merchant..." oninput="applyFilters()"
      style="padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:0.9rem;min-width:200px">
    <select id="freqFilter" onchange="applyFilters()"
      style="padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:0.9rem">
      <option value="">All frequencies</option>
      <option>Weekly</option>
      <option>Monthly</option>
      <option>Annual</option>
    </select>
    <select id="accountFilter" onchange="applyFilters()"
      style="padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:0.9rem">
      <option value="">All accounts</option>
      {% for s in subscriptions %}{% for a in s.accounts.split(', ') %}
      <option>{{ a }}</option>
      {% endfor %}{% endfor %}
    </select>
    <button onclick="resetFilters()"
      style="padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;background:white;cursor:pointer;font-size:0.9rem">Reset</button>
  </div>

    <thead><tr>
      <th class="sortable" onclick="sortTable(0)">Merchant <span class="arrow">↕</span></th>
      <th class="sortable" onclick="sortTable(1)">Account <span class="arrow">↕</span></th>
      <th class="sortable" onclick="sortTable(2)">Frequency <span class="arrow">↕</span></th>
      <th class="sortable" onclick="sortTable(3)">Avg charge <span class="arrow">↕</span></th>
      <th class="sortable" onclick="sortTable(4)">Monthly est. <span class="arrow">↕</span></th>
      <th class="sortable" onclick="sortTable(5)">Seen <span class="arrow">↕</span></th>
    </tr></thead>
    <tbody id="tableBody">
    {% for s in subscriptions %}
    <tr data-freq="{{ s.frequency }}" data-account="{{ s.accounts }}">
      <td>{{ s.merchant }}</td>
      <td style="color:#6b7280;font-size:0.9rem">{{ s.accounts }}</td>
      <td><span class="tag {{ s.frequency }}">{{ s.frequency }}</span></td>
      <td class="amount">&pound;{{ "%.2f"|format(s.avg_amount) }}</td>
      <td class="amount">&pound;{{ "%.2f"|format(s.monthly_estimate) }}</td>
      <td>{{ s.occurrences }}x</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>

  <script>
    let sortCol = 4, sortAsc = false;

    function sortTable(col) {
      if (sortCol === col) sortAsc = !sortAsc;
      else { sortCol = col; sortAsc = col < 3; }
      const tbody = document.getElementById("tableBody");
      const rows = Array.from(tbody.querySelectorAll("tr:not([style*='none'])"));
      rows.sort((a, b) => {
        let av = a.cells[col].innerText.replace(/[£x]/g, "").trim();
        let bv = b.cells[col].innerText.replace(/[£x]/g, "").trim();
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = isNaN(an) ? av.localeCompare(bv) : an - bn;
        return sortAsc ? cmp : -cmp;
      });
      rows.forEach(r => tbody.appendChild(r));
      document.querySelectorAll(".arrow").forEach((a, i) =>
        a.textContent = i === col ? (sortAsc ? "↑" : "↓") : "↕");
    }

    function applyFilters() {
      const search = document.getElementById("search").value.toLowerCase();
      const freq = document.getElementById("freqFilter").value;
      const account = document.getElementById("accountFilter").value;
      document.querySelectorAll("#tableBody tr").forEach(row => {
        const merchant = row.cells[0].innerText.toLowerCase();
        const rowFreq = row.dataset.freq;
        const rowAccount = row.dataset.account;
        const show = merchant.includes(search)
          && (!freq || rowFreq === freq)
          && (!account || rowAccount.includes(account));
        row.style.display = show ? "" : "none";
      });
    }

    function resetFilters() {
      document.getElementById("search").value = "";
      document.getElementById("freqFilter").value = "";
      document.getElementById("accountFilter").value = "";
      applyFilters();
    }
  </script>
  {% else %}
  <p>No recurring charges detected yet. Try connecting more accounts or check back after a few months of data.</p>
  {% endif %}

  <h2>Connected accounts</h2>
  <table>
    <thead><tr><th>Account</th><th>Type</th></tr></thead>
    <tbody>
    {% for a in accounts %}
    <tr><td>{{ a.display_name or a.account_id }}</td><td>{{ a.account_type or "Bank account" }}</td></tr>
    {% endfor %}
    {% for c in cards %}
    <tr><td>{{ c.display_name or c.account_id }}</td><td>Credit card</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <br><a class="btn" style="display:inline-block;padding:10px 20px;background:#dc2626;color:white;border-radius:8px;text-decoration:none;font-size:0.9rem" href="/disconnect">Disconnect bank</a>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True)
