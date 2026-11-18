"""Fig. 3.1 System Architecture — drawn directly as SVG (a layered stack;
Mermaid cannot keep boxes side by side inside linked layers)."""
W, PAD = 900, 20
FONT = "Times New Roman, Liberation Serif, serif"
out = []


def box(x, y, w, h, lines, bold=False, round_=False, cyl=False):
    if cyl:
        out.append(f'<path d="M{x},{y+8} a{w/2},8 0 0 1 {w},0 v{h-16} a{w/2},8 0 0 1 {-w},0 z" fill="#fff" stroke="#333"/>')
        out.append(f'<path d="M{x},{y+8} a{w/2},8 0 0 0 {w},0" fill="none" stroke="#333"/>')
    else:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{16 if round_ else 2}" fill="#fff" stroke="#333"/>')
    n = len(lines); cy = y + h / 2 - (n - 1) * 9
    for i, t in enumerate(lines):
        out.append(f'<text x="{x+w/2}" y="{cy+i*18+5}" text-anchor="middle" font-size="15" '
                   f'font-weight="{"bold" if bold and i == 0 else "normal"}">{t}</text>')


def layer(y, h, title):
    out.append(f'<rect x="{PAD}" y="{y}" width="{W-2*PAD-150}" height="{h}" fill="#f6f6f6" stroke="#777"/>')
    out.append(f'<text x="{PAD+10}" y="{y+18}" font-size="14.5" font-weight="bold">{title}</text>')


def arrow(x1, y1, x2, y2, label=None, lx=None, ly=None, anchor="start"):
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#333" stroke-width="1.6" marker-end="url(#a)"/>')
    if label:
        out.append(f'<text x="{lx}" y="{ly}" font-size="13.5" font-style="italic" text-anchor="{anchor}">{label}</text>')


LW = W - 2 * PAD - 150          # layer width; the right margin carries the long arrows
box(PAD + LW / 2 - 110, 10, 220, 34, ["Network operator (web browser)"], round_=True)
arrow(PAD + LW / 2, 44, PAD + LW / 2, 70)
# 1 presentation
layer(70, 86, "1. Presentation Layer")
box(PAD + 40, 98, 220, 48, ["Login page", "operator session (HttpOnly cookie)"])
box(PAD + 300, 98, 330, 48, ["Dashboard (FastAPI + JavaScript)", "topology, charts, approvals, controls"])
arrow(PAD + LW / 2, 156, PAD + LW / 2, 186, "status, approvals, operator actions", PAD + LW / 2 + 8, 176)
# 2 intelligence
layer(186, 132, "2. Intelligence Layer")
bx = [PAD + 20, PAD + 195, PAD + 370, PAD + 545]
names = [["Failure Prediction", "z-score / logistic"], ["Zero Trust Engine", "trust Eq. 2, threat Eq. 3"],
         ["Decision Engine", "risk Eq. 4, paths Eq. 6"], ["Self-Healing Engine", "execute, verify, roll back"]]
for x, n in zip(bx, names):
    box(x, 222, 160, 52, n, bold=True)
arrow(bx[1] + 160, 248, bx[2], 248)
arrow(bx[3], 248, bx[3], 248)
arrow(bx[2] + 160, 248, bx[3], 248)
out.append(f'<path d="M{bx[0]+80},274 v26 h{bx[2]-bx[0]} v-26" fill="none" stroke="#333" stroke-width="1.6" marker-end="url(#a)"/>')
out.append(f'<text x="{bx[1]+80}" y="296" font-size="13" font-style="italic" text-anchor="middle">failure scores</text>')
# 3 data
layer(350, 96, "3. Data and Analytics Layer")
box(PAD + 120, 380, 260, 50, ["Telemetry Collector", "health evaluation, derived rates"], bold=True)
box(PAD + 430, 378, 190, 56, ["SQLite history", "features, baselines"], cyl=True)
arrow(PAD + 380, 405, PAD + 430, 405)
arrow(PAD + 250, 380, PAD + 250, 320, "measurements, health, features", PAD + 258, 342)
# 4 control
layer(478, 96, "4. Control Layer")
box(PAD + 60, 508, 250, 50, ["NetworkController", "FailureManager"], bold=True)
box(PAD + 360, 508, 270, 50, ["Self-healing Ryu app", "OpenFlow 1.3 + REST API"], bold=True)
arrow(PAD + 495, 508, PAD + 330, 432, "port stats, link probes, flow records", PAD + 425, 470)
# 5 data plane
layer(606, 96, "5. Data Plane")
box(PAD + 20, 636, 190, 50, ["Mininet hosts, servers", "(real traffic, attacks)"])
box(PAD + 235, 636, 180, 50, ["Open vSwitch", "rtr-core, sw-a, sw-b"])
box(PAD + 440, 636, 190, 50, ["Simulated data plane", "(simulation mode)"])
arrow(PAD + 185, 558, PAD + 140, 636)
arrow(PAD + 495, 558, PAD + 325, 636, "OpenFlow rules", PAD + 420, 600)
# right-margin arrow: intelligence -> control (verified actions)
rx = PAD + LW + 40
out.append(f'<path d="M{bx[3]+160},248 H{rx} V533 H{PAD+630}" fill="none" stroke="#333" stroke-width="1.6" marker-end="url(#a)"/>')
out.append(f'<text x="{rx+8}" y="390" font-size="13.5" font-style="italic">verified actions:</text>')
out.append(f'<text x="{rx+8}" y="407" font-size="13.5" font-style="italic">restore, reroute,</text>')
out.append(f'<text x="{rx+8}" y="424" font-size="13.5" font-style="italic">block, quarantine</text>')
H = 712
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">'
       '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
       '<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker></defs>' + "".join(out) + "</svg>")
open(__file__.replace("diagrams/fig3_1_architecture.py", "figs/fig3_1_architecture.svg"), "w").write(svg)
print("written", len(svg))
