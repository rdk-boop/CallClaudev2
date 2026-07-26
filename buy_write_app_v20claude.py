import pandas as pd
import yfinance as yf
import streamlit as st
from datetime import datetime
import base64

# --- User Inputs ---
stock_symbol = st.text_input("Enter Stock Ticker", "OKE")
shares = st.number_input("Number of Shares", value=100)

# Selectable date purchased, default to today
purchase_date = st.date_input("Date Purchased", datetime.today())

# Filter toggle for criteria
filter_criteria = st.checkbox("Show only options that meet criteria", value=False)

# --- Fetch stock data ---
stock = yf.Ticker(stock_symbol)

# --- Get last available close on or before purchase date ---
hist = stock.history(end=pd.Timestamp(purchase_date) + pd.Timedelta(days=1))
if hist.empty or hist['Close'].dropna().empty:
    st.error("No stock price data available for the selected purchase date.")
    st.stop()
stock_price = float(hist['Close'].dropna().iloc[-1])

# --- Dividend series ---
div_series = stock.dividends
if not div_series.empty:
    if div_series.index.tz is None:
        div_series = div_series.tz_localize("America/New_York")

    # --- Calculate dividend frequency from last 12 months ---
    one_year_ago = pd.Timestamp(purchase_date).tz_localize("America/New_York") - pd.DateOffset(years=1)
    recent_divs = div_series[div_series.index >= one_year_ago]
    div_freq = len(recent_divs) if len(recent_divs) > 0 else 1

    # --- Determine yearly dividend ---
    yearly_dividend = recent_divs.sum() if not recent_divs.empty else 0

    # --- Determine next dividend date ---
    next_div_date = div_series[div_series.index > pd.Timestamp(purchase_date).tz_localize("America/New_York")].index.min()
    if pd.isna(next_div_date):
        last_div = div_series.index.max()
        if div_freq >= 12:  # monthly
            next_div_date = last_div + pd.DateOffset(days=30)
        elif div_freq == 6:  # semiannual
            next_div_date = last_div + pd.DateOffset(days=180)
        else:  # quarterly by default
            next_div_date = last_div + pd.DateOffset(days=90)
else:
    div_freq = 1
    yearly_dividend = 0
    next_div_date = None

# --- Timestamp for calculations ---
today = pd.Timestamp(purchase_date).tz_localize("America/New_York")

all_options = []
all_debug_info = []

# --- Identify expirations 6 to 18 months out ---
available_exps = stock.options
filtered_exps = []
for exp_str in available_exps:
    exp_date = pd.Timestamp(exp_str).tz_localize("America/New_York")
    if 6*30 <= (exp_date - today).days <= 18*30:  # approx 6–18 months
        filtered_exps.append(exp_date)

if not filtered_exps:
    st.warning("No expirations available between 6 and 18 months from purchase date.")
    st.stop()

