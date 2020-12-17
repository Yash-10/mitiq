# Copyright (C) 2020 Unitary Fund
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Classes corresponding to different zero-noise extrapolation methods."""
from abc import ABC, abstractmethod
from copy import deepcopy
import inspect
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)
import warnings

import numpy as np
from numpy.lib.polynomial import RankWarning
from scipy.optimize import curve_fit, OptimizeWarning

import pymc3 as pm

from mitiq import QPROGRAM
from mitiq.utils import _are_close_dict


class ExtrapolationError(Exception):
    """Error raised by :class:`.Factory` objects when
    the extrapolation fit fails.
    """

    pass


_EXTR_ERR = (
    "The extrapolation fit failed to converge."
    " The problem may be solved by switching to a more stable"
    " extrapolation model such as `LinearFactory`."
)


class ExtrapolationWarning(Warning):
    """Warning raised by :class:`.Factory` objects when
    the extrapolation fit is ill-conditioned.
    """

    pass


_EXTR_WARN = (
    "The extrapolation fit may be ill-conditioned."
    " Likely, more data points are necessary to fit the parameters"
    " of the model."
)

DATA_MISSING_ERR = (
    "Data is either ill-defined or not enough to evaluate the required"
    " information. Please make sure that the 'run' and 'reduce' methods"
    " have been called and that enough expectation values have been measured."
)


class ConvergenceWarning(Warning):
    """Warning raised by :class:`.Factory` objects when
    their `run_classical` method fails to converge.
    """

    pass


def mitiq_curve_fit(
    ansatz: Callable[..., float],
    scale_factors: Sequence[float],
    exp_values: Sequence[float],
    init_params: Optional[List[float]] = None,
) -> Tuple[List[float], np.ndarray]:
    """This is a wrapping of the `scipy.optimize.curve_fit` function with
    custom errors and warnings. It is used to make a non-linear fit.

    Args:
        ansatz : The model function used for zero-noise extrapolation.
                 The first argument is the noise scale variable,
                 the remaining arguments are the parameters to fit.
        scale_factors: The array of noise scale factors.
        exp_values: The array of expectation values.
        init_params: Initial guess for the parameters.
                     If None, the initial values are set to 1.

    Returns:
        opt_params: The array of optimal parameters.
        params_cov: The covariance matrix of the parameters.
            If ill conditioned, params_cov may contain np.inf elements.

    Raises:
        ExtrapolationError: If the extrapolation fit fails.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """
    try:
        with warnings.catch_warnings(record=True) as warn_list:
            opt_params, params_cov = curve_fit(
                ansatz, scale_factors, exp_values, p0=init_params
            )
        for warn in warn_list:
            # replace OptimizeWarning with ExtrapolationWarning
            if warn.category is OptimizeWarning:
                warn.category = ExtrapolationWarning
                warn.message = _EXTR_WARN  # type: ignore
            # re-raise all warnings
            warnings.warn_explicit(
                warn.message, warn.category, warn.filename, warn.lineno
            )
    except RuntimeError:
        raise ExtrapolationError(_EXTR_ERR) from None
    return list(opt_params), params_cov


def mitiq_polyfit(
    scale_factors: Sequence[float],
    exp_values: Sequence[float],
    deg: int,
    weights: Optional[Sequence[float]] = None,
) -> Tuple[List[float], Union[np.ndarray, None]]:
    """This is a wrapping of the `numpy.polyfit` function with
    custom warnings. It is used to make a polynomial fit.

    Args:
        scale_factors: The array of noise scale factors.
        exp_values: The array of expectation values.
        deg: The degree of the polynomial fit.
        weights: Optional array of weights for each sampled point.
                 This is used to make a weighted least squares fit.

    Returns:
        opt_params: The array of optimal parameters.
        params_cov: The covariance matrix of the parameters.
            If data is not enough to estimate the covariance matrix,
            params_cov is returned as None.

    Raises:
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """

    with warnings.catch_warnings(record=True) as warn_list:
        try:
            opt_params, params_cov = np.polyfit(
                scale_factors, exp_values, deg, w=weights, cov=True
            )
        except (ValueError, np.linalg.LinAlgError):
            opt_params = np.polyfit(scale_factors, exp_values, deg, w=weights)
            params_cov = None

    for warn in warn_list:
        # replace RankWarning with ExtrapolationWarning
        if warn.category is RankWarning:
            warn.category = ExtrapolationWarning
            warn.message = _EXTR_WARN  # type: ignore
        # re-raise all warnings
        warnings.warn_explicit(
            warn.message, warn.category, warn.filename, warn.lineno
        )
    return list(opt_params), params_cov


class Factory(ABC):
    """Abstract base class which performs the classical parts of zero-noise
    extrapolation. This minimally includes:

        * scaling circuits,
        * sending jobs to execute,
        * collecting the results,
        * fitting the collected data,
        * Extrapolating to the zero-noise limit.

    If all scale factors are set a priori, the jobs can be batched. This is
    handled by a BatchedFactory.

    If the next scale factor depends on the previous history of results,
    jobs are run sequentially. This is handled by an AdaptiveFactory.
    """

    def __init__(self) -> None:
        self._instack: List[Dict[str, float]] = []
        self._outstack: List[float] = []
        self._opt_params: Union[List[float], None] = None
        self._params_cov: Union[np.ndarray, None] = None
        self._zne_limit: Union[float, None] = None
        self._zne_error: Union[float, None] = None
        self._zne_curve: Union[Callable[[float], float], None] = None
        self._already_reduced = False

    def get_scale_factors(self) -> np.ndarray:
        """Returns the scale factors at which the factory has computed
        expectation values.
        """
        return np.array(
            [params.get("scale_factor") for params in self._instack]
        )

    def get_expectation_values(self) -> np.ndarray:
        """Returns the expectation values computed by the factory."""
        return np.array(self._outstack)

    def get_optimal_parameters(self) -> np.ndarray:
        """Returns the optimal model parameters produced by the extrapolation
        fit.
        """
        if self._opt_params is None:
            raise ValueError(DATA_MISSING_ERR)
        return np.array(self._opt_params)

    def get_parameters_covariance(self) -> np.ndarray:
        """Returns the covariance matrix of the model parameters produced by
        the extrapolation fit.
        """
        if self._params_cov is None:
            raise ValueError(DATA_MISSING_ERR)
        return np.array(self._params_cov)

    def get_zero_noise_limit(self) -> float:
        """Returns the last evaluation of the zero-noise limit
        computed by the factory. To re-evaluate
        its value, the method 'reduce' should be called first.
        """
        if self._zne_limit is None:
            raise ValueError(DATA_MISSING_ERR)
        return self._zne_limit

    def get_zero_noise_limit_error(self) -> float:
        """Returns the extrapolation error representing the uncertainty
        affecting the zero-noise limit. It is deduced by error propagation
        from the covariance matrix associated to the fit parameters.

        Note: this quantity is only related to the ability of the model
            to fit the measured data. Therefore, it may underestimate the
            actual error existing between the zero-noise limit and the
            true ideal expectation value.
        """
        if self._zne_error is None:
            raise ValueError(DATA_MISSING_ERR)
        return self._zne_error

    def get_extrapolation_curve(self) -> Callable[[float], float]:
        """Returns the extrapolation curve, i.e., a function which
        inputs a noise scale factor and outputs the associated expectation
        value. This function is the solution of the regression problem
        used to evaluate the zero-noise extrapolation.
        """
        if self._zne_curve is None:
            raise ValueError(DATA_MISSING_ERR)
        return self._zne_curve

    @abstractmethod
    def run(
        self,
        qp: QPROGRAM,
        executor: Callable[..., float],
        scale_noise: Callable[[QPROGRAM, float], QPROGRAM],
        num_to_average: int = 1,
    ) -> "Factory":
        """Calls the executor function on noise-scaled quantum circuit and
        stores the results.

        Args:
            qp: Quantum circuit to scale noise in.
            executor: Function which inputs a (list of) quantum circuits and
                outputs a (list of) expectation values.
            scale_noise: Function which inputs a quantum circuit and outputs
                a noise-scaled quantum circuit.
            num_to_average: Number of times the executor function is called
                on each noise-scaled quantum circuit.
        """
        raise NotImplementedError

    @abstractmethod
    def reduce(self) -> float:
        """Returns the extrapolation to the zero-noise limit."""
        raise NotImplementedError

    @abstractmethod
    def run_classical(
        self, scale_factor_to_expectation_value: Callable[..., float],
    ) -> "Factory":
        """Calls the function scale_factor_to_expectation_value at each scale
        factor of the factory, and stores the results.

        Args:
            scale_factor_to_expectation_value: A function which inputs a scale
                factor and outputs an expectation value. This does not have to
                involve a quantum processor making this a "classical analogue"
                of the run method.
        """
        raise NotImplementedError

    def iterate(
        self, noise_to_expval: Callable[..., float], max_iterations: int = 100
    ) -> "Factory":
        warnings.warn(
            "The `iterate` method is deprecated in v0.3.0 and will be removed "
            "in v0.4.0. Use `run_classical` instead.",
            DeprecationWarning,
        )
        return self.run_classical(noise_to_expval)

    def push(
        self, instack_val: Dict[str, float], outstack_val: float
    ) -> "Factory":
        """Appends "instack_val" to "self._instack" and "outstack_val" to
        "self._outstack". Each time a new expectation value is computed this
        method should be used to update the internal state of the Factory.
        """
        if self._already_reduced:
            warnings.warn(
                "You are pushing new data into a factory object despite its "
                ".reduce() method has already been called. Please make "
                "sure your intention is to append new data to the stack of "
                "previous data. Otherwise, the method .reset() can be used "
                "to clean the internal state of the factory.",
                ExtrapolationWarning,
            )
        self._instack.append(instack_val)
        self._outstack.append(outstack_val)
        return self

    def reset(self) -> "Factory":
        """Resets the internal state of the Factory."""

        self._instack = []
        self._outstack = []
        self._opt_params = None
        self._params_cov = None
        self._zne_limit = None
        self._zne_error = None
        self._already_reduced = False
        return self

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Factory):
            return False
        if self._already_reduced != other._already_reduced:
            return False
        if len(self._instack) != len(other._instack):
            return False
        for dict_a, dict_b in zip(self._instack, other._instack):
            if not _are_close_dict(dict_a, dict_b):
                return False
        return np.allclose(self._outstack, other._outstack)


class BatchedFactory(Factory, ABC):
    """Abstract class of a non-adaptive Factory initialized with a
    pre-determined set of scale factors.

    Specific (non-adaptive) extrapolation algorithms are derived from this
    class by defining the `reduce` method.
    """

    def __init__(
        self,
        scale_factors: Sequence[float],
        shot_list: Optional[List[int]] = None,
    ) -> None:
        """Constructs a BatchedFactory.

        Args:
            scale_factors: Sequence of noise scale factors at which expectation
                values should be measured.
            shot_list: Optional sequence of integers corresponding to the
                number of samples taken for each expectation value. If this
                argument is explicitly passed to the factory, it must have the
                same length of scale_factors and the executor function must
                accept "shots" as a valid keyword argument.

        Raises:
            ValueError: If the number of scale factors is less than 2.
            TypeError: If shot_list is provided and has any non-integer values.
        """
        if len(scale_factors) < 2:
            raise ValueError("At least 2 scale factors are necessary.")

        if shot_list and (
            not isinstance(shot_list, Sequence)
            or not all([isinstance(shots, int) for shots in shot_list])
        ):
            raise TypeError(
                "The optional argument shot_list must be None "
                "or a valid iterator of integers."
            )
        if shot_list and (len(scale_factors) != len(shot_list)):
            raise IndexError(
                "The arguments scale_factors and shot_list"
                " must have the same length."
                f" But len(scale_factors) is {len(scale_factors)}"
                f" and len(shot_list) is {len(shot_list)}."
            )

        self._scale_factors = scale_factors
        self._shot_list = shot_list

        super(BatchedFactory, self).__init__()

    @staticmethod
    def _is_executor_batched(
        executor: Union[Callable[..., float], Callable[..., List[float]]],
    ) -> bool:
        """Returns True if the input function is recognized as a "batched
        executor".

        The executor is detected as "batched" only if it is annotated with
        a return type that is one of the following:
            * Iterable[float]
            * List[float]
            * Sequence[float]
            * Tuple[float]
            * numpy.ndarray

        Args:
            executor: A "single executor" (1) or a "batched executor" (2).
                (1) A function which inputs a single circuit and outputs a
                single expectation value of interest.
                (2) A function which inputs a list of circuits and outputs a
                list of expectation values (one for each circuit).

        Returns: True if the executor is detected as batched, False otherwise.
        """
        executor_annotation = inspect.getfullargspec(executor).annotations
        return executor_annotation.get("return") in (
            List[float],
            Sequence[float],
            Tuple[float],
            Iterable[float],
            np.ndarray,
        )

    def run(
        self,
        qp: QPROGRAM,
        executor: Union[Callable[..., float], Callable[..., List[float]]],
        scale_noise: Callable[[QPROGRAM, float], QPROGRAM],
        num_to_average: int = 1,
    ) -> "BatchedFactory":
        """Computes the expectation values at each scale factor and stores them
        in the factory. If the executor returns a single expectation value, the
        circuits are run sequentially. If the executor is batched and returns
        a list of expectation values (one for each circuit), then the circuits
        are sent to the backend as a single job. To detect if an executor is
        batched, it must be annotated with a return type that is one of the
        following:

            * Iterable[float]
            * List[float]
            * Sequence[float]
            * Tuple[float]
            * numpy.ndarray

        Args:
            qp: Quantum circuit to run.
            executor: A "single executor" (1) or a "batched executor" (2).
                (1) A function which inputs a single circuit and outputs a
                single expectation value of interest.
                (2) A function which inputs a list of circuits and outputs a
                list of expectation values (one for each circuit). A batched
                executor can also take an optional "kwargs_list" argument to
                set a list of keyword arguments (one for each circuit). This
                is necessary only if the factory is initialized using the
                optional "shot_list" parameter.
            scale_noise: Noise scaling function.
            num_to_average: The number of circuits executed for each noise
                scale factor. This parameter can be used to increase the
                precision of the "executor" or to average the effect of a
                non-deterministic "scale_noise" function.
        """
        self.reset()
        self._batch_populate_instack()

        # Get all noise-scaled circuits to run
        to_run = self._generate_circuits(qp, scale_noise, num_to_average)

        # Get the list of keywords associated to each circuit in "to_run"
        kwargs_list = self._get_keyword_args(num_to_average)

        if self._is_executor_batched(executor):
            if all([kwargs == {} for kwargs in kwargs_list]):
                res = executor(to_run)
            else:
                res = executor(to_run, kwargs_list=kwargs_list)
        else:
            res = [
                executor(circ, **kwargs)  # type: ignore
                for circ, kwargs in zip(to_run, kwargs_list)
            ]

        # Reshape "res" to have "num_to_average" columns
        res = np.array(res).reshape((-1, num_to_average))

        # Average the "num_to_average" columns
        self._outstack = np.average(res, axis=1)

        return self

    def run_classical(
        self, scale_factor_to_expectation_value: Callable[..., float]
    ) -> "BatchedFactory":
        """Computes expectation values by calling the input function at each
        scale factor.

        Args:
            scale_factor_to_expectation_value: Function mapping a noise scale
                factor to an expectation value. If shot_list is not None,
                "shots" must be an argument of this function.
        """
        self.reset()
        self._batch_populate_instack()
        kwargs_list = self._get_keyword_args(num_to_average=1)

        self._outstack = [
            scale_factor_to_expectation_value(scale_factor, **kwargs)
            for scale_factor, kwargs in zip(self._scale_factors, kwargs_list)
        ]

        return self

    def _generate_circuits(
        self,
        circuit: QPROGRAM,
        scale_noise: Callable[[QPROGRAM, float], QPROGRAM],
        num_to_average: int = 1,
    ) -> List[QPROGRAM]:
        """Returns all noise-scaled circuits to run.

        Args:
            circuit: Base circuit to scale noise in.
            scale_noise: Noise scaling function.
            num_to_average: Number of times to call scale_noise at each scale
                factor.
        """
        to_run = []
        for scale_factor in self.get_scale_factors():
            for _ in range(num_to_average):
                to_run.append(scale_noise(circuit, scale_factor))
        return to_run

    def _batch_populate_instack(self) -> None:
        """Populates the instack with all computed values."""
        if self._shot_list:
            self._instack = [
                {"scale_factor": scale, "shots": shots}
                for scale, shots in zip(self._scale_factors, self._shot_list)
            ]
        else:
            self._instack = [
                {"scale_factor": scale} for scale in self._scale_factors
            ]

    def _get_keyword_args(self, num_to_average: int) -> List[Dict[str, Any]]:
        """Returns a list of keyword dictionaries to be used for
        executing the circuits generated by the method "_generate_circuits".

        Args:
            num_to_average: The number of times the same keywords are used
                for each scale factor. This should correspond to the number
                of circuits executed for each scale factor.

        Returns:
            The output list of keyword dictionaries.
        """
        params = deepcopy(self._instack)
        for d in params:
            _ = d.pop("scale_factor")

        # Repeat each keyword num_to_average times
        return [k for k in params for _ in range(num_to_average)]

    def __eq__(self, other: Any) -> bool:
        return Factory.__eq__(self, other) and np.allclose(
            self._scale_factors, other._scale_factors
        )


class AdaptiveFactory(Factory, ABC):
    """Abstract class designed to adaptively produce a new noise scaling
    parameter based on a historical stack of previous noise scale parameters
    ("self._instack") and previously estimated expectation values
    ("self._outstack").

    Specific zero-noise extrapolation algorithms which are adaptive are derived
    from this class.
    """

    @abstractmethod
    def next(self) -> Dict[str, float]:
        """Returns a dictionary of parameters to execute a circuit at."""
        raise NotImplementedError

    @abstractmethod
    def is_converged(self) -> bool:
        """Returns True if all needed expectation values have been computed,
        else False.
        """
        raise NotImplementedError

    @abstractmethod
    def reduce(self) -> float:
        """Returns the extrapolation to the zero-noise limit."""
        raise NotImplementedError

    def run_classical(
        self,
        scale_factor_to_expectation_value: Callable[..., float],
        max_iterations: int = 100,
    ) -> "AdaptiveFactory":
        """Evaluates a sequence of expectation values until enough
        data is collected (or iterations reach "max_iterations").

        Args:
            scale_factor_to_expectation_value: Function mapping a noise scale
                factor to an expectation value. If shot_list is not None,
                "shots" must be an argument of this function.
            max_iterations: Maximum number of iterations (optional).
                Default: 100.

        Raises:
            ConvergenceWarning: If iteration loop stops before convergence.
        """
        # Reset the instack, outstack, and optimal parameters
        self.reset()

        counter = 0
        while not self.is_converged() and counter < max_iterations:
            next_in_params = self.next()
            next_exec_params = deepcopy(next_in_params)

            # Get next scale factor and remove it from next_exec_params
            scale_factor = next_exec_params.pop("scale_factor")
            next_expval = scale_factor_to_expectation_value(
                scale_factor, **next_exec_params
            )
            self.push(next_in_params, next_expval)
            counter += 1

        if counter == max_iterations:
            warnings.warn(
                "Factory iteration loop stopped before convergence. "
                f"Maximum number of iterations ({max_iterations}) "
                "was reached.",
                ConvergenceWarning,
            )

        return self

    def run(
        self,
        qp: QPROGRAM,
        executor: Callable[..., float],
        scale_noise: Callable[[QPROGRAM, float], QPROGRAM],
        num_to_average: int = 1,
        max_iterations: int = 100,
    ) -> "AdaptiveFactory":
        """Evaluates a sequence of expectation values by executing quantum
        circuits until enough data is collected (or iterations reach
        "max_iterations").

        Args:
            qp: Circuit to mitigate.
            executor: Function executing a circuit; returns an expectation
                value. If shot_list is not None, then "shot" must be
                an additional argument of the executor.
            scale_noise: Function that scales the noise level of a quantum
                circuit.
            num_to_average: Number of times expectation values are computed by
                the executor after each call to scale_noise, then averaged.
            max_iterations: Maximum number of iterations (optional).
        """

        def scale_factor_to_expectation_value(
            scale_factor: float, **exec_params: Any
        ) -> float:
            """Evaluates the quantum expectation value for a given
            scale_factor and other executor parameters."""
            expectation_values = []
            for _ in range(num_to_average):
                scaled_qp = scale_noise(qp, scale_factor)
                expectation_values.append(executor(scaled_qp, **exec_params))
            return np.average(expectation_values)

        return self.run_classical(
            scale_factor_to_expectation_value, max_iterations
        )


class PolyFactory(BatchedFactory):
    """Factory object implementing a zero-noise extrapolation algorithm based on
    a polynomial fit.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        order: Extrapolation order (degree of the polynomial fit).
               It cannot exceed len(scale_factors) - 1.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

    Note:
        RichardsonFactory and LinearFactory are special cases of PolyFactory.
    """

    def __init__(
        self,
        scale_factors: Sequence[float],
        order: int,
        shot_list: Optional[List[int]] = None,
    ) -> None:
        """Instantiates a new object of this Factory class."""
        if order > len(scale_factors) - 1:
            raise ValueError(
                "The extrapolation order cannot exceed len(scale_factors) - 1."
            )
        self.order = order
        super(PolyFactory, self).__init__(scale_factors, shot_list)

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        order: int,
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates a polynomial extrapolation to the
        zero-noise limit.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            order: The extrapolation order (degree of the polynomial fit).
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional information about the
                extrapolated limit is returned too.
        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The error associated to the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """

        opt_params, params_cov = mitiq_polyfit(
            scale_factors, exp_values, order
        )

        zne_limit = opt_params[-1]

        if not full_output:
            return zne_limit

        zne_error = None

        if params_cov is not None:
            if params_cov.shape == (order + 1, order + 1):
                zne_error = np.sqrt(params_cov[order, order])

        def zne_curve(scale_factor: float) -> float:
            return np.polyval(opt_params, scale_factor)

        return zne_limit, zne_error, opt_params, params_cov, zne_curve

    def reduce(self) -> float:
        """Evaluates the zero-noise limit found by fitting a polynomial of degree
        `self.order` to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            self.order,
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit

    def __eq__(self, other: Any) -> bool:
        return BatchedFactory.__eq__(self, other) and self.order == other.order


class RichardsonFactory(BatchedFactory):
    """Factory object implementing Richardson extrapolation.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates the Richardson extrapolation to the
         zero-noise limit.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional results are returned too.
        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The error associated to the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

         Note:
             This method computes the zero-noise limit only from the
             information contained in the input arguments. To extrapolate from
             the internal data of an instantiated Factory object, the bound
             method ".reduce()" should be called instead.
        """
        # Richardson extrapolation is a particular case of a polynomial fit
        # with order equal to the number of data points minus 1.
        order = len(scale_factors) - 1
        return PolyFactory.extrapolate(
            scale_factors, exp_values, order, full_output
        )

    def reduce(self) -> float:
        """Evaluates the zero-noise limit found by applying Richardson
        extrapolation to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit


