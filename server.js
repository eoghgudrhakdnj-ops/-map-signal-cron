import http from "node:http";

const PORT = Number(process.env.PORT || 10000);
const SITE_SIGNAL_URL = process.env.SITE_SIGNAL_URL || "https://map-signal-center.eoghgudrhakdnj.chatgpt.site/api/external-signals";
const SECRET = process.env.BACKGROUND_SCAN_SECRET || "";
const HOSTS = ["https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com", "https://fapi3.binance.com", "https://fapi4.binance.com"];
const EXCLUDED = new Set(["BTC", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD"]);
const INTERVAL_MS = 60_000;
const POSITION_SYMBOLS = (process.env.POSITION_SYMBOLS || "").split(",").map(value => value.trim().toUpperCase()).filter(value => /^[A-Z0-9]{2,12}$/.test(value)).slice(0, 5);
const POSITION_MARKET = (process.env.POSITION_MARKET || "FUTURES").toUpperCase() === "SPOT" ? "현물" : "선물";
let stopping = false, scanning = false, timer;
let state = { status: "starting", lastScanAt: null, scanned: 0, eligible: 0, delivered: 0, top30: [], signals: [], positionActions: [], error: null };

const average = values => values.reduce((sum, value) => sum + value, 0) / Math.max(values.length, 1);
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
function ema(values, period) { const factor = 2 / (period + 1); let value = values[0]; for (let index = 1; index < values.length; index++) value = values[index] * factor + value * (1 - factor); return value; }
function rsi(values, period = 14) { let gain = 0, loss = 0; for (let index = values.length - period; index < values.length; index++) { const change = values[index] - values[index - 1]; if (change >= 0) gain += change; else loss -= change; } return loss === 0 ? 100 : 100 - 100 / (1 + gain / loss); }
function bandWidth(values) { const sample = values.slice(-20), middle = average(sample), deviation = Math.sqrt(average(sample.map(value => (value - middle) ** 2))); return middle ? deviation * 4 / middle : 0; }

function score(rows) {
  if (!Array.isArray(rows) || rows.length < 55) return null;
  const closes = rows.map(row => +row[4]), analysisCloses = closes.slice(0, -1), closed = rows.at(-2), prior = rows.at(-3), current = +closed[4];
  const e20 = ema(analysisCloses, 20), e50 = ema(analysisCloses, 50), momentum = rsi(analysisCloses), volume = +closed[7], volumeAverage = average(rows.slice(-22, -2).map(row => +row[7])), volumeRatio = volume / (volumeAverage || 1);
  const recentTrades = rows.slice(-5, -2), buyVolume = recentTrades.reduce((sum, row) => sum + (+row[9] || 0), 0), totalVolume = recentTrades.reduce((sum, row) => sum + (+row[5] || 0), 0), sellVolume = Math.max(0, totalVolume - buyVolume), executionStrength = Math.round(clamp(buyVolume / (sellVolume || 1) * 100, 0, 999));
  const candleUp = +closed[4] >= +closed[1], change = ((+closed[4] - +prior[4]) / (+prior[4] || 1)) * 100, currentWidth = bandWidth(analysisCloses), priorWidths = Array.from({ length: 20 }, (_, offset) => bandWidth(analysisCloses.slice(0, analysisCloses.length - offset - 1))), squeezeRatio = currentWidth / (average(priorWidths) || currentWidth || 1);
  let strength = 50; strength += e20 > e50 ? 18 : -18; strength += current > e20 ? 12 : -12; strength += momentum >= 55 && momentum <= 75 ? 12 : momentum < 45 ? -10 : 0; if (volumeRatio > 1.3) strength += candleUp ? 10 : -10; strength += clamp(change * 5, -8, 8); if (squeezeRatio < .95) strength += candleUp ? 4 : -4;
  strength = clamp(Math.round(strength), 0, 100); const signal = strength >= 64 ? "롱" : strength <= 36 ? "숏" : "관망", confidence = Math.round(50 + Math.abs(strength - 50));
  return { current, signal, confidence, executionStrength, change, rsi: Math.round(momentum), emaTrend: e20 > e50 ? "상승" : "하락" };
}

async function api(path) {
  const attempts = HOSTS.map(async host => { const response = await fetch(`${host}${path}`, { signal: AbortSignal.timeout(7000), headers: { "User-Agent": "Dopamine-Hunter/2.0" } }); if (!response.ok) throw new Error(`binance-${response.status}`); return response.json(); });
  try { return await Promise.any(attempts); } catch { throw new Error(`binance-unavailable:${path.split("?")[0]}`); }
}

async function mapLimit(items, limit, worker) {
  const results = new Array(items.length); let cursor = 0;
  async function run() { while (cursor < items.length && !stopping) { const index = cursor++; try { results[index] = await worker(items[index]); } catch { results[index] = null; } } }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run)); return results.filter(Boolean);
}