# --- Loop through filtered expirations ---
for option_exp in filtered_exps:
    try:
        opt_chain = stock.option_chain(option_exp.strftime('%Y-%m-%d')).calls
    except Exception as e:
        st.warning(f"Error fetching option chain for {option_exp.date()}: {e}")
        continue

    if opt_chain.empty:
        st.warning(f"No call options for expiration {option_exp.date()}")
        continue

    with st.expander(f"Raw pricing data — {option_exp.date()}"):
        st.dataframe(opt_chain[['strike', 'bid', 'ask', 'lastPrice', 'volume', 'openInterest']].head(10))

    # --- Filter ITM strikes: 10%–40% below stock price ---
    opt_chain['strike'] = pd.to_numeric(opt_chain['strike'], errors='coerce')
    lower_bound = float(stock_price * 0.6)
    upper_bound = float(stock_price * 0.9)
    st.caption(f"Debug — {option_exp.date()}: stock_price={stock_price:.2f}, "
               f"lower_bound={lower_bound:.2f}, upper_bound={upper_bound:.2f}, "
               f"strike dtype={opt_chain['strike'].dtype}, "
               f"strikes available={sorted(opt_chain['strike'].dropna().tolist())}")
    opt_chain = opt_chain[(opt_chain['strike'] >= lower_bound) & (opt_chain['strike'] <= upper_bound)]
    if opt_chain.empty:
        st.warning(f"No ITM options 10–40% below stock price for expiration {option_exp.date()}")
        continue

    # --- Common calculations ---
    # Yahoo/yfinance frequently returns 0 for bid/ask on illiquid or delayed data.
    # Fall back to lastPrice when both bid and ask are 0 or missing.
    mid_price = (opt_chain['bid'] + opt_chain['ask']) / 2
    opt_chain['Option Price'] = mid_price.where(mid_price > 0, opt_chain['lastPrice'])
    opt_chain['Net Debit'] = stock_price - opt_chain['Option Price']
    opt_chain['Option Premium'] = opt_chain['strike'] + opt_chain['Option Price'] - stock_price
    opt_chain['Open Interest'] = opt_chain['openInterest']

    # --- Days Held ---
    days_held = max((option_exp - today).days, 1)
    opt_chain['Days Held'] = days_held
    opt_chain['Option Expiration'] = option_exp.date()

    # --- Dividend at strike ---
    opt_chain['Dividend at Strike Price'] = (yearly_dividend / opt_chain['strike']) * 100

    # --- Calculate actual dividends during full holding period ---
    # Project future dividend dates based on historical payment pattern
    
    # Get the most recent historical dividend dates to establish the pattern
    recent_div_dates = div_series[div_series.index >= one_year_ago].index
    
    if len(recent_div_dates) >= 2:
        # Calculate average days between dividend payments
        date_diffs = [(recent_div_dates[i] - recent_div_dates[i-1]).days for i in range(1, len(recent_div_dates))]
        avg_days_between = sum(date_diffs) / len(date_diffs)
    else:
        # Fallback to frequency-based estimate
        avg_days_between = 365.25 / div_freq if div_freq > 0 else 365.25
    
    # Project dividend dates forward from the last known dividend
    last_known_div = div_series.index.max()
    projected_div_dates = []
    next_div = last_known_div + pd.Timedelta(days=avg_days_between)
    
    # Project until we're past the option expiration
    while next_div <= option_exp:
        projected_div_dates.append(next_div)
        next_div = next_div + pd.Timedelta(days=avg_days_between)
    
    # Count dividends that fall after purchase date and on or before expiration
    divs_in_period = [d for d in projected_div_dates if d > today and d <= option_exp]
    expected_div_payments = len(divs_in_period)
    
    # Calculate total dividends
    single_dividend = yearly_dividend / div_freq if div_freq > 0 else 0
    divs_during_period = single_dividend * expected_div_payments
    
    # Store debug info for later display
    debug_info = {
        'option_exp': option_exp,
        'last_known_div': last_known_div,
        'avg_days_between': avg_days_between,
        'divs_in_period': divs_in_period,
        'expected_div_payments': expected_div_payments,
        'single_dividend': single_dividend,
        'divs_during_period': divs_during_period
    }

    # --- Scenario: Hold Dividend (hold to expiration, receive all dividends) ---
    hold = pd.DataFrame(index=opt_chain.index)
    hold['Dividend + Premium'] = (opt_chain['Option Premium'].values * shares) + (divs_during_period * shares)
    hold['Total %'] = hold['Dividend + Premium'] / (shares * opt_chain['Net Debit'].values) * 100
    hold['Annualized %'] = hold['Total %'] * (365 / days_held)

    # --- Scenario: Called Early (called 0 days before last dividend) ---
    # For early call, assume called 0 days before the last dividend payment
    if len(divs_in_period) > 0:
        # Get the last dividend date
        last_div_date = divs_in_period[-1]
        
        # Early call happens 0 days before the last dividend
        early_call_date = last_div_date - pd.Timedelta(days=0)
        
        # Count how many dividends occur before the early call date
        early_divs = [d for d in divs_in_period if d < early_call_date]
        expected_payments_early = len(early_divs)
        divs_received_early = single_dividend * expected_payments_early
        
        days_held_early = max((early_call_date - today).days, 1)
    else:
        # No dividends - early call is same as hold scenario
        expected_payments_early = 0
        divs_received_early = 0
        early_call_date = option_exp
        days_held_early = days_held

    # Store early call debug info
    debug_info['early_call'] = {
        'last_div_date': divs_in_period[-1] if len(divs_in_period) > 0 else None,
        'early_call_date': early_call_date,
        'expected_payments_early': expected_payments_early,
        'divs_received_early': divs_received_early,
        'days_held_early': days_held_early
    }

    early = pd.DataFrame(index=opt_chain.index)
    early['Dividend + Premium'] = (opt_chain['Option Premium'].values * shares) + (divs_received_early * shares)
    early['Total %'] = early['Dividend + Premium'] / (shares * opt_chain['Net Debit'].values) * 100
    early['Annualized %'] = early['Total %'] * (365 / days_held_early)

    # --- Premium after one dividend payment (for reference) ---
    opt_chain['Premium - Single Dividend'] = opt_chain['Option Premium'] - single_dividend

    # --- Combine scenarios into final DataFrame ---
    combined = pd.DataFrame({
        'Date Purchased': today.date(),
        'Stock': stock_symbol,
        'Stock Price': stock_price,
        'Forward Dividend $': yearly_dividend,
        'Forward Dividend %': (yearly_dividend / stock_price) * 100,
        'Dividend Frequency': div_freq,
        'Next Dividend Date': next_div_date.date() if next_div_date is not None else None,
        'Option Expiration': option_exp.date(),
        'Strike': opt_chain['strike'],
        'Option Price': opt_chain['Option Price'],
        'Net Debit': opt_chain['Net Debit'],
        'Option Premium': opt_chain['Option Premium'],
        'Open Interest': opt_chain['openInterest'],
        'Premium - Single Dividend': opt_chain['Premium - Single Dividend'],
        'Dividend at Strike Price': opt_chain['Dividend at Strike Price'],
        'Hold Dividend: # of Payments': expected_div_payments,
        'Hold Dividend: Dividend + Premium': hold['Dividend + Premium'],
        'Hold Dividend: Total %': hold['Total %'],
        'Hold Dividend: Annualized %': hold['Annualized %'],
        'Called Early: # of Payments': expected_payments_early,
        'Called Early: Dividend + Premium': early['Dividend + Premium'],
        'Called Early: Total %': early['Total %'],
        'Called Early: Annualized %': early['Annualized %']
    })

    all_options.append(combined)
    all_debug_info.append(debug_info)