class FakeNodesFactory(BatchedFactory):
    """Factory object implementing a modified version [De2020polynomial]_ of
    Richardson extrapolation. In this version the original set of scale factors
    is mapped to a new set of fake nodes, known as Chebyshev-Lobatto points.
    This method may give a better interpolation for particular types of curves
    and if the number of scale factors is large (> 10). One should be aware
    that, in many other cases, the fake nodes extrapolation method is usually
    not superior to standard Richardson extrapolation.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

    .. [De2020polynomial]: S.De Marchia. F. Marchetti, E.Perracchionea
        and D.Poggialia,
        "Polynomial interpolation via mapped bases without resampling,"
        *Journ of Comp. and App. Math.* **364**, 112347 (2020),
        (https://www.sciencedirect.com/science/article/abs/pii/S0377042719303449).
    """

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:

        if not FakeNodesFactory._is_equally_spaced(scale_factors):
            raise ValueError("The scale factors must be equally spaced.")

        # Define interval [a, b] for which the scale_factors are mapped to
        a = 0.0
        b = min(scale_factors) + max(scale_factors)

        # Mapping to the fake nodes
        fake_nodes = FakeNodesFactory._map_to_fake_nodes(scale_factors, a, b)

        if not full_output:
            return RichardsonFactory.extrapolate(fake_nodes, exp_values)

        (
            zne_limit,
            zne_error,
            opt_params,
            params_cov,
            zne_curve,
        ) = RichardsonFactory.extrapolate(fake_nodes, exp_values, True)

        # Convert zne_curve from the "fake node space" to the real space.
        # Note: since a=0.0, this conversion is not necessary for zne_limit.
        def new_curve(scale_factor: float) -> float:
            """Get real zne_cruve from the curve based on fake nodes."""
            return zne_curve(
                FakeNodesFactory._map_to_fake_nodes(scale_factor, a, b)
            )

        return zne_limit, zne_error, opt_params, params_cov, zne_curve

    def reduce(self) -> float:
        """Evaluates the zero-noise limit found by applying the fake node
        Richardson extrapolation to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """

        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            full_output=True,
        )

        self._already_reduced = True
        return self._zne_limit

    @staticmethod
    def _map_to_fake_nodes(
        x: Union[Sequence[float], float], a: float, b: float
    ) -> Sequence[float]:
        """A function that maps inputs to Chebyshev-Lobatto points. Based on
        the function [De2020polynomial]_:
            S(x) = (a - b)/2 * cos(pi * (x - a)/(b - a)) + (a + b)/2.
        Where a and b are the endpoints of the interval [a, b] of CL points
        we are mapping to.

        Args:
            x:
                Sequence[float]: Set of values to be mapped to CL points.
                float: A single value to be mapped to a CL point.
            a: A float representing the interval starting at a
            b: A float representing the interval ending at b
        Returns:
            A new sequence of fake nodes (Chebyshev-Lobatto points).

        .. [De2020polynomial]: S.De Marchia. F. Marchetti, E.Perracchionea
            and D.Poggialia,
            "Polynomial interpolation via mapped bases without resampling,"
            *Journ of Comp. and App. Math.* **364**, 112347 (2020),
            (https://www.sciencedirect.com/science/article/abs/pii/S0377042719303449).
        """

        # The mapping function
        def S(x):
            return (a - b) / 2 * np.cos(np.pi * (x - a) / (b - a)) + (
                a + b
            ) / 2

        if isinstance(x, float):
            return S(x)

        return np.array([S(y) for y in x])

    @staticmethod
    def _is_equally_spaced(arr: Sequence[float]) -> bool:
        """Checks if the sequence is equally spaced."""

        diff_arr = np.diff(np.sort(arr))
        return np.allclose(diff_arr, diff_arr[0])