async function topThirty() {
  const [info, tickers] = await Promise.all([api("/fapi/v1/exchangeInfo"), api("/fapi/v1/ticker/24hr")]), tickerMap = new Map(tickers.map(item => [item.symbol, item]));
  const candidates = (info.symbols || []).filter(item => item.quoteAsset === "USDT" && item.status === "TRADING" && item.contractType === "PERPETUAL" && /^[A-Z0-9]{2,12}$/.test(item.baseAsset) && !EXCLUDED.has(item.baseAsset) && !/(UP|DOWN|BULL|BEAR)$/.test(item.baseAsset)).map(item => ({ symbol: item.baseAsset, dayVolume: +(tickerMap.get(item.symbol)?.quoteVolume || 0) })).sort((a, b) => b.dayVolume - a.dayVolume).slice(0, 60);
  const ranked = await mapLimit(candidates, 12, async item => { const rows5m = await api(`/fapi/v1/klines?symbol=${item.symbol}USDT&interval=5m&limit=60`), liveVolume = rows5m.slice(-4, -1).reduce((sum, row) => sum + (+row[7] || 0), 0); return { ...item, rows5m, liveVolume }; });
  return ranked.sort((a, b) => b.liveVolume - a.liveVolume).slice(0, 30);
}

async function findHundredPointSurges(top30) {
  const analyses = await mapLimit(top30, 8, async item => { const [rows15m, rows30m, rows1h] = await Promise.all(["15m", "30m", "1h"].map(interval => api(`/fapi/v1/klines?symbol=${item.symbol}USDT&interval=${interval}&limit=60`))), five = score(item.rows5m), fifteen = score(rows15m), thirty = score(rows30m), hour = score(rows1h); if (!five || !fifteen || !thirty || !hour || hour.signal !== "롱" || ![five, fifteen, thirty].every(result => result.signal === hour.signal) || ![five, fifteen, thirty, hour].every(result => result.confidence === 100)) return null; return { symbol: item.symbol, current: five.current, score5m: five.confidence, score15m: fifteen.confidence, score30m: thirty.confidence, score1h: hour.confidence, executionStrength: five.executionStrength, change5m: five.change, liveVolume: item.liveVolume }; });
  return analyses.sort((a, b) => b.executionStrength - a.executionStrength || b.liveVolume - a.liveVolume).slice(0, 5).map(({ liveVolume, ...result }) => result);
}

async function findPositionActions() {
  return mapLimit(POSITION_SYMBOLS, 3, async symbol => {
    const [rows4h, rows1d] = await Promise.all([
      api(`/fapi/v1/klines?symbol=${symbol}USDT&interval=4h&limit=60`),
      api(`/fapi/v1/klines?symbol=${symbol}USDT&interval=1d&limit=60`),
    ]);
    const four = score(rows4h), day = score(rows1d);
    if (!four || !day) return null;
    const alignedLong = four.signal === "롱" && day.signal === "롱";
    const alignedShort = four.signal === "숏" && day.signal === "숏";
    const action = alignedLong ? "보유" : alignedShort ? "손절" : "관망";
    const averaging = POSITION_MARKET === "선물" ? "물타기 금지" : alignedLong && four.rsi < 70 && day.rsi < 70 && four.emaTrend === "상승" && day.emaTrend === "상승" ? "소액 분할 검토" : alignedLong ? "추가매수 대기" : "물타기 금지";
    return { symbol, market: POSITION_MARKET, current: four.current, action, averaging, signal4h: four.signal, signal1d: day.signal, score4h: four.confidence, score1d: day.confidence, rsi4h: four.rsi, rsi1d: day.rsi };
  });
}

async function deliver(trend100, positionActions) {
  if (!SECRET) throw new Error("BACKGROUND_SCAN_SECRET-missing");
  const response = await fetch(SITE_SIGNAL_URL, { method: "POST", headers: { "Authorization": `Bearer ${SECRET}`, "Content-Type": "application/json" }, body: JSON.stringify({ trend100, positionActions }), signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`delivery-${response.status}`); return response.json();
}

async function scan() {
  if (scanning || stopping) return; scanning = true;
  try { const [top30, positionActions] = await Promise.all([topThirty(), findPositionActions()]), trend100 = await findHundredPointSurges(top30), delivery = await deliver(trend100, positionActions); state = { status: "ok", lastScanAt: new Date().toISOString(), scanned: top30.length, eligible: trend100.length, delivered: Number(delivery.sent || 0), top30: top30.map(item => item.symbol), signals: trend100.map(item => item.symbol), positionActions: positionActions.map(item => `${item.symbol}:${item.action}`), error: null }; console.log(JSON.stringify({ event: "hour-trend-100-scan", ...state })); }
  catch (error) { state = { ...state, status: "error", lastScanAt: new Date().toISOString(), error: error instanceof Error ? error.message : "unknown" }; console.error(JSON.stringify({ event: "scan-error", ...state })); }
  finally { scanning = false; }
}

const server = http.createServer((request, response) => { if (request.url === "/health" || request.url === "/") { response.writeHead(state.status === "error" ? 503 : 200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" }); response.end(JSON.stringify(state)); return; } response.writeHead(404); response.end("Not found"); });
server.listen(PORT, "0.0.0.0", () => { console.log(`Dopamine Hunter worker listening on ${PORT}`); scan(); timer = setInterval(scan, INTERVAL_MS); });
async function shutdown() { stopping = true; clearInterval(timer); server.close(); const deadline = Date.now() + 25_000; while (scanning && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 250)); process.exit(0); }
process.on("SIGTERM", shutdown); process.on("SIGINT", shutdown);
