"""Render the dashboard as a PNG image using Pillow (no browser needed)."""

from PIL import Image, ImageDraw, ImageFont
import json, math

# Load sim data
from dashboard import _generate_sim_data
data = _generate_sim_data()

W, H = 1400, 2200
BG = (10, 14, 23)
CARD = (17, 24, 39)
BORDER = (30, 41, 59)
TEXT = (226, 232, 240)
MUTED = (100, 116, 139)
GREEN = (34, 197, 94)
RED = (239, 68, 68)
BLUE = (59, 130, 246)
AMBER = (245, 158, 11)
PURPLE = (168, 85, 247)
CYAN = (6, 182, 212)

img = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

try:
    font_b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    font_l = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    font_xl = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    font_xs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    font_m = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
except:
    font_b = ImageFont.load_default()
    font_l = font_b
    font_xl = font_b
    font_s = font_b
    font_xs = font_b
    font_m = font_b

def rect(x, y, w, h, fill=CARD, outline=BORDER):
    draw.rounded_rectangle([x, y, x+w, y+h], radius=12, fill=fill, outline=outline)

def fmt(n):
    return f"+${n:.2f}" if n >= 0 else f"-${abs(n):.2f}"

# ── HEADER ──
draw.rectangle([0, 0, W, 60], fill=(17, 24, 39))
draw.line([0, 60, W, 60], fill=BORDER)
draw.text((32, 16), "KALSHI LONGSHOT BIAS BOT", fill=CYAN, font=font_b)
# Badges
draw.rounded_rectangle([W-260, 16, W-200, 42], radius=10, fill=(50, 40, 10), outline=AMBER)
draw.text((W-254, 20), "PAPER", fill=AMBER, font=font_xs)
draw.rounded_rectangle([W-185, 16, W-125, 42], radius=10, fill=(35, 25, 50), outline=PURPLE)
draw.text((W-180, 20), "DEMO", fill=PURPLE, font=font_xs)
# Pulse + uptime
draw.ellipse([W-108, 24, W-100, 32], fill=GREEN)
draw.text((W-95, 20), data["uptime"], fill=MUTED, font=font_xs)

y = 80

# ── TOP STATS ──
stats = [
    ("PORTFOLIO VALUE", f"${data['portfolio_value']:,.2f}", fmt(data['total_pnl']) + f" ({data['roi_pct']:.2f}%)", GREEN if data['total_pnl'] >= 0 else RED),
    ("WIN RATE", f"{data['win_rate']}%", f"{data['wins']}W / {data['losses']}L", BLUE),
    ("UNREALIZED P&L", fmt(data['unrealized_pnl']), f"${data['total_exposure']:.0f} exposed", GREEN if data['unrealized_pnl'] >= 0 else RED),
    ("REALIZED P&L", fmt(data['realized_pnl']), f"{data['total_trades']} closed trades", GREEN if data['realized_pnl'] >= 0 else RED),
    ("MAX DRAWDOWN", f"{data['risk']['drawdown_pct']:.2f}%", f"Last scan: {data['last_scan']}", GREEN),
]
cw = (W - 24*2 - 16*4) // 5
for i, (label, value, sub, color) in enumerate(stats):
    x = 24 + i * (cw + 16)
    rect(x, y, cw, 100)
    draw.text((x + 16, y + 14), label, fill=MUTED, font=font_xs)
    draw.text((x + 16, y + 34), value, fill=color, font=font_l)
    draw.text((x + 16, y + 72), sub, fill=MUTED, font=font_xs)

y += 120

# ── EQUITY CHART ──
rect(24, y, W-48, 220)
draw.text((40, y + 14), "Equity Curve (7 Days)", fill=TEXT, font=font_m)
draw.text((W-240, y + 14), f"Peak: ${data['peak_value']:,.2f}", fill=MUTED, font=font_xs)

