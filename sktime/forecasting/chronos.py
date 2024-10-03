"""Implements Chronos forecaster."""

__author__ = ["Z-Fran", "benheid"]
# __all__ = ["ChronosForecaster"]

from typing import Optional

import numpy as np
import pandas as pd
from skbase.utils.dependencies import _check_soft_dependencies

from sktime.forecasting.base import ForecastingHorizon, _BaseGlobalForecaster
from sktime.libs.chronos import ChronosPipeline

if _check_soft_dependencies("torch", severity="none"):
    import torch
else:

    class torch:
        """Dummy class if torch is unavailable."""

        bfloat16 = None


if _check_soft_dependencies("transformers", severity="none"):
    import transformers


class ChronosForecaster(_BaseGlobalForecaster):
    """Chronos forecaster.

    Parameters
    ----------
    model_path : str
        Path to the Chronos' huggingface model.
    config : dict, default={}
        Configuration to use for the model.
    seed: int, optional, default=None
        Random seed for transformers.

    Examples
    --------
    >>> from sktime.datasets import load_airline
    >>> from sktime.forecasting.chronos import ChronosForecaster
    >>> from sktime.split import temporal_train_test_split
    >>> from sktime.forecasting.base import ForecastingHorizon
    >>> y = load_airline()
    >>> y_train, y_test = temporal_train_test_split(y)
    >>> fh = ForecastingHorizon(y_test.index, is_relative=False)
    >>> forecaster = ChronosForecaster("amazon/chronos-t5-tiny")
    >>> forecaster.fit(y_train)
    >>> y_pred = forecaster.predict(fh)
    """

    # tag values are "safe defaults" which can usually be left as-is
    _tags = {
        "python_dependencies": ["torch", "transformers"],
        "requires-fh-in-fit": False,
        "X-y-must-have-same-index": True,
        "enforce_index_type": None,
        "handles-missing-data": False,
        "capability:pred_int": False,
        "X_inner_mtype": ["pd.DataFrame", "pd-multiindex", "pd_multiindex_hier"],
        "y_inner_mtype": [
            "pd.DataFrame",
            "pd-multiindex",
            "pd_multiindex_hier",
        ],
        "scitype:y": "univariate",
        "capability:insample": False,
        "capability:pred_int:insample": False,
        "capability:global_forecasting": True,
        "authors": ["Z-Fran", "benheid"],  # TODO original authors!,
    }

    _default_config = {
        "num_samples": None,  # int, use value from pretrained model if None
        "temperature": None,  # float, use value from pretrained model if None
        "top_k": None,  # int, use value from pretrained model if None
        "top_p": None,  # float, use value from pretrained model if None
        "limit_prediction_length": False,  # bool
        "torch_dtype": torch.bfloat16,  # torch.dtype
        "device_map": "cpu",  # str, use "cpu" for CPU inference, "cuda" for gpu and "mps" for Apple Silicon # noqa
    }

    def __init__(
        self,
        model_path: str,
        config: dict = None,
        seed: Optional[int] = None,
    ):
        super().__init__()

        # set random seed
        self.seed = seed
        self._seed = np.random.randint(0, 2**31) if seed is None else seed

        # set config
        self.config = config
        _config = self._default_config.copy()
        _config.update(config if config is not None else {})
        self._config = _config

        self.model_path = model_path
        self.model_pipeline = None
        self.context = None

    def _fit(self, y, X=None, fh=None):
        """Fit forecaster to training data.

        private _fit containing the core logic, called from fit

        Parameters
        ----------
        y : pd.Series
            Target time series to which to fit the forecaster.
        fh : guaranteed to be ForecastingHorizon or None, optional (default=None)
            The forecasting horizon with the steps ahead to predict.
        X : pd.DataFrame, optional (default=None)
            Exogenous variables are ignored.

        Returns
        -------
        self : reference to self
        """
        if self.model_pipeline is None:
            self.model_pipeline = ChronosPipeline.from_pretrained(
                self.model_path,
                torch_dtype=self._config["torch_dtype"],
                device_map=self._config["device_map"],
            )

    def _predict(self, fh, y=None, X=None):
        """Forecast time series at future horizon.

        private _predict containing the core logic, called from predict

        Parameters
        ----------
        fh : guaranteed to be ForecastingHorizon or None, optional (default=None)
            The forecasting horizon with the steps ahead to predict.
        X : pd.DataFrame, optional (default=None)
            Exogenous variables are ignored.

        Returns
        -------
        y_pred : pd.DataFrame
            Predicted forecasts.
        """
        transformers.set_seed(self._seed)
        if fh is not None:
            # needs to be integer not np.int64
            prediction_length = int(max(fh.to_relative(self.cutoff)))
        else:
            prediction_length = 1

        _y = self._y.copy()
        if y is not None:
            _y = y.copy()
        _y_df = _y

        index_names = _y.index.names
        if isinstance(_y.index, pd.MultiIndex):
            _y = _frame2numpy(_y)
        else:
            _y = _y.values.reshape(1, -1, 1)

        results = []
        for i in range(_y.shape[0]):
            _y_i = _y[i, :, 0]
            _y_i = _y_i[-self.model_pipeline.model.config.context_length :]
            prediction_results = self.model_pipeline.predict(
                torch.Tensor(_y_i),
                prediction_length,
                num_samples=self._config["num_samples"],
                temperature=self._config["temperature"],
                top_k=self._config["top_k"],
                top_p=self._config["top_p"],
                limit_prediction_length=False,
            )

            values = np.median(prediction_results[0].numpy(), axis=0)
            results.append(values)

        pred = np.stack(results, axis=1)
        if isinstance(_y_df.index, pd.MultiIndex):
            ins = np.array(
                list(np.unique(_y_df.index.droplevel(-1)).repeat(pred.shape[0]))
            )
            ins = [ins[..., i] for i in range(ins.shape[-1])] if ins.ndim > 1 else [ins]

            idx = (
                ForecastingHorizon(range(1, pred.shape[0] + 1), freq=self.fh.freq)
                .to_absolute(self._cutoff)
                ._values.tolist()
                * pred.shape[1]
            )
            index = pd.MultiIndex.from_arrays(
                ins + [idx],
                names=_y_df.index.names,
            )
        else:
            index = (
                ForecastingHorizon(range(1, pred.shape[0] + 1))
                .to_absolute(self._cutoff)
                ._values
            )
        pred_out = fh.get_expected_pred_idx(_y, cutoff=self.cutoff)

        pred = pd.DataFrame(
            pred.reshape(-1, 1),
            index=index,
            columns=_y_df.columns,
        )
        dateindex = pred.index.get_level_values(-1).map(lambda x: x in pred_out)
        pred.index.names = index_names

        return pred.loc[dateindex]

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return ``"default"`` set.

        Returns
        -------
        params : dict or list of dict
        """
        test_params = []
        test_params.append(
            {
                "model_path": "amazon/chronos-t5-tiny",
            }
        )
        test_params.append(
            {
                "model_path": "amazon/chronos-t5-tiny",
                "config": {
                    "num_samples": 20,
                },
                "seed": 42,
            }
        )

        return test_params


def _same_index(data):
    data = data.groupby(level=list(range(len(data.index.levels) - 1))).apply(
        lambda x: x.index.get_level_values(-1)
    )
    assert data.map(
        lambda x: x.equals(data.iloc[0])
    ).all(), "All series must has the same index"
    return data.iloc[0], len(data.iloc[0])


def _frame2numpy(data):
    idx, length = _same_index(data)
    arr = np.array(data.values, dtype=np.float32).reshape(
        (-1, length, len(data.columns))
    )
    return arr
