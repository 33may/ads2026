"""Create the report figure from the recorded Phase 4 summary, without pooling runs."""
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    output = Path(__file__).resolve().parent
    source = output.parents[1] / "results/phase4/matrix-20261007/summary.csv"
    with source.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 12
    assert all(int(row["requests"]) == 1200 for row in rows)
    assert all(row["complete"] == "True" for row in rows)

    plt.rcParams.update({"font.size": 9, "pdf.fonttype": 42})
    fig, axes = plt.subplots(1, 2, figsize=(7.1, 2.7), sharey=True)
    styles = [("#326b98", ""), ("#d2a048", "///"), ("#b8c4cd", "xxx")]
    for ax, (policy, title) in zip(axes, [
        ("least-connections", "Least Connections (LC)"),
        ("lrt", "Least Response Time (LRT)"),
    ]):
        for repetition, (color, hatch) in enumerate(styles, start=1):
            selected = [next(row for row in rows if row["policy"] == policy
                             and row["ft"] == ft
                             and int(row["repetition"]) == repetition)
                        for ft in ("off", "on")]
            positions = [group + (repetition - 2) * .23 for group in (0, 1)]
            counts = [int(row["failures"]) for row in selected]
            ax.bar(positions, counts, width=.21, color=color, hatch=hatch,
                   edgecolor="#333333", linewidth=.6, label=f"Repetition {repetition}")
            for x, count in zip(positions, counts):
                ax.annotate(str(count), (x, count), xytext=(0, 4),
                            textcoords="offset points", ha="center", fontsize=9)
        ax.set_title(title, fontsize=10)
        ax.set_xticks([0, 1], ["FT off", "FT on"])
        ax.set_ylim(0, 280)
        ax.set_yticks([0, 50, 100, 150, 200, 250])
        ax.grid(axis="y", color="#e4e4e4", linewidth=.6)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="both", length=3)
    axes[0].set_ylabel("Failed queries / 1,200")
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper center",
               ncol=3, frameon=False, bbox_to_anchor=(.53, 1.02))
    fig.subplots_adjust(left=.09, right=.99, bottom=.16, top=.79, wspace=.14)
    for extension in ("pdf", "png"):
        fig.savefig(output / f"phase4-failures.{extension}", dpi=220)
    plt.close(fig)


if __name__ == "__main__":
    main()
