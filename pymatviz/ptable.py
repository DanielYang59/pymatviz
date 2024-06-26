"""Various periodic table heatmaps with matplotlib and plotly."""

from __future__ import annotations

import itertools
import math
import warnings
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Literal, Union

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.figure_factory as ff
from matplotlib.colors import Colormap, LogNorm, Normalize
from matplotlib.patches import Rectangle
from pandas.api.types import is_numeric_dtype, is_string_dtype
from pymatgen.core import Composition, Element

from pymatviz._preprocess_data import (
    SupportedDataType,
    SupportedValueType,
    preprocess_ptable_data,
)
from pymatviz.colors import ELEM_COLORS_JMOL, ELEM_COLORS_VESTA, ELEM_TYPE_COLORS
from pymatviz.enums import ElemColorMode, ElemColors, ElemCountMode, Key
from pymatviz.utils import df_ptable, pick_bw_for_contrast, si_fmt


if TYPE_CHECKING:
    from typing import Any, Callable

    import plotly.graph_objects as go
    from matplotlib.typing import ColorType


ElemValues = Union[dict[Union[str, int], float], pd.Series, Sequence[str]]


def count_elements(
    values: ElemValues,
    count_mode: ElemCountMode = ElemCountMode.composition,
    exclude_elements: Sequence[str] = (),
    fill_value: float | None = 0,
) -> pd.Series:
    """Count element occurrence in list of formula strings or dict-like compositions.
    If passed values are already a map from element symbol to counts, ensure the
    data is a pd.Series filled with zero values for missing element symbols.

    Provided as standalone function for external use or to cache long computations.
    Caching long element counts is done by refactoring
        ptable_heatmap(long_list_of_formulas) # slow
    to
        elem_counts = count_elements(long_list_of_formulas) # slow
        ptable_heatmap(elem_counts) # fast, only rerun this line to update the plot

    Args:
        values (dict[str, int | float] | pd.Series | list[str]): Iterable of
            composition strings/objects or map from element symbols to heatmap values.
        count_mode ('(element|fractional|reduced)_composition'):
            Only used when values is a list of composition strings/objects.
            - composition (default): Count elements in each composition as is,
                i.e. without reduction or normalization.
            - fractional_composition: Convert to normalized compositions in which the
                amounts of each species sum to before counting.
                Example: Fe2 O3 -> Fe0.4 O0.6
            - reduced_composition: Convert to reduced compositions (i.e. amounts
                normalized by greatest common denominator) before counting.
                Example: Fe4 P4 O16 -> Fe P O4.
            - occurrence: Count the number of times each element occurs in a list of
                formulas irrespective of compositions. E.g. [Fe2 O3, Fe O, Fe4 P4 O16]
                counts to {Fe: 3, O: 3, P: 1}.
        exclude_elements (Sequence[str]): Elements to exclude from the count. Defaults
            to ().
        fill_value (float | None): Value to fill in for missing elements. Defaults to 0.

    Returns:
        pd.Series: Map element symbols to heatmap values.
    """
    valid_count_modes = list(ElemCountMode.key_val_dict())
    if count_mode not in valid_count_modes:
        raise ValueError(f"Invalid {count_mode=} must be one of {valid_count_modes}")
    # Ensure values is Series if we got dict/list/tuple
    srs = pd.Series(values)

    if is_numeric_dtype(srs):
        pass
    elif is_string_dtype(srs) or {*map(type, srs)} <= {str, Composition}:
        # all items are formula strings or Composition objects
        if count_mode == "occurrence":
            srs = pd.Series(
                itertools.chain.from_iterable(
                    map(str, Composition(comp, allow_negative=True)) for comp in srs
                )
            ).value_counts()
        else:
            attr = (
                "element_composition" if count_mode == Key.composition else count_mode
            )
            srs = pd.DataFrame(
                getattr(Composition(formula, allow_negative=True), attr).as_dict()
                for formula in srs
            ).sum()  # sum up element occurrences
    else:
        raise ValueError(
            "Expected values to be map from element symbols to heatmap values or "
            f"list of compositions (strings or Pymatgen objects), got {values}"
        )

    try:
        # If index consists entirely of strings representing integers, convert to ints
        srs.index = srs.index.astype(int)
    except (ValueError, TypeError):
        pass

    if pd.api.types.is_integer_dtype(srs.index):
        # If index is all integers, assume they represent atomic
        # numbers and map them to element symbols (H: 1, He: 2, ...)
        idx_min, idx_max = srs.index.min(), srs.index.max()
        if idx_max > 118 or idx_min < 1:
            raise ValueError(
                "element value keys were found to be integers and assumed to represent "
                f"atomic numbers, but values range from {idx_min} to {idx_max}, "
                "expected range [1, 118]."
            )
        map_atomic_num_to_elem_symbol = (
            df_ptable.reset_index().set_index("atomic_number").symbol
        )
        srs.index = srs.index.map(map_atomic_num_to_elem_symbol)

    # Ensure all elements are present in returned Series (with value zero if they
    # weren't in values before)
    srs = srs.reindex(df_ptable.index, fill_value=fill_value).rename("count")

    if len(exclude_elements) > 0:
        if isinstance(exclude_elements, str):
            exclude_elements = [exclude_elements]
        if isinstance(exclude_elements, tuple):
            exclude_elements = list(exclude_elements)
        try:
            srs = srs.drop(exclude_elements)
        except KeyError as exc:
            bad_symbols = ", ".join(x for x in exclude_elements if x not in srs)
            raise ValueError(
                f"Unexpected symbol(s) {bad_symbols} in {exclude_elements=}"
            ) from exc

    return srs