# --- Combine all expirations ---
if all_options:
    final_df = pd.concat(all_options, ignore_index=True)
else:
    st.warning("No ITM options 10–40% below stock price found in the 6–18 month window.")
    st.stop()

# --- Add "Meet Criteria" column BEFORE formatting ---
# Criteria: Option Premium > 0, Hold Dividend Total % > 10%, Hold Dividend Annualized % > 10%
final_df['Meet Criteria'] = (
    (final_df['Option Premium'] > 0) & 
    (final_df['Premium - Single Dividend'] > 0) &
    (final_df['Hold Dividend: Total %'] > 10) & 
    (final_df['Hold Dividend: Annualized %'] > 10)
)

# --- Store numeric value for sorting BEFORE formatting ---
final_df['Hold_Total_Numeric'] = final_df['Hold Dividend: Total %']

# --- Round numeric columns for clean display ---
final_df['Strike'] = final_df['Strike'].round(2)
final_df['Stock Price'] = final_df['Stock Price'].round(2)
final_df['Forward Dividend $'] = final_df['Forward Dividend $'].round(2)
final_df['Option Price'] = final_df['Option Price'].round(2)
final_df['Net Debit'] = final_df['Net Debit'].round(2)
final_df['Option Premium'] = final_df['Option Premium'].round(2)
final_df['Premium - Single Dividend'] = final_df['Premium - Single Dividend'].round(2)
final_df['Hold Dividend: Dividend + Premium'] = final_df['Hold Dividend: Dividend + Premium'].round(2)
final_df['Called Early: Dividend + Premium'] = final_df['Called Early: Dividend + Premium'].round(2)

# --- Format % columns ---
pct_cols = [
    'Forward Dividend %',
    'Dividend at Strike Price',
    'Hold Dividend: Total %','Hold Dividend: Annualized %',
    'Called Early: Total %','Called Early: Annualized %'
]
for col in pct_cols:
    final_df[col] = final_df[col].map(lambda x: f"{x:.2f}%")

# --- Apply filter if checkbox is selected ---
if filter_criteria:
    final_df = final_df[final_df['Meet Criteria'] == True].reset_index(drop=True)

