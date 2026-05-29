import os
import requests
from flask import Flask, redirect, request, session, render_template_string
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

CLIENT_ID = os.getenv("TRUELAYER_CLIENT_ID")
CLIENT_SECRET = os.getenv("TRUELAYER_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TRUELAYER_REDIRECT_URI")

AUTH_URL = "https://auth.truelayer-sandbox.com"
API_URL = "https://api.truelayer-sandbox.com"

SCOPES = "info accounts balance transactions cards"


@app.route("/")
def index():
    connected = "access_token" in session
    return render_template_string(HOME_HTML, connected=connected)


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
    resp.raise_for_status()
    session["access_token"] = resp.json()["access_token"]
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    token = session.get("access_token")
    if not token:
        return redirect("/")

    headers = {"Authorization": f"Bearer {token}"}

    accounts = _get_json(f"{API_URL}/data/v1/accounts", headers).get("results", [])
    cards = _get_json(f"{API_URL}/data/v1/cards", headers).get("results", [])

    all_transactions = []
    for acc in accounts:
        txns = _get_json(f"{API_URL}/data/v1/accounts/{acc['account_id']}/transactions", headers).get("results", [])
        all_transactions.extend(txns)
    for card in cards:
        txns = _get_json(f"{API_URL}/data/v1/cards/{card['account_id']}/transactions", headers).get("results", [])
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
        if amount > 0 and date_str:
            by_merchant[name].append({"amount": amount, "date": date_str})

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
            subscriptions.append({
                "merchant": merchant,
                "frequency": "Weekly" if avg_gap <= 9 else ("Monthly" if avg_gap <= 35 else "Annual"),
                "avg_amount": round(avg_amount, 2),
                "monthly_estimate": round(monthly, 2),
                "occurrences": len(charges),
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
    <p class="connected">&#10003; Bank connected</p>
    <a class="btn" href="/dashboard">View Dashboard</a>
    <a class="btn" style="background:#dc2626;margin-left:8px" href="/disconnect">Disconnect</a>
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
    <thead><tr><th>Merchant</th><th>Frequency</th><th>Avg charge</th><th>Monthly est.</th><th>Seen</th></tr></thead>
    <tbody>
    {% for s in subscriptions %}
    <tr>
      <td>{{ s.merchant }}</td>
      <td><span class="tag {{ s.frequency }}">{{ s.frequency }}</span></td>
      <td class="amount">&pound;{{ "%.2f"|format(s.avg_amount) }}</td>
      <td class="amount">&pound;{{ "%.2f"|format(s.monthly_estimate) }}</td>
      <td>{{ s.occurrences }}x</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
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
