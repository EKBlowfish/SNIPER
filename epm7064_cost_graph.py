"""
EPM7064SLC44 PLCC44 — landed-cost comparison to Venlo (NL)
1688 + Superbuy  vs  AliExpress, as a function of order quantity.

Model is built from live data gathered in-session:
  * 1688 chip price tiers (Superbuy FX  Y7 = EUR 0.95):
        qty   1..99   -> EUR 0.95/pc  (Y7,   MOQ-1 listing 919187433345)
        qty 100..2499 -> EUR 0.745/pc (Y5.50, bulk listing 1041577014379)
        qty 2500+     -> EUR 0.61/pc  (Y4.50)
  * 1688 domestic freight to Superbuy Guangzhou warehouse: ~EUR 0.70 flat
  * Superbuy international freight (cheapest line, measured quotes to NL):
        ~100 g  -> EUR 5.92  (SF Air Mail Small)
        ~300 g  -> EUR 10.77 (SF Air Mail)
        ~500 g  -> EUR 11.58 (YunExpress Air Cargo)
        weight(g) ~= 60 (packaging) + 2.2 * qty
  * NL import VAT 21% applied to the Superbuy parcel (AliExpress price already incl. VAT)
  * AliExpress: EUR 1.63/pc (VAT incl) + EUR 5.86 shipping per order,
        orders split to stay <= EUR 150 (IOSS) -> orders = ceil(1.63*qty/150)
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

q = np.arange(1, 301)

# ---------- AliExpress ----------
AE_CHIP = 1.63                       # EUR/pc, VAT included
ae_items = AE_CHIP * q
ae_orders = np.ceil(ae_items / 150.0)     # keep each order <= 150 EUR for IOSS
ae_ship = 5.86 * ae_orders
ae_total = ae_items + ae_ship
ae_per = ae_total / q

# ---------- 1688 + Superbuy ----------
def chip_price(n):
    if n < 100:   return 0.95
    if n < 2500:  return 0.745
    return 0.61
sb_chip = np.array([chip_price(n) for n in q])
sb_items = sb_chip * q
SB_DOMESTIC = 0.70

# international freight, interpolated from measured Superbuy quotes
intl_x = [1,    20,   100,   200,   300]
intl_y = [5.92, 5.92, 10.77, 11.58, 12.39]
sb_intl = np.interp(q, intl_x, intl_y)

sb_pretax = sb_items + SB_DOMESTIC + sb_intl
sb_total = sb_pretax * 1.21               # + 21% NL import VAT
sb_per = sb_total / q

# ---------- crossover ----------
cheaper = sb_total < ae_total
crossover = int(q[np.argmax(cheaper)]) if cheaper.any() else None

# ---------- plot ----------
plt.rcParams.update({"font.size": 11})
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

# Panel 1: total landed cost
ax1.plot(q, ae_total, color="#e62e04", lw=2.2, label="AliExpress (to door, VAT incl)")
ax1.plot(q, sb_total, color="#ff7a00", lw=2.2, label="1688 + Superbuy (to door, incl 21% VAT)")
ax1.set_xscale("log"); ax1.set_yscale("log")
ax1.set_xlabel("Quantity (chips)")
ax1.set_ylabel("Total landed cost in Venlo (EUR)")
ax1.set_title("Total cost vs quantity")
ax1.grid(True, which="both", ls=":", alpha=0.5)
for axx in (ax1,):
    axx.xaxis.set_major_formatter(ScalarFormatter()); axx.yaxis.set_major_formatter(ScalarFormatter())
ax1.set_xticks([1,2,5,10,20,50,100,200,300])
ax1.legend(loc="upper left", fontsize=9)

# Panel 2: per-chip landed cost
ax2.plot(q, ae_per, color="#e62e04", lw=2.2, label="AliExpress")
ax2.plot(q, sb_per, color="#ff7a00", lw=2.2, label="1688 + Superbuy")
ax2.axhline(0.745*1.21, color="#ff7a00", ls="--", lw=1, alpha=0.6)
ax2.text(120, 0.745*1.21+0.05, "wholesale floor ~EUR 0.90/chip", color="#ff7a00", fontsize=8)
ax2.axhline(1.63, color="#e62e04", ls="--", lw=1, alpha=0.6)
ax2.text(2, 1.70, "AliExpress floor EUR 1.63/chip", color="#e62e04", fontsize=8)
ax2.set_xscale("log")
ax2.set_xlabel("Quantity (chips)")
ax2.set_ylabel("Cost per chip (EUR)")
ax2.set_title("Cost per chip vs quantity")
ax2.grid(True, which="both", ls=":", alpha=0.5)
ax2.xaxis.set_major_formatter(ScalarFormatter())
ax2.set_xticks([1,2,5,10,20,50,100,200,300])
ax2.set_ylim(0, 8)

# annotate crossover
if crossover:
    for ax in (ax1, ax2):
        ax.axvline(crossover, color="gray", ls="-.", lw=1)
    ax2.annotate(f"crossover ~{crossover} chips\n(AliExpress only cheaper below this)",
                 xy=(crossover, sb_per[crossover-1]), xytext=(6, 5.6),
                 fontsize=8.5, arrowprops=dict(arrowstyle="->", color="gray"))

# annotate optimum (bulk price unlocks at 100, shipping already amortized)
for ax in (ax1, ax2):
    ax.axvline(100, color="#1f7a1f", ls=":", lw=1.4)
ax2.annotate("sweet spot ~100 chips\nwholesale tier unlocked +\nshipping fully amortized",
             xy=(100, sb_per[99]), xytext=(11, 3.2),
             fontsize=8.5, color="#1f7a1f",
             arrowprops=dict(arrowstyle="->", color="#1f7a1f"))
ax2.legend(loc="upper right", fontsize=9)

fig.suptitle("EPM7064SLC44 PLCC44 — cost to Venlo (NL): 1688+Superbuy vs AliExpress",
             fontsize=13, fontweight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.96])
out = r"C:\Users\kerbe\OneDrive\Documents\GitHub\SNIPER\epm7064_cost_comparison.png"
fig.savefig(out, dpi=130)
print("crossover qty:", crossover)
for n in (1,2,5,10,20,50,100,200,300):
    i = n-1
    print(f"qty {n:>4}: AE EUR {ae_total[i]:7.2f} ({ae_per[i]:.2f}/pc) | "
          f"SB EUR {sb_total[i]:7.2f} ({sb_per[i]:.2f}/pc)")
print("saved:", out)
