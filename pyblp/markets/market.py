"""Market underlying the BLP model."""

from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import numpy.lib.recfunctions

from .. import exceptions, options
from ..configurations.formulation import ColumnFormulation
from ..configurations.iteration import Iteration
from ..economies.economy import Economy
from ..parameters import NonlinearParameter, NonlinearParameters, RandomCoefficientParameter, RhoParameter
from ..utilities.algebra import (
    approximately_invert, approximately_solve, multiply_matrix_and_tensor, multiply_tensor_and_matrix
)
from ..utilities.basics import Array, Error, Groups, RecArray


class Market(object):
    """A market underlying the BLP model."""

    products: RecArray
    agents: RecArray
    groups: Groups
    J: int
    I: int
    K1: int
    K2: int
    K3: int
    D: int
    H: int
    X1_formulations: Tuple[ColumnFormulation, ...]
    X2_formulations: Tuple[ColumnFormulation, ...]
    X3_formulations: Tuple[ColumnFormulation, ...]
    demographics_formulations: Tuple[ColumnFormulation, ...]
    sigma: Array
    pi: Array
    beta: Array
    group_rho: Array
    rho: Array
    delta: Array
    mu: Array

    def __init__(
            self, economy: Economy, t: Any, sigma: Array, pi: Array, rho: Array, beta: Optional[Array] = None,
            delta: Optional[Array] = None, data_override: Optional[Dict] = None) -> None:
        """Store or compute information about formulations, data, parameters, and utility."""

        # store data
        self.products = numpy.lib.recfunctions.rec_drop_fields(
            economy.products[economy._product_market_indices[t]],
            set(economy.products.dtype.names) & {'market_ids', 'X1', 'X3', 'ZD', 'ZS', 'demand_ids', 'supply_ids'}
        )
        self.agents = numpy.lib.recfunctions.rec_drop_fields(
            economy.agents[economy._agent_market_indices[t]], 'market_ids'
        )

        # create nesting groups
        self.groups = Groups(self.products.nesting_ids)

        # count dimensions
        self.J = self.products.shape[0]
        self.I = self.agents.shape[0]
        self.K1 = economy.K1
        self.K2 = economy.K2
        self.K3 = economy.K3
        self.D = economy.D
        self.H = self.groups.unique.size

        # identify column formulations
        self._X1_formulations = economy._X1_formulations
        self._X2_formulations = economy._X2_formulations
        self._X3_formulations = economy._X3_formulations
        self._demographics_formulations = economy._demographics_formulations

        # override any data
        if data_override is not None:
            for name, variable in data_override.items():
                self.products[name][:] = variable[:]
            for index, formulation in enumerate(self._X2_formulations):
                if any(n in formulation.names for n in data_override):
                    self.products.X2[:, [index]] = formulation.evaluate(self.products)

        # store parameters (expand rho to all groups and all products)
        self.sigma = sigma
        self.pi = pi
        self.beta = beta
        if rho.size == 1:
            self.group_rho = np.full((self.H, 1), float(rho))
            self.rho = np.full((self.J, 1), float(rho))
        else:
            self.group_rho = rho[np.searchsorted(economy.unique_nesting_ids, self.groups.unique)]
            self.rho = self.groups.expand(self.group_rho)

        # store delta and compute mu
        self.delta = None if delta is None else delta[economy._product_market_indices[t]]
        self.mu = self.compute_mu()

    def get_membership_matrix(self) -> Array:
        """Build a membership matrix from nesting IDs."""
        tiled_ids = np.tile(self.products.nesting_ids, self.J)
        return np.where(tiled_ids == tiled_ids.T, 1, 0)

    def get_ownership_matrix(self, firms_index: int = 0) -> Array:
        """Get a pre-computed ownership matrix or build one. By default, use unchanged firm IDs."""

        # get a pre-computed ownership matrix
        if self.products.ownership.shape[1] > 0:
            offset = firms_index * self.products.ownership.shape[1] // self.products.firm_ids.shape[1]
            return self.products.ownership[:, offset:offset + self.J]

        # build a standard ownership matrix
        tiled_ids = np.tile(self.products.firm_ids[:, [firms_index]], self.J)
        return np.where(tiled_ids == tiled_ids.T, 1, 0)

    def compute_random_coefficients(self) -> Array:
        """Compute the random coefficients by weighting agent characteristics with nonlinear parameters."""
        coefficients = self.sigma @ self.agents.nodes.T
        if self.D > 0:
            coefficients += self.pi @ self.agents.demographics.T
        return coefficients

    def compute_mu(self, X2: Optional[Array] = None) -> Array:
        """Compute mu. By default, use the unchanged X2."""
        if X2 is None:
            X2 = self.products.X2
        return X2 @ self.compute_random_coefficients()

    def update_delta_with_variable(self, name: str, variable: Array) -> Array:
        """Update delta to reflect a changed variable by adding any parameter-weighted characteristic changes to X1."""
        assert self.beta is not None and self.delta is not None

        # if the variable does not contribute to X1, delta remains unchanged
        if not any(name in f.names for f in self._X1_formulations):
            return self.delta

        # if the variable does contribute to X1, delta may change
        delta = self.delta.copy()
        override = {name: variable}
        for index, formulation in enumerate(self._X1_formulations):
            if name in formulation.names:
                delta += self.beta[index] * (formulation.evaluate(self.products, override) - self.products[name])
        return delta

    def update_mu_with_variable(self, name: str, variable: Array) -> Array:
        """Update mu to reflect a changed variable by re-computing mu under the changed X2."""

        # if the variable does not contribute to X2, mu remains unchanged
        if not any(name in f.names for f in self._X2_formulations):
            return self.mu

        # if the variable does contribute to X2, mu may change
        X2 = self.products.X2.copy()
        override = {name: variable}
        for index, formulation in enumerate(self._X2_formulations):
            if name in formulation.names:
                X2[:, [index]] = formulation.evaluate(self.products, override)
        return self.compute_mu(X2)

    def compute_X1_derivatives(self, name: str, variable: Optional[Array] = None) -> Array:
        """Compute derivatives of X1 with respect to a variable. By default, use unchanged variable values."""
        override = None if variable is None else {name: variable}
        derivatives = np.zeros((self.J, self.K1), options.dtype)
        for index, formulation in enumerate(self._X1_formulations):
            if name in formulation.names:
                derivatives[:, [index]] = formulation.evaluate_derivative(name, self.products, override)
        return derivatives

    def compute_X2_derivatives(self, name: str, variable: Optional[Array] = None) -> Array:
        """Compute derivatives of X2 with respect to a variable. By default, use unchanged variable values."""
        override = None if variable is None else {name: variable}
        derivatives = np.zeros((self.J, self.K2), options.dtype)
        for index, formulation in enumerate(self._X2_formulations):
            if name in formulation.names:
                derivatives[:, [index]] = formulation.evaluate_derivative(name, self.products, override)
        return derivatives

    def compute_utility_derivatives(self, name: str, variable: Optional[Array] = None) -> Array:
        """Compute derivatives of utility with respect to a variable. By default, use unchanged variable values."""
        assert self.beta is not None
        derivatives = np.tile(self.compute_X1_derivatives(name, variable) @ self.beta, self.I)
        if self.K2 > 0:
            derivatives += self.compute_X2_derivatives(name, variable) @ self.compute_random_coefficients()
        return derivatives

    def compute_probabilities(
            self, delta: Array = None, mu: Optional[Array] = None, linear: bool = True,
            numerator: Optional[Array] = None, eliminate_product: Optional[int] = None,
            keep_conditionals: bool = False) -> Union[Tuple[Array, Optional[Array]], Array]:
        """Compute choice probabilities. By default, use unchanged delta and mu values. If linear is False, delta and mu
        must be specified and already be exponentiated. If the numerator is specified, it will be used as the numerator
        in the non-nested Logit expression. If eliminate_product is specified, eliminate the product associated with the
        specified index from the choice set. If keep_conditionals is True, return a tuple in which if there is nesting,
        the second element are conditional probabilities given that an alternative in a nest is chosen.
        """
        if delta is None:
            assert self.delta is not None
            delta = self.delta
        if mu is None:
            mu = self.mu
        if self.K2 == 0:
            mu = int(not linear)

        # compute exponentiated utilities, optionally eliminating a product from the choice set
        exp_utilities = np.exp(delta + mu) if linear else np.array(delta * mu)
        if eliminate_product is not None:
            exp_utilities[eliminate_product] = 0

        # compute standard or nested probabilities
        if self.H == 0:
            conditionals = None
            if numerator is None:
                numerator = exp_utilities
            probabilities = numerator / (1 + exp_utilities.sum(axis=0))
        else:
            exp_weighted_utilities = exp_utilities**(1 / (1 - self.rho))
            exp_inclusives = self.groups.sum(exp_weighted_utilities)
            exp_weighted_inclusives = exp_inclusives**(1 - self.group_rho)
            conditionals = exp_weighted_utilities / self.groups.expand(exp_inclusives)
            marginals = exp_weighted_inclusives / (1 + exp_weighted_inclusives.sum(axis=0))
            probabilities = conditionals * self.groups.expand(marginals)

        # return either probabilities and their conditional counterparts or just probabilities
        return (probabilities, conditionals) if keep_conditionals else probabilities

    def compute_capital_lamda(self, value_derivatives: Array) -> Array:
        """Compute the diagonal capital lambda matrix used to decompose markups."""
        diagonal = value_derivatives @ self.agents.weights
        if self.H > 0:
            diagonal /= 1 - self.rho
        return np.diagflat(diagonal)

    def compute_capital_gamma(
            self, value_derivatives: Array, probabilities: Array, conditionals: Optional[Array]) -> Array:
        """Compute  the dense capital gamma matrix used to decompose markups."""
        weighted_value_derivatives = self.agents.weights * value_derivatives.T
        capital_gamma = probabilities @ weighted_value_derivatives
        if self.H > 0:
            membership = self.get_membership_matrix()
            capital_gamma += self.rho / (1 - self.rho) * membership * (conditionals @ weighted_value_derivatives)
        return capital_gamma

    def compute_eta(
            self, ownership_matrix: Optional[Array] = None, utility_derivatives: Optional[Array] = None,
            prices: Optional[Array] = None) -> Tuple[Array, List[Error]]:
        """Compute the markup term in the BLP-markup equation. By default, get an unchanged ownership matrix, compute
        derivatives of utilities with respect to prices, and use unchanged prices.
        """
        errors: List[Error] = []
        if ownership_matrix is None:
            ownership_matrix = self.get_ownership_matrix()
        if utility_derivatives is None:
            utility_derivatives = self.compute_utility_derivatives('prices')
        if prices is None:
            probabilities, conditionals = self.compute_probabilities(keep_conditionals=True)
            shares = self.products.shares
        else:
            delta = self.update_delta_with_variable('prices', prices)
            mu = self.update_mu_with_variable('prices', prices)
            probabilities, conditionals = self.compute_probabilities(delta, mu)
            shares = probabilities @ self.agents.weights
        jacobian = self.compute_shares_by_variable_jacobian(utility_derivatives, probabilities, conditionals)
        intra_firm_jacobian = ownership_matrix * jacobian
        eta, replacement = approximately_solve(intra_firm_jacobian, -shares)
        if replacement:
            errors.append(exceptions.IntraFirmJacobianInversionError(intra_firm_jacobian, replacement))
        return eta, errors

    def compute_zeta(
            self, costs: Array, ownership_matrix: Optional[Array] = None, utility_derivatives: Optional[Array] = None,
            prices: Optional[Array] = None) -> Tuple[Array, List[Error]]:
        """Compute the markup term in the zeta-markup equation. By default, get an unchanged ownership matrix, compute
        derivatives of utilities with respect to prices, and use unchanged prices.
        """
        if ownership_matrix is None:
            ownership_matrix = self.get_ownership_matrix()
        if utility_derivatives is None:
            utility_derivatives = self.compute_utility_derivatives('prices')
        if prices is None:
            probabilities, conditionals = self.compute_probabilities(keep_conditionals=True)
            shares = self.products.shares
        else:
            delta = self.update_delta_with_variable('prices', prices)
            mu = self.update_mu_with_variable('prices', prices)
            probabilities, conditionals = self.compute_probabilities(delta, mu, keep_conditionals=True)
            shares = probabilities @ self.agents.weights
        value_derivatives = probabilities * utility_derivatives
        capital_lamda_inverse = np.diag(1 / self.compute_capital_lamda(value_derivatives).diagonal())
        capital_gamma = self.compute_capital_gamma(value_derivatives, probabilities, conditionals)
        tilde_capital_omega = capital_lamda_inverse @ (ownership_matrix * capital_gamma).T
        return tilde_capital_omega @ (prices - costs) - capital_lamda_inverse @ shares

    def compute_equilibrium_prices(
            self, costs: Array, iteration: Iteration, firms_index: int = 0, prices: Optional[Array] = None) -> (
            Tuple[Array, bool, int, int]):
        """Compute equilibrium prices by iterating over the zeta-markup equation. By default, use unchanged firm IDs
        and use unchanged prices as initial values.
        """
        if prices is None:
            prices = self.products.prices

        # derivatives of utilities with respect to prices change during iteration only if they depend on prices
        formulations = self._X1_formulations + self._X2_formulations
        if any(s.name == 'prices' for f in formulations for s in f.differentiate('prices').free_symbols):
            get_derivatives = lambda p: self.compute_utility_derivatives('prices', p)
        else:
            derivatives = self.compute_utility_derivatives('prices')
            get_derivatives = lambda _: derivatives

        # solve the fixed point problem
        ownership_matrix = self.get_ownership_matrix(firms_index)
        contraction = lambda p: costs + self.compute_zeta(costs, ownership_matrix, get_derivatives(p), p)
        prices, converged, iterations, evaluations = iteration._iterate(prices, contraction)
        return prices, converged, iterations, evaluations

    def compute_utility_derivatives_by_parameter_tangent(
            self, parameter: NonlinearParameter, X1_derivatives: Array, X2_derivatives: Array, beta_tangent: Array) -> (
            Array):
        """Compute the tangent with respect to a nonlinear parameter of derivatives of utility with respect to a
        variable.
        """
        tangent = np.tile(X1_derivatives @ beta_tangent, self.I)
        if isinstance(parameter, RandomCoefficientParameter):
            v = parameter.get_agent_characteristic(self.agents)
            tangent += X2_derivatives[:, [parameter.location[0]]] @ v.T
        return tangent

    def compute_probabilities_by_parameter_tangent(
            self, parameter: NonlinearParameter, probabilities: Array, conditionals: Optional[Array],
            delta: Optional[Array] = None, mu: Optional[Array] = None) -> Tuple[Array, Optional[Array]]:
        """Compute the tangent of probabilities with respect to a nonlinear parameter. By default, use unchanged delta
        and mu.
        """
        if delta is None:
            assert self.delta is not None
            delta = self.delta
        if mu is None:
            mu = self.mu

        # without nesting, compute only the tangent of probabilities with respect to the parameter
        if self.H == 0:
            assert isinstance(parameter, RandomCoefficientParameter)
            v = parameter.get_agent_characteristic(self.agents)
            x = parameter.get_product_characteristic(self.products)
            probabilities_tangent = probabilities * v.T * (x - x.T @ probabilities)
            return probabilities_tangent, None

        # marginal probabilities are needed to compute tangents with nesting
        marginals = self.groups.sum(probabilities)

        # compute the tangent of conditional and marginal probabilities with respect to the parameter
        if isinstance(parameter, RandomCoefficientParameter):
            v = parameter.get_agent_characteristic(self.agents)
            x = parameter.get_product_characteristic(self.products)

            # compute the tangent of conditional probabilities with respect to the parameter
            A = conditionals * x
            A_sums = self.groups.sum(A)
            conditionals_tangent = conditionals * v.T * (x - self.groups.expand(A_sums)) / (1 - self.rho)

            # compute the tangent of marginal probabilities with respect to the parameter
            B = marginals * A_sums * v.T
            marginals_tangent = B - marginals * B.sum(axis=0)
        else:
            assert isinstance(parameter, RhoParameter)
            group_associations = parameter.get_group_associations(self.groups)
            associations = self.groups.expand(group_associations)

            # utilities are needed to compute tangents with respect to rho
            weighted_utilities = (delta + mu) / (1 - self.rho)

            # compute the tangent of conditional probabilities with respect to the parameter
            A = conditionals * weighted_utilities / (1 - self.rho)
            A_sums = self.groups.sum(A)
            conditionals_tangent = associations * (A - conditionals * self.groups.expand(A_sums))

            # compute the tangent of marginal probabilities with respect to the parameter
            B = marginals * (A_sums * (1 - self.group_rho) - np.log(self.groups.sum(np.exp(weighted_utilities))))
            marginals_tangent = group_associations * B - marginals * (group_associations.T @ B)

        # compute the tangent of probabilities with respect to the parameter
        probabilities_tangent = (
            conditionals_tangent * self.groups.expand(marginals) +
            conditionals * self.groups.expand(marginals_tangent)
        )
        return probabilities_tangent, conditionals_tangent

    def compute_probabilities_by_xi_tensor(
            self, probabilities: Array, conditionals: Optional[Array]) -> Tuple[Array, Optional[Array]]:
        """Use choice probabilities to compute their tensor derivatives with respect to xi (equivalently, to delta),
        indexed with the first axis.
        """
        probabilities_tensor = -probabilities[None] * probabilities[None].swapaxes(0, 1)
        probabilities_tensor[np.diag_indices(self.J)] += probabilities
        conditionals_tensor = None
        if self.H > 0:
            assert conditionals is not None
            membership = self.get_membership_matrix()
            multiplied_probabilities = self.rho / (1 - self.rho) * probabilities
            multiplied_conditionals = 1 / (1 - self.rho) * conditionals
            probabilities_tensor -= membership[..., None] * (
                conditionals[None] * multiplied_probabilities[None].swapaxes(0, 1)
            )
            conditionals_tensor = -membership[..., None] * (
                conditionals[None] * multiplied_conditionals[None].swapaxes(0, 1)
            )
            probabilities_tensor[np.diag_indices(self.J)] += multiplied_probabilities
            conditionals_tensor[np.diag_indices(self.J)] += multiplied_conditionals
        return probabilities_tensor, conditionals_tensor

    def compute_shares_by_variable_jacobian(
            self, utility_derivatives: Array, probabilities: Optional[Array] = None,
            conditionals: Optional[Array] = None) -> Array:
        """Compute the Jacobian of market shares with respect to a variable. By default, compute unchanged choice
        probabilities.
        """
        if probabilities is None or conditionals is None:
            probabilities, conditionals = self.compute_probabilities(keep_conditionals=True)
        value_derivatives = probabilities * utility_derivatives
        capital_lamda = self.compute_capital_lamda(value_derivatives)
        capital_gamma = self.compute_capital_gamma(value_derivatives, probabilities, conditionals)
        return capital_lamda - capital_gamma

    def compute_shares_by_xi_jacobian(self, probabilities: Array, conditionals: Optional[Array]) -> Array:
        """Compute the Jacobian of shares with respect to xi (equivalently, to delta)."""
        diagonal_shares = np.diagflat(self.products.shares)
        weighted_probabilities = self.agents.weights * probabilities.T
        jacobian = diagonal_shares - probabilities @ weighted_probabilities
        if self.H > 0:
            membership = self.get_membership_matrix()
            jacobian += self.rho / (1 - self.rho) * (
                diagonal_shares - membership * (conditionals @ weighted_probabilities)
            )
        return jacobian

    def compute_shares_by_theta_jacobian(
            self, nonlinear_parameters: NonlinearParameters, delta: Array, probabilities: Array,
            conditionals: Optional[Array]) -> Array:
        """Compute the Jacobian of shares with respect to theta."""
        jacobian = np.zeros((self.J, nonlinear_parameters.P), options.dtype)
        for p, parameter in enumerate(nonlinear_parameters.unfixed):
            tangent, _ = self.compute_probabilities_by_parameter_tangent(
                parameter, probabilities, conditionals, delta
            )
            jacobian[:, [p]] = tangent @ self.agents.weights
        return jacobian

    def compute_capital_lamda_by_parameter_tangent(
            self, parameter: NonlinearParameter, value_derivatives: Array, value_derivatives_tangent: Array) -> Array:
        """Compute the tangent of the diagonal capital lambda matrix with respect to a nonlinear parameter."""
        diagonal = value_derivatives_tangent @ self.agents.weights
        if self.H > 0:
            diagonal /= 1 - self.rho
            if isinstance(parameter, RhoParameter):
                associations = self.groups.expand(parameter.get_group_associations(self.groups))
                diagonal += associations / (1 - self.rho)**2 * (value_derivatives @ self.agents.weights)
        return np.diagflat(diagonal)

    def compute_capital_lamda_by_xi_tensor(self, value_derivatives_tensor: Array) -> Array:
        """Compute the tensor derivative of the diagonal capital lambda matrix with respect to xi, indexed by the first
        axis.
        """
        diagonal = np.squeeze(multiply_tensor_and_matrix(value_derivatives_tensor, self.agents.weights))
        if self.H > 0:
            diagonal /= 1 - self.rho
        tensor = np.zeros((self.J, self.J, self.J), options.dtype)
        tensor[:, np.arange(self.J), np.arange(self.J)] = diagonal
        return tensor

    def compute_capital_gamma_by_parameter_tangent(
            self, parameter: NonlinearParameter, value_derivatives: Array, value_derivatives_tangent: Array,
            probabilities: Array, probabilities_tangent: Array, conditionals: Optional[Array],
            conditionals_tangent: Optional[Array]) -> Array:
        """Compute the tangent of the dense capital gamma matrix with respect to a nonlinear parameter."""
        weighted_value_derivatives = self.agents.weights * value_derivatives.T
        weighted_value_derivatives_tangent = self.agents.weights * value_derivatives_tangent.T
        tangent = (
            probabilities_tangent @ weighted_value_derivatives +
            probabilities @ weighted_value_derivatives_tangent
        )
        if self.H > 0:
            assert conditionals is not None and conditionals_tangent is not None
            membership = self.get_membership_matrix()
            tangent += membership * self.rho / (1 - self.rho) * (
                conditionals_tangent @ weighted_value_derivatives +
                conditionals @ weighted_value_derivatives_tangent
            )
            if isinstance(parameter, RhoParameter):
                associations = self.groups.expand(parameter.get_group_associations(self.groups))
                tangent += associations * membership / (1 - self.rho)**2 * (conditionals @ weighted_value_derivatives)
        return tangent

    def compute_capital_gamma_by_xi_tensor(
            self, value_derivatives: Array, value_derivatives_tensor: Array, probabilities: Array,
            probabilities_tensor: Array, conditionals: Optional[Array], conditionals_tensor: Optional[Array]) -> Array:
        """Compute the tensor derivative of the dense capital gamma matrix with respect to xi, indexed with the first
        axis.
        """
        weighted_value_derivatives = self.agents.weights * value_derivatives.T
        weighted_probabilities = self.agents.weights.T * probabilities
        tensor = (
            multiply_tensor_and_matrix(probabilities_tensor, weighted_value_derivatives) +
            multiply_matrix_and_tensor(weighted_probabilities, value_derivatives_tensor.swapaxes(1, 2))
        )
        if self.H > 0:
            assert conditionals is not None and conditionals_tensor is not None
            membership = self.get_membership_matrix()
            weighted_conditionals = self.agents.weights.T * conditionals
            tensor += membership[None] * self.rho[None] / (1 - self.rho[None]) * (
                multiply_tensor_and_matrix(conditionals_tensor, weighted_value_derivatives) +
                multiply_matrix_and_tensor(weighted_conditionals, value_derivatives_tensor.swapaxes(1, 2))
            )
        return tensor

    def compute_eta_by_beta_jacobian(self) -> Tuple[Array, List[Error]]:
        """Compute the Jacobian of the markup term in the BLP-markup equation with respect to beta."""
        errors: List[Error] = []

        # compute derivatives of aggregate inclusive values with respect to prices
        probabilities, conditionals = self.compute_probabilities(keep_conditionals=True)
        utility_derivatives = self.compute_utility_derivatives('prices')
        value_derivatives = probabilities * utility_derivatives

        # compute the matrix A, which, when inverted and multiplied by shares, gives eta (negative the intra-firm
        #   Jacobian of shares with respect to prices)
        ownership = self.get_ownership_matrix()
        capital_lamda = self.compute_capital_lamda(value_derivatives)
        capital_gamma = self.compute_capital_gamma(value_derivatives, probabilities, conditionals)
        A = -ownership * (capital_lamda - capital_gamma)

        # compute the inverse of A and use it to compute eta
        A_inverse, replacement = approximately_invert(A)
        if replacement:
            errors.append(exceptions.IntraFirmJacobianInversionError(A, replacement))
        eta = A_inverse @ self.products.shares

        # compute the derivatives of X1 with respect to prices
        X1_derivatives = self.compute_X1_derivatives('prices')

        # fill the Jacobian of eta with respect to beta parameter-by-parameter
        eta_jacobian = np.zeros((self.J, self.K1), options.dtype)
        for k in range(self.K1):
            # columns associated with exogenous characteristics are zero
            if 'prices' not in self._X1_formulations[k].names:
                continue

            # compute the tangent with respect to the parameter of derivatives of aggregate inclusive values
            value_derivatives_tangent = probabilities * X1_derivatives[:, [k]]

            # compute the tangent of A with respect to the parameters (only the derivatives of aggregate inclusive
            #   values are functions of beta, so the functions for computing capital lambda and gamma can be used
            #   directly)
            capital_lamda_tangent = self.compute_capital_lamda(value_derivatives_tangent)
            capital_gamma_tangent = self.compute_capital_gamma(value_derivatives_tangent, probabilities, conditionals)
            A_tangent = -ownership * (capital_lamda_tangent - capital_gamma_tangent)

            # compute the associated tangent of eta
            eta_jacobian[:, [k]] = -A_inverse @ (A_tangent @ eta)

        # return the filled Jacobian
        return eta_jacobian, errors

    def compute_eta_by_theta_jacobian(
            self, xi_jacobian: Array, beta_jacobian: Array, nonlinear_parameters: NonlinearParameters) -> (
            Tuple[Array, List[Error]]):
        """Compute the Jacobian of the markup term in the BLP-markup equation with respect to theta."""
        errors: List[Error] = []

        # compute derivatives of aggregate inclusive values with respect to prices
        probabilities, conditionals = self.compute_probabilities(keep_conditionals=True)
        utility_derivatives = self.compute_utility_derivatives('prices')
        value_derivatives = probabilities * utility_derivatives

        # compute the matrix A, which, when inverted and multiplied by shares, gives eta (negative the intra-firm
        #   Jacobian of shares with respect to prices)
        ownership = self.get_ownership_matrix()
        capital_lamda = self.compute_capital_lamda(value_derivatives)
        capital_gamma = self.compute_capital_gamma(value_derivatives, probabilities, conditionals)
        A = -ownership * (capital_lamda - capital_gamma)

        # compute the inverse of A and use it to compute eta
        A_inverse, replacement = approximately_invert(A)
        if replacement:
            errors.append(exceptions.IntraFirmJacobianInversionError(A, replacement))
        eta = A_inverse @ self.products.shares

        # compute the tensor derivative with respect to xi (equivalently, to delta), indexed with the first axis, of
        #   derivatives of aggregate inclusive values
        probabilities_tensor, conditionals_tensor = self.compute_probabilities_by_xi_tensor(probabilities, conditionals)
        value_derivatives_tensor = probabilities_tensor * utility_derivatives

        # compute the tensor derivative of A with respect to xi (equivalently, to delta)
        capital_lamda_tensor = self.compute_capital_lamda_by_xi_tensor(value_derivatives_tensor)
        capital_gamma_tensor = self.compute_capital_gamma_by_xi_tensor(
            value_derivatives, value_derivatives_tensor, probabilities, probabilities_tensor, conditionals,
            conditionals_tensor
        )
        A_tensor = -ownership[None] * (capital_lamda_tensor - capital_gamma_tensor)

        # compute the product of the tensor and eta
        A_tensor_times_eta = np.squeeze(multiply_tensor_and_matrix(A_tensor, eta))

        # compute derivatives of X1 and X2 with respect to prices
        X1_derivatives = self.compute_X1_derivatives('prices')
        X2_derivatives = self.compute_X2_derivatives('prices')

        # fill the Jacobian of eta with respect to theta parameter-by-parameter
        eta_jacobian = np.zeros((self.J, nonlinear_parameters.P), options.dtype)
        for p, parameter in enumerate(nonlinear_parameters.unfixed):
            # compute the tangent with respect to the parameter of derivatives of aggregate inclusive values
            probabilities_tangent, conditionals_tangent = self.compute_probabilities_by_parameter_tangent(
                parameter, probabilities, conditionals
            )
            utility_derivatives_tangent = self.compute_utility_derivatives_by_parameter_tangent(
                parameter, X1_derivatives, X2_derivatives, beta_jacobian[:, [p]]
            )
            value_derivatives_tangent = (
                probabilities_tangent * utility_derivatives +
                probabilities * utility_derivatives_tangent
            )

            # compute the tangent of A with respect to the parameter
            capital_lamda_tangent = self.compute_capital_lamda_by_parameter_tangent(
                parameter, value_derivatives, value_derivatives_tangent
            )
            capital_gamma_tangent = self.compute_capital_gamma_by_parameter_tangent(
                parameter, value_derivatives, value_derivatives_tangent, probabilities, probabilities_tangent,
                conditionals, conditionals_tangent
            )
            A_tangent = -ownership * (capital_lamda_tangent - capital_gamma_tangent)

            # extract the tangent of xi with respect to the parameter and compute the associated tangent of eta
            eta_jacobian[:, [p]] = -A_inverse @ (A_tangent @ eta + A_tensor_times_eta.T @ xi_jacobian[:, [p]])

        # return the filled Jacobian
        return eta_jacobian, errors

    def compute_xi_by_theta_jacobian(
            self, nonlinear_parameters: NonlinearParameters, delta: Optional[Array] = None) -> (
            Tuple[Array, List[Error]]):
        """Use the Implicit Function Theorem to compute the Jacobian of xi (equivalently, of delta) with respect to
        theta. By default, use unchanged delta values.
        """
        errors: List[Error] = []
        if delta is None:
            assert self.delta is not None
            delta = self.delta

        # configure NumPy to identify floating point errors
        with np.errstate(divide='call', over='call', under='ignore', invalid='call'):
            np.seterrcall(lambda *_: errors.append(exceptions.XiByThetaJacobianFloatingPointError()))

            # compute the Jacobian
            probabilities, conditionals = self.compute_probabilities(delta, keep_conditionals=True)
            shares_by_xi_jacobian = self.compute_shares_by_xi_jacobian(probabilities, conditionals)
            shares_by_theta_jacobian = self.compute_shares_by_theta_jacobian(
                nonlinear_parameters, delta, probabilities, conditionals
            )
            xi_by_theta_jacobian, replacement = approximately_solve(shares_by_xi_jacobian, -shares_by_theta_jacobian)
            if replacement:
                errors.append(exceptions.SharesByXiJacobianInversionError(shares_by_xi_jacobian, replacement))
            return xi_by_theta_jacobian, errors

    def compute_omega_by_theta_jacobian(
            self, tilde_costs: Array, xi_jacobian: Array, beta_jacobian: Array,
            nonlinear_parameters: NonlinearParameters, costs_type: str) -> Tuple[Array, List[Error]]:
        """Compute the Jacobian of omega (equivalently, of transformed marginal costs) with respect to theta."""
        errors: List[Error] = []

        # configure NumPy to identify floating point errors
        with np.errstate(divide='call', over='call', under='ignore', invalid='call'):
            np.seterrcall(lambda *_: errors.append(exceptions.OmegaByThetaJacobianFloatingPointError()))

            # compute the Jacobian
            eta_jacobian, eta_jacobian_errors = self.compute_eta_by_theta_jacobian(
                xi_jacobian, beta_jacobian, nonlinear_parameters
            )
            errors.extend(eta_jacobian_errors)
            if costs_type == 'linear':
                omega_jacobian = -eta_jacobian
            else:
                assert costs_type == 'log'
                omega_jacobian = -eta_jacobian / np.exp(tilde_costs)
            return omega_jacobian, errors

    def compute_omega_by_beta_jacobian(self, tilde_costs: Array, costs_type: str) -> Tuple[Array, List[Error]]:
        """Compute the Jacobian of omega (equivalently, of transformed marginal costs) with respect to beta."""
        errors: List[Error] = []

        # configure NumPy to identify floating point errors
        with np.errstate(divide='call', over='call', under='ignore', invalid='call'):
            np.seterrcall(lambda *_: errors.append(exceptions.OmegaByBetaJacobianFloatingPointError()))

            # compute the Jacobian
            eta_jacobian, eta_jacobian_errors = self.compute_eta_by_beta_jacobian()
            errors.extend(eta_jacobian_errors)
            if costs_type == 'linear':
                omega_jacobian = -eta_jacobian
            else:
                assert costs_type == 'log'
                omega_jacobian = -eta_jacobian / np.exp(tilde_costs)
            return omega_jacobian, errors