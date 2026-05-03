"""
Bek Rate Desk — ALM Analytics Platform
=======================================
Flask application — all routes. Port 5001.
Sprint 1: Behavioral / Monte Carlo / Alerts
Sprint 2: Funding Engine / FTP Pricing / Visualization Upgrade
"""

import os, sys, json, webbrowser
from datetime import datetime
from threading import Timer
from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv
load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(SCRIPT_DIR, 'data')
sys.path.insert(0, SCRIPT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY']    = 'bek-rate-desk-2025'
app.config['UPLOAD_FOLDER'] = os.path.join(SCRIPT_DIR, 'static', 'downloads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

EXPECTED_USER = os.environ.get('TRADING_APP_USER', 'deniz')
EXPECTED_PASS = os.environ.get('TRADING_APP_PASS', '123963')

def _unauthorized():
    return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Bek Rate Desk"'})

@app.before_request
def require_basic_auth():
    if request.path.startswith('/static'): return
    auth = request.authorization
    if not auth or auth.username != EXPECTED_USER or auth.password != EXPECTED_PASS:
        return _unauthorized()

# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — CORE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index(): return render_template('index.html')

@app.route('/nii-sensitivity')
def nii_sensitivity(): return render_template('nii_sensitivity.html')

@app.route('/eve-duration-gap')
def eve_duration_gap(): return render_template('eve_duration_gap.html')

@app.route('/fx-gap')
def fx_gap(): return render_template('fx_gap.html')

@app.route('/liquidity-ratios')
def liquidity_ratios(): return render_template('liquidity_ratios.html')

@app.route('/ftp-curve')
def ftp_curve(): return render_template('ftp_curve.html')

@app.route('/bond-pricer')
def bond_pricer(): return render_template('bond_pricer.html')

@app.route('/oas-calculator')
def oas_calculator(): return render_template('oas_calculator.html')

@app.route('/rate-delta')
def rate_delta(): return render_template('rate_delta.html')

@app.route('/yield-curve')
def yield_curve(): return render_template('yield_curve.html')

@app.route('/arbitrage')
def arbitrage(): return render_template('arbitrage.html')

# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — SPRINT 1
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/behavioral-modeling')
def behavioral_modeling(): return render_template('behavioral_modeling.html')

@app.route('/monte-carlo')
def monte_carlo(): return render_template('monte_carlo.html')

@app.route('/alerts')
def alerts_page(): return render_template('alerts.html')

# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — SPRINT 2
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/funding-engine')
def funding_engine(): return render_template('funding_engine.html')

@app.route('/ftp-pricing')
def ftp_pricing(): return render_template('ftp_pricing.html')

@app.route('/risk-heatmap')
def risk_heatmap(): return render_template('risk_heatmap.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — NII SENSITIVITY
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/nii/calculate', methods=['POST'])
def api_nii_calculate():
    try:
        from utils.alm_engine import run_nii_sensitivity
        from utils.results_cache import cache_result
        result = run_nii_sensitivity(request.get_json())
        cache_result("nii", result)
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/nii/save-scenario', methods=['POST'])
def api_nii_save():
    try:
        data = request.get_json()
        path = os.path.join(DATA_DIR, 'nii_scenarios.json')
        existing = []
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        existing.append(data)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2)
        return jsonify({'status': 'saved', 'count': len(existing)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/nii/load-scenarios')
def api_nii_load():
    path = os.path.join(DATA_DIR, 'nii_scenarios.json')
    if not os.path.exists(path):
        return jsonify([])
    with open(path, 'r', encoding='utf-8') as f:
        return jsonify(json.load(f))

# ══════════════════════════════════════════════════════════════════════════════
# API — EVE / DURATION GAP
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/eve/calculate', methods=['POST'])
def api_eve_calculate():
    try:
        from utils.alm_engine import run_eve_analysis
        from utils.results_cache import cache_result
        result = run_eve_analysis(request.get_json())
        cache_result("eve", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — FX GAP
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/fx-gap/calculate', methods=['POST'])
def api_fx_calculate():
    try:
        from utils.alm_engine import run_fx_gap
        from utils.results_cache import cache_result
        result = run_fx_gap(request.get_json())
        cache_result("fx_gap", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/fx-gap/rates')
def api_fx_rates():
    try:
        from utils.alm_engine import fetch_fx_rates
        return jsonify(fetch_fx_rates())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — LIQUIDITY RATIOS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/liquidity/calculate', methods=['POST'])
def api_liquidity_calculate():
    try:
        from utils.alm_engine import run_liquidity_ratios
        from utils.results_cache import cache_result
        result = run_liquidity_ratios(request.get_json())
        cache_result("lcr", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — FTP CURVE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/ftp/calculate', methods=['POST'])
def api_ftp_calculate():
    try:
        from utils.alm_engine import run_ftp_curve
        return jsonify(run_ftp_curve(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — RATE DELTA  (now includes TR data)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/rate-delta/data')
def api_rate_delta():
    try:
        from utils.rates_engine import fetch_rate_delta_data, fetch_tr_analytics
        data = fetch_rate_delta_data()
        data['tr'] = fetch_tr_analytics()
        return jsonify(data)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — YIELD CURVE  (now includes TR data)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/yield-curve/data')
def api_yield_curve():
    try:
        from utils.rates_engine import fetch_yield_curve_data, fetch_tr_analytics, fetch_tr_yield_curve
        data = fetch_yield_curve_data()
        data['tr']       = fetch_tr_analytics()
        data['tr_curve'] = fetch_tr_yield_curve()
        return jsonify(data)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/tr-yield-curve')
def api_tr_yield_curve_standalone():
    try:
        from utils.rates_engine import fetch_tr_yield_curve
        return jsonify(fetch_tr_yield_curve())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — OAS CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/oas/calculate', methods=['POST'])
def api_oas_calculate():
    try:
        from utils.oas_engine import calculate_oas_full
        return jsonify(calculate_oas_full(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/oas/fred-curves', methods=['POST'])
def api_oas_fred():
    try:
        from utils.oas_engine import fetch_treasury_curve, fetch_credit_spreads, fetch_move_vol
        data  = request.get_json()
        return jsonify({
            'treasury_curve': fetch_treasury_curve(),
            'credit_spread':  fetch_credit_spreads(data.get('rating','BBB')),
            'rate_vol':       fetch_move_vol(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — BOND PRICER
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/bond/price', methods=['POST'])
def api_bond_price():
    try:
        from utils.oas_engine import price_bond_full
        return jsonify(price_bond_full(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — ARBITRAGE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/peer-valuation', methods=['POST'])
def api_peer_valuation():
    try:
        import yfinance as yf
        import numpy as np
        data    = request.get_json()
        tickers = data.get('tickers', [])
        items   = []
        yields  = []
        for ticker in tickers[:30]:
            try:
                tk    = yf.Ticker(ticker)
                info  = tk.info or {}
                price = info.get('regularMarketPrice') or info.get('previousClose')
                if price is None:
                    hist  = tk.history(period='5d')
                    price = float(hist['Close'].iloc[-1]) if not hist.empty else None
                if price is None: continue
                divs      = tk.dividends
                annual_div= float(divs.last('365D').sum()) if not divs.empty else (info.get('dividendRate') or 0)
                yld       = (annual_div / price * 100) if price and annual_div else None
                items.append({'ticker': ticker, 'current_price': round(price,4),
                              'annual_div': round(annual_div,4),
                              'current_yield': round(yld,4) if yld else None,
                              'spread_vs_median': None})
                if yld: yields.append(yld)
            except Exception: continue
        median = float(np.median(yields)) if yields else None
        for item in items:
            if item['current_yield'] and median:
                item['spread_vs_median'] = round(item['current_yield'] - median, 4)
        return jsonify({'items': items, 'universe_median_yield': round(median,4) if median else None})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/z-pair-analysis')
def api_z_pair():
    try:
        import yfinance as yf
        import numpy as np
        import pandas as pd
        from statsmodels.regression.linear_model import OLS
        from statsmodels.tools import add_constant
        t1 = request.args.get('ticker1','').upper()
        t2 = request.args.get('ticker2','').upper()
        lb = int(request.args.get('lookback', 504))
        if not t1 or not t2: return jsonify({'error': 'ticker1 and ticker2 required'}), 400
        p1 = yf.download(t1, period='2y', progress=False)['Close'].squeeze().dropna()
        p2 = yf.download(t2, period='2y', progress=False)['Close'].squeeze().dropna()
        common = p1.index.intersection(p2.index)
        if len(common) < 60: return jsonify({'error': f'Insufficient data ({len(common)} days)'}), 400
        p1 = p1.loc[common].tail(lb)
        p2 = p2.loc[common].tail(lb)
        ratio     = p1 / p2
        roll_mean = ratio.rolling(252).mean()
        roll_std  = ratio.rolling(252).std()
        z_series  = (ratio - roll_mean) / roll_std
        current_z = float(z_series.dropna().iloc[-1])
        model     = OLS(p1.values, add_constant(p2.values)).fit()
        hedge_ratio = float(model.params[1])
        spread    = p1 - hedge_ratio * p2
        m2        = OLS(np.diff(spread.values), add_constant(spread.values[:-1])).fit()
        beta      = m2.params[1]
        half_life = float(-np.log(2)/beta) if beta < 0 else None
        return jsonify({
            'ticker1': t1, 'ticker2': t2,
            'current_z': round(current_z,4),
            'z_max': round(float(z_series.dropna().max()),4),
            'z_min': round(float(z_series.dropna().min()),4),
            'correlation': round(float(p1.corr(p2)),4),
            'hedge_ratio': round(hedge_ratio,4),
            'half_life': round(half_life,2) if half_life else None,
            'z_series': [{"date": str(i.date()), "z": round(float(z),4)}
                         for i,z in z_series.dropna().tail(252).items()],
        })
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 1: BEHAVIORAL MODELING
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/behavioral/deposit-beta', methods=['POST'])
def api_deposit_beta():
    try:
        from utils.behavioral_engine import run_deposit_beta_model
        return jsonify(run_deposit_beta_model(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/behavioral/stickiness', methods=['POST'])
def api_stickiness():
    try:
        from utils.behavioral_engine import run_deposit_stickiness
        return jsonify(run_deposit_stickiness(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/behavioral/prepayment', methods=['POST'])
def api_prepayment():
    try:
        from utils.behavioral_engine import run_prepayment_model
        return jsonify(run_prepayment_model(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/behavioral/nii', methods=['POST'])
def api_behavioral_nii():
    try:
        from utils.behavioral_engine import run_behavioral_nii
        return jsonify(run_behavioral_nii(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 1: MONTE CARLO
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/monte-carlo/run', methods=['POST'])
@app.route('/api/monte-carlo/simulate', methods=['POST'])
def api_monte_carlo():
    """
    Monte Carlo NII simulation.
    Handles both /run and /simulate — the two legacy URLs are unified here.
    Result is always written to the results cache (key "mc") so the dashboard
    and export endpoints see fresh data without a second computation.
    """
    try:
        from utils.monte_carlo_engine import run_monte_carlo
        from utils.results_cache import cache_result
        result = run_monte_carlo(request.get_json())
        cache_result("mc", result)
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/monte-carlo/stress-grid', methods=['POST'])
def api_monte_carlo_stress_grid():
    """NII stress grid: parallel rate shocks × slope shocks."""
    try:
        from utils.monte_carlo_engine import run_stress_grid
        return jsonify(run_stress_grid(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 1: ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/alerts/check', methods=['POST'])
def api_alerts_check():
    try:
        from utils.alert_engine import evaluate_alerts, save_alerts
        data   = request.get_json()
        result = evaluate_alerts(data)
        save_alerts(result)
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/alerts/history')
def api_alerts_history():
    try:
        from utils.alert_engine import load_alert_history
        return jsonify(load_alert_history())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/alerts/acknowledge', methods=['POST'])
def api_alerts_acknowledge():
    try:
        from utils.alert_engine import acknowledge_alert
        return jsonify(acknowledge_alert(request.get_json().get('alert_id','')))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/alerts/thresholds', methods=['GET','POST'])
def api_alerts_thresholds():
    try:
        from utils.alert_engine import load_thresholds, save_thresholds
        if request.method == 'POST':
            return jsonify(save_thresholds(request.get_json()))
        return jsonify(load_thresholds())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/alerts/send-email', methods=['POST'])
def api_alerts_email():
    try:
        from utils.alert_engine import send_email_alert
        data = request.get_json()
        return jsonify(send_email_alert(data.get('smtp_config',{}), data.get('alerts',[])))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 2: FUNDING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/funding/analyze', methods=['POST'])
def api_funding_analyze():
    try:
        from utils.funding_engine import run_funding_analysis
        return jsonify(run_funding_analysis(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/funding/optimize', methods=['POST'])
def api_funding_optimize():
    try:
        from utils.funding_engine import run_funding_optimizer
        return jsonify(run_funding_optimizer(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 2: FTP LOAN PRICING
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/ftp/loan-price', methods=['POST'])
def api_ftp_loan_price():
    try:
        from utils.funding_engine import run_loan_pricer
        return jsonify(run_loan_pricer(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/ftp/business-lines', methods=['POST'])
def api_ftp_business_lines():
    try:
        from utils.funding_engine import run_business_line_profitability
        return jsonify(run_business_line_profitability(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 2: RISK HEATMAP
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/risk-heatmap/data', methods=['POST'])
def api_risk_heatmap():
    try:
        from utils.funding_engine import build_risk_heatmap
        return jsonify(build_risk_heatmap(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — SPRINT 3
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/morning-brief')
def morning_brief(): return render_template('morning_brief.html')

@app.route('/hedge-engine')
def hedge_engine(): return render_template('hedge_engine.html')

@app.route('/alco-memo')
def alco_memo(): return render_template('alco_memo.html')

@app.route('/scenarios')
def scenarios(): return render_template('scenarios.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 3: MORNING BRIEF
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/morning-brief', methods=['POST'])
def api_morning_brief():
    try:
        from utils.sprint3_engine import build_morning_brief
        return jsonify(build_morning_brief(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/prefill/<target>')
def api_prefill(target):
    """
    Returns pre-filled field values for a target module, assembled from
    cached calculation results + live market rates.
    Targets: morning_brief | hedge | alco | eod
    """
    try:
        from utils.results_cache import get_prefill
        return jsonify(get_prefill(target))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 3: HEDGE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/hedge/recommend', methods=['POST'])
def api_hedge_recommend():
    try:
        from utils.sprint3_engine import run_hedge_engine
        return jsonify(run_hedge_engine(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 3: ALCO MEMO
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/alco-memo/generate', methods=['POST'])
def api_alco_memo():
    try:
        from utils.sprint3_engine import generate_alco_memo
        return jsonify(generate_alco_memo(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 3: SCENARIO LIBRARY
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/scenarios', methods=['GET'])
def api_scenarios_list():
    try:
        from utils.sprint3_engine import list_scenarios
        return jsonify(list_scenarios())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scenarios/<scenario_id>', methods=['GET'])
def api_scenario_get(scenario_id):
    try:
        from utils.sprint3_engine import get_scenario
        s = get_scenario(scenario_id)
        if not s: return jsonify({'error': 'Not found'}), 404
        return jsonify(s)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scenarios', methods=['POST'])
def api_scenario_save():
    try:
        from utils.sprint3_engine import save_scenario
        return jsonify(save_scenario(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scenarios/<scenario_id>', methods=['DELETE'])
def api_scenario_delete(scenario_id):
    try:
        from utils.sprint3_engine import delete_scenario
        return jsonify(delete_scenario(scenario_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — SPRINT 4
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/pricing-simulator')
def pricing_simulator(): return render_template('pricing_simulator.html')

@app.route('/fx-ladder')
def fx_ladder(): return render_template('fx_ladder.html')

@app.route('/hedge-book')
def hedge_book(): return render_template('hedge_book.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 4: PRICING SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/pricing/loan', methods=['POST'])
def api_loan_pricing():
    try:
        from utils.sprint4_engine import simulate_loan_pricing
        return jsonify(simulate_loan_pricing(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/pricing/deposit', methods=['POST'])
def api_deposit_pricing():
    try:
        from utils.sprint4_engine import simulate_deposit_pricing
        return jsonify(simulate_deposit_pricing(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 4: FX LIQUIDITY LADDER
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/fx-ladder/build', methods=['POST'])
def api_fx_ladder():
    try:
        from utils.sprint4_engine import build_fx_ladder
        return jsonify(build_fx_ladder(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/fx-ladder/sample')
def api_fx_ladder_sample():
    try:
        from utils.sprint4_engine import fx_ladder_sample
        return jsonify(fx_ladder_sample())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 4: HEDGE BOOK
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/hedge-book', methods=['GET'])
def api_hedge_book_get():
    try:
        from utils.sprint4_engine import get_book
        rate = float(request.args.get('rate', 42.5))
        return jsonify(get_book(rate))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hedge-book/trade', methods=['POST'])
def api_hedge_book_add():
    try:
        from utils.sprint4_engine import add_trade
        return jsonify(add_trade(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/hedge-book/trade/<trade_id>', methods=['DELETE'])
def api_hedge_book_close(trade_id):
    try:
        from utils.sprint4_engine import close_trade
        return jsonify(close_trade(trade_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hedge-book/trade/<trade_id>', methods=['PUT'])
def api_hedge_book_update(trade_id):
    try:
        from utils.sprint4_engine import update_trade
        return jsonify(update_trade(trade_id, request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hedge-book/load-sample', methods=['POST'])
def api_hedge_book_sample():
    try:
        from utils.sprint4_engine import load_sample_trades
        return jsonify(load_sample_trades())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — SPRINT 5
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/import')
def import_page(): return render_template('import.html')

@app.route('/compare')
def compare_page(): return render_template('compare.html')

@app.route('/regulatory')
def regulatory_page(): return render_template('regulatory.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 5: IMPORT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/import/csv', methods=['POST'])
def api_import_csv():
    try:
        from utils.data_engine import parse_csv_import
        data = request.get_json()
        return jsonify(parse_csv_import(data.get('csv_text',''), data.get('side','assets')))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/import/excel', methods=['POST'])
def api_import_excel():
    try:
        from utils.data_engine import parse_excel_import
        import base64
        data       = request.get_json()
        file_b64   = data.get('file_base64','')
        file_bytes = base64.b64decode(file_b64)
        side       = data.get('side','assets')
        sheet      = data.get('sheet_name')
        return jsonify(parse_excel_import(file_bytes, side, sheet))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/import/template/csv')
def api_template_csv():
    from utils.data_engine import generate_template_csv
    side = request.args.get('side','assets')
    csv_text = generate_template_csv(side)
    from flask import Response
    return Response(csv_text, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=bek_template_{side}.csv'})

@app.route('/api/import/template/excel')
def api_template_excel():
    from utils.data_engine import generate_template_excel_bytes
    from flask import Response
    xl = generate_template_excel_bytes()
    return Response(xl,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=bek_bilanco_template.xlsx'})

@app.route('/api/import/connectors')
def api_connectors():
    from utils.data_engine import list_connector_profiles
    return jsonify(list_connector_profiles())

@app.route('/api/import/connector-guide/<system>')
def api_connector_guide(system):
    from utils.data_engine import generate_connector_guide
    return jsonify(generate_connector_guide(system))

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 5: SCENARIO COMPARISON
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/compare', methods=['POST'])
def api_compare():
    try:
        from utils.data_engine import compare_scenarios
        return jsonify(compare_scenarios(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — SPRINT 5: REGULATORY OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/regulatory/lcr', methods=['POST'])
def api_reg_lcr():
    try:
        from utils.data_engine import generate_lcr_report
        return jsonify(generate_lcr_report(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/regulatory/irrbb', methods=['POST'])
def api_reg_irrbb():
    try:
        from utils.data_engine import generate_irrbb_report
        data = request.get_json()
        return jsonify(generate_irrbb_report(
            data.get('eve_data',{}), data.get('nii_data',{})))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/regulatory/html', methods=['POST'])
def api_reg_html():
    try:
        from utils.data_engine import generate_html_report
        from flask import Response
        data        = request.get_json()
        html        = generate_html_report(data.get('report_data',{}), data.get('report_type','lcr'))
        return Response(html, mimetype='text/html')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — TRADING DESK
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/trading-desk')
def trading_desk(): return render_template('trading_desk.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — TRADING DESK
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/trading/spot', methods=['POST'])
def api_trading_spot():
    try:
        from utils.trading_engine import calculate_spot_fx
        return jsonify(calculate_spot_fx(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/forward', methods=['POST'])
def api_trading_forward():
    try:
        from utils.trading_engine import calculate_forward
        return jsonify(calculate_forward(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/fx-swap', methods=['POST'])
def api_trading_fxswap():
    try:
        from utils.trading_engine import calculate_fx_swap
        return jsonify(calculate_fx_swap(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/cross-rates', methods=['POST'])
def api_trading_cross():
    try:
        from utils.trading_engine import calculate_cross_rates
        return jsonify(calculate_cross_rates(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/positions', methods=['GET'])
def api_trading_positions():
    try:
        from utils.trading_engine import get_position_book
        rates = {k:float(v) for k,v in request.args.items() if v}
        return jsonify(get_position_book(rates))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/positions', methods=['POST'])
def api_trading_add_position():
    try:
        from utils.trading_engine import add_position
        return jsonify(add_position(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/positions/<pos_id>', methods=['DELETE'])
def api_trading_close_position(pos_id):
    try:
        from utils.trading_engine import close_position
        rate = request.args.get('rate')
        return jsonify(close_position(pos_id, float(rate) if rate else None))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/sample-market')
def api_trading_sample():
    try:
        from utils.trading_engine import get_sample_market_data
        return jsonify(get_sample_market_data())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/live-rates')
def api_trading_live_rates():
    """Live FX rates via yfinance — for periodic auto-refresh on the trading desk."""
    try:
        from utils.trading_engine import get_sample_market_data
        data = get_sample_market_data()
        data['_refreshed'] = __import__('datetime').datetime.now().strftime('%H:%M:%S')
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — MONEY MARKET
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/money-market')
def money_market(): return render_template('money_market.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — MONEY MARKET
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/mm/liquidity', methods=['POST'])
def api_mm_liquidity():
    try:
        from utils.money_market_engine import calculate_liquidity_position
        return jsonify(calculate_liquidity_position(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/repo', methods=['POST'])
def api_mm_repo():
    try:
        from utils.money_market_engine import calculate_repo
        return jsonify(calculate_repo(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/tcmb-auction', methods=['POST'])
def api_mm_tcmb():
    try:
        from utils.money_market_engine import simulate_tcmb_auction
        return jsonify(simulate_tcmb_auction(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/ib-limits', methods=['GET'])
def api_mm_ib_get():
    try:
        from utils.money_market_engine import get_ib_limits
        return jsonify(get_ib_limits())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/ib-limits', methods=['POST'])
def api_mm_ib_update():
    try:
        from utils.money_market_engine import update_ib_limit
        d = request.get_json()
        return jsonify(update_ib_limit(d.get('bank',''), float(d.get('used_m',0))))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/ib-limits/reset', methods=['POST'])
def api_mm_ib_reset():
    try:
        from utils.money_market_engine import reset_ib_limits
        return jsonify(reset_ib_limits())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/funding-report', methods=['POST'])
def api_mm_report():
    try:
        from utils.money_market_engine import generate_funding_report
        return jsonify(generate_funding_report(request.get_json()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/sample')
def api_mm_sample():
    try:
        from utils.money_market_engine import get_mm_sample
        return jsonify(get_mm_sample())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/mm/live-rates')
def api_mm_live_rates():
    """Live TCMB policy rate + USD/TRY — for money market desk auto-fill."""
    try:
        from utils.money_market_engine import get_live_mm_rates
        return jsonify(get_live_mm_rates())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES — DEALER TOOLS (FX Options, NOP, P&L, Rate Sheet, Calendar)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/fx-options')
def fx_options(): return render_template('fx_options.html')

@app.route('/nop-tracker')
def nop_tracker(): return render_template('nop_tracker.html')

@app.route('/dealer-pnl')
def dealer_pnl(): return render_template('dealer_pnl.html')

@app.route('/rate-sheet')
def rate_sheet(): return render_template('rate_sheet.html')

@app.route('/eco-calendar')
def eco_calendar(): return render_template('eco_calendar.html')

# ══════════════════════════════════════════════════════════════════════════════
# API — FX OPTIONS (Garman-Kohlhagen)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/options/price', methods=['POST'])
def api_options_price():
    try:
        from utils.options_engine import price_option_full
        return jsonify(price_option_full(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/options/strategy', methods=['POST'])
def api_options_strategy():
    try:
        from utils.options_engine import price_strategy
        payload = request.get_json() or {}
        result  = price_strategy(payload)
        # Engine now returns canonical keys: strategy_type, net_cost, leg.option_type
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/options/vol-surface', methods=['GET', 'POST'])
def api_options_vol_surface():
    try:
        from utils.options_engine import vol_surface
        if request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            # Template sends atm_vol as fraction (value/100); convert to pct for engine
            if 'atm_vol' in payload and 'atm_vol_pct' not in payload:
                payload['atm_vol_pct'] = float(payload['atm_vol']) * 100
        else:
            payload = {}
        result = vol_surface(payload)
        # Alias each surface row to match template field names
        for row in result.get('surface', []):
            row.setdefault('atm_vol',      row.get('atm_vol_pct',  0))   # toFixed(2)%
            row.setdefault('vol',          row.get('atm_vol_pct',  0))   # volClass check
            row.setdefault('rr_25d',       row.get('rr_25d_pct',   0))
            row.setdefault('bf_25d',       row.get('bf_25d_pct',   0))
            row.setdefault('call_vol_25d', row.get('call_vol_pct', 0))   # template: call_vol_25d
            row.setdefault('put_vol_25d',  row.get('put_vol_pct',  0))   # template: put_vol_25d
            row.setdefault('tenor_days',   row.get('days',          0))  # template: tenor_days
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/options/greeks-scan', methods=['POST'])
def api_options_greeks_scan():
    try:
        from utils.options_engine import gk_price, gk_greeks
        import math
        data        = request.get_json()
        S           = float(data.get('spot', 38.5))
        strikes     = [float(k) for k in data.get('strikes', [])]
        r_d         = float(data.get('r_dom_pct', 46.0)) / 100
        r_f         = float(data.get('r_for_pct', 4.33)) / 100
        sigma       = float(data.get('vol_pct', 18.0)) / 100
        T           = float(data.get('tenor_days', 90)) / 365
        opt_type    = data.get('option_type', 'call')
        rows = []
        for K in strikes:
            g = gk_greeks(S, K, r_d, r_f, sigma, T, opt_type)
            p = gk_price(S, K, r_d, r_f, sigma, T, opt_type)
            rows.append({'strike': K, 'price': round(p, 6), **g})
        return jsonify({'rows': rows, 'spot': S, 'option_type': opt_type})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# API — DEALER TOOLS (NOP, Rate Sheet, Daily P&L)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/dealer/nop', methods=['GET', 'POST'])
def api_dealer_nop():
    try:
        from utils.dealer_engine import calculate_nop
        from utils.trading_engine import _load_positions
        if request.method == 'GET':
            # Auto-load open FX positions and compute NOP with live defaults
            fx_positions = [p for p in _load_positions()
                            if p.get('status') == 'open' and
                            p.get('asset_class', '').upper() in ('FX', 'FOREX', 'FX_SPOT', 'FX_FORWARD')]
            data = {'positions': fx_positions}
        else:
            data = request.get_json() or {}
            if 'positions' not in data:
                data['positions'] = [p for p in _load_positions() if p.get('status') == 'open']
        result = calculate_nop(data)
        # Alias: template reads nop_total_m, engine returns total_nop_m
        result.setdefault('nop_total_m', result.get('total_nop_m', 0))
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/dealer/rate-sheet', methods=['POST'])
def api_dealer_rate_sheet():
    try:
        from utils.dealer_engine import build_rate_sheet
        return jsonify(build_rate_sheet(request.get_json()))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/dealer/pnl', methods=['GET'])
def api_dealer_pnl():
    try:
        from utils.dealer_engine import get_daily_pnl_detail
        from utils.trading_engine import _load_positions
        _skip_keys = {'date', 'currency', 'period', 'format', 'filter'}
        rates = {k: float(v) for k, v in request.args.items()
                 if v and k not in _skip_keys
                 and v.replace('.', '', 1).replace('-', '', 1).isdigit()}
        positions = _load_positions()
        r = get_daily_pnl_detail({'current_rates': rates, 'positions_file': positions})
        # Aliases: template reads *_m fields and d.by_pair list
        r['realized_pnl_m']   = r.get('realized_pnl', 0)
        r['unrealized_pnl_m'] = r.get('unrealized_pnl', 0)
        r['total_pnl_m']      = r.get('total_pnl', 0)
        # Reshape pnl_by_pair dict → by_pair list for template iteration
        by_pair_dict = r.get('pnl_by_pair', {})
        r['by_pair'] = [
            {
                'pair':            k,
                # dealer_engine uses keys: 'realized', 'unrealized', 'total'
                'realized_pnl_m':   round(v.get('realized',   v.get('realized_pnl',   v.get('realized_pnl_m',   0))), 2),
                'unrealized_pnl_m': round(v.get('unrealized', v.get('unrealized_pnl', v.get('unrealized_pnl_m', 0))), 2),
                'total_pnl_m':      round(v.get('total',      v.get('total_pnl',      v.get('total_pnl_m',      0))), 2),
            }
            for k, v in by_pair_dict.items()
        ] if isinstance(by_pair_dict, dict) else (by_pair_dict if isinstance(by_pair_dict, list) else [])
        return jsonify(r)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# PAGE + API — ALM ADVISOR (Aksiyon Motoru)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/alm-advisor')
def alm_advisor(): return render_template('alm_advisor.html')

@app.route('/api/alm/recommend', methods=['POST'])
def api_alm_recommend():
    try:
        from utils.alm_advisor_engine import generate_recommendations
        data = request.get_json() or {}
        return jsonify(generate_recommendations(data))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# PAGE + API — SPRINT 6: DEALER ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/risk-ladder')
def risk_ladder(): return render_template('risk_ladder.html')

@app.route('/carry-calc')
def carry_calc(): return render_template('carry_calc.html')

@app.route('/portfolio-oas')
def portfolio_oas(): return render_template('portfolio_oas.html')

@app.route('/rv-matrix')
def rv_matrix(): return render_template('rv_matrix.html')

@app.route('/carry-trade')
def carry_trade(): return render_template('carry_trade.html')

@app.route('/api/risk-ladder/data', methods=['POST'])
def api_risk_ladder():
    try:
        from utils.dealer_analytics_engine import build_risk_ladder
        data = request.get_json() or {}
        positions = data.get('positions', [])
        return jsonify(build_risk_ladder(positions))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/dealer/portfolio', methods=['GET', 'PUT'])
def api_dealer_portfolio():
    try:
        from utils.dealer_analytics_engine import load_portfolio, save_portfolio
        if request.method == 'GET':
            return jsonify(load_portfolio())
        else:
            data = request.get_json() or []
            result = save_portfolio(data)
            return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/carry/calculate', methods=['POST'])
def api_carry_calculate():
    try:
        from utils.dealer_analytics_engine import calculate_carry_rolldown
        data = request.get_json() or {}
        curve = data.pop('curve', {})
        return jsonify(calculate_carry_rolldown(data, curve))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio-oas/run', methods=['POST'])
def api_portfolio_oas():
    try:
        from utils.dealer_analytics_engine import run_portfolio_oas
        data = request.get_json() or {}
        positions = data.get('positions', [])
        return jsonify(run_portfolio_oas(positions))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/rv-matrix/data')
def api_rv_matrix():
    try:
        from utils.dealer_analytics_engine import build_rv_matrix
        return jsonify(build_rv_matrix())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/rv-matrix/history/<spread_name>')
def api_rv_history(spread_name):
    try:
        from utils.dealer_analytics_engine import get_spread_history
        window = int(request.args.get('window', 252))
        days   = max(window + 120, 420)
        return jsonify(get_spread_history(spread_name, days=days, window=window))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/carry-trade/calculate', methods=['POST'])
def api_carry_trade():
    try:
        from utils.dealer_analytics_engine import calculate_carry_trade
        data = request.get_json() or {}
        return jsonify(calculate_carry_trade(data))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist', methods=['GET', 'PUT'])
def api_watchlist():
    try:
        from utils.dealer_analytics_engine import load_watchlist, save_watchlist
        if request.method == 'GET':
            return jsonify(load_watchlist())
        else:
            data = request.get_json() or []
            result = save_watchlist(data)
            return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist/check', methods=['POST'])
def api_watchlist_check():
    try:
        from utils.dealer_analytics_engine import check_watchlist_live
        data = request.get_json() or []
        return jsonify(check_watchlist_live(data))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# SPRINT 7 — LIMIT DASHBOARD, P&L ATTRIBUTION, STRESS LIBRARY, EOD BRIEF, EVDS
# ══════════════════════════════════════════════════════════════════════════════

# ── Limit Dashboard ───────────────────────────────────────────────────────────
@app.route('/limit-dashboard')
def limit_dashboard(): return render_template('limit_dashboard.html')

@app.route('/api/limit-dashboard/data')
def api_limit_data():
    try:
        from utils.limit_engine import calculate_limit_dashboard
        return jsonify(calculate_limit_dashboard())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/limit-dashboard/config', methods=['GET', 'PUT'])
def api_limit_config():
    try:
        from utils.limit_engine import load_limits, save_limits
        if request.method == 'GET':
            return jsonify(load_limits())
        data = request.get_json() or {}
        return jsonify(save_limits(data))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ── P&L Attribution ───────────────────────────────────────────────────────────
@app.route('/pnl-attribution')
def pnl_attribution(): return render_template('pnl_attribution.html')

@app.route('/api/pnl-attribution/calculate')
def api_pnl_attribution():
    try:
        from utils.pnl_attribution_engine import calculate_attribution
        return jsonify(calculate_attribution())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/pnl-attribution/snapshot', methods=['POST'])
def api_pnl_snapshot():
    try:
        from utils.pnl_attribution_engine import take_snapshot
        data  = request.get_json() or {}
        label = data.get('label', 'manual')
        return jsonify(take_snapshot(label=label))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/pnl-attribution/history')
def api_pnl_history():
    try:
        from utils.pnl_attribution_engine import load_pnl_history
        return jsonify(load_pnl_history())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ── Stress Library ────────────────────────────────────────────────────────────
@app.route('/stress-library')
def stress_library(): return render_template('stress_library.html')

@app.route('/api/stress/scenarios')
def api_stress_scenarios():
    try:
        from utils.stress_engine import get_all_scenarios
        return jsonify(get_all_scenarios())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/stress/run/<scenario_id>')
def api_stress_run(scenario_id):
    try:
        from utils.stress_engine import apply_scenario_to_portfolio
        return jsonify(apply_scenario_to_portfolio(scenario_id))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/stress/run-all')
def api_stress_run_all():
    try:
        from utils.stress_engine import run_all_scenarios
        return jsonify(run_all_scenarios())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/stress/custom', methods=['POST'])
def api_stress_custom_save():
    try:
        from utils.stress_engine import save_custom_scenario
        data = request.get_json() or {}
        return jsonify(save_custom_scenario(data))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ── EOD Brief ─────────────────────────────────────────────────────────────────
@app.route('/eod-brief')
def eod_brief(): return render_template('eod_brief.html')

@app.route('/api/eod/generate')
def api_eod_generate():
    try:
        from utils.eod_engine import generate_eod_brief
        return jsonify(generate_eod_brief(save_cache=True))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/eod/cached')
def api_eod_cached():
    try:
        from utils.eod_engine import load_cached_eod, generate_eod_brief
        cached = load_cached_eod()
        if cached:
            return jsonify(cached)
        return jsonify(generate_eod_brief(save_cache=True))
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ── TCMB EVDS ─────────────────────────────────────────────────────────────────
@app.route('/api/tcmb/status')
def api_tcmb_status():
    try:
        from utils.tcmb_evds import get_evds_status
        return jsonify(get_evds_status())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/tcmb/config', methods=['POST'])
def api_tcmb_config():
    try:
        from utils.tcmb_evds import save_evds_key
        data = request.get_json() or {}
        key  = data.get('evds_key', '').strip()
        if not key:
            return jsonify({'error': 'Geçerli bir EVDS anahtarı girin'}), 400
        save_evds_key(key)
        return jsonify({'status': 'saved'})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/tcmb/yield-curve')
def api_tcmb_curve():
    try:
        from utils.tcmb_evds import fetch_tr_yield_curve_evds
        return jsonify(fetch_tr_yield_curve_evds())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/tcmb/macro')
def api_tcmb_macro():
    try:
        from utils.tcmb_evds import fetch_tr_macro
        return jsonify(fetch_tr_macro())
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# APP STARTUP
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# API — DATA HEALTH
# ══════════════════════════════════════════════════════════════════════════════

# ═════════════════════════���═══════════════════════���════════════════════════════
# BALANCE SHEET SESSION
# ═════════════════════════════════════════════════���══════════════════════════���═

@app.route('/balance-sheet')
def balance_sheet_page():
    from utils.balance_sheet_session import get_session_summary
    summary = get_session_summary()
    return render_template('balance_sheet.html', summary=summary)


@app.route('/api/session/balance-sheet', methods=['GET'])
def api_session_get():
    from utils.balance_sheet_session import load_session
    session = load_session()
    if session is None:
        return jsonify({'has_session': False})
    return jsonify({'has_session': True, **session})


@app.route('/api/session/balance-sheet', methods=['POST'])
def api_session_save():
    from utils.balance_sheet_session import save_session
    try:
        payload = request.get_json(force=True)
        saved = save_session(payload)
        return jsonify({'ok': True, 'session': saved})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/session/balance-sheet', methods=['DELETE'])
def api_session_clear():
    from utils.balance_sheet_session import clear_session
    clear_session()
    return jsonify({'ok': True})


@app.route('/api/session/summary')
def api_session_summary():
    from utils.balance_sheet_session import get_session_summary
    return jsonify(get_session_summary())


@app.route('/api/session/sample')
def api_session_sample():
    from utils.balance_sheet_session import load_sample_session
    return jsonify(load_sample_session())


@app.route('/api/export/nii', methods=['POST'])
def api_export_nii():
    """Export NII Sensitivity results to Excel. Body: same payload as /api/nii/calculate."""
    try:
        from utils.alm_engine import run_nii_sensitivity
        from utils.excel_export import export_nii
        from flask import send_file
        result = run_nii_sensitivity(request.get_json())
        buf = export_nii(result)
        fname = f"nii_sensitivity_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/eve', methods=['POST'])
def api_export_eve():
    """Export EVE / Duration Gap results to Excel. Body: same payload as /api/eve/calculate."""
    try:
        from utils.alm_engine import run_eve_analysis
        from utils.excel_export import export_eve
        from flask import send_file
        result = run_eve_analysis(request.get_json())
        buf = export_eve(result)
        fname = f"eve_analysis_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/repricing-gap', methods=['POST'])
def api_export_repricing_gap():
    """Export Repricing Gap to Excel. Body: same payload as /api/nii/calculate."""
    try:
        from utils.alm_engine import run_nii_sensitivity
        from utils.excel_export import export_repricing_gap
        from flask import send_file
        result = run_nii_sensitivity(request.get_json())
        buf = export_repricing_gap(result)
        fname = f"repricing_gap_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/rv-matrix')
def api_export_rv_matrix():
    """Export RV Matrix to Excel (GET — uses live data)."""
    try:
        from utils.dealer_analytics_engine import build_rv_matrix
        from utils.excel_export import export_rv_matrix
        from flask import send_file
        result = build_rv_matrix()
        buf = export_rv_matrix(result)
        fname = f"rv_matrix_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/data-health')
def api_data_health():
    try:
        from utils.data_health import get_health
        return jsonify(get_health())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS PAGE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/settings')
def settings_page():
    from utils.tcmb_evds import get_evds_status, _get_evds_key
    from utils.rates_engine import get_fred_status, _get_fred_key
    evds = get_evds_status()
    fred = get_fred_status()
    return render_template('settings.html', evds=evds, fred=fred)


@app.route('/api/settings/save-evds-key', methods=['POST'])
def api_save_evds_key():
    from utils.tcmb_evds import save_evds_key, get_evds_status
    from utils.api_cache import invalidate_all as _flush_api_cache
    data = request.get_json(force=True)
    key  = (data.get('key') or '').strip()
    if not key:
        return jsonify({'ok': False, 'msg': 'Anahtar boş olamaz'}), 400
    save_evds_key(key)
    # Flush in-process cache so the new EVDS key is used immediately
    _flush_api_cache()
    status = get_evds_status()
    return jsonify({'ok': True, 'status': status})


@app.route('/api/settings/save-fred-key', methods=['POST'])
def api_save_fred_key():
    from utils.rates_engine import save_fred_key, get_fred_status
    from utils.api_cache import invalidate_all as _flush_api_cache
    data = request.get_json(force=True)
    key  = (data.get('key') or '').strip()
    if not key:
        return jsonify({'ok': False, 'msg': 'Anahtar boş olamaz'}), 400
    save_fred_key(key)
    # Flush the in-process API cache so the new key is used on next fetch
    _flush_api_cache()
    status = get_fred_status()
    return jsonify({'ok': True, 'status': status})


@app.route('/api/settings/test-evds', methods=['POST'])
def api_test_evds():
    from utils.tcmb_evds import get_evds_status
    try:
        status = get_evds_status()
        return jsonify({'ok': True, 'status': status})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500


@app.route('/api/settings/test-fred', methods=['POST'])
def api_test_fred():
    from utils.rates_engine import get_fred_status
    try:
        status = get_fred_status()
        return jsonify({'ok': True, 'status': status})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500


@app.route('/api/settings/cache-stats', methods=['GET'])
def api_cache_stats():
    """Return in-process API cache hit/miss metrics and active keys."""
    from utils.api_cache import cache_stats
    return jsonify(cache_stats())


@app.route('/api/settings/cache-flush', methods=['POST'])
def api_cache_flush():
    """Manually flush the entire in-process API cache (admin utility)."""
    from utils.api_cache import invalidate_all as _flush
    _flush()
    return jsonify({'ok': True, 'msg': 'Cache flushed'})


def open_browser():
    webbrowser.open_new('http://127.0.0.1:5001/')

if __name__ == '__main__':
    Timer(1, open_browser).start()
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
