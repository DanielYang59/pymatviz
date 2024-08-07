from __future__ import annotations

import re
from typing import TYPE_CHECKING, get_args

import numpy as np
import pandas as pd
import pytest
from numpy.testing import assert_allclose

from pymatviz.enums import Key
from pymatviz.ptable._process_data import PTableData, SupportedDataType


if TYPE_CHECKING:
    from typing import ClassVar


class TestPTableDataBasicInit:
    """Test basic init functionality of PTableData."""

    test_dict: ClassVar = {
        "H": 1,  # int
        "He": [2.0, 4.0],  # float list
        "Li": np.array([6.0, 8.0]),  # float array
        "Na": 11.0,  # float
        "Mg": {"a": -1, "b": 14.0}.values(),  # dict_values
        "Al": {-1, 2.3},  # mixed int/float set
    }

    @staticmethod
    def _validate_output_df(output_df: pd.DataFrame) -> None:
        assert isinstance(output_df, pd.DataFrame)

        assert list(output_df) == [Key.heat_val]
        assert list(output_df.index) == ["H", "He", "Li", "Na", "Mg", "Al"]

        assert_allclose(output_df.loc["H", Key.heat_val], [1.0])
        assert_allclose(output_df.loc["He", Key.heat_val], [2.0, 4.0])
        assert_allclose(output_df.loc["Li", Key.heat_val], [6.0, 8.0])
        assert_allclose(output_df.loc["Na", Key.heat_val], [11.0])
        assert_allclose(output_df.loc["Mg", Key.heat_val], [-1.0, 14.0])

        assert output_df.attrs["vmin"] == -1.0
        assert output_df.attrs["mean"] == 4.63
        assert output_df.attrs["vmax"] == 14.0

    def test_from_pd_dataframe(self) -> None:
        input_df: pd.DataFrame = pd.DataFrame(
            self.test_dict.items(), columns=[Key.element, Key.heat_val]
        ).set_index(Key.element)

        ptable_data = PTableData(input_df, check_missing=False, check_infinity=False)
        output_df: pd.DataFrame = ptable_data.data

        self._validate_output_df(output_df)

    def test_from_bad_pd_dataframe(self) -> None:
        """Test auto-fix of badly formatted pd.DataFrame."""
        test_dict = {
            "He": [2.0, 4.0],  # float list
            "Li": np.array([6.0, 8.0]),  # float array
            "Mg": {"a": -1, "b": 14.0}.values(),  # dict_values
        }

        # Elements as a row, and no proper row/column names
        df_out = PTableData(test_dict, check_missing=False, check_infinity=False).data

        assert_allclose(df_out.loc["He", Key.heat_val], [2.0, 4.0])
        assert_allclose(df_out.loc["Li", Key.heat_val], [6.0, 8.0])
        assert_allclose(df_out.loc["Mg", Key.heat_val], [-1.0, 14.0])

        # Elements as a column, and no proper row/column names
        ptable_data = PTableData(
            pd.DataFrame(test_dict).transpose(),
            check_missing=False,
            check_infinity=False,
        )
        df_out_transp = ptable_data.data

        assert_allclose(df_out_transp.loc["He", Key.heat_val], [2.0, 4.0])
        assert_allclose(df_out_transp.loc["Li", Key.heat_val], [6.0, 8.0])
        assert_allclose(df_out_transp.loc["Mg", Key.heat_val], [-1.0, 14.0])

    def test_df_cannot_fix(self) -> None:
        # No complete elements column/row
        df_without_complete_elem = pd.DataFrame(
            {
                "Hello": [2.0, 4.0],  # Not an element
                "Li": np.array([6.0, 8.0]),
                "Mg": {"a": -1, "b": 14.0}.values(),
            }
        )
        with pytest.raises(KeyError, match="Cannot handle dataframe="):
            PTableData(
                df_without_complete_elem, check_missing=False, check_infinity=False
            )

    def test_from_pd_series(self) -> None:
        input_series: pd.Series = pd.Series(self.test_dict)

        ptable_data = PTableData(
            input_series, check_missing=False, check_infinity=False
        )

        self._validate_output_df(ptable_data.data)

    def test_from_dict(self) -> None:
        input_dict = self.test_dict

        ptable_data = PTableData(input_dict, check_missing=False, check_infinity=False)

        self._validate_output_df(ptable_data.data)

    def test_unsupported_type(self) -> None:
        for invalid_data in ([0, 1, 2], range(5), "test", None):
            err_msg = (
                f"{type(invalid_data).__name__} unsupported, "
                f"choose from {get_args(SupportedDataType)}"
            )
            with pytest.raises(TypeError, match=re.escape(err_msg)):
                PTableData(invalid_data, check_missing=False, check_infinity=False)

    def test_get_vmin_vmax(self) -> None:
        # Test without nested list/array
        test_dict_0 = {"H": 1, "He": [2, 4], "Li": np.array([6, 8])}

        processor = PTableData(test_dict_0, check_missing=False, check_infinity=False)

        output_df_0 = processor.data

        assert output_df_0.attrs["vmin"] == 1
        assert output_df_0.attrs["mean"] == 4.2
        assert output_df_0.attrs["vmax"] == 8

        # Test with nested list/array
        test_dict_1 = {
            "H": 1,
            "He": [[2, 3], [4, 5]],
            "Li": [np.array([6, 7]), np.array([8, 9])],
        }

        output_df_1 = PTableData(
            test_dict_1, check_missing=False, check_infinity=False
        ).data

        assert output_df_1.attrs["vmin"] == 1
        assert output_df_1.attrs["mean"] == 5
        assert output_df_1.attrs["vmax"] == 9

    def test_drop_elements(self) -> None:
        test_dict = {"H": 1, "He": [2, 4], "Li": np.array([6, 8])}

        ptable_data = PTableData(test_dict)
        ptable_data.drop_elements(["H", "He"])

        assert list(ptable_data.data.index) == ["Li"]

        # Make sure metadata get updated too
        assert ptable_data.data.attrs["vmin"] == 6, ptable_data.data
        assert ptable_data.data.attrs["vmax"] == 8


