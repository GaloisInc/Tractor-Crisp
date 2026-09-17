import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path


def plot_gepa_results_TestCorpus(data: np.ndarray, save_path: str | Path):
    """
    Input `data` as a 4x4 array, save as a stacked column chart to `save_path`.
    Rows correspond to ["B01_organic", "B01_synthetic", "B02_organic", "B02_synthetic"].
    Columns correspond to ["Doesn't compile", "Compiles, Tests fail", "Compiles, Tests pass, Unsafe", "Compiles, Tests pass, Safe"].
    All values are percentages.
    """
    datasets = ["B01\norganic", "B01\nsynthetic", "B02\norganic", "B02\nsynthetic"]
    labels = ["Doesn't compile", "Compiles, Tests fail", "Compiles, Tests pass, Unsafe", "Compiles, Tests pass, Safe"]
    colors = ["salmon", "orange", "yellow", "limegreen"]

    _, ax = plt.subplots(figsize=(4, 6))

    bottom = np.zeros(len(data))

    for i in range(data.shape[1]):
        values = data[:, i]

        # Plot
        ax.bar(
            x = datasets,
            height = values,
            bottom = bottom,
            color = colors[i],
            edgecolor = "black",
            label = labels[i]
        )

        # Put percentage labels in the middle of each section
        for j, value in enumerate(values):
            if value == 0:
                continue
            ax.text(
                x = j,
                y = bottom[j] + value/2,
                s = f"{value}%",
                ha = "center",
                va = "center",
                fontsize = 12,
                color = "black"
            )

        bottom += values

    ax.set_ylim(-2, 102)
    plt.yticks([])

    # Use this to get legend once, then save that separately
    # ax.legend(loc='upper center', ncols=1, bbox_to_anchor=(1.5, 0.5))

    plt.savefig(save_path, dpi=600, bbox_inches='tight', pad_inches=0.1)


if __name__ == "__main__":
    plot_gepa_results_TestCorpus(
        data = np.array([
            [16, 5, 58, 21],
            [23, 12, 33, 32],
            [63, 2, 33, 2],
            [55, 10, 24, 11],
        ]),
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompt_2/results.png',
    )
    plot_gepa_results_TestCorpus(
        data = np.array([
            [16, 13, 0, 71],
            [7, 12, 0, 81],
            [49, 11, 0, 40],
            [42, 13, 0, 45],
        ]),
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/20260616_taskGPT5p5_reflGPT5p5/results.png',
    )