class PTableProjector:
    """Project (nest) a custom plot into a periodic table.

    Attributes:
        cmap (Colormap | None): Colormap.
        data (pd.DataFrame): Data for plotting.
        norm (Normalize): Data min-max normalizer.
        hide_f_block (bool): Whether to hide f-block.
        elem_types (set[str]): Types of elements present.
        elem_type_colors (dict[str, str]): Element typed based colors.
        elem_colors (dict): Element based colors.

    Scopes mentioned in this plotter:
        plot: Refers to the global Figure.
        axis: Refers to the Axis where child plotter would plot.
        child: Refers to the child plotter, for example, ax.plot().
    """

    def __init__(
        self,
        *,
        data: SupportedDataType,
        colormap: str | Colormap = "viridis",
        plot_kwargs: dict[str, Any] | None = None,
        hide_f_block: bool | None = None,
        elem_type_colors: dict[str, str] | None = None,
        elem_colors: ElemColors | dict[str, ColorType] | None = None,
    ) -> None:
        """Initialize a ptable projector.

        Default figsize is set to (0.75 * n_groups, 0.75 * n_periods),
        and axes would be turned off by default.

        Args:
            data (SupportedDataType): The data to be visualized.
            colormap (str | Colormap): The colormap to use. Defaults to "viridis".
            plot_kwargs (dict): Additional keyword arguments to
                pass to the plt.subplots function call.
            hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults
                to None, meaning hide if no data provided for f-block elements.
            elem_type_colors (dict | None): Element typed based colors.
            elem_colors (dict | None): Element-specific colors.
        """
        # Set colors
        self.cmap: Colormap = colormap
        self._elem_type_colors = ELEM_TYPE_COLORS | (elem_type_colors or {})
        self.elem_colors = elem_colors  # type: ignore[assignment]

        # Preprocess data
        self.data: pd.DataFrame = data

        self.hide_f_block = hide_f_block  # type: ignore[assignment]

        # Initialize periodic table canvas
        n_periods = df_ptable.row.max()
        n_groups = df_ptable.column.max()

        if self.hide_f_block:
            n_periods -= 3

        # Set figure size
        plot_kwargs = plot_kwargs or {}
        plot_kwargs.setdefault("figsize", (0.75 * n_groups, 0.75 * n_periods))

        self.fig, self.axes = plt.subplots(n_periods, n_groups, **plot_kwargs)

        # Turn off all axes
        for ax in self.axes.flat:
            ax.axis("off")

    @property
    def cmap(self) -> Colormap | None:
        """The periodic table's matplotlib Colormap instance."""
        return self._cmap

    @cmap.setter
    def cmap(self, colormap: str | Colormap | None) -> None:
        """Set the periodic table's matplotlib Colormap instance."""
        self._cmap = None if colormap is None else plt.get_cmap(colormap)

    @property
    def data(self) -> pd.DataFrame:
        """The preprocessed data."""
        return self._data

    @data.setter
    def data(self, data: SupportedDataType) -> None:
        """Set and preprocess the data. Also set normalizer."""
        # Preprocess data
        self._data = preprocess_ptable_data(data)

        # Normalize data for colorbar
        vmin = self._data.attrs["vmin"]
        vmax = self._data.attrs["vmax"]
        self._norm = Normalize(vmin=vmin, vmax=vmax)

    @property
    def norm(self) -> Normalize:
        """Data min-max normalizer."""
        return self._norm

    @property
    def hide_f_block(self) -> bool:
        """Whether to hide f-block in plots."""
        return self._hide_f_block

    @hide_f_block.setter
    def hide_f_block(self, hide_f_block: bool | None) -> None:
        """If hide_f_block is None, would detect if data is present."""
        if hide_f_block is None:
            f_block_elements_with_data = {
                atom_num
                for atom_num in [*range(57, 72), *range(89, 104)]  # rare earths
                # Check if data is present for f-block elements
                if (elem := Element.from_Z(atom_num).symbol) in self.data.index
                and len(self.data.loc[elem, Key.heat_val]) > 0
            }
            self._hide_f_block = not bool(f_block_elements_with_data)

        else:
            self._hide_f_block = hide_f_block

    @property
    def elem_types(self) -> set[str]:
        """Element types present in data."""
        return set(df_ptable.loc[self.data.index, "type"])

    @property
    def elem_type_colors(self) -> dict[str, str]:
        """Element type based colors.

        Example:
            dict(Nonmetal="green", Halogen="teal", Metal="lightblue")
        """
        return self._elem_type_colors or {}

    @elem_type_colors.setter
    def elem_type_colors(self, elem_type_colors: dict[str, str]) -> None:
        self._elem_type_colors |= elem_type_colors or {}

    @property
    def elem_colors(self) -> dict[str, ColorType]:
        """Element-based colors."""
        return self._elem_colors

    @elem_colors.setter
    def elem_colors(
        self,
        elem_colors: ElemColors | dict[str, ColorType] | None = ElemColors.vesta,
    ) -> None:
        """Args:
        elem_colors ("vesta" | "jmol" | dict[str, ColorType]): Use VESTA or Jmol color
            mapping, or a custom {"element", Color} mapping. Defaults to "vesta".
        """
        if elem_colors in ("vesta", None):
            self._elem_colors = ELEM_COLORS_VESTA
        elif elem_colors == "jmol":
            self._elem_colors = ELEM_COLORS_JMOL
        elif isinstance(elem_colors, dict):
            self._elem_colors = elem_colors
        else:
            raise ValueError(
                f"elem_colors must be 'vesta', 'jmol', or a custom dict, "
                f"got {elem_colors=}"
            )

    def get_elem_type_color(
        self,
        elem_symbol: str,
        default: str = "white",
    ) -> str:
        """Get element type color by element symbol."""
        elem_type = df_ptable.loc[elem_symbol].get("type", None)
        return self.elem_type_colors.get(elem_type, default)

    def add_child_plots(
        self,
        child_plotter: Callable[..., None],
        *,
        child_kwargs: dict[str, Any] | None = None,
        tick_kwargs: dict[str, Any] | None = None,
        ax_kwargs: dict[str, Any] | None = None,
        on_empty: Literal["hide", "show"] = "hide",
    ) -> None:
        """Add custom child plots to the periodic table grid.

        Args:
            child_plotter: A callable for the child plotter.
            child_kwargs: Arguments to pass to the child plotter call.
            tick_kwargs: kwargs to pass to ax.tick_params().
            ax_kwargs: Keyword arguments to pass to ax.set().
            on_empty: Whether to "show" or "hide" tiles for elements without data.
        """
        # Update kwargs
        child_kwargs = child_kwargs or {}
        ax_kwargs = ax_kwargs or {}
        tick_kwargs = {"axis": "both", "which": "major", "labelsize": 8} | (
            tick_kwargs or {}
        )

        for element in Element:
            # Hide f-block
            if self.hide_f_block and (element.is_lanthanoid or element.is_actinoid):
                continue

            # Get axis index by element symbol
            symbol: str = element.symbol
            row, column = df_ptable.loc[symbol, ["row", "column"]]
            ax: plt.Axes = self.axes[row - 1][column - 1]

            # Get and check tile data
            try:
                plot_data: np.ndarray | Sequence[float] = self.data.loc[
                    symbol, Key.heat_val
                ]
            except KeyError:  # skip element without data
                plot_data = None

            if (plot_data is None or len(plot_data) == 0) and on_empty == "hide":
                continue

            # Call child plotter
            child_plotter(ax, plot_data, tick_kwargs=tick_kwargs, **child_kwargs)

            # Pass axis kwargs
            if ax_kwargs:
                ax.set(**ax_kwargs)

    def add_elem_symbols(
        self,
        text: str | Callable[[Element], str] = lambda elem: elem.symbol,
        *,
        pos: tuple[float, float] = (0.5, 0.5),
        text_color: str = "black",
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Add element symbols for each tile.

        Args:
            text (str | Callable): The text to add to the tile.
                If a callable, it should accept a pymatgen Element and return a
                string. If a string, it can contain a format
                specifier for an `elem` variable which will be replaced by the element.
            pos (tuple): The position of the text relative to the axes.
            text_color (bool): The color of the text. Defaults to "black".
                Pass "element-types" to color symbol by self.elem_type_colors.
            kwargs (dict): Additional keyword arguments to pass to the `ax.text`.
        """
        # Update symbol kwargs
        kwargs = kwargs or {}
        kwargs.setdefault("fontsize", 12)

        # Add symbol for each element
        for element in Element:
            # Hide f-block
            if self.hide_f_block and (element.is_lanthanoid or element.is_actinoid):
                continue

            # Get axis index by element symbol
            symbol: str = element.symbol
            if symbol not in self.data.index:
                continue

            row, column = df_ptable.loc[symbol, ["row", "column"]]
            ax: plt.Axes = self.axes[row - 1][column - 1]

            content = text(element) if callable(text) else text.format(elem=element)

            elem_type_color = self.get_elem_type_color(symbol, default="black")

            ax.text(
                *pos,
                content,
                color=elem_type_color
                if text_color == ElemColorMode.element_types
                else text_color,
                ha="center",
                va="center",
                transform=ax.transAxes,
                **kwargs,
            )

    def add_colorbar(
        self,
        title: str,
        *,
        coords: tuple[float, float, float, float] = (0.18, 0.8, 0.42, 0.02),
        cbar_kwargs: dict[str, Any] | None = None,
        title_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Add a global colorbar.

        Args:
            title: Title for the colorbar.
            coords: Coordinates of the colorbar (left, bottom, width, height).
                Defaults to (0.18, 0.8, 0.42, 0.02).
            cbar_kwargs: Additional keyword arguments to pass to fig.colorbar().
            title_kwargs: Additional keyword arguments for the colorbar title.
        """
        # Update colorbar kwargs
        cbar_kwargs = {"orientation": "horizontal"} | (cbar_kwargs or {})
        title_kwargs = {"fontsize": 12, "pad": 10, "label": title} | (
            title_kwargs or {}
        )

        # Check colormap
        if self.cmap is None:
            raise ValueError("Cannot add colorbar without a colormap.")

        # Add colorbar
        cbar_ax = self.fig.add_axes(coords)

        self.fig.colorbar(
            plt.cm.ScalarMappable(norm=self._norm, cmap=self.cmap),
            cax=cbar_ax,
            **cbar_kwargs,
        )

        # Set colorbar title
        cbar_ax.set_title(**title_kwargs)

    def add_elem_type_legend(
        self,
        kwargs: dict[str, Any] | None,
    ) -> None:
        """Add a legend to show the colors based on element types.

        Args:
            kwargs (dict | None): Keyword arguments passed to
                plt.legend() for customizing legend appearance.
        """
        kwargs = kwargs or {}

        font_size = 10

        # Get present elements
        legend_elements = [
            plt.Line2D(
                *([0], [0]),
                marker="s",
                color="w",
                label=elem_class,
                markerfacecolor=color,
                markersize=1.2 * font_size,
            )
            for elem_class, color in self.elem_type_colors.items()
            if elem_class in self.elem_types
        ]

        default_legend_kwargs = dict(
            loc="center left",
            bbox_to_anchor=(0, -42),
            ncol=6,
            frameon=False,
            fontsize=font_size,
            handlelength=1,  # more compact legend
        )
        kwargs = default_legend_kwargs | kwargs

        plt.legend(handles=legend_elements, **kwargs)

    def set_elem_background_color(self, alpha: float = 0.1) -> None:
        """Set element tile background color by element type.

        Args:
            alpha (float): transparency
        """
        for element in Element:
            # Hide f-block
            if self.hide_f_block and (element.is_lanthanoid or element.is_actinoid):
                continue

            # Get element axis
            symbol = element.symbol
            row, column = df_ptable.loc[symbol, ["row", "column"]]
            ax: plt.Axes = self.axes[row - 1][column - 1]

            # Set background color by element type
            rgb = mpl.colors.to_rgb(self.get_elem_type_color(symbol))
            ax.set_facecolor((*rgb, alpha))


class ChildPlotters:
    """Child plotters for PTableProjector with methods to make different types
    (line/scatter/histogram) for individual element tiles.
    """

    @staticmethod
    def rectangle(
        ax: plt.axes,
        data: SupportedValueType,
        norm: Normalize,
        cmap: Colormap,
        start_angle: float,
        tick_kwargs: dict[str, Any],  # noqa: ARG004
    ) -> None:
        """Rectangle heatmap plotter. Could be evenly split,
        depending on the length of the data (could mix and match).

        Args:
            ax (plt.axes): The axis to plot on.
            data (SupportedValueType): The values for the child plotter.
            norm (Normalize): Normalizer for data-color mapping.
            cmap (Colormap): Colormap used for value mapping.
            start_angle (float): The starting angle for the splits in degrees,
                and the split proceeds counter-clockwise (0 refers to the x-axis).
            tick_kwargs: For compatibility with other plotters.
        """
        # Map values to colors
        if isinstance(data, (Sequence, np.ndarray)):
            colors = [cmap(norm(value)) for value in data]
        else:
            raise TypeError("Unsupported data type.")

        # Add the pie chart
        ax.pie(
            np.ones(len(colors)),
            colors=colors,
            startangle=start_angle,
            wedgeprops=dict(clip_on=True),
        )

        # Crop the central rectangle from the pie chart
        rect = Rectangle((-0.5, -0.5), 1, 1, fc="none", ec="none")
        ax.set_clip_path(rect)

        ax.set(xlim=(-0.5, 0.5), ylim=(-0.5, 0.5))

    @staticmethod
    def scatter(
        ax: plt.axes,
        data: SupportedValueType,
        norm: Normalize,
        cmap: Colormap,
        tick_kwargs: dict[str, Any],
        **child_kwargs: Any,
    ) -> None:
        """Scatter plotter.

        Args:
            ax (plt.axes): The axis to plot on.
            data (SupportedValueType): The values for the child plotter.
            norm (Normalize): Normalizer for data-color mapping.
            cmap (Colormap): Colormap.
            child_kwargs (dict): kwargs to pass to the child plotter call.
            tick_kwargs (dict): kwargs to pass to ax.tick_params().
        """
        # Add scatter
        if len(data) == 2:
            ax.scatter(x=data[0], y=data[1], **child_kwargs)
        elif len(data) == 3:
            ax.scatter(x=data[0], y=data[1], c=cmap(norm(data[2])), **child_kwargs)

        # Set tick labels
        ax.tick_params(**tick_kwargs)

        # Hide the right and top spines
        ax.axis("on")  # turned off by default
        ax.spines[["right", "top"]].set_visible(False)

    @staticmethod
    def line(
        ax: plt.axes,
        data: SupportedValueType,
        tick_kwargs: dict[str, Any],
        **child_kwargs: Any,
    ) -> None:
        """Line plotter.

        Args:
            ax (plt.axes): The axis to plot on.
            data (SupportedValueType): The values for the child plotter.
            child_kwargs (dict): kwargs to pass to the child plotter call.
            tick_kwargs (dict): kwargs to pass to ax.tick_params().
        """
        # Add line
        ax.plot(data[0], data[1], **child_kwargs)

        # Set tick labels
        ax.tick_params(**tick_kwargs)

        # Hide the right and top spines
        ax.axis("on")  # turned off by default
        ax.spines[["right", "top"]].set_visible(False)

    @staticmethod
    def histogram(
        ax: plt.axes,
        data: SupportedValueType,
        cmap: Colormap,
        cbar_axis: Literal["x", "y"],
        tick_kwargs: dict[str, Any],
        **child_kwargs: Any,
    ) -> None:
        """Histogram plotter.

        Taken from https://stackoverflow.com/questions/23061657/
        plot-histogram-with-colors-taken-from-colormap

        Args:
            ax (plt.axes): The axis to plot on.
            data (SupportedValueType): The values for the child plotter.
            cmap (Colormap): Colormap.
            cbar_axis ("x" | "y"): The axis colormap
                would be based on.
            child_kwargs: kwargs to pass to the child plotter call.
            tick_kwargs (dict): kwargs to pass to ax.tick_params().
        """
        # Preprocess x_range if only one boundary is given
        x_range = child_kwargs.pop("range", None)

        if x_range is None or x_range == [None, None]:
            x_range = [np.min(data), np.max(data)]

        elif x_range[0] is None:
            x_range = [np.min(data), x_range[1]]

        else:
            x_range = [x_range[0], np.max(data)]

        # Add histogram
        n, bins, patches = ax.hist(data, range=x_range, **child_kwargs)

        # Scale values according to axis
        if cbar_axis == "x":
            bin_centers = 0.5 * (bins[:-1] + bins[1:])

            cols = bin_centers - min(bin_centers)
            cols /= max(cols)

        else:
            cols = (n - n.min()) / (n.max() - n.min())

        # Apply colors
        for col, patch in zip(cols, patches):
            plt.setp(patch, "facecolor", cmap(col))

        # Set tick labels
        ax.tick_params(**tick_kwargs)
        ax.set(yticklabels=(), yticks=())
        # Set x-ticks to min/max only
        ax.set(xticks=[math.floor(x_range[0]), math.ceil(x_range[1])])

        # Hide the right, left and top spines
        ax.axis("on")
        ax.spines[["right", "top", "left"]].set_visible(False)


def ptable_heatmap(
    values: ElemValues,
    *,
    log: bool | Normalize = False,
    ax: plt.Axes | None = None,
    count_mode: ElemCountMode = ElemCountMode.composition,
    cbar_title: str = "Element Count",
    cbar_range: tuple[float | None, float | None] | None = None,
    cbar_coords: tuple[float, float, float, float] = (0.18, 0.8, 0.42, 0.05),
    cbar_kwargs: dict[str, Any] | None = None,
    colorscale: str = "viridis",
    show_scale: bool = True,
    show_values: bool = True,
    infty_color: str = "lightskyblue",
    na_color: str = "white",
    heat_mode: Literal["value", "fraction", "percent"] | None = "value",
    fmt: str | Callable[..., str] | None = None,
    cbar_fmt: str | Callable[..., str] | None = None,
    text_color: str | tuple[str, str] = "auto",
    exclude_elements: Sequence[str] = (),
    zero_color: str = "#eff",  # light gray
    zero_symbol: str | float = "-",
    text_style: dict[str, Any] | None = None,
    label_font_size: int = 16,
    value_font_size: int = 12,
    tile_size: float | tuple[float, float] = 0.9,
    f_block_voffset: float = 0.5,
    hide_f_block: bool | None = None,
    **kwargs: Any,
) -> plt.Axes:
    """Plot a heatmap across the periodic table of elements.

    Args:
        values (dict[str, int | float] | pd.Series | list[str]): Map from element
            symbols to heatmap values or iterable of composition strings/objects.
        log (bool | Normalize, optional): Whether colormap scale is log or linear. Can
            also take any matplotlib.colors.Normalize subclass such as SymLogNorm as
            custom color scale. Defaults to False.
        ax (Axes, optional): matplotlib Axes on which to plot. Defaults to None.
        count_mode ("composition" | "fractional_composition" | "reduced_composition"):
            Reduce or normalize compositions before counting. See count_elements() for
            details. Only used when values is list of composition strings/objects.
        cbar_title (str, optional): Color bar title. Defaults to "Element Count".
        cbar_range (tuple[float | None, float | None], optional): Color bar range.
            Can be used e.g. to create multiple plots with identical color bars for
            visual comparison. Defaults to automatic based on data range.
        cbar_coords (tuple[float, float, float, float], optional): Color bar position
            and size: [x, y, width, height] anchored at lower left corner. Defaults to
            (0.18, 0.8, 0.42, 0.05).
        cbar_kwargs (dict[str, Any], optional): Additional keyword arguments passed to
            fig.colorbar(). Defaults to None.
        colorscale (str, optional): Matplotlib colormap name to use. Defaults to
            "viridis". See https://matplotlib.org/stable/users/explain/colors/colormaps
            for available options.
        show_scale (bool, optional): Whether to show the color bar. Defaults to True.
        show_values (bool, optional): Whether to show the heatmap values in each tile.
            Defaults to True.
        infty_color: Color to use for elements with value infinity. Defaults to
            "lightskyblue".
        na_color: Color to use for elements with value infinity. Defaults to "white".
        heat_mode ("value" | "fraction" | "percent" | None): Whether to display heat
            values as is, normalized as a fraction of the total, as percentages
            or not at all (None). Defaults to "value".
            "fraction" and "percent" can be used to make the colors in different
            ptable_heatmap() (and ptable_heatmap_ratio()) plots comparable.
        fmt (str): f-string format option for tile values. Defaults to ".1%"
            (1 decimal place) if heat_mode="percent" else ".3g". Use e.g. ",.0f" to
            format values with thousands separators and no decimal places.
        cbar_fmt (str): f-string format option to set a different color bar tick
            label format. Defaults to the above fmt.
        text_color (str | tuple[str, str]): What color to use for element symbols and
            heat labels. Must be a valid color name, or a 2-tuple of names, one to use
            for the upper half of the color scale, one for the lower half. The special
            value "auto" applies "black" on the lower and "white" on the upper half of
            the color scale. "auto_reverse" does the opposite. Defaults to "auto".
        exclude_elements (list[str]): Elements to exclude from the heatmap. E.g. if
            oxygen overpowers everything, you can try log=True or
            exclude_elements=["O"]. Defaults to ().
        zero_color (str): Hex color or recognized matplotlib color name to use for
            elements with value zero. Defaults to "#eff" (light gray).
        zero_symbol (str | float): Symbol to use for elements with value zero.
            Defaults to "-".
        text_style (dict[str, Any]): Additional keyword arguments passed to
            ax.text(). Defaults to dict(
                ha="center", fontsize=label_font_size, fontweight="semibold"
            )
        label_font_size (int): Font size for element symbols. Defaults to 16.
        value_font_size (int): Font size for heat values. Defaults to 12.
        tile_size (float | tuple[float, float]): Size of each tile in the periodic
            table as a fraction of available space before touching neighboring tiles.
            1 or (1, 1) means no gaps between tiles. Defaults to 0.9.
        cbar_coords (tuple[float, float, float, float]): Color bar position and size:
            [x, y, width, height] anchored at lower left corner of the bar. Defaults to
            (0.18, 0.8, 0.42, 0.05).
        f_block_voffset (float): Vertical offset for lanthanides and actinides
            (row 6 and 7) from the rest of the periodic table. Defaults to 0.5.
        hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults to
            None, meaning hide if no data is provided for f-block elements.
        **kwargs: Additional keyword arguments passed to plt.figure().

    Returns:
        plt.Axes: matplotlib Axes with the heatmap.
    """

    def tick_fmt(val: float, _pos: int) -> str:
        # val: value at color axis tick (e.g. 10.0, 20.0, ...)
        # pos: zero-based tick counter (e.g. 0, 1, 2, ...)
        default_fmt = (
            ".0%" if heat_mode == "percent" else (".0f" if val < 1e4 else ".2g")
        )
        return f"{val:{cbar_fmt or fmt or default_fmt}}"

    if fmt is None:
        fmt = partial(si_fmt, fmt=".1%" if heat_mode == "percent" else ".0f")
    if cbar_fmt is None:
        cbar_fmt = fmt

    valid_logs = (bool, Normalize)
    if not isinstance(log, valid_logs):
        raise TypeError(f"Invalid {log=}, must be instance of {valid_logs}")

    if log and heat_mode in ("fraction", "percent"):
        raise ValueError(
            "Combining log color scale and heat_mode='fraction'/'percent' unsupported"
        )
    if "cmap" in kwargs:
        colorscale = kwargs.pop("cmap")
        warnings.warn(
            "cmap argument is deprecated, use colorscale instead",
            category=DeprecationWarning,
            stacklevel=2,
        )

    values = count_elements(values, count_mode, exclude_elements)

    # replace positive and negative infinities with NaN values, then drop all NaNs
    clean_vals = values.replace([np.inf, -np.inf], np.nan).dropna()

    if heat_mode in ("fraction", "percent"):
        # ignore inf values in sum() else all would be set to 0 by normalizing
        values /= clean_vals.sum()
        clean_vals /= clean_vals.sum()  # normalize as well for norm.autoscale() below

    color_map = plt.get_cmap(colorscale)

    n_rows = df_ptable.row.max()
    n_columns = df_ptable.column.max()

    # TODO can we pass as a kwarg and still ensure aspect ratio respected?
    fig = plt.figure(figsize=(0.75 * n_columns, 0.7 * n_rows), **kwargs)

    ax = ax or plt.gca()

    if isinstance(tile_size, (float, int)):
        tile_width = tile_height = tile_size
    else:
        tile_width, tile_height = tile_size

    norm_map = {True: LogNorm(), False: Normalize()}
    norm = norm_map.get(log, log)

    norm.autoscale(clean_vals.to_numpy())
    if cbar_range is not None and len(cbar_range) == 2:
        if cbar_range[0] is not None:
            norm.vmin = cbar_range[0]
        if cbar_range[1] is not None:
            norm.vmax = cbar_range[1]

    text_style = dict(
        horizontalalignment="center", fontsize=label_font_size, fontweight="semibold"
    ) | (text_style or {})

    for symbol, row, column, *_ in df_ptable.itertuples():
        if hide_f_block and (row in (6, 7)):
            continue

        period = n_rows - row  # invert row count to make periodic table right side up
        tile_value = values.get(symbol)

        # inf (float/0) or NaN (0/0) are expected when passing in values from
        # ptable_heatmap_ratio
        if symbol in exclude_elements:
            color = "white"
            label = "excl."
        elif tile_value == np.inf:
            color = infty_color  # not in denominator
            label = r"$\infty$"
        elif pd.isna(tile_value):
            color = na_color  # neither numerator nor denominator
            label = r"$0\,/\,0$"
        elif tile_value == 0:
            color = zero_color
            label = str(zero_symbol)
        else:
            color = color_map(norm(tile_value))

            if callable(fmt):
                label = fmt(tile_value)

            elif heat_mode == "percent":
                label = f"{tile_value:{fmt or '.1f'}}"
            else:
                fmt = fmt or (".0f" if tile_value > 100 else ".1f")
                label = f"{tile_value:{fmt}}"
            # replace shortens scientific notation 1e+01 to 1e1 so it fits inside cells
            label = label.replace("e+0", "e")
        if period < 3:  # vertical offset for lanthanides + actinides
            period += f_block_voffset
        rect = Rectangle(
            (column, period), tile_width, tile_height, edgecolor="gray", facecolor=color
        )

        if heat_mode is None or not show_values:
            # no value to display below in colored rectangle so center element symbol
            text_style.setdefault("verticalalignment", "center")

        if symbol in exclude_elements:
            _text_color = "black"
        elif text_color == "auto":
            if isinstance(color, (tuple, list)) and len(color) >= 3:
                # treat color as RGB tuple and choose black or white text for contrast
                _text_color = pick_bw_for_contrast(color)
            else:
                _text_color = "black"
        elif isinstance(text_color, (tuple, list)):
            _text_color = text_color[0] if norm(tile_value) > 0.5 else text_color[1]
        else:
            _text_color = text_color

        symbol_text = ax.text(
            column + 0.5 * tile_width,
            # 0.45 needed to achieve vertical centering, not sure why 0.5 is off
            period + (0.5 if show_values else 0.45) * tile_height,
            symbol,
            **{"color": _text_color} | text_style,
        )

        if heat_mode is not None and show_values:
            ax.text(
                column + 0.5 * tile_width,
                period + 0.1 * tile_height,
                label,
                fontsize=value_font_size,
                horizontalalignment="center",
                color=symbol_text.get_color(),
            )

        ax.add_patch(rect)

    if heat_mode is not None and show_scale:
        # colorbar position and size: [x, y, width, height]
        # anchored at lower left corner
        cbar_kwargs = cbar_kwargs or {}
        cbar_ax = cbar_kwargs.pop("cax", None) or ax.inset_axes(
            cbar_coords, transform=ax.transAxes
        )
        # format major and minor ticks
        # TODO maybe give user direct control over labelsize, instead of hard-coding
        # 8pt smaller than default
        cbar_ax.tick_params(which="both", labelsize=text_style["fontsize"])

        mappable = plt.cm.ScalarMappable(norm=norm, cmap=colorscale)

        if callable(cbar_fmt):
            # 2nd _pos arg is always passed by matplotlib but we don't need it
            tick_fmt = lambda val, _pos: cbar_fmt(val)

        cbar = fig.colorbar(
            mappable,
            cax=cbar_ax,
            orientation=cbar_kwargs.pop("orientation", "horizontal"),
            format=cbar_kwargs.pop("format", tick_fmt),
            **cbar_kwargs,
        )

        cbar.outline.set_linewidth(1)
        if text_style.get("color") == "white":
            text_style.pop("color")  # don't want to apply possibly 'white' default
            # text color (depending on colorscale) to color bar with white background
        cbar_ax.set_title(cbar_title, pad=10, **text_style)

    plt.ylim(0.3, n_rows + 0.1)
    plt.xlim(0.9, n_columns + 1)

    plt.axis("off")
    return ax


def ptable_heatmap_splits(
    data: pd.DataFrame | pd.Series | dict[str, list[list[float]]],
    *,
    # Heatmap-split specific
    start_angle: float = 135,
    # Figure-scope
    colormap: str | Colormap = "viridis",
    on_empty: Literal["hide", "show"] = "hide",
    hide_f_block: bool | None = None,
    plot_kwargs: dict[str, Any]
    | Callable[[Sequence[float]], dict[str, Any]]
    | None = None,
    # Axis-scope
    ax_kwargs: dict[str, Any] | None = None,
    # Symbol
    symbol_text: str | Callable[[Element], str] = lambda elem: elem.symbol,
    symbol_pos: tuple[float, float] = (0.5, 0.5),
    symbol_kwargs: dict[str, Any] | None = None,
    # Colorbar
    cbar_title: str = "Values",
    cbar_title_kwargs: dict[str, Any] | None = None,
    cbar_coords: tuple[float, float, float, float] = (0.18, 0.8, 0.42, 0.02),
    cbar_kwargs: dict[str, Any] | None = None,
) -> plt.Figure:
    """Plot evenly-split heatmaps, nested inside a periodic table.

    Args:
        data (pd.DataFrame | pd.Series | dict[str, list[list[float]]]):
            Map from element symbols to plot data. E.g. if dict,
            {"Fe": [1, 2], "Co": [3, 4]}, where the 1st value would
            be plotted on the lower-left corner and the 2nd on the upper-right.
            If pd.Series, index is element symbols and values lists.
            If pd.DataFrame, column names are element symbols,
            plots are created from each column.
        start_angle (float): The starting angle for the splits in degrees,
                and the split proceeds counter-clockwise (0 refers to
                the x-axis). Defaults to 135 degrees.
        colormap (str): Matplotlib colormap name to use.
        on_empty ("hide" | "show"): Whether to show or hide tiles for elements without
            data. Defaults to "hide".
        hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults to
            None, meaning hide if no data is provided for f-block elements.
        plot_kwargs (dict): Additional keyword arguments to
                pass to the plt.subplots function call.
        ax_kwargs (dict): Keyword arguments passed to ax.set() for each plot.
            Use to set x/y labels, limits, etc. Defaults to None. Example:
            dict(title="Periodic Table", xlabel="x-axis", ylabel="y-axis", xlim=(0, 10),
            ylim=(0, 10), xscale="linear", yscale="log"). See ax.set() docs for options:
            https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.set.html#matplotlib-axes-axes-set
        symbol_text (str | Callable[[Element], str]): Text to display for
            each element symbol. Defaults to lambda elem: elem.symbol.
        symbol_pos (tuple[float, float]): Position of element symbols
            relative to the lower left corner of each tile.
            Defaults to (0.5, 0.5). (1, 1) is the upper right corner.
        symbol_kwargs (dict): Keyword arguments passed to ax.text() for
            element symbols. Defaults to None.

        cbar_title (str): Colorbar title. Defaults to "Values".
        cbar_title_kwargs (dict): Keyword arguments passed to
            cbar.ax.set_title(). Defaults to dict(fontsize=12, pad=10).
        cbar_coords (tuple[float, float, float, float]): Colorbar
            position and size: [x, y, width, height] anchored at lower left
            corner of the bar. Defaults to (0.25, 0.77, 0.35, 0.02).
        cbar_kwargs (dict): Keyword arguments passed to fig.colorbar().

    Notes:
        Default figsize is set to (0.75 * n_groups, 0.75 * n_periods).

    Returns:
        plt.Figure: periodic table with a subplot in each element tile.
    """
    # Initialize periodic table plotter
    projector = PTableProjector(
        data=data,
        colormap=colormap,
        plot_kwargs=plot_kwargs,  # type: ignore[arg-type]
        hide_f_block=hide_f_block,
    )

    # Call child plotter: evenly split rectangle
    child_kwargs = {
        "start_angle": start_angle,
        "cmap": projector.cmap,
        "norm": projector.norm,
    }

    projector.add_child_plots(
        ChildPlotters.rectangle,
        child_kwargs=child_kwargs,
        ax_kwargs=ax_kwargs,
        on_empty=on_empty,
    )

    # Add element symbols
    projector.add_elem_symbols(
        text=symbol_text,
        pos=symbol_pos,
        kwargs=symbol_kwargs,
    )

    # Add colorbar
    projector.add_colorbar(
        title=cbar_title,
        coords=cbar_coords,
        cbar_kwargs=cbar_kwargs,
        title_kwargs=cbar_title_kwargs,
    )

    return projector.fig


def ptable_heatmap_ratio(
    values_num: ElemValues,
    values_denom: ElemValues,
    *,
    count_mode: ElemCountMode = ElemCountMode.composition,
    normalize: bool = False,
    cbar_title: str = "Element Ratio",
    not_in_numerator: tuple[str, str] | None = ("#eff", "gray: not in 1st list"),
    not_in_denominator: tuple[str, str] | None = (
        "lightskyblue",
        "blue: not in 2nd list",
    ),
    not_in_either: tuple[str, str] | None = ("white", "white: not in either"),
    **kwargs: Any,
) -> plt.Axes:
    """Display the ratio of two maps from element symbols to heat values or of two sets
    of compositions.

    Args:
        values_num (dict[str, int | float] | pd.Series | list[str]): Map from
            element symbols to heatmap values or iterable of composition strings/objects
            in the numerator.
        values_denom (dict[str, int | float] | pd.Series | list[str]): Map from
            element symbols to heatmap values or iterable of composition strings/objects
            in the denominator.
        normalize (bool): Whether to normalize heatmap values so they sum to 1. Makes
            different ptable_heatmap_ratio plots comparable. Defaults to False.
        count_mode ("composition" | "fractional_composition" | "reduced_composition"):
            Reduce or normalize compositions before counting. See count_elements() for
            details. Only used when values is list of composition strings/objects.
        cbar_title (str): Title for the colorbar. Defaults to "Element Ratio".
        not_in_numerator (tuple[str, str]): Color and legend description used for
            elements missing from numerator. Defaults to
            ("#eff", "gray: not in 1st list").
        not_in_denominator (tuple[str, str]): See not_in_numerator. Defaults to
            ("lightskyblue", "blue: not in 2nd list").
        not_in_either (tuple[str, str]): See not_in_numerator. Defaults to
            ("white", "white: not in either").
        **kwargs: Additional keyword arguments passed to ptable_heatmap().

    Returns:
        plt.Axes: matplotlib Axes object
    """
    values_num = count_elements(values_num, count_mode)

    values_denom = count_elements(values_denom, count_mode)

    values = values_num / values_denom

    if normalize:
        values /= values.sum()

    ax = ptable_heatmap(values, cbar_title=cbar_title, **kwargs)

    # add legend handles
    for tup in (
        (0.18, "zero", *(not_in_numerator or ())),
        (0.12, "infty", *(not_in_denominator or ())),
        (0.06, "na", *(not_in_either or ())),
    ):
        if len(tup) < 3:
            continue
        y_pos, key, color, txt = tup
        kwargs[f"{key}_color"] = color
        bbox = dict(facecolor=color, edgecolor="gray")
        ax.text(0.005, y_pos, txt, fontsize=10, bbox=bbox, transform=ax.transAxes)

    return ax


def ptable_heatmap_plotly(
    values: ElemValues,
    *,
    count_mode: ElemCountMode = ElemCountMode.composition,
    colorscale: str | Sequence[str] | Sequence[tuple[float, str]] = "viridis",
    show_scale: bool = True,
    show_values: bool = True,
    heat_mode: Literal["value", "fraction", "percent"] | None = "value",
    fmt: str | None = None,
    hover_props: Sequence[str] | dict[str, str] | None = None,
    hover_data: dict[str, str | int | float] | pd.Series | None = None,
    font_colors: Sequence[str] = (),
    gap: float = 5,
    font_size: int | None = None,
    bg_color: str | None = None,
    color_bar: dict[str, Any] | None = None,
    cscale_range: tuple[float | None, float | None] = (None, None),
    exclude_elements: Sequence[str] = (),
    log: bool = False,
    fill_value: float | None = None,
    label_map: dict[str, str] | Callable[[str], str] | Literal[False] | None = None,
    **kwargs: Any,
) -> go.Figure:
    """Create a Plotly figure with an interactive heatmap of the periodic table.
    Supports hover tooltips with custom data or atomic reference data like
    electronegativity, atomic_radius, etc. See kwargs hover_data and hover_props, resp.

    Args:
        values (dict[str, int | float] | pd.Series | list[str]): Map from element
            symbols to heatmap values e.g. dict(Fe=2, O=3) or iterable of composition
            strings or Pymatgen composition objects.
        count_mode ("composition" | "fractional_composition" | "reduced_composition"):
            Reduce or normalize compositions before counting. See count_elements() for
            details. Only used when values is list of composition strings/objects.
        colorscale (str | list[str] | list[tuple[float, str]]): Color scale for heatmap.
            Defaults to "viridis". See plotly.com/python/builtin-colorscales for names
            of other builtin color scales. Note "YlGn" and px.colors.sequential.YlGn are
            equivalent. Custom scales are specified as ["blue", "red"] or
            [[0, "rgb(0,0,255)"], [0.5, "rgb(0,255,0)"], [1, "rgb(255,0,0)"]].
        show_scale (bool): Whether to show a bar for the color scale. Defaults to True.
        show_values (bool): Whether to show numbers on heatmap tiles. Defaults to True.
        heat_mode ("value" | "fraction" | "percent" | None): Whether to display heat
            values as is (value), normalized as a fraction of the total, as percentages
            or not at all (None). Defaults to "value".
            "fraction" and "percent" can be used to make the colors in different
            periodic table heatmap plots comparable.
        fmt (str): f-string format option for heat labels. Defaults to ".1%"
            (1 decimal place) if heat_mode="percent" else ".3g".
        hover_props (list[str] | dict[str, str]): Elemental properties to display in the
            hover tooltip. Can be a list of property names to display only the values
            themselves or a dict mapping names to what they should display as. E.g.
            dict(atomic_mass="atomic weight") will display as `"atomic weight = {x}"`.
            Defaults to None.
            Available properties are: symbol, row, column, name,
            atomic_number, atomic_mass, n_neutrons, n_protons, n_electrons, period,
            group, phase, radioactive, natural, metal, nonmetal, metalloid, type,
            atomic_radius, electronegativity, first_ionization, density, melting_point,
            boiling_point, number_of_isotopes, discoverer, year, specific_heat,
            n_shells, n_valence.
        hover_data (dict[str, str | int | float] | pd.Series): Map from element symbols
            to additional data to display in the hover tooltip. dict(Fe="this appears in
            the hover tooltip on a new line below the element name"). Defaults to None.
        font_colors (list[str]): One color name or two for [min_color, max_color].
            min_color is applied to annotations with heatmap values less than
            (max_val - min_val) / 2. Defaults to None, meaning auto-set to maximize
            contrast with color scale: white text for dark background and vice versa.
            swapped depending on the colorscale.
        gap (float): Gap in pixels between tiles of the periodic table. Defaults to 5.
        font_size (int): Element symbol and heat label text size. Any valid CSS size
            allowed. Defaults to automatic font size based on plot size. Element symbols
            will be bold and 1.5x this size.
        bg_color (str): Plot background color. Defaults to "rgba(0, 0, 0, 0)".
        color_bar (dict[str, Any]): Plotly colorbar properties documented at
            https://plotly.com/python/reference#heatmap-colorbar. Defaults to
            dict(orientation="h"). Commonly used keys are:
            - title: colorbar title
            - titleside: "top" | "bottom" | "right" | "left"
            - tickmode: "array" | "auto" | "linear" | "log" | "date" | "category"
            - tickvals: list of tick values
            - ticktext: list of tick labels
            - tickformat: f-string format option for tick labels
            - len: fraction of plot height or width depending on orientation
            - thickness: fraction of plot height or width depending on orientation
        cscale_range (tuple[float | None, float | None]): Colorbar range. Defaults to
            (None, None) meaning the range is automatically determined from the data.
        exclude_elements (list[str]): Elements to exclude from the heatmap. E.g. if
            oxygen overpowers everything, you can do exclude_elements=["O"].
            Defaults to ().
        log (bool): Whether to use a logarithmic color scale. Defaults to False.
            Piece of advice: colorscale="viridis" and log=True go well together.
        fill_value (float | None): Value to fill in for missing elements. Defaults to 0.
        label_map (dict[str, str] | Callable[[str], str] | None): Map heat values (after
            string formatting) to target strings. Set to False to disable. Defaults to
            dict.fromkeys((np.nan, None, "nan"), " ") so as not to display "nan" for
            missing values.
        **kwargs: Additional keyword arguments passed to
            plotly.figure_factory.create_annotated_heatmap().

    Returns:
        Figure: Plotly Figure object.
    """
    if log and heat_mode in ("fraction", "percent"):
        raise ValueError(
            "Combining log color scale and heat_mode='fraction'/'percent' unsupported"
        )
    if len(cscale_range) != 2:
        raise ValueError(f"{cscale_range=} should have length 2")

    if isinstance(colorscale, (str, type(None))):
        colorscale = px.colors.get_colorscale(colorscale or "viridis")
    elif not isinstance(colorscale, Sequence) or not isinstance(
        colorscale[0], (str, list, tuple)
    ):
        raise TypeError(
            f"{colorscale=} should be string, list of strings or list of "
            "tuples(float, str)"
        )

    color_bar = color_bar or {}
    color_bar.setdefault("orientation", "h")
    # if values is a series with a name, use it as the colorbar title
    if isinstance(values, pd.Series) and values.name:
        color_bar.setdefault("title", values.name)

    values = count_elements(values, count_mode, exclude_elements, fill_value)

    if heat_mode in ("fraction", "percent"):
        # normalize heat values
        clean_vals = values.replace([np.inf, -np.inf], np.nan).dropna()
        # ignore inf values in sum() else all would be set to 0 by normalizing
        heat_value_element_map = values / clean_vals.sum()
    else:
        heat_value_element_map = values

    n_rows, n_columns = 10, 18
    # initialize tile text and hover tooltips to empty strings
    tile_texts, hover_texts = np.full([2, n_rows, n_columns], "", dtype=object)
    heatmap_values = np.full([n_rows, n_columns], np.nan)

    if label_map is None:
        # default to space string for None, np.nan and "nan". space is needed
        # for <br> in tile_text to work so all element symbols are vertically aligned
        label_map = dict.fromkeys([np.nan, None, "nan"], " ")  # type: ignore[list-item]

    for symbol, period, group, name, *_ in df_ptable.itertuples():
        # build table from bottom up so that period 1 becomes top row
        row = n_rows - period
        col = group - 1

        label = ""  # label (if not None) is placed below the element symbol
        if show_values:
            if symbol in exclude_elements:
                label = "excl."
            elif heat_value := heat_value_element_map.get(symbol):
                if heat_mode == "percent":
                    label = f"{heat_value:{fmt or '.1%'}}"
                else:
                    default_prec = ".1f" if heat_value < 100 else ",.0f"
                    if heat_value > 1e5:
                        default_prec = ".2g"
                    label = f"{heat_value:{fmt or default_prec}}".replace("e+0", "e")

            if callable(label_map):
                label = label_map(label)
            elif isinstance(label_map, dict):
                label = label_map.get(label, label)
        style = f"font-weight: bold; font-size: {1.5 * (font_size or 12)};"
        tile_text = (
            f"<span {style=}>{symbol}</span>{f'<br>{label}' if show_values else ''}"
        )

        tile_texts[row][col] = tile_text

        hover_text = name

        if hover_data is not None and symbol in hover_data:
            hover_text += f"<br>{hover_data[symbol]}"

        if hover_props is not None:
            if unsupported_keys := set(hover_props) - set(df_ptable):
                raise ValueError(
                    f"Unsupported hover_props: {', '.join(unsupported_keys)}. Available"
                    f" keys are: {', '.join(df_ptable)}.\nNote that some keys have "
                    "missing values."
                )
            df_row = df_ptable.loc[symbol]
            if isinstance(hover_props, dict):
                for col_name, col_label in hover_props.items():
                    hover_text += f"<br>{col_label} = {df_row[col_name]}"
            elif isinstance(hover_props, (list, tuple)):
                hover_text += "<br>" + "<br>".join(
                    f"{col_name} = {df_row[col_name]}" for col_name in hover_props
                )
            else:
                raise ValueError(
                    f"hover_props must be dict or sequence of str, got {hover_props}"
                )

        hover_texts[row][col] = hover_text

        # TODO maybe there's a more elegant way to handle excluded elements?
        if symbol in exclude_elements:
            continue

        color_val = heat_value_element_map[symbol]
        if log and color_val > 0:
            color_val = np.log10(color_val)
        # until https://github.com/plotly/plotly.js/issues/975 is resolved, we need to
        # insert transparency (rgba0) at low end of colorscale (+1e-6) to not show any
        # colors on empty tiles of the periodic table
        heatmap_values[row][col] = color_val

    if isinstance(font_colors, str):
        font_colors = [font_colors]
    if cscale_range == (None, None):
        cscale_range = (values.min(), values.max())

    fig = ff.create_annotated_heatmap(
        heatmap_values,
        annotation_text=tile_texts,
        text=hover_texts,
        showscale=show_scale,
        colorscale=colorscale,
        font_colors=font_colors or None,
        hoverinfo="text",
        xgap=gap,
        ygap=gap,
        zmin=None if log else cscale_range[0],
        zmax=None if log else cscale_range[1],
        # zauto=False if cscale_range is set, needed for zmin, zmax to work
        # see https://github.com/plotly/plotly.py/issues/193
        zauto=cscale_range == (None, None),
        **kwargs,
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10, pad=10),
        paper_bgcolor=bg_color,
        plot_bgcolor="rgba(0, 0, 0, 0)",
        xaxis=dict(zeroline=False, showgrid=False),
        yaxis=dict(zeroline=False, showgrid=False, scaleanchor="x"),
        font_size=font_size,
        width=1000,
        height=500,
    )

    if color_bar.get("orientation") == "h":
        dct = dict(x=0.4, y=0.75, titleside="top", len=0.4)
        color_bar = {**dct, **color_bar}
    else:  # make title vertical
        dct = dict(titleside="right", len=0.87)
        color_bar = {**dct, **color_bar}
        if title := color_bar.get("title"):
            # <br><br> to increase title offset
            color_bar["title"] = f"<br><br>{title}"

    fig.update_traces(colorbar=dict(lenmode="fraction", thickness=15, **color_bar))
    return fig