class TestPTableDataAdvanced:
    """Test advanced data preprocessing functionality."""

    def test_apply(self) -> None:
        data_index = {"H": 1, "He": [-2.0, 3]}
        ptable_data = PTableData(data_index)

        # Test apply absolute function and meta data
        ptable_data.apply(abs)
        assert_allclose(ptable_data.data.loc["He", Key.heat_val], [2, 3])
        assert ptable_data.data.attrs["vmin"] == 1

    def test_df_without_anomalies(self) -> None:
        normal_df = pd.DataFrame(
            {"Fe": [1, 2, 3], "O": [4, 5, 6]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        ptable_data = PTableData(normal_df)

        assert ptable_data.anomalies == {}

    def test_check_and_replace_missing_zero(self) -> None:
        df_with_missing = pd.DataFrame(
            {"Fe": [1, 2, np.nan], "O": [4, 5, 6]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        with pytest.warns(match="NaN found in data"):
            ptable_data = PTableData(df_with_missing, missing_strategy="zero")

        assert ptable_data.anomalies == {"Fe": {"nan"}}
        assert_allclose(ptable_data.data.loc["Fe", Key.heat_val], [1, 2, 0])
        assert_allclose(ptable_data.data.loc["O", Key.heat_val], [4, 5, 6])

    def test_check_and_replace_missing_mean(self) -> None:
        df_with_missing = pd.DataFrame(
            {"Fe": [1, 2, np.nan], "O": [4, 5, 6]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        with pytest.warns(match="NaN found in data"):
            ptable_data = PTableData(df_with_missing, missing_strategy="mean")

        assert ptable_data.anomalies == {"Fe": {"nan"}}
        assert_allclose(ptable_data.data.loc["Fe", Key.heat_val], [1, 2, 3.6])
        assert_allclose(ptable_data.data.loc["O", Key.heat_val], [4, 5, 6])

    def test_check_and_replace_infinity(self) -> None:
        df_with_inf = pd.DataFrame(
            {"Fe": [1, 2, np.inf], "O": [4, 5, -np.inf]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        with pytest.warns(match="Infinity found in data"):
            ptable_data = PTableData(df_with_inf)

        assert ptable_data.anomalies == {"Fe": {"inf"}, "O": {"inf"}}
        assert_allclose(ptable_data.data.loc["Fe", Key.heat_val], [1, 2, 5])
        assert_allclose(ptable_data.data.loc["O", Key.heat_val], [4, 5, 1])

    def test_check_and_replace_both_nan_and_inf(self) -> None:
        # Test DataFrame with missing value (NaN) and infinity
        df_with_nan_inf = pd.DataFrame(
            {"Fe": [1, 2, np.inf], "O": [4, 5, np.nan]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        ptable_data_nan_inf = PTableData(df_with_nan_inf)

        assert ptable_data_nan_inf.anomalies == {"Fe": {"inf"}, "O": {"nan"}}
        assert_allclose(ptable_data_nan_inf.data.loc["Fe", Key.heat_val], [1, 2, 5])
        assert_allclose(ptable_data_nan_inf.data.loc["O", Key.heat_val], [4, 5, 3])

        # NaN and inf for the same element
        df_with_nan_inf_same_elem = pd.DataFrame(
            {"Fe": [np.nan, 2, np.inf], "O": [4, 5, 6]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        ptable_data_nan_inf_same_elem = PTableData(
            df_with_nan_inf_same_elem, missing_strategy="zero"
        )

        assert ptable_data_nan_inf_same_elem.anomalies == {"Fe": {"inf", "nan"}}
        assert_allclose(
            ptable_data_nan_inf_same_elem.data.loc["Fe", Key.heat_val], [0, 2, 6]
        )
        assert_allclose(
            ptable_data_nan_inf_same_elem.data.loc["O", Key.heat_val], [4, 5, 6]
        )

    def test_too_deep_nest(self) -> None:
        df_level_2 = pd.DataFrame(
            {"Fe": [1, 2, 3], "O": [[4, 5], [6, np.nan]]}.items(),
            columns=[Key.element, Key.heat_val],
        ).set_index(Key.element)

        err_msg = "Unable to replace NaN and inf for nest_level>1"
        with pytest.raises(NotImplementedError, match=err_msg):
            PTableData(df_level_2)

    def test_normalize_data(self) -> None:
        # Test normalize single value data
        data_out = PTableData({"H": 1.0, "He": 4.0}, normalize=True).data

        assert_allclose(data_out.loc["H", Key.heat_val], [0.2])
        assert_allclose(data_out.loc["He", Key.heat_val], [0.8])

        # Test normalize multi value data
        data_out = PTableData({"H": 1.0, "He": [2, 7]}, normalize=True).data

        assert_allclose(data_out.loc["H", Key.heat_val], [0.1])
        assert_allclose(data_out.loc["He", Key.heat_val], [0.2, 0.7])