class LinearFactory(BatchedFactory):
    """
    Factory object implementing zero-noise extrapolation based
    on a linear fit.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.
    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    Example:
        >>> NOISE_LEVELS = [1.0, 2.0, 3.0]
        >>> fac = LinearFactory(NOISE_LEVELS)
    """

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates the linear extrapolation to the
        zero-noise limit.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional results are returned too.
        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The error associated to the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """
        # Linear extrapolation is equivalent to a polynomial fit with order=1
        return PolyFactory.extrapolate(
            scale_factors, exp_values, 1, full_output
        )

    def reduce(self) -> float:
        """Returns the zero-noise limit found by fitting a line to the internal
            data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit


class ExpFactory(BatchedFactory):
    """
    Factory object implementing a zero-noise extrapolation algorithm assuming
    an exponential ansatz y(x) = a + b * exp(-c * x), with c > 0.

    If y(x->inf) is unknown, the ansatz y(x) is fitted with a non-linear
    optimization.

    If y(x->inf) is given and avoid_log=False, the exponential
    model is mapped into a linear model by a logarithmic transformation.

    Args:
        scale_factors: Sequence of noise scale factors at which expectation
            values should be measured.
        asymptote: Infinite-noise limit (optional argument).
        avoid_log: If set to True, the exponential model is not linearized
            with a logarithm and a non-linear fit is applied even if asymptote
            is not None. The default value is False.
        shot_list: Optional sequence of integers corresponding to the number
            of samples taken for each expectation value. If this argument is
            explicitly passed to the factory, it must have the same length of
            scale_factors and the executor function must accept "shots" as a
            valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationError: If the extrapolation fit fails.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """

    def __init__(
        self,
        scale_factors: Sequence[float],
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        shot_list: Optional[List[int]] = None,
    ) -> None:
        """Instantiate an new object of this Factory class."""
        super(ExpFactory, self).__init__(scale_factors, shot_list)
        if not (asymptote is None or isinstance(asymptote, float)):
            raise ValueError(
                "The argument 'asymptote' must be either a float or None"
            )
        self.asymptote = asymptote
        self.avoid_log = avoid_log

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        eps: float = 1.0e-6,
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates the extrapolation to the zero-noise
        limit assuming an exponential ansatz y(x) = a + b * exp(-c * x),
        with c > 0.

        If y(x->inf) is unknown, the ansatz y(x) is fitted with a non-linear
        optimization.

        If y(x->inf) is given and avoid_log=False, the exponential
        model is mapped into a linear model by a logarithmic transformation.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            asymptote: The infinite-noise limit y(x->inf) (optional argument).
            avoid_log: If set to True, the exponential model is not linearized
                with a logarithm and a non-linear fit is applied even if
                asymptote is not None. The default value is False.
            eps: Epsilon to regularize log(sign(scale_factors - asymptote))
                when the argument is to close to zero or negative.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional information about the
                extrapolated limit is returned too.

        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The error associated to the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ValueError: If the arguments are not consistent with the
                extrapolation model.
            ExtrapolationError: If the extrapolation fit fails.
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """
        return PolyExpFactory.extrapolate(
            scale_factors,
            exp_values,
            order=1,
            asymptote=asymptote,
            avoid_log=avoid_log,
            eps=eps,
            full_output=full_output,
        )

    def reduce(self) -> float:
        """Returns the zero-noise limit found by fitting an exponential
        model to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            asymptote=self.asymptote,
            avoid_log=self.avoid_log,
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ExpFactory):
            return False
        if (
            self.asymptote
            and other.asymptote is None
            or self.asymptote is None
            and other.asymptote
        ):
            return False
        if self.asymptote is None and other.asymptote is None:
            return (
                BatchedFactory.__eq__(self, other)
                and self.avoid_log == other.avoid_log
            )
        return (
            BatchedFactory.__eq__(self, other)
            and np.isclose(self.asymptote, other.asymptote)
            and self.avoid_log == other.avoid_log
        )