def ptable_hists(
    data: pd.DataFrame | pd.Series | dict[str, list[float]],
    *,
    # Histogram-specific
    bins: int = 20,
    x_range: tuple[float | None, float | None] | None = None,
    log: bool = False,
    # Figure-scope
    colormap: str | Colormap | None = "viridis",
    on_empty: Literal["show", "hide"] = "hide",
    hide_f_block: bool | None = None,
    plot_kwargs: dict[str, Any] | None = None,
    # Axis-scope
    ax_kwargs: dict[str, Any] | None = None,
    child_kwargs: dict[str, Any] | None = None,
    # Colorbar
    cbar_axis: Literal["x", "y"] = "x",
    cbar_title: str = "Values",
    cbar_title_kwargs: dict[str, Any] | None = None,
    cbar_coords: tuple[float, float, float, float] = (0.18, 0.8, 0.42, 0.02),
    cbar_kwargs: dict[str, Any] | None = None,
    # Symbol
    symbol_pos: tuple[float, float] = (0.5, 0.8),
    symbol_text: str | Callable[[Element], str] = lambda elem: elem.symbol,
    symbol_kwargs: dict[str, Any] | None = None,
    # Element types based colors and legend
    color_elem_strategy: Literal["symbol", "background", "both", "off"] = "background",
    elem_type_colors: dict[str, str] | None = None,
    add_elem_type_legend: bool = False,
    elem_type_legend_kwargs: dict[str, Any] | None = None,
) -> plt.Figure:
    """Plot histograms for each element laid out in a periodic table.

    Args:
        data (pd.DataFrame | pd.Series | dict[str, list[float]]): Map from element
            symbols to histogram values. E.g. if dict, {"Fe": [1, 2, 3], "O": [4, 5]}.
            If pd.Series, index is element symbols and values lists. If pd.DataFrame,
            column names are element symbols histograms are plotted from each column.

        bins (int): Number of bins for the histograms. Defaults to 20.
        x_range (tuple[float | None, float | None]): x-axis range for all histograms.
            Defaults to None.
        log (bool): Whether to log scale y-axis of each histogram. Defaults to False.

        colormap (str): Matplotlib colormap name to use. Defaults to 'viridis'. See
            options at https://matplotlib.org/stable/users/explain/colors/colormaps.
        on_empty ("hide" | "show"): Whether to show or hide tiles for elements without
            data. Defaults to "hide".
        hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults to
            None, meaning hide if no data is provided for f-block elements.
        plot_kwargs (dict): Additional keyword arguments to
            pass to the plt.subplots function call.

        ax_kwargs (dict): Keyword arguments passed to ax.set() for each plot.
            Use to set x/y labels, limits, etc. Defaults to None. Example:
            dict(title="Periodic Table", xlabel="x-axis", ylabel="y-axis", xlim=(0, 10),
            ylim=(0, 10), xscale="linear", yscale="log"). See ax.set() docs for options:
            https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.set.html#matplotlib-axes-axes-set
        child_kwargs (dict): Keywords passed to ax.hist() for each histogram.
            Defaults to None.

        cbar_axis ("x" | "y"): The axis colormap would be based on.
        cbar_title (str): Color bar title. Defaults to "Histogram Value".
        cbar_title_kwargs (dict): Keyword arguments passed to cbar.ax.set_title().
            Defaults to dict(fontsize=12, pad=10).
        cbar_coords (tuple[float, float, float, float]): Color bar position and size:
            [x, y, width, height] anchored at lower left corner of the bar. Defaults to
            (0.25, 0.77, 0.35, 0.02).
        cbar_kwargs (dict): Keyword arguments passed to fig.colorbar().

        symbol_pos (tuple[float, float]): Position of element symbols relative to the
            lower left corner of each tile. Defaults to (0.5, 0.8). (1, 1) is the upper
            right corner.
        symbol_text (str | Callable[[Element], str]): Text to display for each element
            symbol. Defaults to lambda elem: elem.symbol.
        symbol_kwargs (dict): Keyword arguments passed to ax.text() for element
            symbols. Defaults to None.

        color_elem_strategy ("symbol" | "background" | "both" | "off"): Whether to
            color element symbols, tile backgrounds, or both based on element type.
            Defaults to "background".
        elem_type_colors (dict | None): dict to map element types to colors.
            None to use default element type colors.
        add_elem_type_legend (bool): Whether to show a legend for element
            types. Defaults to True.
        elem_type_legend_kwargs (dict): kwargs to plt.legend(), e.g. to
            set the legend title, use {"title": "Element Types"}.

    Returns:
        plt.Figure: periodic table with a histogram in each element tile.
    """
    # Initialize periodic table plotter
    projector = PTableProjector(
        data=data,
        colormap=colormap,
        plot_kwargs=plot_kwargs,
        hide_f_block=hide_f_block,
        elem_type_colors=elem_type_colors,
    )

    # Call child plotter: histogram
    child_kwargs = child_kwargs or {}
    child_kwargs |= {
        "bins": bins,
        "range": x_range,
        "log": log,
        "cbar_axis": cbar_axis,
        "cmap": projector.cmap,
    }

    projector.add_child_plots(
        ChildPlotters.histogram,
        child_kwargs=child_kwargs,
        ax_kwargs=ax_kwargs,
        on_empty=on_empty,
    )

    # Add element symbols
    projector.add_elem_symbols(
        text=symbol_text,
        pos=symbol_pos,
        text_color=ElemColorMode.element_types
        if color_elem_strategy in {"both", "symbol"}
        else "black",
        kwargs=symbol_kwargs,
    )

    # Color element tile background
    if color_elem_strategy in {"both", "background"}:
        projector.set_elem_background_color()

    # Add colorbar
    if colormap is not None:
        projector.add_colorbar(
            title=cbar_title,
            coords=cbar_coords,
            cbar_kwargs=cbar_kwargs,
            title_kwargs=cbar_title_kwargs,
        )

    # Add element type legend
    if add_elem_type_legend:
        projector.add_elem_type_legend(
            kwargs=elem_type_legend_kwargs,
        )

    return projector.fig