eq = data["equity_curve"]
mn, mx = min(eq), max(eq)
rng = mx - mn if mx != mn else 1
chart_x, chart_y, chart_w, chart_h = 40, y + 42, W - 88, 150
bar_w = max(1, chart_w // len(eq))

for i, v in enumerate(eq):
    bh = max(2, int(((v - mn) / rng) * chart_h))
    bx = chart_x + i * bar_w
    by = chart_y + chart_h - bh
    color = BLUE if v >= data["initial_balance"] else RED
    draw.rectangle([bx, by, bx + bar_w - 1, chart_y + chart_h], fill=color)

draw.text((chart_x, chart_y + chart_h + 4), "7 days ago", fill=MUTED, font=font_xs)
draw.text((chart_x + chart_w - 30, chart_y + chart_h + 4), "Now", fill=MUTED, font=font_xs)

y += 240

# ── ACTIVE POSITIONS ──
pw = (W - 48 - 16) // 2
rect(24, y, pw, 280)
draw.text((40, y + 14), "Active Positions", fill=TEXT, font=font_m)
draw.text((pw - 20, y + 14), f"{len(data['positions'])}/8", fill=MUTED, font=font_xs)

cols = ["MARKET", "SIDE", "TYPE", "ENTRY", "NOW", "SIZE", "P&L"]
col_x = [40, 200, 260, 340, 400, 460, 530]
for ci, col in enumerate(cols):
    draw.text((col_x[ci], y + 46), col, fill=MUTED, font=font_xs)
draw.line([40, y + 64, 24+pw-16, y + 64], fill=BORDER)

for pi, pos in enumerate(data["positions"]):
    py_ = y + 72 + pi * 60
    draw.text((col_x[0], py_), pos["ticker"][:25], fill=TEXT, font=font_s)
    draw.text((col_x[0], py_ + 18), pos["title"][:35], fill=MUTED, font=font_xs)

    sc = GREEN if pos["side"] == "YES" else RED
    draw.rounded_rectangle([col_x[1], py_, col_x[1]+40, py_+18], radius=4, fill=(sc[0]//5, sc[1]//5, sc[2]//5))
    draw.text((col_x[1]+6, py_+2), pos["side"], fill=sc, font=font_xs)

    tc = PURPLE if pos["type"] == "longshot" else CYAN
    draw.text((col_x[2], py_+2), pos["type"].upper(), fill=tc, font=font_xs)

    draw.text((col_x[3], py_+2), f"{pos['entry_price']:.2f}", fill=TEXT, font=font_s)
    draw.text((col_x[4], py_+2), f"{pos['current_price']:.2f}", fill=TEXT, font=font_s)
    draw.text((col_x[5], py_+2), f"${pos['size_usd']:.0f}", fill=TEXT, font=font_s)

    pc = GREEN if pos["pnl"] >= 0 else RED
    draw.text((col_x[6], py_+2), fmt(pos["pnl"]), fill=pc, font=font_s)

# ── RISK MANAGEMENT ──
rx = 24 + pw + 16
rect(rx, y, pw, 280)
draw.text((rx + 16, y + 14), "Risk Management", fill=TEXT, font=font_m)
draw.rounded_rectangle([rx+pw-80, y+10, rx+pw-16, y+32], radius=10, fill=(10,40,20), outline=GREEN)
draw.text((rx+pw-72, y+14), "ACTIVE", fill=GREEN, font=font_xs)

risks = [
    ("Daily P&L", abs(data["risk"]["daily_pnl_pct"]), data["risk"]["daily_limit"],
     f"{data['risk']['daily_pnl_pct']:.2f}% / -{data['risk']['daily_limit']}%"),
    ("Drawdown", data["risk"]["drawdown_pct"], data["risk"]["drawdown_limit"],
     f"{data['risk']['drawdown_pct']:.2f}% / {data['risk']['drawdown_limit']}%"),
    ("Consecutive Losses", data["risk"]["consecutive_losses"], data["risk"]["max_consec"],
     f"{data['risk']['consecutive_losses']} / {data['risk']['max_consec']}"),
    ("Open Positions", data["risk"]["positions_used"], data["risk"]["positions_max"],
     f"{data['risk']['positions_used']} / {data['risk']['positions_max']}"),
]

for ri, (label, val, maxv, display) in enumerate(risks):
    ry = y + 52 + ri * 54
    draw.text((rx + 16, ry), label, fill=TEXT, font=font_s)
    draw.text((rx + pw - 16 - len(display)*7, ry), display, fill=MUTED, font=font_xs)
    pct = (val / maxv) * 100 if maxv > 0 else 0
    bar_color = GREEN if pct < 50 else (AMBER if pct < 80 else RED)
    # Bar background
    draw.rounded_rectangle([rx+16, ry+22, rx+pw-16, ry+30], radius=3, fill=BORDER)
    # Bar fill
    fill_w = int((pw - 32) * min(pct, 100) / 100)
    if fill_w > 0:
        draw.rounded_rectangle([rx+16, ry+22, rx+16+fill_w, ry+30], radius=3, fill=bar_color)

y += 300

# ── RECENT SIGNALS ──
rect(24, y, pw, 380)
draw.text((40, y + 14), "Recent Signals", fill=TEXT, font=font_m)
draw.text((pw - 60, y + 14), f"Last scan: {data['last_scan']}", fill=MUTED, font=font_xs)

sig_cols = ["TIME", "MARKET", "SIDE", "TYPE", "YES", "EDGE", "SCORE", "ACTION"]
sig_x = [40, 110, 240, 300, 380, 420, 470, 540]
for ci, col in enumerate(sig_cols):
    draw.text((sig_x[ci], y + 46), col, fill=MUTED, font=font_xs)
draw.line([40, y + 64, 24+pw-16, y + 64], fill=BORDER)

for si, sig in enumerate(data["signals"]):
    sy = y + 72 + si * 48
    draw.text((sig_x[0], sy), sig["time"], fill=MUTED, font=font_xs)
    draw.text((sig_x[1], sy), sig["ticker"][:18], fill=TEXT, font=font_xs)

    sc = GREEN if sig["side"] == "YES" else RED
    draw.rounded_rectangle([sig_x[2], sy-2, sig_x[2]+32, sy+14], radius=4, fill=(sc[0]//5, sc[1]//5, sc[2]//5))
    draw.text((sig_x[2]+5, sy), sig["side"], fill=sc, font=font_xs)

    tc = PURPLE if sig["type"] == "longshot" else CYAN
    draw.text((sig_x[3], sy), sig["type"][:8].upper(), fill=tc, font=font_xs)

    draw.text((sig_x[4], sy), f"{sig['yes_price']*100:.0f}c", fill=TEXT, font=font_xs)
    draw.text((sig_x[5], sy), f"{sig['edge']*100:.1f}%", fill=TEXT, font=font_xs)

    # Score bar
    bar_bg = [sig_x[6], sy+4, sig_x[6]+40, sy+8]
    draw.rounded_rectangle(bar_bg, radius=2, fill=BORDER)
    sw = int(40 * sig["score"])
    bar_c = GREEN if sig["score"] >= 0.65 else (AMBER if sig["score"] >= 0.55 else RED)
    draw.rounded_rectangle([sig_x[6], sy+4, sig_x[6]+sw, sy+8], radius=2, fill=bar_c)
    draw.text((sig_x[6]+44, sy), f"{sig['score']:.2f}", fill=TEXT, font=font_xs)

    ac = GREEN if sig["action"] == "TRADED" else MUTED
    draw.text((sig_x[7], sy), sig["action"], fill=ac, font=font_xs)

# ── PERFORMANCE BY STRATEGY ──
rect(rx, y, pw, 380)
draw.text((rx + 16, y + 14), "Performance by Strategy", fill=TEXT, font=font_m)

types = [
    ("LONGSHOT BIAS", data["performance"]["longshot"], PURPLE),
    ("FAVORITE BIAS", data["performance"]["favorite"], CYAN),
]
for ti, (label, perf, color) in enumerate(types):
    tx = rx + 16 + ti * (pw//2)
    ty = y + 52
    draw.rounded_rectangle([tx, ty, tx + pw//2 - 16, ty + 90], radius=8, fill=(20, 28, 45))
    draw.text((tx + 12, ty + 10), label, fill=color, font=font_xs)
    wr = f"{perf['wins']/max(perf['trades'],1)*100:.0f}%"
    draw.text((tx + 12, ty + 30), wr, fill=TEXT, font=font_l)
    draw.text((tx + 12, ty + 66), f"Win Rate", fill=MUTED, font=font_xs)
    draw.text((tx + 90, ty + 30), str(perf["trades"]), fill=TEXT, font=font_l)
    draw.text((tx + 90, ty + 66), "Trades", fill=MUTED, font=font_xs)
    pc = GREEN if perf["pnl"] >= 0 else RED
    draw.text((tx + 160, ty + 30), fmt(perf["pnl"]), fill=pc, font=font_l)
    draw.text((tx + 160, ty + 66), "P&L", fill=MUTED, font=font_xs)

# Category exposure
draw.text((rx + 16, y + 165), "CATEGORY EXPOSURE", fill=MUTED, font=font_xs)
cats = data["risk"]["category_exposure"]
for ci, (cat, val) in enumerate(cats.items()):
    cy = y + 190 + ci * 32
    draw.text((rx + 16, cy), cat, fill=MUTED, font=font_s)
    bar_x = rx + 120
    bar_w_full = pw - 180
    draw.rounded_rectangle([bar_x, cy+4, bar_x+bar_w_full, cy+14], radius=4, fill=BORDER)
    fill = int(bar_w_full * min(val / 30, 1.0))
    if fill > 0:
        bc = AMBER if val > 25 else BLUE
        draw.rounded_rectangle([bar_x, cy+4, bar_x+fill, cy+14], radius=4, fill=bc)
    draw.text((bar_x + bar_w_full + 8, cy), f"{val:.1f}%", fill=TEXT, font=font_xs)

y += 400

# ── CLOSED TRADES ──
rect(24, y, W - 48, 340)
draw.text((40, y + 14), "Recent Closed Trades", fill=TEXT, font=font_m)

cl_cols = ["CLOSED", "MARKET", "SIDE", "TYPE", "ENTRY", "EXIT", "P&L", "RESULT"]
cl_x = [40, 140, 400, 480, 600, 680, 760, 860]
for ci, col in enumerate(cl_cols):
    draw.text((cl_x[ci], y + 46), col, fill=MUTED, font=font_xs)
draw.line([40, y + 64, W - 68, y + 64], fill=BORDER)

for ci, trade in enumerate(data["closed_trades"]):
    cy = y + 72 + ci * 32
    draw.text((cl_x[0], cy), trade["closed"], fill=MUTED, font=font_xs)
    draw.text((cl_x[1], cy), trade["ticker"][:30], fill=TEXT, font=font_xs)

    sc = GREEN if trade["side"] == "YES" else RED
    draw.rounded_rectangle([cl_x[2], cy-2, cl_x[2]+32, cy+14], radius=4, fill=(sc[0]//5, sc[1]//5, sc[2]//5))
    draw.text((cl_x[2]+5, cy), trade["side"], fill=sc, font=font_xs)

    tc = PURPLE if trade["type"] == "longshot" else CYAN
    draw.text((cl_x[3], cy), trade["type"].upper(), fill=tc, font=font_xs)

    draw.text((cl_x[4], cy), f"{trade['entry']:.2f}", fill=TEXT, font=font_xs)
    draw.text((cl_x[5], cy), f"{trade['exit']:.2f}", fill=TEXT, font=font_xs)
    pc = GREEN if trade["pnl"] >= 0 else RED
    draw.text((cl_x[6], cy), fmt(trade["pnl"]), fill=pc, font=font_xs)

    rc = GREEN if trade["result"] == "win" else RED
    draw.rounded_rectangle([cl_x[7], cy-2, cl_x[7]+36, cy+14], radius=4, fill=(rc[0]//5, rc[1]//5, rc[2]//5))
    draw.text((cl_x[7]+5, cy), trade["result"].upper(), fill=rc, font=font_xs)

# ── FOOTER ──
draw.text((W//2 - 160, H - 30), "Kalshi Longshot Bias Bot — Dashboard — Auto-refreshes every 5s", fill=MUTED, font=font_xs)

img.save("/home/user/Kalshi/dashboard_screenshot.png", "PNG")
print(f"Saved {W}x{H} dashboard screenshot")
