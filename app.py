import json
import os
import re
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
CONFIG_FILE = 'funds.json'
MAX_WORKERS = 10


# ----------------------
# Data helpers
# ----------------------

def load_holdings():
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (OSError, json.JSONDecodeError):
        return []
    return []


def save_holdings(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_random_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "http://fund.eastmoney.com/"
    }


# ----------------------
# Data sources
# ----------------------

def fetch_from_eastmoney(code):
    """Realtime estimate from Eastmoney fund API."""
    try:
        ts = int(time.time() * 1000)
        url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={ts}"
        res = requests.get(url, headers=get_random_headers(), timeout=2)
        match = re.search(r'jsonpgz\((.*?)\);', res.text)
        if not match:
            return None
        data = json.loads(match.group(1))
        return {
            "source": "eastmoney",
            "name": data.get('name', f"基金{code}"),
            "gsz": float(data.get('gsz', 0)),
            "dwjz": float(data.get('dwjz', 0)),
            "gszzl": float(data.get('gszzl', 0)),
            "update_time": data.get('gztime', '')
        }
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return None


def fetch_from_sina(code):
    """Official net value from Sina, usually reliable off-hours."""
    try:
        url = f"http://hq.sinajs.cn/list=f_{code}"
        res = requests.get(url, headers=get_random_headers(), timeout=2)
        try:
            content = res.content.decode('gbk')
        except UnicodeDecodeError:
            content = res.text
        match = re.search(r'="(.*?)"', content)
        if not match:
            return None
        data = match.group(1).split(',')
        if len(data) < 5:
            return None
        name = data[0]
        gsz = float(data[1])
        dwjz = float(data[3])
        gszzl = 0.0
        if dwjz:
            gszzl = (gsz - dwjz) / dwjz * 100
        return {
            "source": "sina",
            "name": name,
            "gsz": gsz,
            "dwjz": dwjz,
            "gszzl": gszzl,
            "update_time": data[4]
        }
    except (requests.RequestException, ValueError, IndexError):
        return None


def get_best_data(code):
    now = time.localtime()
    is_weekend = now.tm_wday >= 5
    is_trading_time = 9 <= now.tm_hour <= 15

    eastmoney = fetch_from_eastmoney(code)
    if is_trading_time and eastmoney:
        return eastmoney

    sina = fetch_from_sina(code)
    if is_weekend:
        return sina or eastmoney

    return eastmoney or sina


# ----------------------
# Business logic
# ----------------------

def format_update_time(value):
    if not value:
        return '--'
    return value


def compute_fund_view(holding, remote):
    code = holding['code']
    name = holding.get('name') or f"基金{code}"
    shares = float(holding.get('shares', 0))
    cost = float(holding.get('cost', 0))

    if remote:
        gsz = float(remote.get('gsz', cost))
        dwjz = float(remote.get('dwjz', cost))
        gszzl = float(remote.get('gszzl', 0))
        name = remote.get('name') or name
        update_time = format_update_time(remote.get('update_time'))
        source = remote.get('source', 'unknown')
    else:
        gsz = cost
        dwjz = cost
        gszzl = 0
        update_time = '--'
        source = 'offline'

    market_value = shares * gsz
    day_profit = (gsz - dwjz) * shares
    total_profit = (gsz - cost) * shares

    return {
        "code": code,
        "name": name,
        "shares": shares,
        "cost": cost,
        "gsz": gsz,
        "dwjz": dwjz,
        "gszzl": gszzl,
        "market_value": market_value,
        "day_profit": day_profit,
        "total_profit": total_profit,
        "update_time": update_time,
        "source": source
    }


# ----------------------
# Routes
# ----------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/holdings')
def get_holdings():
    holdings = load_holdings()
    return jsonify({"data": holdings})


@app.route('/api/add_fund', methods=['POST'])
def add_fund():
    data = request.get_json(force=True)
    code = str(data.get('code', '')).strip()
    if not code:
        return jsonify({"error": "基金代码不能为空"}), 400

    shares = float(data.get('shares', 0))
    cost = float(data.get('cost', 0))
    name = data.get('name', '').strip()

    if shares <= 0:
        return jsonify({"error": "持有份额必须大于0"}), 400
    if cost <= 0:
        return jsonify({"error": "成本价必须大于0"}), 400

    holdings = load_holdings()
    updated = False
    for item in holdings:
        if item['code'] == code:
            item['shares'] = shares
            item['cost'] = cost
            if name:
                item['name'] = name
            updated = True
            break

    if not updated:
        holdings.append({
            "code": code,
            "name": name or f"基金{code}",
            "shares": shares,
            "cost": cost
        })

    save_holdings(holdings)
    return jsonify({"success": True})


@app.route('/api/delete_fund', methods=['POST'])
def delete_fund():
    data = request.get_json(force=True)
    code = str(data.get('code', '')).strip()
    holdings = load_holdings()
    new_holdings = [item for item in holdings if item['code'] != code]
    save_holdings(new_holdings)
    return jsonify({"success": True})


@app.route('/api/valuations')
def get_valuations():
    holdings = load_holdings()
    data = []
    total_market = 0
    total_day = 0
    total_profit = 0

    for holding in holdings:
        remote = get_best_data(holding['code'])
        view = compute_fund_view(holding, remote)
        data.append(view)
        total_market += view['market_value']
        total_day += view['day_profit']
        total_profit += view['total_profit']

    response = {
        "data": data,
        "summary": {
            "total_market_value": total_market,
            "total_day_profit": total_day,
            "total_hold_profit": total_profit,
            "updated_at": datetime.now().strftime('%H:%M:%S')
        }
    }
    return jsonify(response)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
