import time
import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone


# ==========================================================
# CONFIGURACIÓN DE LA ESTRATEGIA
# ==========================================================

TP_PCT = 0.03        # Take Profit 3%
SL_PCT = 0.0075      # Stop Loss 0.75%

EMA_FAST = 10
EMA_CONFIRM = 20
EMA_MID = 60
EMA_TREND = 180

LOOKBACK_CROSSES = 10
CANDLES_LIMIT = 300  # OKX suele trabajar bien con 300 velas recientes

REQUEST_SLEEP = 0.25

# Telegram se lee desde variables de entorno.
# NO pongas tu token directo en este archivo.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Archivo para evitar alertas repetidas
SENT_ALERTS_FILE = "sent_alerts.json"


# ==========================================================
# MONEDAS Y TEMPORALIDADES A REVISAR
# ==========================================================

WATCHLIST = [
    {"priority": 1, "symbol": "BTCUSDT", "interval": "4h", "level": "Principal"},
    {"priority": 2, "symbol": "ETHUSDT", "interval": "4h", "level": "Principal"},
    {"priority": 3, "symbol": "XRPUSDT", "interval": "4h", "level": "Principal"},
    {"priority": 4, "symbol": "ADAUSDT", "interval": "4h", "level": "Principal"},
    {"priority": 5, "symbol": "SOLUSDT", "interval": "4h", "level": "Principal"},
    {"priority": 6, "symbol": "BNBUSDT", "interval": "4h", "level": "Principal"},

    {"priority": 7, "symbol": "XRPUSDT", "interval": "30m", "level": "Secundario agresivo"},
    {"priority": 8, "symbol": "SOLUSDT", "interval": "30m", "level": "Secundario agresivo"},

    {"priority": 9, "symbol": "ETHUSDT", "interval": "15m", "level": "Secundario"},
    {"priority": 10, "symbol": "XRPUSDT", "interval": "15m", "level": "Secundario"},
    {"priority": 11, "symbol": "BNBUSDT", "interval": "15m", "level": "Secundario"},
]


# ==========================================================
# REQUESTS GENERALES
# ==========================================================

def request_with_retries(url: str, params: dict, retries: int = 3, sleep_seconds: float = 1.5):
    last_error = None

    headers = {
        "User-Agent": "Mozilla/5.0 scanner-telegram-crypto/1.0"
    }

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=20)

            if response.status_code != 200:
                raise Exception(f"Status code {response.status_code}: {response.text}")

            return response.json()

        except Exception as error:
            last_error = error
            print(f"Error request intento {attempt}/{retries}: {error}")
            time.sleep(sleep_seconds)

    raise Exception(f"No se pudo completar request. Error final: {last_error}")


# ==========================================================
# REQUESTS A OKX
# ==========================================================

OKX_BASE_URL = "https://www.okx.com"

INTERVAL_MAP_OKX = {
    "15m": "15m",
    "30m": "30m",
    "4h": "4H",
}


def symbol_to_okx_inst_id(symbol: str) -> str:
    """
    Convierte BTCUSDT -> BTC-USDT-SWAP.
    Usamos swaps USDT de OKX como referencia de mercado.
    """
    if not symbol.endswith("USDT"):
        raise ValueError(f"Símbolo no soportado: {symbol}")

    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"


def interval_to_milliseconds(interval: str) -> int:
    if interval == "15m":
        return 15 * 60 * 1000

    if interval == "30m":
        return 30 * 60 * 1000

    if interval == "4h":
        return 4 * 60 * 60 * 1000

    raise ValueError(f"Intervalo no soportado: {interval}")


def fetch_klines(symbol: str, interval: str, limit: int = CANDLES_LIMIT) -> pd.DataFrame:
    inst_id = symbol_to_okx_inst_id(symbol)
    bar = INTERVAL_MAP_OKX.get(interval)

    if not bar:
        raise ValueError(f"Intervalo no soportado para OKX: {interval}")

    url = f"{OKX_BASE_URL}/api/v5/market/candles"

    params = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    }

    data = request_with_retries(url, params)

    if data.get("code") != "0":
        raise Exception(f"OKX error {data.get('code')}: {data.get('msg')}")

    candles = data.get("data", [])

    if not candles:
        raise Exception(f"OKX no devolvió velas para {inst_id} {interval}")

    interval_ms = interval_to_milliseconds(interval)
    rows = []

    for item in candles:
        # OKX: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        open_time_ms = int(item[0])
        confirm = int(item[8]) if len(item) > 8 and str(item[8]).isdigit() else 1

        rows.append({
            "open_time": open_time_ms,
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "close_time": open_time_ms + interval_ms - 1,
            "confirm": confirm,
        })

    df = pd.DataFrame(rows)

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    # OKX devuelve primero las velas más recientes; ordenamos de antigua a reciente.
    df = df.sort_values(by="open_time").reset_index(drop=True)
    df = df.dropna().reset_index(drop=True)

    return df


