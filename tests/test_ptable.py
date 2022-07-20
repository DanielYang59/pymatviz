from __future__ import annotations

import pandas as pd
import pytest
from matplotlib.axes import Axes
from plotly.exceptions import PlotlyError
from plotly.graph_objs._figure import Figure
from pymatgen.core import Composition

from pymatviz import (
    count_elements,
    hist_elemental_prevalence,
    ptable_heatmap,
    ptable_heatmap_plotly,
    ptable_heatmap_ratio,
)
from pymatviz.utils import df_ptable


@pytest.fixture
def glass_formulas() -> list[str]:
    """The result of
    from matminer.datasets import load_dataset

    load_dataset("matbench_glass").composition.head(20)
    """
    return (
        "Al,Al(NiB)2,Al10Co21B19,Al10Co23B17,Al10Co27B13,Al10Co29B11,Al10Co31B9,"
        "Al10Co33B7,Al10Cr3Si7,Al10Fe23B17,Al10Fe27B13,Al10Fe31B9,Al10Fe33B7,"
        "Al10Ni23B17,Al10Ni27B13,Al10Ni29B11,Al10Ni31B9,Al10Ni33B7,Al11(CrSi2)3"
    ).split(",")


@pytest.fixture
def glass_elem_counts(glass_formulas: pd.Series[Composition]) -> pd.Series[int]:
    return count_elements(glass_formulas)


@pytest.fixture
def steel_formulas() -> list[str]:
    """Unusually fractional compositions, good for testing edge cases. The result of:
    from matminer.datasets import load_dataset

    load_dataset("matbench_steels").composition.head(2)
    """
    return [
        "Fe0.620C0.000953Mn0.000521Si0.00102Cr0.000110Ni0.192Mo0.0176V0.000112Nb0.0000616Co0.146Al0.00318Ti0.0185",  # noqa: E501
        "Fe0.623C0.00854Mn0.000104Si0.000203Cr0.147Ni0.0000971Mo0.0179V0.00515N0.00163Nb0.0000614Co0.188W0.00729Al0.000845",  # noqa: E501
    ]


@pytest.fixture
def steel_elem_counts(steel_formulas: pd.Series[Composition]) -> pd.Series[int]:
    return count_elements(steel_formulas)


@pytest.mark.parametrize(
    "count_mode, counts",
    [
        ("element_composition", {"Fe": 22, "O": 63, "P": 12}),
        ("fractional_composition", {"Fe": 2.5, "O": 5, "P": 0.5}),
        ("reduced_composition", {"Fe": 13, "O": 27, "P": 3}),
    ],
)
def test_count_elements(count_mode, counts):
    series = count_elements(["Fe2 O3"] * 5 + ["Fe4 P4 O16"] * 3, count_mode=count_mode)
    expected = pd.Series(counts, index=df_ptable.index, name="count").fillna(0)
    assert series.equals(expected)


def test_count_elements_by_atomic_nums():
    series_in = pd.Series(1, index=range(1, 119))
    el_cts = count_elements(series_in)
    expected = pd.Series(1, index=df_ptable.index, name="count")

    pd.testing.assert_series_equal(expected, el_cts)


@pytest.mark.parametrize("range_limits", [(-1, 10), (100, 200)])
def test_count_elements_bad_atomic_nums(range_limits):
    with pytest.raises(ValueError, match="assumed to represent atomic numbers"):
        count_elements({idx: 0 for idx in range(*range_limits)})

    with pytest.raises(ValueError, match="assumed to represent atomic numbers"):
        # string and integer keys for atomic numbers should be handled equally
        count_elements({str(idx): 0 for idx in range(*range_limits)})


def test_hist_elemental_prevalence(glass_formulas):
    ax = hist_elemental_prevalence(glass_formulas)
    assert isinstance(ax, Axes)

    hist_elemental_prevalence(glass_formulas, log=True)

    hist_elemental_prevalence(glass_formulas, keep_top=10)

    hist_elemental_prevalence(glass_formulas, keep_top=10, bar_values="count")


def test_ptable_heatmap(glass_formulas, glass_elem_counts):
    ax = ptable_heatmap(glass_formulas)
    assert isinstance(ax, Axes)

    ptable_heatmap(glass_formulas, log=True)

    # custom color map
    ptable_heatmap(glass_formulas, log=True, cmap="summer")

    # heat_mode normalized to total count
    ptable_heatmap(glass_formulas, heat_mode="fraction")
    ptable_heatmap(glass_formulas, heat_mode="percent")

    # without heatmap values
    ptable_heatmap(glass_formulas, heat_mode=None)
    ptable_heatmap(glass_formulas, log=True, heat_mode=None)

    # element properties as heatmap values
    ptable_heatmap(df_ptable.atomic_mass)

    # element properties as heatmap values
    ptable_heatmap(df_ptable.atomic_mass, text_color=("red", "blue"))

    # custom max color bar value
    ptable_heatmap(glass_formulas, cbar_max=1e2)
    ptable_heatmap(glass_formulas, log=True, cbar_max=1e2)

    # element counts
    ptable_heatmap(glass_elem_counts)

    with pytest.raises(ValueError, match="Combining log color scale and"):
        ptable_heatmap(glass_formulas, log=True, heat_mode="percent")

    ptable_heatmap(glass_elem_counts, exclude_elements=["O", "P"])

    with pytest.raises(ValueError, match=r"Unexpected symbol\(s\) foobar"):
        ptable_heatmap(glass_elem_counts, exclude_elements=["foobar"])


def test_ptable_heatmap_ratio(
    steel_formulas, glass_formulas, steel_elem_counts, glass_elem_counts
):
    # composition strings
    ax = ptable_heatmap_ratio(glass_formulas, steel_formulas)
    assert isinstance(ax, Axes)

    # element counts
    ptable_heatmap_ratio(glass_elem_counts, steel_elem_counts, normalize=True)

    # mixed element counts and composition
    ptable_heatmap_ratio(glass_formulas, steel_elem_counts)
    ptable_heatmap_ratio(glass_elem_counts, steel_formulas)


def test_ptable_heatmap_plotly(glass_formulas):
    fig = ptable_heatmap_plotly(glass_formulas)
    assert isinstance(fig, Figure)
    assert len(fig.layout.annotations) == 18 * 10  # n_cols * n_rows
    assert sum(anno.text != "" for anno in fig.layout.annotations) == 118  # n_elements

    ptable_heatmap_plotly(
        glass_formulas, hover_props=["atomic_mass", "atomic_number", "density"]
    )
    ptable_heatmap_plotly(
        glass_formulas,
        hover_data="density = " + df_ptable.density.astype(str) + " g/cm^3",
    )
    ptable_heatmap_plotly(df_ptable.density, precision=".1f")

    ptable_heatmap_plotly(glass_formulas, heat_mode="percent")

    with pytest.raises(ValueError, match="should be string, list of strings or list"):
        # test that bad colorscale raises ValueError
        ptable_heatmap_plotly(
            glass_formulas, colorscale=lambda: "bad color scale"  # type: ignore
        )

    # test that unknown builtin colorscale raises ValueError
    with pytest.raises(PlotlyError, match="Colorscale foobar is not a built-in scale"):
        ptable_heatmap_plotly(glass_formulas, colorscale="foobar")


@pytest.mark.parametrize(
    "clr_scl", ["YlGn", ["blue", "red"], [(0, "blue"), (1, "red")]]
)
def test_ptable_heatmap_plotly_colorscale(glass_formulas, clr_scl):
    fig = ptable_heatmap_plotly(glass_formulas, colorscale=clr_scl)
    assert isinstance(fig, Figure)