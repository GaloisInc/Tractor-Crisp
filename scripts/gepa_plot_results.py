import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
import numpy as np
import pandas as pd
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


def plot_gepa_results_zlib_unsafe_vs_attempts(
    results_csv_paths: list[Path],
    results_names: list[str],
    save_path: str | Path,
    starting_unsafe: int = 622,
    yaxis_interval: int = 20
):
    plt.figure(figsize=(7.2, 5.4))
    for results_csv_path, results_name in zip(results_csv_paths, results_names):
        df = pd.read_csv(results_csv_path)
        plt.plot(
            pd.concat([pd.Series([starting_unsafe]), df['unsafe_remaining']], ignore_index=True),
            label = results_name,
            linewidth = 2
        )
    plt.grid()
    plt.xlabel("Attempts")
    plt.ylabel("Unsafe remaining")
    plt.xticks(range(0, len(df)+1, 2))
    plt.gca().yaxis.set_major_locator(MultipleLocator(yaxis_interval))
    plt.legend()
    plt.savefig(save_path, dpi=600, bbox_inches='tight', pad_inches=0.1)


def plot_gepa_results_zlib_unsafe_vs_cost(
    results_csv_paths: list[Path],
    results_names: list[str],
    model_costs_per_Mouttok: list[float],
    save_path: str | Path,
    starting_unsafe: int = 622,
    yaxis_interval: int = 20
):
    plt.figure(figsize=(7.2, 5.4))
    for results_csv_path, results_name, model_cost_per_Mouttok in zip(results_csv_paths, results_names, model_costs_per_Mouttok):
        df = pd.read_csv(results_csv_path)
        xaxis = df['agent_safety_prompt_output_tokens'].cumsum() / 1e6 * model_cost_per_Mouttok
        plt.plot(
            pd.concat([pd.Series([0.]), xaxis], ignore_index=True),
            pd.concat([pd.Series([starting_unsafe]), df['unsafe_remaining']], ignore_index=True),
            label = results_name,
            linewidth = 2
        )
    plt.grid()
    plt.xlabel("Total output token cost (USD)")
    plt.ylabel("Unsafe remaining")
    plt.gca().yaxis.set_major_locator(MultipleLocator(yaxis_interval))
    plt.legend()
    plt.savefig(save_path, dpi=600, bbox_inches='tight', pad_inches=0.1)


def plot_gepa_results_zlib_unsafe_vs_time(
    results_csv_paths: list[Path],
    results_names: list[str],
    save_path: str | Path,
    starting_unsafe: int = 622,
    yaxis_interval: int = 20
):
    plt.figure(figsize=(7.2, 5.4))
    for results_csv_path, results_name in zip(results_csv_paths, results_names):
        df = pd.read_csv(results_csv_path)
        xaxis = df['agent_safety_prompt_call_duration_sec'].cumsum() / 60.
        plt.plot(
            pd.concat([pd.Series([0.]), xaxis], ignore_index=True),
            pd.concat([pd.Series([starting_unsafe]), df['unsafe_remaining']], ignore_index=True),
            label = results_name,
            linewidth = 2
        )
    plt.grid()
    plt.xlabel("Total time taken for agent calls (minutes)")
    plt.ylabel("Unsafe remaining")
    plt.xticks(np.arange(0, max(xaxis)+5, 15))
    plt.gca().yaxis.set_major_locator(MultipleLocator(yaxis_interval))
    plt.legend()
    plt.savefig(save_path, dpi=600, bbox_inches='tight', pad_inches=0.1)