class PolyExpFactory(BatchedFactory):
    """
    Factory object implementing a zero-noise extrapolation algorithm assuming
    an (almost) exponential ansatz with a non linear exponent, i.e.:

    y(x) = a + sign * exp(z(x)), where z(x) is a polynomial of a given order.

    The parameter "sign" is a sign variable which can be either 1 or -1,
    corresponding to decreasing and increasing exponentials, respectively.
    The parameter "sign" is automatically deduced from the data.

    If y(x->inf) is unknown, the ansatz y(x) is fitted with a non-linear
    optimization.

    If y(x->inf) is given and avoid_log=False, the exponential
    model is mapped into a polynomial model by logarithmic transformation.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        order: Extrapolation order (degree of the polynomial z(x)).
               It cannot exceed len(scale_factors) - 1.
               If asymptote is None, order cannot exceed
               len(scale_factors) - 2.
        asymptote: The infinite-noise limit y(x->inf) (optional argument).
        avoid_log: If set to True, the exponential model is not linearized
                   with a logarithm and a non-linear fit is applied even
                   if asymptote is not None. The default value is False.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationError: If the extrapolation fit fails.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """

    def __init__(
        self,
        scale_factors: Sequence[float],
        order: int,
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        shot_list: Optional[List[int]] = None,
    ) -> None:
        """Instantiates a new object of this Factory class."""
        super(PolyExpFactory, self).__init__(scale_factors, shot_list)
        if not (asymptote is None or isinstance(asymptote, float)):
            raise ValueError(
                "The argument 'asymptote' must be either a float or None"
            )
        self.order = order
        self.asymptote = asymptote
        self.avoid_log = avoid_log

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        order: int,
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        eps: float = 1.0e-6,
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates the extrapolation to the
        zero-noise limit with an exponential ansatz (whose exponent
        is a polynomial of degree "order").

        The exponential ansatz is y(x) = a + sign * exp(z(x)) where z(x) is a
        polynomial and "sign" is either +1 or -1 corresponding to decreasing
        and increasing exponentials, respectively. The parameter "sign" is
        automatically deduced from the data.

        It is also assumed that z(x-->inf) = -inf, such that y(x-->inf) --> a.

        If asymptote is None, the ansatz y(x) is fitted with a non-linear
        optimization.

        If asymptote is given and avoid_log=False, a linear fit with respect to
        z(x) := log[sign * (y(x) - asymptote)] is performed.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            asymptote: The infinite-noise limit y(x->inf) (optional argument).
            order: The degree of the polynomial z(x).
            avoid_log: If set to True, the exponential model is not linearized
                with a logarithm and a non-linear fit is applied even if
                asymptote is not None. The default value is False.
            eps: Epsilon to regularize log(sign(scale_factors - asymptote))
                when the argument is to close to zero or negative.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional information about the
                extrapolated limit is returned too.

        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The error associated to the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ValueError: If the arguments are not consistent with the
                extrapolation model.
            ExtrapolationError: If the extrapolation fit fails.
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """

        # Shift is 0 if asymptote is given, 1 if asymptote is not given
        shift = int(asymptote is None)

        # Check arguments
        error_str = (
            "Data is not enough: at least two data points are necessary."
        )
        if scale_factors is None or exp_values is None:
            raise ValueError(error_str)
        if len(scale_factors) != len(exp_values) or len(scale_factors) < 2:
            raise ValueError(error_str)
        if order > len(scale_factors) - (1 + shift):
            raise ValueError(
                "Extrapolation order is too high. "
                "The order cannot exceed the number"
                f" of data points minus {1 + shift}."
            )

        # Initialize default errors
        zne_error = None
        params_cov = None

        # Deduce "sign" parameter of the exponential ansatz
        linear_params, _ = mitiq_polyfit(scale_factors, exp_values, deg=1)
        sign = np.sign(-linear_params[0])

        def _ansatz_unknown(x: float, *coeffs: float) -> float:
            """Ansatz of generic order with unknown asymptote."""
            # Coefficients of the polynomial to be exponentiated
            z_coeffs = coeffs[2:][::-1]
            return coeffs[0] + coeffs[1] * np.exp(x * np.polyval(z_coeffs, x))

        def _ansatz_known(x: float, *coeffs: float) -> float:
            """Ansatz of generic order with known asymptote."""
            # Coefficients of the polynomial to be exponentiated
            z_coeffs = coeffs[1:][::-1]
            return asymptote + coeffs[0] * np.exp(x * np.polyval(z_coeffs, x))

        # CASE 1: asymptote is None.
        if asymptote is None:
            # First guess for the parameters
            p_zero = [0.0, sign, -1.0] + [0.0 for _ in range(order - 1)]
            opt_params, params_cov = mitiq_curve_fit(
                _ansatz_unknown, scale_factors, exp_values, p_zero
            )
            # The zero noise limit is ansatz(0)= asympt + b
            zne_limit = opt_params[0] + opt_params[1]

            def zne_curve(scale_factor: float) -> float:
                return _ansatz_unknown(scale_factor, *opt_params)

            # Use propagation of errors to calculate zne_error
            if params_cov is not None:
                if params_cov.shape == (order + 2, order + 2):
                    zne_error = np.sqrt(
                        params_cov[0, 0]
                        + 2 * params_cov[0, 1]
                        + params_cov[1, 1]
                    )

            if full_output:
                return (
                    zne_limit,
                    zne_error,
                    opt_params,
                    params_cov,
                    zne_curve,
                )

            return zne_limit

        # CASE 2: asymptote is given and "avoid_log" is True
        if avoid_log:
            # First guess for the parameters
            p_zero = [sign, -1.0] + [0.0 for _ in range(order - 1)]
            opt_params, params_cov = mitiq_curve_fit(
                _ansatz_known, scale_factors, exp_values, p_zero
            )
            # The zero noise limit is ansatz(0)= asymptote + b
            zne_limit = asymptote + opt_params[0]

            def zne_curve(scale_factor: float) -> float:
                return _ansatz_known(scale_factor, *opt_params)

            # Use propagation of errors to calculate zne_error
            if params_cov is not None:
                if params_cov.shape == (order + 1, order + 1):
                    zne_error = np.sqrt(params_cov[0, 0])

            opt_params = [asymptote] + list(opt_params)

            if full_output:
                return (
                    zne_limit,
                    zne_error,
                    opt_params,
                    params_cov,
                    zne_curve,
                )

            return zne_limit

        # CASE 3: asymptote is given and "avoid_log" is False
        # Polynomial fit of z(x).
        shifted_y = [max(sign * (y - asymptote), eps) for y in exp_values]
        zstack = np.log(shifted_y)

        # Get coefficients {z_j} of z(x)= z_0 + z_1*x + z_2*x**2...
        # Note: coefficients are ordered from high powers to powers of x
        # Weights "w" are used to compensate for error propagation
        # after the log transformation y --> z
        z_coefficients, param_cov = mitiq_polyfit(
            scale_factors,
            zstack,
            deg=order,
            weights=np.sqrt(np.abs(shifted_y)),
        )
        # The zero noise limit is ansatz(0)
        zero_limit = asymptote + sign * np.exp(z_coefficients[-1])

        def _zne_curve(scale_factor: float) -> float:
            return asymptote + sign * np.exp(
                np.polyval(z_coefficients, scale_factor)
            )

        # Use propagation of errors to calculate zne_error
        if params_cov is not None:
            if params_cov.shape == (order + 1, order + 1):
                zne_error = np.exp(z_coefficients[-1]) * np.sqrt(
                    params_cov[order + 1, order + 1]
                )

        # Parameters from low order to high order
        opt_params = [asymptote] + list(z_coefficients[::-1])

        if full_output:
            return zero_limit, zne_error, opt_params, params_cov, _zne_curve
        return zne_limit

    def __eq__(self, other: Any) -> bool:
        return (
            BatchedFactory.__eq__(self, other)
            and isinstance(other, PolyExpFactory)
            and np.isclose(self.asymptote, other.asymptote)
            and self.avoid_log == other.avoid_log
            and self.order == other.order
        )

    def reduce(self) -> float:
        """Returns the zero-noise limit found by fitting an the
        model to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            self.order,
            self.asymptote,
            self.avoid_log,
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit


