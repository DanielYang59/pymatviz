"""Testing related utils."""

from __future__ import annotations

import os
import tempfile

import matplotlib.pyplot as plt
import pytest

from pymatviz.utils import ROOT


TEST_FILES: str = f"{ROOT}/tests/files"


def interactive_check(
    plot: plt.Figure | plt.Axes,
    /,
    *,
    elem_to_check: str = "figure",
) -> bool | None:
    """Ask a human to visually inspect a plot.

    Args:
        plot (plt.Figure | plt.Axes): Plot to inspect.
        elem_to_check (str): Prompt for what element in the plot to check.

    Returns:
        bool: whether this passes inspection (KeyboardInterrupt would
            be treated as a quick "y").
        None: if running in CI.
    """
    # Skip CI runs
    if os.getenv("CI"):
        return None

    # Handle matplotlib Axes
    if isinstance(plot, plt.Axes):
        plot = plot.figure
    elif not isinstance(plot, plt.Figure):
        raise TypeError(f"plot type {type(plot).__name__} is not supported.")

    # Save the figure to as a temporary file (ensure it matches the savefig style)
    with tempfile.NamedTemporaryFile(suffix=".png") as tmpfile:
        temp_filepath = tmpfile.name

    plot.savefig(temp_filepath, dpi=300)
    plt.close(plot)

    # Show figure
    img = plt.imread(temp_filepath)
    plt.imshow(img)  # Display the saved image in the plot window
    plt.axis("off")  # Hide axes for cleaner inspection

    plt.show(block=False)

    # Ask the user to check (treat KeyboardInterrupt as "yes")
    try:
        good_enough: bool = (
            input(f"Please check the {elem_to_check}. Is it looking good? (y/n): ")
            .strip()
            .lower()
        ) == "y"
    except KeyboardInterrupt:
        good_enough = True

    finally:
        plt.close()

    if not good_enough:
        pytest.fail(reason=f"{elem_to_check} would need attention.")

    return good_enough
