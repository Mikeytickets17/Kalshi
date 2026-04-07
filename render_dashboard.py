"""Render the multi-strategy dashboard as a PNG."""
from PIL import Image, ImageDraw, ImageFont
import random, time

random.seed(42)

W, H = 1400, 2600
BG = (10, 14, 23); CARD = (17, 24, 39); BORDER = (30, 41, 59)
TEXT = (226, 232, 240); MUTED = (100, 116, 139)
GREEN = (34, 197, 94); RED = (239, 68, 68); BLUE = (59, 130, 246)
AMBER = (245, 158, 11); PURPLE = (168, 85, 247); CYAN = (6, 182, 212)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

try:
    fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    fl = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
    fxl = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    fxs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
except:
    fb=fl=fxl=fs=fxs=fm=ImageFont.load_default()

def box(x,y,w,h): d.rounded_rectangle([x,y,x+w,y+h], radius=10, fill=CARD, outline=BORDER)
def fmt(n): return f"+${n:.2f}" if n>=0 else f"-${abs(n):.2f}"
def pnl_c(n): return GREEN if n>=0 else RED

# ═══ HEADER ═══
d.rectangle([0,0,W,55], fill=(17,24,39)); d.line([0,55,W,55], fill=BORDER)
d.text((24,14), "MULTI-STRATEGY TRADING BOT", fill=CYAN, font=fb)
d.text((380,14), "Kalshi + Binance + Alpaca", fill=MUTED, font=fs)
for i,(txt,col) in enumerate([("PAPER",AMBER),("NJ-LEGAL",GREEN),("4 STRATEGIES",PURPLE)]):
    x = W-350+i*110
    d.rounded_rectangle([x,14,x+95,38], radius=10, fill=(col[0]//6,col[1]//6,col[2]//6), outline=col)
    d.text((x+8,18), txt, fill=col, font=fxs)

y = 70

# ═══ STATS ROW ═══
stats = [
    ("PORTFOLIO", "$12,847.33", "+$2,847 (28.5%)", GREEN),
    ("WIN RATE", "87.3%", "142W / 21L", BLUE),
    ("TODAY", "+$184.52", "12 trades", GREEN),
    ("ACTIVE", "7 positions", "3 venues", CYAN),
    ("DRAWDOWN", "4.2%", "Peak: $13,410", GREEN),
]
cw = (W-48-64)//5
for i,(lab,val,sub,col) in enumerate(stats):
    x = 24+i*(cw+16)
    box(x,y,cw,90)
    d.text((x+12,y+10), lab, fill=MUTED, font=fxs)
    d.text((x+12,y+28), val, fill=col, font=fl)
    d.text((x+12,y+62), sub, fill=MUTED, font=fxs)

y += 110

# ═══ CEX PRICE FEEDS ═══
box(24,y,W-48,80)
d.text((40,y+10), "CEX PRICE FEEDS", fill=TEXT, font=fm)
d.text((40,y+34), "BTC", fill=TEXT, font=fb)
d.text((90,y+36), "$69,247.83", fill=GREEN, font=fm)
d.text((210,y+36), "Binance: $69,248 (0.1s)  Coinbase: $69,247 (0.2s)  Spread: 0.001%", fill=MUTED, font=fxs)
d.text((40,y+54), "ETH", fill=TEXT, font=fb)
d.text((90,y+56), "$3,482.41", fill=GREEN, font=fm)
d.text((210,y+56), "Binance: $3,482 (0.1s)  Coinbase: $3,483 (0.3s)  Spread: 0.003%", fill=MUTED, font=fxs)

y += 100

# ═══ ACTIVE POSITIONS ═══
pw = (W-48-16)//2
box(24,y,pw,320)
d.text((40,y+10), "ACTIVE POSITIONS (7)", fill=TEXT, font=fm)

positions = [
    ("ARB", "YES", "BTC-UP-69200-15m", "Kalshi", 0.55, "$320", "+$18.40", GREEN),
    ("ARB", "NO", "ETH-UP-3490-30m", "Kalshi", 0.62, "$280", "+$12.80", GREEN),
    ("TRUMP", "BUY", "BTC SPOT", "Binance", 68500, "$540", "+$42.18", GREEN),
    ("TRUMP", "YES", "BTC-RESERVE", "Kalshi", 0.27, "$288", "+$98.40", GREEN),
    ("TRUMP", "YES", "TARIFF-CHINA", "Kalshi", 0.45, "$200", "+$89.00", GREEN),
    ("NEWS", "BUY", "SPY", "Alpaca", 521, "$400", "-$8.20", RED),
    ("NEWS", "LONG", "BTC 3x", "Binance", 69100, "$600", "+$31.50", GREEN),
]
cols_x = [40, 95, 140, 310, 390, 445, 510]
d.text((cols_x[0],y+36), "TYPE", fill=MUTED, font=fxs)
d.text((cols_x[1],y+36), "SIDE", fill=MUTED, font=fxs)
d.text((cols_x[2],y+36), "ASSET", fill=MUTED, font=fxs)
d.text((cols_x[3],y+36), "VENUE", fill=MUTED, font=fxs)
d.text((cols_x[4],y+36), "ENTRY", fill=MUTED, font=fxs)
d.text((cols_x[5],y+36), "SIZE", fill=MUTED, font=fxs)
d.text((cols_x[6],y+36), "P&L", fill=MUTED, font=fxs)
d.line([40,y+52,24+pw-16,y+52], fill=BORDER)

for i,(typ,side,asset,venue,entry,size,pnl,col) in enumerate(positions):
    py = y+58+i*36
    tc = PURPLE if typ=="ARB" else (AMBER if typ=="TRUMP" else CYAN)
    d.text((cols_x[0],py), typ, fill=tc, font=fxs)
    sc = GREEN if side in ("YES","BUY","LONG") else RED
    d.text((cols_x[1],py), side, fill=sc, font=fxs)
    d.text((cols_x[2],py), asset[:18], fill=TEXT, font=fxs)
    d.text((cols_x[3],py), venue, fill=MUTED, font=fxs)
    d.text((cols_x[4],py), str(entry), fill=TEXT, font=fxs)
    d.text((cols_x[5],py), size, fill=TEXT, font=fxs)
    d.text((cols_x[6],py), pnl, fill=col, font=fxs)

# ═══ NEWS FEED ═══
rx = 24+pw+16
box(rx,y,pw,320)
d.text((rx+16,y+10), "LIVE NEWS FEED", fill=TEXT, font=fm)
d.ellipse([rx+pw-30,y+14,rx+pw-22,y+22], fill=GREEN)  # live dot

news = [
    ("13:10:57", "CRITICAL", "Trump: Strategic Bitcoin Reserve EO signed", AMBER),
    ("13:10:42", "HIGH", "Bitcoin surges past $75,000 on institutions", GREEN),
    ("13:09:15", "CRITICAL", "Fed holds rates, signals cut in June", CYAN),
    ("13:08:30", "HIGH", "CPI comes in at 2.1%, below 2.5% est", GREEN),
    ("13:06:44", "HIGH", "NVIDIA beats earnings, up 8% after hours", GREEN),
    ("13:05:12", "HIGH", "China retaliates with 45% tariffs on US", RED),
    ("13:03:30", "CRITICAL", "Trump: 60% tariffs on China immediately", RED),
    ("12:58:00", "HIGH", "S&P 500 hits all-time high on earnings", GREEN),
]
for i,(t,pri,text,col) in enumerate(news):
    ny = y+38+i*34
    d.text((rx+16,ny), t, fill=MUTED, font=fxs)
    pc = RED if pri=="CRITICAL" else AMBER
    d.rounded_rectangle([rx+75,ny-2,rx+75+60,ny+14], radius=4, fill=(pc[0]//6,pc[1]//6,pc[2]//6))
    d.text((rx+79,ny), pri[:4], fill=pc, font=fxs)
    d.text((rx+142,ny), text[:38], fill=col, font=fxs)

y += 340

# ═══ TRUMP MONITOR ═══
box(24,y,W-48,180)
d.text((40,y+10), "TRUMP TRUTH SOCIAL MONITOR", fill=AMBER, font=fm)
d.ellipse([380,y+14,388,y+22], fill=GREEN)
d.text((394,y+12), "Polling every 3s", fill=MUTED, font=fxs)

# Latest post
d.rounded_rectangle([40,y+36,W-64,y+100], radius=8, fill=(30,28,20), outline=AMBER)
d.text((56,y+42), "LATEST POST (2 min ago)", fill=AMBER, font=fxs)
d.text((56,y+58), '"I am hereby ordering the establishment of a Strategic Bitcoin Reserve.', fill=TEXT, font=fs)
d.text((56,y+74), 'America will be the crypto superpower!"', fill=TEXT, font=fs)

# Analysis result
d.text((40,y+110), "Sentiment:", fill=MUTED, font=fxs)
d.text((120,y+110), "BULLISH", fill=GREEN, font=fb)
d.text((230,y+112), "Confidence: 90%", fill=MUTED, font=fxs)
d.text((370,y+112), "Expected BTC move: +5.0%", fill=GREEN, font=fxs)

d.text((40,y+132), "Trades executed:", fill=MUTED, font=fxs)
d.text((160,y+132), "BUY BTC $540 (Binance)", fill=GREEN, font=fxs)
d.text((370,y+132), "YES BTC-RESERVE $288 (Kalshi)", fill=GREEN, font=fxs)
d.text((630,y+132), "YES CRYPTO-REG $288 (Kalshi)", fill=GREEN, font=fxs)

d.text((40,y+152), "Total deployed:", fill=MUTED, font=fxs)
d.text((150,y+152), "$1,404 across 4 trades in 0.3 seconds", fill=CYAN, font=fxs)

y += 200

# ═══ TRADE LOG ═══
box(24,y,W-48,320)
d.text((40,y+10), "TRADE LOG (last 15 trades)", fill=TEXT, font=fm)

trades = [
    ("13:10:57", "TRUMP", "BUY", "BTC", "Binance", "$540", "+$42.18", "WIN"),
    ("13:10:57", "TRUMP", "YES", "BTC-RESERVE", "Kalshi", "$288", "+$98.40", "WIN"),
    ("13:10:57", "TRUMP", "YES", "TARIFF-CHINA", "Kalshi", "$200", "+$89.00", "WIN"),
    ("13:10:42", "NEWS", "BUY", "BTC", "Binance", "$350", "+$28.70", "WIN"),
    ("13:09:15", "NEWS", "BUY", "SPY", "Alpaca", "$400", "+$12.80", "WIN"),
    ("13:09:15", "NEWS", "LONG", "BTC 3x", "Binance", "$600", "+$94.20", "WIN"),
    ("13:08:30", "NEWS", "BUY", "QQQ", "Alpaca", "$300", "+$8.40", "WIN"),
    ("13:06:44", "NEWS", "BUY", "NVDA", "Alpaca", "$250", "+$20.00", "WIN"),
    ("13:05:12", "NEWS", "SELL", "SPY", "Alpaca", "$400", "-$12.30", "LOSS"),
    ("13:03:30", "TRUMP", "SELL", "BTC", "Binance", "$500", "+$87.50", "WIN"),
    ("13:00:15", "ARB", "YES", "BTC-UP-69200", "Kalshi", "$320", "+$18.40", "WIN"),
    ("12:58:30", "ARB", "NO", "ETH-UP-3490", "Kalshi", "$280", "+$12.80", "WIN"),
    ("12:55:00", "ARB", "YES", "BTC-UP-69150", "Kalshi", "$300", "-$150.00", "LOSS"),
    ("12:52:10", "ARB", "NO", "ETH-UP-3500", "Kalshi", "$260", "+$15.60", "WIN"),
    ("12:50:00", "NEWS", "BUY", "TSLA", "Alpaca", "$200", "+$14.20", "WIN"),
]

hx = [40, 110, 170, 215, 340, 420, 490, 570]
d.text((hx[0],y+36), "TIME", fill=MUTED, font=fxs)
d.text((hx[1],y+36), "STRAT", fill=MUTED, font=fxs)
d.text((hx[2],y+36), "SIDE", fill=MUTED, font=fxs)
d.text((hx[3],y+36), "ASSET", fill=MUTED, font=fxs)
d.text((hx[4],y+36), "VENUE", fill=MUTED, font=fxs)
d.text((hx[5],y+36), "SIZE", fill=MUTED, font=fxs)
d.text((hx[6],y+36), "P&L", fill=MUTED, font=fxs)
d.text((hx[7],y+36), "RESULT", fill=MUTED, font=fxs)
d.line([40,y+52,W-64,y+52], fill=BORDER)

for i,(t,strat,side,asset,venue,size,pnl,result) in enumerate(trades):
    ty = y+58+i*17
    d.text((hx[0],ty), t, fill=MUTED, font=fxs)
    sc = PURPLE if strat=="ARB" else (AMBER if strat=="TRUMP" else CYAN)
    d.text((hx[1],ty), strat, fill=sc, font=fxs)
    sd = GREEN if side in ("YES","BUY","LONG") else RED
    d.text((hx[2],ty), side, fill=sd, font=fxs)
    d.text((hx[3],ty), asset[:14], fill=TEXT, font=fxs)
    d.text((hx[4],ty), venue, fill=MUTED, font=fxs)
    d.text((hx[5],ty), size, fill=TEXT, font=fxs)
    pc = GREEN if not pnl.startswith("-") else RED
    d.text((hx[6],ty), pnl, fill=pc, font=fxs)
    rc = GREEN if result=="WIN" else RED
    d.text((hx[7],ty), result, fill=rc, font=fxs)

y += 340

# ═══ RISK MANAGEMENT ═══
box(24,y,pw,200)
d.text((40,y+10), "RISK MANAGEMENT", fill=TEXT, font=fm)
d.rounded_rectangle([pw-60,y+8,pw,y+28], radius=8, fill=(10,40,20), outline=GREEN)
d.text((pw-52,y+12), "ACTIVE", fill=GREEN, font=fxs)

risks = [
    ("Daily P&L", 1.8, 20, "+1.8% / -20%"),
    ("Max Drawdown", 4.2, 40, "4.2% / 40%"),
    ("Consecutive Losses", 1, 8, "1 / 8"),
    ("Open Positions", 7, 25, "7 / 25"),
    ("Category: Crypto", 28, 50, "28% / 50%"),
]
for i,(lab,val,mx,disp) in enumerate(risks):
    ry = y+40+i*32
    d.text((40,ry), lab, fill=TEXT, font=fxs)
    d.text((pw-120,ry), disp, fill=MUTED, font=fxs)
    pct = val/mx*100
    bc = GREEN if pct<50 else (AMBER if pct<80 else RED)
    d.rounded_rectangle([40,ry+16,pw-16,ry+22], radius=3, fill=BORDER)
    fw = int((pw-56)*min(pct,100)/100)
    if fw > 0:
        d.rounded_rectangle([40,ry+16,40+fw,ry+22], radius=3, fill=bc)

# ═══ STRATEGY PERFORMANCE ═══
box(rx,y,pw,200)
d.text((rx+16,y+10), "STRATEGY PERFORMANCE", fill=TEXT, font=fm)

strats = [
    ("LATENCY ARB", "92% WR", "48 trades", "+$847", PURPLE),
    ("TRUMP NEWS", "85% WR", "28 trades", "+$1,240", AMBER),
    ("BREAKING NEWS", "72% WR", "67 trades", "+$560", CYAN),
    ("KALSHI CONTRACTS", "78% WR", "20 trades", "+$200", BLUE),
]
for i,(name,wr,trades,pnl,col) in enumerate(strats):
    sy = y+40+i*38
    d.text((rx+16,sy), name, fill=col, font=fxs)
    d.text((rx+140,sy), wr, fill=TEXT, font=fs)
    d.text((rx+220,sy), trades, fill=MUTED, font=fxs)
    d.text((rx+310,sy), pnl, fill=GREEN, font=fb)

# ═══ FOOTER ═══
d.text((W//2-200, H-30), "Multi-Strategy Bot — Kalshi + Binance + Alpaca — All US Legal from NJ", fill=MUTED, font=fxs)

img.save("/home/user/Kalshi/dashboard_screenshot.png", "PNG")
print(f"Saved {W}x{H} dashboard")