def ptable_scatters(
    data: pd.DataFrame | pd.Series | dict[str, list[list[float]]],
    *,
    # Figure-scope
    colormap: str | Colormap | None = None,
    on_empty: Literal["hide", "show"] = "hide",
    hide_f_block: bool | None = None,
    plot_kwargs: dict[str, Any]
    | Callable[[Sequence[float]], dict[str, Any]]
    | None = None,
    # Axis-scope
    ax_kwargs: dict[str, Any] | None = None,
    child_kwargs: dict[str, Any] | None = None,
    # Colorbar
    cbar_title: str = "Values",
    cbar_title_kwargs: dict[str, Any] | None = None,
    cbar_coords: tuple[float, float, float, float] = (0.18, 0.8, 0.42, 0.02),
    cbar_kwargs: dict[str, Any] | None = None,
    # Symbol
    symbol_text: str | Callable[[Element], str] = lambda elem: elem.symbol,
    symbol_pos: tuple[float, float] = (0.5, 0.8),
    symbol_kwargs: dict[str, Any] | None = None,
    # Element types based colors and legend
    color_elem_strategy: Literal["symbol", "background", "both", "off"] = "background",
    elem_type_colors: dict[str, str] | None = None,
    add_elem_type_legend: bool = False,
    elem_type_legend_kwargs: dict[str, Any] | None = None,
) -> plt.Figure:
    """Make scatter plots for each element, nested inside a periodic table.

    Args:
        data (pd.DataFrame | pd.Series | dict[str, list[list[float]]]):
            Map from element symbols to plot data. E.g. if dict,
            {"Fe": [1, 2], "Co": [3, 4]}, where the 1st value would
            be plotted on the lower-left corner and the 2nd on the upper-right.
            If pd.Series, index is element symbols and values lists.
            If pd.DataFrame, column names are element symbols,
            plots are created from each column.
        colormap (str): Matplotlib colormap name to use. Defaults to None. See
            options at https://matplotlib.org/stable/users/explain/colors/colormaps.
        on_empty ("hide" | "show"): Whether to show or hide tiles for elements without
            data. Defaults to "hide".
        hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults to
            None, meaning hide if no data is provided for f-block elements.
        plot_kwargs (dict): Additional keyword arguments to
                pass to the plt.subplots function call.
        ax_kwargs (dict): Keyword arguments passed to ax.set() for each plot.
            Use to set x/y labels, limits, etc. Defaults to None. Example:
            dict(title="Periodic Table", xlabel="x-axis", ylabel="y-axis", xlim=(0, 10),
            ylim=(0, 10), xscale="linear", yscale="log"). See ax.set() docs for options:
            https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.set.html#matplotlib-axes-axes-set
        child_kwargs: Arguments to pass to the child plotter call.
        cbar_title (str): Color bar title. Defaults to "Histogram Value".
        cbar_title_kwargs (dict): Keyword arguments passed to cbar.ax.set_title().
            Defaults to dict(fontsize=12, pad=10).
        cbar_coords (tuple[float, float, float, float]): Color bar position and size:
            [x, y, width, height] anchored at lower left corner of the bar. Defaults to
            (0.25, 0.77, 0.35, 0.02).
        cbar_kwargs (dict): Keyword arguments passed to fig.colorbar().
        symbol_text (str | Callable[[Element], str]): Text to display for
            each element symbol. Defaults to lambda elem: elem.symbol.
        symbol_pos (tuple[float, float]): Position of element symbols
            relative to the lower left corner of each tile.
            Defaults to (0.5, 0.5). (1, 1) is the upper right corner.
        symbol_kwargs (dict): Keyword arguments passed to ax.text() for
            element symbols. Defaults to None.

        color_elem_strategy ("symbol" | "background" | "both" | "off"): Whether to
            color element symbols, tile backgrounds, or both based on element type.
            Defaults to "background".
        elem_type_colors (dict | None): dict to map element types to colors.
            None to use default element type colors.
        add_elem_type_legend (bool): Whether to show a legend for element
            types. Defaults to True.
        elem_type_legend_kwargs (dict): kwargs to plt.legend(), e.g. to
            set the legend title, use {"title": "Element Types"}.
    """
    # Initialize periodic table plotter
    projector = PTableProjector(
        data=data,
        colormap=colormap,
        plot_kwargs=plot_kwargs,  # type: ignore[arg-type]
        hide_f_block=hide_f_block,
        elem_type_colors=elem_type_colors,
    )

    # Call child plotter: Scatter
    child_kwargs = child_kwargs or {}
    child_kwargs |= {"cmap": projector.cmap, "norm": projector.norm}

    projector.add_child_plots(
        ChildPlotters.scatter,
        child_kwargs=child_kwargs,
        ax_kwargs=ax_kwargs,
        on_empty=on_empty,
    )

    # Add element symbols
    projector.add_elem_symbols(
        text=symbol_text,
        pos=symbol_pos,
        text_color=ElemColorMode.element_types
        if color_elem_strategy in {"both", "symbol"}
        else "black",
        kwargs=symbol_kwargs,
    )

    # Color element tile background
    if color_elem_strategy in {"both", "background"}:
        projector.set_elem_background_color()

    # Add colorbar if colormap is given and data length is 3
    if colormap is not None:
        projector.add_colorbar(
            title=cbar_title,
            coords=cbar_coords,
            cbar_kwargs=cbar_kwargs,
            title_kwargs=cbar_title_kwargs,
        )

    # Add element type legend
    if add_elem_type_legend:
        projector.add_elem_type_legend(
            kwargs=elem_type_legend_kwargs,
        )

    return projector.fig