# Keep a log of the optimization process storing:
# noise value(s), expectation value(s), parameters, and zero limit
OptimizationHistory = List[
    Tuple[List[Dict[str, float]], List[float], List[float], float]
]


class ExpBayesFactory(BatchedFactory):
    """Factory object implementing a zero-noise extrapolation algorithm based on
    Bayesian Inference. The exponential ansatz for the expecation value:
        E(lambda) = a + b * e**(-c*lambda),
    where a, b and c are model parameters that need to be estimated,
    while lambda is the noise scale factor.

    Args:
        scale_factors: Sequence of noise scale factors at which
                       expectation values should be measured.
        shot_list: Optional sequence of integers corresponding to the number
                   of samples taken for each expectation value. If this
                   argument is explicitly passed to the factory, it must have
                   the same length of scale_factors and the executor function
                   must accept "shots" as a valid keyword argument.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
    Note:
        RichardsonFactory and LinearFactory are special cases of PolyFactory.
    """

    @staticmethod
    def _exp_ansatz(
        a: float,
        b: float,
        c: float,
        scale_factor: float
    ) -> float:
        """
        Calculates the expecation given a scale factor and model
        parameters.

        Args:
            a: Model parameter.
            b: Model parameter.
            c: Model parameter.
            scale_factors: The array of noise scale factors.

        Returns:
            The expected value according to the model parameters and
            scale factor.
        """

        return a + b * np.exp(-1.0 * c * scale_factor)

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        full_output: bool = False,
    ) -> Union[

        float,
    ]:
        """Static method which evaluates an exponential extrapolation to the
        zero-noise limit using Bayesian inference.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional information about the
                extrapolated limit is returned too.
        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            opt_params: The parameter array of the best fitting model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """
        with pm.Model():
            """
            We assumme that the priors for the model parameters is a uniform
            distribution with an upper limit 1 and a lower limit 0,
            except for b with a lower limit -1.
            """
            a = pm.Uniform('a', 0, 1)
            b = pm.Uniform('b', -1, 1)
            c = pm.Uniform('c', 0, 1)
            eps = pm.Uniform('eps', 0, 0.5)

            pm.Normal(
                'expval',
                mu=ExpBayesFactory._exp_ansatz(a, b, c, scale_factors),
                sd=eps,
                observed=exp_values,
            )

            trace = pm.sample(target_accept=0.95)

        # Optimal model parameters:
        a = trace['a'].mean()
        b = trace['b'].mean()
        c = trace['c'].mean()
        opt_params, zne_error = [a, b, c], trace['eps'].mean()

        def zne_curve(scale_factor: float) -> float:
            return ExpBayesFactory._exp_ansatz(a, b, c, scale_factor)

        zne_limit = ExpBayesFactory._exp_ansatz(a, b, c, 0.0)
        if not full_output:
            return zne_limit

        params_cov = None
        return zne_limit, zne_error, opt_params, params_cov, zne_curve

    def reduce(self) -> float:
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            full_output=True,
        )
        self._already_reduced = True
        return self._zne_limit

    def __eq__(self, other: Any) -> bool:
        return BatchedFactory.__eq__(self, other)