def fetch_current_price(symbol: str) -> float:
    inst_id = symbol_to_okx_inst_id(symbol)
    url = f"{OKX_BASE_URL}/api/v5/market/ticker"

    params = {
        "instId": inst_id,
    }

    data = request_with_retries(url, params)

    if data.get("code") != "0":
        raise Exception(f"OKX error {data.get('code')}: {data.get('msg')}")

    ticker_list = data.get("data", [])

    if not ticker_list:
        raise Exception(f"No se encontró ticker para {inst_id}")

    return float(ticker_list[0]["last"])


# ==========================================================
# FUNCIONES AUXILIARES
# ==========================================================

def remove_open_candle(df: pd.DataFrame) -> pd.DataFrame:
    """
    Elimina la vela actual si todavía está abierta.
    Así evitamos señales falsas.
    """

    if df.empty:
        return df

    # OKX incluye confirm=0 cuando la vela aún no está cerrada.
    if "confirm" in df.columns and int(df["confirm"].iloc[-1]) == 0:
        df = df.iloc[:-1].copy()
        return df.reset_index(drop=True)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    last_close_time_ms = int(df["close_time"].iloc[-1].timestamp() * 1000)

    if last_close_time_ms > now_ms:
        df = df.iloc[:-1].copy()

    return df.reset_index(drop=True)


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for period in [EMA_FAST, EMA_CONFIRM, EMA_MID, EMA_TREND]:
        df[f"ema{period}"] = df["close"].ewm(span=period, adjust=False).mean()

    return df


def cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    """
    Detecta cuando el precio cruza hacia abajo una EMA.
    """
    return (a.shift(1) >= b.shift(1)) & (a < b)


def format_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    elif value >= 1:
        return f"{value:.4f}"
    else:
        return f"{value:.6f}"


# ==========================================================
# EVALUACIÓN DE SEÑAL SHORT
# ==========================================================

def evaluate_short_signal(symbol: str, interval: str) -> dict:
    df = fetch_klines(symbol, interval)
    df = remove_open_candle(df)

    if len(df) < EMA_TREND:
        raise Exception(f"No hay suficientes velas cerradas para calcular EMA{EMA_TREND}")

    df = add_emas(df)

    current_price = fetch_current_price(symbol)

    last = df.iloc[-1]

    ema10 = df["ema10"]

    price_cross_down_ema10 = cross_down(df["close"], ema10)
    recent_down_crosses = int(price_cross_down_ema10.rolling(LOOKBACK_CROSSES).sum().iloc[-1])

    # Condiciones para señal SHORT
    condition_1 = last["close"] < last["ema180"]
    condition_2 = last["ema20"] < last["ema60"]
    condition_3 = last["ema60"] < last["ema180"]
    condition_4 = recent_down_crosses >= 2
    condition_5 = last["close"] < last["ema20"]

    signal = (
        condition_1 and
        condition_2 and
        condition_3 and
        condition_4 and
        condition_5
    )

    entry_price = current_price
    take_profit = entry_price * (1 - TP_PCT)
    stop_loss = entry_price * (1 + SL_PCT)

    if signal:
        status = "SEÑAL SHORT"
    else:
        status = "SIN SEÑAL"

    return {
        "symbol": symbol,
        "interval": interval,
        "status": status,

        "last_closed_candle": str(last["close_time"]),
        "current_price": format_price(current_price),
        "last_close": format_price(last["close"]),

        "ema10": format_price(last["ema10"]),
        "ema20": format_price(last["ema20"]),
        "ema60": format_price(last["ema60"]),
        "ema180": format_price(last["ema180"]),

        "close_below_ema180": "OK" if condition_1 else "NO",
        "ema20_below_ema60": "OK" if condition_2 else "NO",
        "ema60_below_ema180": "OK" if condition_3 else "NO",
        "crosses_down_ema10_last10": recent_down_crosses,
        "close_below_ema20": "OK" if condition_5 else "NO",

        "entry_short_now": format_price(entry_price) if signal else "",
        "take_profit_3pct": format_price(take_profit) if signal else "",
        "stop_loss_075pct": format_price(stop_loss) if signal else "",
    }


# ==========================================================
# RESUMEN SIMPLE PARA TERMINAL
# ==========================================================