# Check if we have any rows left after filtering
if final_df.empty:
    st.warning("No options meet the criteria. Try unchecking the filter.")
    st.stop()

# --- Display columns ---
display_cols = [
    'Meet Criteria','Date Purchased','Stock','Stock Price','Forward Dividend $','Forward Dividend %',
    'Dividend Frequency','Next Dividend Date',
    'Option Expiration','Strike','Option Price','Net Debit','Option Premium','Premium - Single Dividend',
    'Dividend at Strike Price','Open Interest',
    'Hold Dividend: # of Payments','Hold Dividend: Dividend + Premium','Hold Dividend: Total %','Hold Dividend: Annualized %',
    'Called Early: # of Payments','Called Early: Dividend + Premium','Called Early: Total %','Called Early: Annualized %'
]

# --- Highlight top 3 ROI rows ---
top_3_indices = final_df.nlargest(3, 'Hold_Total_Numeric').index

def highlight_top_3_rows(x):
    if x.name == top_3_indices[0]:
        color = 'background-color: lightgreen'
    elif x.name in top_3_indices[1:].tolist():
        color = 'background-color: lightyellow'
    else:
        color = ''
    return [color] * len(x)

st.subheader(f"{stock_symbol} Buy-Write Dashboard (6–18 Months, ITM 10–40% below stock)")
st.dataframe(final_df[display_cols].style.apply(highlight_top_3_rows, axis=1))

# --- Best Overall Option ---
best_option_df = pd.DataFrame([final_df.loc[top_3_indices[0]]])
st.subheader("Best Overall Option (Hold Dividend scenario)")
st.dataframe(best_option_df[display_cols])

# --- Download CSV ---
def get_table_download_link(df, filename="options_data.csv"):
    csv = df.to_csv(index=False)
    b64 = base64.b64encode(csv.encode()).decode()
    href = f'<a href="data:file/csv;base64,{b64}" download="{filename}">Download CSV</a>'
    return href

st.markdown(get_table_download_link(final_df), unsafe_allow_html=True)
st.markdown(get_table_download_link(best_option_df, filename="best_option.csv"), unsafe_allow_html=True)

# --- What-If Scenario Calculator ---
st.subheader("What-If Scenario Calculator")
st.write("Enter custom values to calculate potential returns")

col1, col2, col3, col4 = st.columns(4)
with col1:
    whatif_stock_price = st.number_input("Stock Price", value=float(stock_price), step=0.01, key="whatif_stock")
with col2:
    whatif_strike = st.number_input("Strike Price", value=float(stock_price * 0.75), step=0.01, key="whatif_strike")
with col3:
    whatif_expiration = st.date_input("Expiration Date", value=datetime.today().replace(year=datetime.today().year + 1), key="whatif_exp")
with col4:
    whatif_option_price = st.number_input("Option Price (bid+ask)/2", value=10.0, step=0.01, key="whatif_option")