class AdaExpFactory(AdaptiveFactory):
    """Factory object implementing an adaptive zero-noise extrapolation
    algorithm assuming an exponential ansatz y(x) = a + b * exp(-c * x),
    with c > 0.

    The noise scale factors are are chosen adaptively at each step,
    depending on the history of collected results.

    If y(x->inf) is unknown, the ansatz y(x) is fitted with a non-linear
    optimization.

    If y(x->inf) is given and avoid_log=False, the exponential
    model is mapped into a linear model by logarithmic transformation.

    Args:
        steps: The number of optimization steps. At least 3 are necessary.
        scale_factor: The second noise scale factor (the first is always 1.0).
            Further scale factors are adaptively determined.
        asymptote: The infinite-noise limit y(x->inf) (optional argument).
        avoid_log: If set to True, the exponential model is not linearized
            with a logarithm and a non-linear fit is applied even if asymptote
            is not None. The default value is False.
        max_scale_factor: Maximum noise scale factor. Default is 6.0.

    Raises:
        ValueError: If data is not consistent with the extrapolation model.
        ExtrapolationError: If the extrapolation fit fails.
        ExtrapolationWarning: If the extrapolation fit is ill-conditioned.
    """

    _SHIFT_FACTOR = 1.27846
    _EPSILON = 1.0e-9

    def __init__(
        self,
        steps: int,
        scale_factor: float = 2.0,
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        max_scale_factor: float = 6.0,
    ) -> None:
        """Instantiate a new object of this Factory class."""
        super(AdaExpFactory, self).__init__()
        if not (asymptote is None or isinstance(asymptote, float)):
            raise ValueError(
                "The argument 'asymptote' must be either a float or None"
            )
        if scale_factor <= 1:
            raise ValueError(
                "The argument 'scale_factor' must be strictly larger than one."
            )
        if steps < 3 + int(asymptote is None):
            raise ValueError(
                "The argument 'steps' must be an integer"
                " greater or equal to 3. "
                "If 'asymptote' is None, 'steps' must be"
                " greater or equal to 4."
            )
        if max_scale_factor <= 1:
            raise ValueError(
                "The argument 'max_scale_factor' must be"
                " strictly larger than one."
            )
        self._steps = steps
        self._scale_factor = scale_factor
        self.asymptote = asymptote
        self.avoid_log = avoid_log
        self.max_scale_factor = max_scale_factor
        self.history: OptimizationHistory = []

    def next(self) -> Dict[str, float]:
        """Returns a dictionary of parameters to execute a circuit at."""
        # The 1st scale factor is always 1
        if len(self._instack) == 0:
            return {"scale_factor": 1.0}
        # The 2nd scale factor is self._scale_factor
        if len(self._instack) == 1:
            return {"scale_factor": self._scale_factor}
        # If asymptote is None we use 2 * scale_factor as third noise parameter
        if (len(self._instack) == 2) and (self.asymptote is None):
            return {"scale_factor": 2 * self._scale_factor}

        with warnings.catch_warnings():
            # This is an intermediate fit, so we suppress its warning messages
            warnings.simplefilter("ignore", ExtrapolationWarning)
            # Call reduce() to fit the exponent and save it in self.history
            self.reduce()
            # The next line avoids warnings after intermediate extrapolations
            self._already_reduced = False

        # Get the most recent fitted parameters from self.history
        _, _, params, _ = self.history[-1]
        # The exponent parameter is the 3rd element of params
        exponent = params[2]
        # Further noise scale factors are determined with
        # an adaptive rule which depends on self.exponent
        next_scale_factor = min(
            1.0 + self._SHIFT_FACTOR / np.abs(exponent + self._EPSILON),
            self.max_scale_factor,
        )
        return {"scale_factor": next_scale_factor}

    def is_converged(self) -> bool:
        """Returns True if all the needed expectation values have been
        computed, else False.
        """
        if len(self._outstack) != len(self._instack):
            raise IndexError(
                f"The length of 'self._instack' ({len(self._instack)}) "
                f"and 'self._outstack' ({len(self._outstack)}) must be equal."
            )
        return len(self._outstack) == self._steps

    @staticmethod
    def extrapolate(
        scale_factors: Sequence[float],
        exp_values: Sequence[float],
        asymptote: Optional[float] = None,
        avoid_log: bool = False,
        eps: float = 1.0e-6,
        full_output: bool = False,
    ) -> Union[
        float,
        Tuple[
            float,
            Union[float, None],
            List[float],
            Union[np.ndarray, None],
            Callable[[float], float],
        ],
    ]:
        """Static method which evaluates the extrapolation to the zero-noise
        limit assuming an exponential ansatz y(x) = a + b * exp(-c * x),
        with c > 0.

        If y(x->inf) is unknown, the ansatz y(x) is fitted with a non-linear
        optimization.

        If y(x->inf) is given and avoid_log=False, the exponential
        model is mapped into a linear model by a logarithmic transformation.

        Args:
            scale_factors: The array of noise scale factors.
            exp_values: The array of expectation values.
            asymptote: The infinite-noise limit y(x->inf) (optional argument).
            avoid_log: If set to True, the exponential model is not linearized
                with a logarithm and a non-linear fit is applied even if
                asymptote is not None. The default value is False.
            eps: Epsilon to regularize log(sign(scale_factors - asymptote))
                when the argument is to close to zero or negative.
            full_output: If False (default), only the zero-noise limit is
                returned. If True, additional results are returned too.

        Returns:
            zne_limit: The extrapolated zero-noise limit. If "full_output"
                is False (default value), only this parameter is returned.
            zne_error: The standard deviation of the extrapolated zero-noise
                limit deduced from the covariance matrix "params_cov".
            opt_params: The parameter array of the best fitting model.
            params_cov: The parameter covariance matrix of the best fitting
                model.
            zne_curve: The callable function which best fit the input data.
                It maps a real noise scale factor to a real expectation value.
                It is equal "zne_limit" when evaluated at zero.

        Raises:
            ValueError: If the arguments are not consistent with the
                extrapolation model.
            ExtrapolationError: If the extrapolation fit fails.
            ExtrapolationWarning: If the extrapolation fit is ill-conditioned.

        Note:
            This method computes the zero-noise limit only from the information
            contained in the input arguments. To extrapolate from the internal
            data of an instantiated Factory object, the bound method
            ".reduce()" should be called instead.
        """
        return ExpFactory.extrapolate(
            scale_factors,
            exp_values,
            asymptote=asymptote,
            avoid_log=avoid_log,
            eps=eps,
            full_output=full_output,
        )

    def reduce(self) -> float:
        """Returns the zero-noise limit found by fitting an exponential
        model to the internal data stored in the factory.

        Returns:
            The zero-noise limit.
        """
        (
            self._zne_limit,
            self._zne_error,
            self._opt_params,
            self._params_cov,
            self._zne_curve,
        ) = self.extrapolate(  # type: ignore
            self.get_scale_factors(),
            self.get_expectation_values(),
            asymptote=self.asymptote,
            avoid_log=self.avoid_log,
            full_output=True,
        )
        # Update optimization history
        self.history.append(
            (self._instack, self._outstack, self._opt_params, self._zne_limit)
        )
        self._already_reduced = True
        return self._zne_limit

    def __eq__(self, other: Any) -> bool:
        return (
            Factory.__eq__(self, other)
            and isinstance(other, AdaExpFactory)
            and self._steps == other._steps
            and self._scale_factor == other._scale_factor
            and np.isclose(self.asymptote, other.asymptote)
            and self.avoid_log == other.avoid_log
            and np.allclose(self.history, other.history)
        )