if __name__ == "__main__":


    ############################################################
    # Results on Test Corpus
    ############################################################

    # plot_gepa_results_TestCorpus(
    #     data = np.array([
    #         [16, 5, 58, 21],
    #         [23, 12, 33, 32],
    #         [63, 2, 33, 2],
    #         [55, 10, 24, 11],
    #     ]),
    #     save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompt_2/results.png',
    # )

    # plot_gepa_results_TestCorpus(
    #     data = np.array([
    #         [16, 13, 0, 71],
    #         [7, 12, 0, 81],
    #         [49, 11, 0, 40],
    #         [42, 13, 0, 45],
    #     ]),
    #     save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/20260616_taskGPT5p5_reflGPT5p5/results.png',
    # )


    ############################################################
    # Results on zlib
    ############################################################

    results_csv_paths_622 = [
        Path(__file__).parent.parent / f"gepa_artifacts/{f}" for f in [
            "seed_prompts_agents/results_zlib_start622_GPT5p6terra.csv",
            "20260906C_reflGPT5p6/results_zlib_start622_GPT5p6terra.csv",
            "20260908_reflGPT5p6/results_zlib_start622_GPT5p6terra.csv",
            "seed_prompts_agents/results_zlib_start622_GPT5p6sol.csv",
            "20260906C_reflGPT5p6/results_zlib_start622_GPT5p6sol.csv",
            "20260908_reflGPT5p6/results_zlib_start622_GPT5p6sol.csv",
        ]
    ]
    results_names_622 = [
        "Seed, Terra",
        "GEPA safety-cost balance, Terra",
        "GEPA safety-only, Terra",
        "Seed, Sol",
        "GEPA safety-cost balance, Sol",
        "GEPA safety-only, Sol",
    ]
    results_csv_paths_6604 = [
        Path(__file__).parent.parent / f"gepa_artifacts/{f}" for f in [
            "seed_prompts_agents/results_zlib_start6604_GPT5p6sol.csv",
            "20260906C_reflGPT5p6/results_zlib_start6604_GPT5p6sol.csv",
            "20260908B_reflGPT5p6/results_zlib_start6604_GPT5p6sol.csv",
        ]
    ]
    results_names_6604 = [
        "Seed, Sol",
        "GEPA safety-cost balance, Sol",
        "GEPA safety-only, Sol",
    ]
    model_cost_per_Mouttok_sol = 20.
    model_cost_per_Mouttok_terra = 12.

    plot_gepa_results_zlib_unsafe_vs_attempts(
        results_csv_paths = results_csv_paths_622,
        results_names = results_names_622,
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe622_vs_attempts.png',
        starting_unsafe = 622,
        yaxis_interval = 20
    )

    plot_gepa_results_zlib_unsafe_vs_attempts(
        results_csv_paths = results_csv_paths_6604,
        results_names = results_names_6604,
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe6604_vs_attempts.png',
        starting_unsafe = 6604,
        yaxis_interval = 300
    )

    plot_gepa_results_zlib_unsafe_vs_cost(
        results_csv_paths = results_csv_paths_622,
        results_names = results_names_622,
        model_costs_per_Mouttok = 3*[model_cost_per_Mouttok_terra] + 3*[model_cost_per_Mouttok_sol],
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe622_vs_cost.png',
        starting_unsafe = 622,
        yaxis_interval = 20
    )

    plot_gepa_results_zlib_unsafe_vs_cost(
        results_csv_paths = results_csv_paths_6604,
        results_names = results_names_6604,
        model_costs_per_Mouttok = 3*[model_cost_per_Mouttok_sol],
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe6604_vs_cost.png',
        starting_unsafe = 6604,
        yaxis_interval = 300
    )

    plot_gepa_results_zlib_unsafe_vs_time(
        results_csv_paths = results_csv_paths_622,
        results_names = results_names_622,
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe622_vs_time.png',
        starting_unsafe = 622,
        yaxis_interval = 20
    )

    plot_gepa_results_zlib_unsafe_vs_time(
        results_csv_paths = results_csv_paths_6604,
        results_names = results_names_6604,
        save_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/zlib_plots/results_zlib_unsafe6604_vs_time.png',
        starting_unsafe = 6604,
        yaxis_interval = 300
    )