def ptable_lines(
    data: pd.DataFrame | pd.Series | dict[str, list[list[float]]],
    *,
    # Figure-scope
    on_empty: Literal["hide", "show"] = "hide",
    hide_f_block: bool | None = None,
    plot_kwargs: dict[str, Any]
    | Callable[[Sequence[float]], dict[str, Any]]
    | None = None,
    # Axis-scope
    ax_kwargs: dict[str, Any] | None = None,
    child_kwargs: dict[str, Any] | None = None,
    # Symbol
    symbol_kwargs: dict[str, Any] | None = None,
    symbol_text: str | Callable[[Element], str] = lambda elem: elem.symbol,
    symbol_pos: tuple[float, float] = (0.5, 0.8),
    # Element types based colors and legend
    color_elem_strategy: Literal["symbol", "background", "both", "off"] = "background",
    elem_type_colors: dict[str, str] | None = None,
    add_elem_type_legend: bool = False,
    elem_type_legend_kwargs: dict[str, Any] | None = None,
) -> plt.Figure:
    """Line plots for each element, nested inside a periodic table.

    Args:
        data (pd.DataFrame | pd.Series | dict[str, list[list[float]]]):
            Map from element symbols to plot data. E.g. if dict,
            {"Fe": [1, 2], "Co": [3, 4]}, where the 1st value would
            be plotted on the lower-left corner and the 2nd on the upper-right.
            If pd.Series, index is element symbols and values lists.
            If pd.DataFrame, column names are element symbols,
            plots are created from each column.

        on_empty ("hide" | "show"): Whether to show or hide tiles for elements without
            data. Defaults to "hide".
        hide_f_block (bool): Hide f-block (Lanthanum and Actinium series). Defaults to
            None, meaning hide if no data is provided for f-block elements.
        plot_kwargs (dict): Additional keyword arguments to
                pass to the plt.subplots function call.

        ax_kwargs (dict): Keyword arguments passed to ax.set() for each plot.
            Use to set x/y labels, limits, etc. Defaults to None. Example:
            dict(title="Periodic Table", xlabel="x-axis", ylabel="y-axis", xlim=(0, 10),
            ylim=(0, 10), xscale="linear", yscale="log"). See ax.set() docs for options:
            https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.set.html#matplotlib-axes-axes-set
        child_kwargs: Arguments to pass to the child plotter call.

        symbol_text (str | Callable[[Element], str]): Text to display for
            each element symbol. Defaults to lambda elem: elem.symbol.
        symbol_pos (tuple[float, float]): Position of element symbols
            relative to the lower left corner of each tile.
            Defaults to (0.5, 0.5). (1, 1) is the upper right corner.
        symbol_kwargs (dict): Keyword arguments passed to ax.text() for
            element symbols. Defaults to None.

        color_elem_strategy ("symbol" | "background" | "both" | "off"): Whether to
            color element symbols, tile backgrounds, or both based on element type.
            Defaults to "background".
        elem_type_colors (dict | None): dict to map element types to colors.
            None to use default element type colors.
        add_elem_type_legend (bool): Whether to show a legend for element
            types. Defaults to True.
        elem_type_legend_kwargs (dict): kwargs to plt.legend(), e.g. to
            set the legend title, use {"title": "Element Types"}.
    """
    # Initialize periodic table plotter
    projector = PTableProjector(
        data=data,
        colormap=None,
        plot_kwargs=plot_kwargs,  # type: ignore[arg-type]
        hide_f_block=hide_f_block,
        elem_type_colors=elem_type_colors,
    )

    # Call child plotter: line
    projector.add_child_plots(
        ChildPlotters.line,
        child_kwargs=child_kwargs,
        ax_kwargs=ax_kwargs,
        on_empty=on_empty,
    )

    # Add element symbols
    projector.add_elem_symbols(
        text=symbol_text,
        pos=symbol_pos,
        text_color=ElemColorMode.element_types
        if color_elem_strategy in {"both", "symbol"}
        else "black",
        kwargs=symbol_kwargs,
    )

    # Color element tile background
    if color_elem_strategy in {"both", "background"}:
        projector.set_elem_background_color()

    # Add element type legend
    if add_elem_type_legend:
        projector.add_elem_type_legend(
            kwargs=elem_type_legend_kwargs,
        )

    return projector.fig