if st.button("Calculate What-If Scenario"):
    # Calculate fields
    whatif_net_debit = whatif_stock_price - whatif_option_price
    whatif_option_premium = whatif_strike + whatif_option_price - whatif_stock_price
    whatif_premium_minus_div = whatif_option_premium - single_dividend
    
    # Calculate days held
    whatif_exp_ts = pd.Timestamp(whatif_expiration).tz_localize("America/New_York")
    whatif_days_held = max((whatif_exp_ts - today).days, 1)
    
    # Project dividends for what-if scenario
    whatif_divs_in_period = [d for d in projected_div_dates if d > today and d <= whatif_exp_ts]
    whatif_expected_payments = len(whatif_divs_in_period)
    whatif_divs_during_period = single_dividend * whatif_expected_payments
    
    # Hold scenario
    whatif_hold_total = (whatif_option_premium * shares) + (whatif_divs_during_period * shares)
    whatif_hold_pct = (whatif_hold_total / (shares * whatif_net_debit)) * 100
    whatif_hold_ann = whatif_hold_pct * (365 / whatif_days_held)
    
    # Early call scenario
    if len(whatif_divs_in_period) > 0:
        whatif_last_div = whatif_divs_in_period[-1]
        whatif_early_call = whatif_last_div - pd.Timedelta(days=0)
        whatif_early_divs = [d for d in whatif_divs_in_period if d < whatif_early_call]
        whatif_early_payments = len(whatif_early_divs)
        whatif_divs_early = single_dividend * whatif_early_payments
        whatif_days_early = max((whatif_early_call - today).days, 1)
    else:
        whatif_early_payments = 0
        whatif_divs_early = 0
        whatif_days_early = whatif_days_held
    
    whatif_early_total = (whatif_option_premium * shares) + (whatif_divs_early * shares)
    whatif_early_pct = (whatif_early_total / (shares * whatif_net_debit)) * 100
    whatif_early_ann = whatif_early_pct * (365 / whatif_days_early)
    
    # Check criteria
    whatif_meets_criteria = (whatif_option_premium > 0) and (whatif_hold_pct > 10) and (whatif_hold_ann > 10)
    
    # Format percentages
    fwd_div_pct = f"{(yearly_dividend / whatif_stock_price) * 100:.2f}%"
    div_at_strike_pct = f"{(yearly_dividend / whatif_strike) * 100:.2f}%"
    hold_total_pct = f"{whatif_hold_pct:.2f}%"
    hold_ann_pct = f"{whatif_hold_ann:.2f}%"
    early_total_pct = f"{whatif_early_pct:.2f}%"
    early_ann_pct = f"{whatif_early_ann:.2f}%"
    
    # Create results dataframe
    whatif_results = pd.DataFrame({
        'Meet Criteria': [whatif_meets_criteria],
        'Date Purchased': [today.date()],
        'Stock': [stock_symbol],
        'Stock Price': [whatif_stock_price],
        'Forward Dividend $': [yearly_dividend],
        'Forward Dividend %': [fwd_div_pct],
        'Dividend Frequency': [div_freq],
        'Next Dividend Date': [next_div_date.date() if next_div_date is not None else None],
        'Option Expiration': [whatif_expiration],
        'Strike': [whatif_strike],
        'Option Price': [whatif_option_price],
        'Net Debit': [whatif_net_debit],
        'Option Premium': [whatif_option_premium],
        'Open Interest': ['N/A'],
        'Premium - Single Dividend': [whatif_premium_minus_div],
        'Dividend at Strike Price': [div_at_strike_pct],
        'Hold Dividend: # of Payments': [whatif_expected_payments],
        'Hold Dividend: Dividend + Premium': [whatif_hold_total],
        'Hold Dividend: Total %': [hold_total_pct],
        'Hold Dividend: Annualized %': [hold_ann_pct],
        'Called Early: # of Payments': [whatif_early_payments],
        'Called Early: Dividend + Premium': [whatif_early_total],
        'Called Early: Total %': [early_total_pct],
        'Called Early: Annualized %': [early_ann_pct]
    })
    
    st.dataframe(whatif_results[display_cols])
    st.markdown(get_table_download_link(whatif_results, filename="whatif_scenario.csv"), unsafe_allow_html=True)

# --- Debug Information ---
st.subheader("Debug Information")
for debug_info in all_debug_info:
    st.write(f"=== Expiration {debug_info['option_exp'].date()} ===")
    st.write(f"Last known dividend: {debug_info['last_known_div'].date()}")
    st.write(f"Avg days between dividends: {debug_info['avg_days_between']:.1f}")
    st.write(f"Projected dividend dates in holding period:")
    for d in debug_info['divs_in_period']:
        st.write(f"  - {d.date()}")
    st.write(f"Number of dividend payments: {debug_info['expected_div_payments']}")
    st.write(f"Single dividend: ${debug_info['single_dividend']:.4f}")
    st.write(f"Total dividends: ${debug_info['divs_during_period']:.4f}")
    st.write(f"")
    st.write(f"Early call scenario:")
    if debug_info['early_call']['last_div_date']:
        st.write(f"  Last dividend date: {debug_info['early_call']['last_div_date'].date()}")
    else:
        st.write(f"  Last dividend date: N/A")
    st.write(f"  Early call date (0 days before last div): {debug_info['early_call']['early_call_date'].date()}")
    st.write(f"  Dividends received if called early: {debug_info['early_call']['expected_payments_early']}")
    st.write(f"  Total dividends: ${debug_info['early_call']['divs_received_early']:.4f}")
    st.write(f"  Days held: {debug_info['early_call']['days_held_early']}")
    st.write(f"===")
    st.write("")