def print_simple_summary(df_results: pd.DataFrame):
    error_df = df_results[df_results["status"].astype(str).str.startswith("ERROR")].copy()
    signals_df = df_results[df_results["status"] == "SEÑAL SHORT"].copy()
    signals_df = signals_df.sort_values(by="priority")

    print("\n======================================================")
    print(" RESUMEN SIMPLE DEL SCANNER")
    print("======================================================\n")

    if not error_df.empty:
        print("PARES CON ERROR DE DATOS:")
        for _, row in error_df.iterrows():
            print(f"- {row['symbol']} {row['interval']}: {row['status']}")
        print("")

    if signals_df.empty:
        print("No hay señales SHORT activas.")
        print("Lectura: mejor esperar. Ninguna moneda cumple toda la estrategia.")
        print("\nScanner terminado.")
        return

    best_signal = signals_df.iloc[0]

    print("Hay señales SHORT activas.\n")

    principal_signals = signals_df[signals_df["level"] == "Principal"]

    if principal_signals.empty:
        print("No hay señal principal en 4H.")
        print("Las señales actuales son de 15m o 30m.\n")
    else:
        print("Sí hay señal principal en 4H.\n")

    print(f"La mejor por prioridad es {best_signal['symbol']} {best_signal['interval']}.")
    print(f"Entrada: {best_signal['entry_short_now']}")
    print(f"TP: {best_signal['take_profit_3pct']}")
    print(f"SL: {best_signal['stop_loss_075pct']}")

    other_signals = signals_df.iloc[1:]

    if not other_signals.empty:
        print("\nOTRAS SEÑALES:")

        for _, row in other_signals.iterrows():
            print(
                f"- {row['symbol']} {row['interval']} | "
                f"Entrada: {row['entry_short_now']} | "
                f"TP: {row['take_profit_3pct']} | "
                f"SL: {row['stop_loss_075pct']}"
            )

    print("\nScanner terminado.")


# ==========================================================
# ALERTAS TELEGRAM
# ==========================================================

def load_sent_alerts() -> set:
    if not os.path.exists(SENT_ALERTS_FILE):
        return set()

    try:
        with open(SENT_ALERTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return set(data)
    except Exception:
        return set()


def save_sent_alerts(sent_alerts: set):
    with open(SENT_ALERTS_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(list(sent_alerts)), file, indent=2, ensure_ascii=False)


def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, data=payload, timeout=20)
        response.raise_for_status()
        print("Alerta enviada a Telegram.")
        return True
    except Exception as error:
        print(f"Error enviando alerta a Telegram: {error}")
        return False


def send_signals_to_telegram(df_results: pd.DataFrame):
    signals_df = df_results[df_results["status"] == "SEÑAL SHORT"].copy()
    signals_df = signals_df.sort_values(by="priority")

    if signals_df.empty:
        print("No hay señales para enviar a Telegram.")
        return

    sent_alerts = load_sent_alerts()
    new_alerts_count = 0

    for _, row in signals_df.iterrows():
        alert_key = f"{row['symbol']}_{row['interval']}_{row['last_closed_candle']}"

        if alert_key in sent_alerts:
            print(f"Alerta repetida, no se envía otra vez: {alert_key}")
            continue

        message = f"""
🚨 <b>SEÑAL SHORT CONFIRMADA</b>

<b>Par:</b> {row['symbol']}
<b>Temporalidad:</b> {row['interval']}
<b>Nivel:</b> {row['level']}
<b>Fuente:</b> OKX USDT-SWAP

<b>Entrada:</b> {row['entry_short_now']}
<b>TP 3%:</b> {row['take_profit_3pct']}
<b>SL 0.75%:</b> {row['stop_loss_075pct']}

<b>Última vela cerrada:</b> {row['last_closed_candle']}

✅ Close &lt; EMA180
✅ EMA20 &lt; EMA60
✅ EMA60 &lt; EMA180
✅ Cruces EMA10: {row['crosses_down_ema10_last10']}
✅ Close &lt; EMA20

⚠️ Alerta informativa. No abre operaciones automáticamente.
"""

        sent = send_telegram_message(message.strip())

        if sent:
            sent_alerts.add(alert_key)
            new_alerts_count += 1

    save_sent_alerts(sent_alerts)

    print(f"Alertas nuevas enviadas: {new_alerts_count}")


# ==========================================================
# MAIN
# ==========================================================

def main():
    print("\n======================================================")
    print(" SCANNER SHORT ONLY - EMA")
    print("======================================================")
    print("Analizando mercado...")
    print("Este scanner no abre operaciones reales.")
    print("Fuente de datos: OKX USDT-SWAP\n")

    results = []

    for item in WATCHLIST:
        priority = item["priority"]
        symbol = item["symbol"]
        interval = item["interval"]
        level = item["level"]

        try:
            result = evaluate_short_signal(symbol, interval)

            result["priority"] = priority
            result["level"] = level

            results.append(result)

            time.sleep(REQUEST_SLEEP)

        except Exception as error:
            results.append({
                "priority": priority,
                "symbol": symbol,
                "interval": interval,
                "level": level,
                "status": f"ERROR: {error}",
                "last_closed_candle": "",
                "current_price": "",
                "last_close": "",
                "ema10": "",
                "ema20": "",
                "ema60": "",
                "ema180": "",
                "close_below_ema180": "",
                "ema20_below_ema60": "",
                "ema60_below_ema180": "",
                "crosses_down_ema10_last10": "",
                "close_below_ema20": "",
                "entry_short_now": "",
                "take_profit_3pct": "",
                "stop_loss_075pct": "",
            })

    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values(by="priority")

    print_simple_summary(df_results)
    send_signals_to_telegram(df_results)


if __name__ == "__main__":
    main()
