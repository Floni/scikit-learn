"""Transformers for missing value imputation"""
# Authors: Nicolas Tresegnie <nicolas.tresegnie@gmail.com>
#          Ashim Bhattarai <"ashimb9" + "\100gmail\56com">
# License: BSD 3 clause

import warnings

import numpy as np
import numpy.ma as ma
from scipy import sparse
from scipy import stats

from .base import BaseEstimator, TransformerMixin
from .utils import check_array, _get_n_jobs
from .utils.sparsefuncs import _get_median
from .utils.validation import check_is_fitted
from .utils.validation import FLOAT_DTYPES
from .metrics import pairwise_distances

from .neighbors.base import _check_weights
from .neighbors.base import _get_weights

from .externals import six

zip = six.moves.zip
map = six.moves.map

__all__ = [
    'SimpleImputer',
    'KNNImputer',
]


def _get_mask(X, value_to_mask):
    """Compute the boolean mask X == missing_values."""
    if value_to_mask == "NaN" or np.isnan(value_to_mask):
        return np.isnan(X)
    else:
        return X == value_to_mask


def _most_frequent(array, extra_value, n_repeat):
    """Compute the most frequent value in a 1d array extended with
       [extra_value] * n_repeat, where extra_value is assumed to be not part
       of the array."""
    # Compute the most frequent value in array only
    if array.size > 0:
        mode = stats.mode(array)
        most_frequent_value = mode[0][0]
        most_frequent_count = mode[1][0]
    else:
        most_frequent_value = 0
        most_frequent_count = 0

    # Compare to array + [extra_value] * n_repeat
    if most_frequent_count == 0 and n_repeat == 0:
        return np.nan
    elif most_frequent_count < n_repeat:
        return extra_value
    elif most_frequent_count > n_repeat:
        return most_frequent_value
    elif most_frequent_count == n_repeat:
        # Ties the breaks. Copy the behaviour of scipy.stats.mode
        if most_frequent_value < extra_value:
            return most_frequent_value
        else:
            return extra_value


class SimpleImputer(BaseEstimator, TransformerMixin):
    """Imputation transformer for completing missing values.

    Read more in the :ref:`User Guide <impute>`.

    Parameters
    ----------
    missing_values : integer or "NaN", optional (default="NaN")
        The placeholder for the missing values. All occurrences of
        `missing_values` will be imputed. For missing values encoded as np.nan,
        use the string value "NaN".

    strategy : string, optional (default="mean")
        The imputation strategy.

        - If "mean", then replace missing values using the mean along
          each column.
        - If "median", then replace missing values using the median along
          each column.
        - If "most_frequent", then replace missing using the most frequent
          value along each column.

    verbose : integer, optional (default=0)
        Controls the verbosity of the imputer.

    copy : boolean, optional (default=True)
        If True, a copy of X will be created. If False, imputation will
        be done in-place whenever possible. Note that, in the following cases,
        a new copy will always be made, even if `copy=False`:

        - If X is not an array of floating values;
        - If X is sparse and `missing_values=0`;
        - If X is encoded as a CSR matrix.

    Attributes
    ----------
    statistics_ : array of shape (n_features,)
        The imputation fill value for each feature.

    Notes
    -----
    Columns which only contained missing values at `fit` are discarded upon
    `transform`.

    """
    def __init__(self, missing_values="NaN", strategy="mean",
                 verbose=0, copy=True):
        self.missing_values = missing_values
        self.strategy = strategy
        self.verbose = verbose
        self.copy = copy

    def fit(self, X, y=None):
        """Fit the imputer on X.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            Input data, where ``n_samples`` is the number of samples and
            ``n_features`` is the number of features.

        Returns
        -------
        self : SimpleImputer
        """
        # Check parameters
        allowed_strategies = ["mean", "median", "most_frequent"]
        if self.strategy not in allowed_strategies:
            raise ValueError("Can only use these strategies: {0} "
                             " got strategy={1}".format(allowed_strategies,
                                                        self.strategy))

        X = check_array(X, accept_sparse='csc', dtype=FLOAT_DTYPES,
                        force_all_finite='allow-nan'
                        if self.missing_values == 'NaN'
                        or np.isnan(self.missing_values) else True)

        if sparse.issparse(X):
            self.statistics_ = self._sparse_fit(X,
                                                self.strategy,
                                                self.missing_values)
        else:
            self.statistics_ = self._dense_fit(X,
                                               self.strategy,
                                               self.missing_values)

        return self

    def _sparse_fit(self, X, strategy, missing_values):
        """Fit the transformer on sparse data."""
        # Count the zeros
        if missing_values == 0:
            n_zeros_axis = np.zeros(X.shape[1], dtype=int)
        else:
            n_zeros_axis = X.shape[0] - np.diff(X.indptr)

        # Mean
        if strategy == "mean":
            if missing_values != 0:
                n_non_missing = n_zeros_axis

                # Mask the missing elements
                mask_missing_values = _get_mask(X.data, missing_values)
                mask_valids = np.logical_not(mask_missing_values)

                # Sum only the valid elements
                new_data = X.data.copy()
                new_data[mask_missing_values] = 0
                X = sparse.csc_matrix((new_data, X.indices, X.indptr),
                                      copy=False)
                sums = X.sum(axis=0)

                # Count the elements != 0
                mask_non_zeros = sparse.csc_matrix(
                    (mask_valids.astype(np.float64),
                     X.indices,
                     X.indptr), copy=False)
                s = mask_non_zeros.sum(axis=0)
                n_non_missing = np.add(n_non_missing, s)

            else:
                sums = X.sum(axis=0)
                n_non_missing = np.diff(X.indptr)

            # Ignore the error, columns with a np.nan statistics_
            # are not an error at this point. These columns will
            # be removed in transform
            with np.errstate(all="ignore"):
                return np.ravel(sums) / np.ravel(n_non_missing)

        # Median + Most frequent
        else:
            # Remove the missing values, for each column
            columns_all = np.hsplit(X.data, X.indptr[1:-1])
            mask_missing_values = _get_mask(X.data, missing_values)
            mask_valids = np.hsplit(np.logical_not(mask_missing_values),
                                    X.indptr[1:-1])

            # astype necessary for bug in numpy.hsplit before v1.9
            columns = [col[mask.astype(bool, copy=False)]
                       for col, mask in zip(columns_all, mask_valids)]

            # Median
            if strategy == "median":
                median = np.empty(len(columns))
                for i, column in enumerate(columns):
                    median[i] = _get_median(column, n_zeros_axis[i])

                return median

            # Most frequent
            elif strategy == "most_frequent":
                most_frequent = np.empty(len(columns))

                for i, column in enumerate(columns):
                    most_frequent[i] = _most_frequent(column,
                                                      0,
                                                      n_zeros_axis[i])

                return most_frequent

    def _dense_fit(self, X, strategy, missing_values):
        """Fit the transformer on dense data."""
        X = check_array(X, force_all_finite='allow-nan'
                        if self.missing_values == 'NaN'
                        or np.isnan(self.missing_values) else True)
        mask = _get_mask(X, missing_values)
        masked_X = ma.masked_array(X, mask=mask)

        # Mean
        if strategy == "mean":
            mean_masked = np.ma.mean(masked_X, axis=0)
            # Avoid the warning "Warning: converting a masked element to nan."
            mean = np.ma.getdata(mean_masked)
            mean[np.ma.getmask(mean_masked)] = np.nan

            return mean

        # Median
        elif strategy == "median":
            median_masked = np.ma.median(masked_X, axis=0)
            # Avoid the warning "Warning: converting a masked element to nan."
            median = np.ma.getdata(median_masked)
            median[np.ma.getmaskarray(median_masked)] = np.nan

            return median

        # Most frequent
        elif strategy == "most_frequent":
            # scipy.stats.mstats.mode cannot be used because it will no work
            # properly if the first element is masked and if its frequency
            # is equal to the frequency of the most frequent valid element
            # See https://github.com/scipy/scipy/issues/2636

            # To be able access the elements by columns
            X = X.transpose()
            mask = mask.transpose()

            most_frequent = np.empty(X.shape[0])

            for i, (row, row_mask) in enumerate(zip(X[:], mask[:])):
                row_mask = np.logical_not(row_mask).astype(np.bool)
                row = row[row_mask]
                most_frequent[i] = _most_frequent(row, np.nan, 0)

            return most_frequent

    def transform(self, X):
        """Impute all missing values in X.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            The input data to complete.
        """
        check_is_fitted(self, 'statistics_')
        X = check_array(X, accept_sparse='csc', dtype=FLOAT_DTYPES,
                        force_all_finite='allow-nan'
                        if self.missing_values == 'NaN'
                        or np.isnan(self.missing_values) else True,
                        copy=self.copy)
        statistics = self.statistics_
        if X.shape[1] != statistics.shape[0]:
            raise ValueError("X has %d features per sample, expected %d"
                             % (X.shape[1], self.statistics_.shape[0]))

        # Delete the invalid columns
        invalid_mask = np.isnan(statistics)
        valid_mask = np.logical_not(invalid_mask)
        valid_statistics = statistics[valid_mask]
        valid_statistics_indexes = np.flatnonzero(valid_mask)
        missing = np.arange(X.shape[1])[invalid_mask]

        if invalid_mask.any():
            if self.verbose:
                warnings.warn("Deleting features without "
                              "observed values: %s" % missing)
            X = X[:, valid_statistics_indexes]

        # Do actual imputation
        if sparse.issparse(X) and self.missing_values != 0:
            mask = _get_mask(X.data, self.missing_values)
            indexes = np.repeat(np.arange(len(X.indptr) - 1, dtype=np.int),
                                np.diff(X.indptr))[mask]

            X.data[mask] = valid_statistics[indexes].astype(X.dtype,
                                                            copy=False)
        else:
            if sparse.issparse(X):
                X = X.toarray()

            mask = _get_mask(X, self.missing_values)
            n_missing = np.sum(mask, axis=0)
            values = np.repeat(valid_statistics, n_missing)

            coordinates = np.where(mask.transpose())[::-1]

            X[coordinates] = values

        return X


class KNNImputer(BaseEstimator, TransformerMixin):
    """Imputation for completing missing values using k-Nearest Neighbors.

    Each sample's missing values are imputed using values from ``n_neighbors``
    nearest neighbors found in the training set. Each missing feature is then
    imputed as the average, either weighted or unweighted, of these neighbors.
    Note that if a sample has more than one feature missing, then the
    neighbors for that sample can be different depending on the particular
    feature being imputed. Finally, where the number of donor neighbors is
    less than ``n_neighbors``, the training set average for that feature is
    used during imputation.

    Parameters
    ----------
    missing_values : integer or "NaN", optional (default = "NaN")
        The placeholder for the missing values. All occurrences of
        `missing_values` will be imputed. For missing values encoded as
        ``np.nan``, use the string value "NaN".

    n_neighbors : int, optional (default = 5)
        Number of neighboring samples to use for imputation.

    weights : str or callable, optional (default = "uniform")
        Weight function used in prediction.  Possible values:

        - 'uniform' : uniform weights.  All points in each neighborhood
          are weighted equally.
        - 'distance' : weight points by the inverse of their distance.
          in this case, closer neighbors of a query point will have a
          greater influence than neighbors which are further away.
        - [callable] : a user-defined function which accepts an
          array of distances, and returns an array of the same shape
          containing the weights.

    metric : str or callable, optional (default = "masked_euclidean")
        Distance metric for searching neighbors. Possible values:
        - 'masked_euclidean'
        - [callable] : a user-defined function which conforms to the
        definition of _pairwise_callable(X, Y, metric, **kwds). In other
        words, the function accepts two arrays, X and Y, and a
        ``missing_values`` keyword in **kwds and returns a scalar distance
        value.

    row_max_missing : float, optional (default = 0.5)
        The maximum fraction of columns (i.e. features) that can be missing
        before the sample is excluded from nearest neighbor imputation. It
        means that such rows will not be considered a potential donor in
        ``fit()``, and in ``transform()`` their missing feature values will be
        imputed to be the column mean for the entire dataset.

    col_max_missing : float, optional (default = 0.8)
        The maximum fraction of rows (or samples) that can be missing
        for any feature beyond which an error is raised.

    copy : boolean, optional (default = True)
        If True, a copy of X will be created. If False, imputation will
        be done in-place whenever possible. Note that, if metric is
        "masked_euclidean" and copy=False then missing_values in the
        input matrix X will be overwritten with zeros.

    Attributes
    ----------
    statistics_ : 1-D array of length {n_features}
        The 1-D array contains the mean of each feature calculated using
        observed (i.e. non-missing) values. This is used for imputing
        missing values in samples that are either excluded from nearest
        neighbors search because they have too many ( > row_max_missing)
        missing features or because all of the sample's k-nearest neighbors
        (i.e., the potential donors) also have the relevant feature value
        missing.

    References
    ----------
    * Olga Troyanskaya, Michael Cantor, Gavin Sherlock, Pat Brown, Trevor
      Hastie, Robert Tibshirani, David Botstein and Russ B. Altman, Missing
      value estimation methods for DNA microarrays, BIOINFORMATICS Vol. 17
      no. 6, 2001 Pages 520-525.

    Examples
    --------
    >>> from sklearn.impute import KNNImputer
    >>> nan = float("NaN")
    >>> X = [[1, 2, nan], [3, 4, 3], [nan, 6, 5], [8, 8, 7]]
    >>> imputer = KNNImputer(n_neighbors=2, weights="uniform")
    >>> imputer.fit_transform(X)
    array([[1. , 2. , 4. ],
           [3. , 4. , 3. ],
           [5.5, 6. , 5. ],
           [8. , 8. , 7. ]])
    """

    def __init__(self, missing_values="NaN", n_neighbors=5,
                 weights="uniform", metric="masked_euclidean",
                 row_max_missing=0.5, col_max_missing=0.8, copy=True):

        self.missing_values = missing_values
        self.n_neighbors = n_neighbors
        self.weights = weights
        self.metric = metric
        self.row_max_missing = row_max_missing
        self.col_max_missing = col_max_missing
        self.copy = copy

    def _impute(self, dist, X, fitted_X, mask, mask_fx):
        """Helper function to find and impute missing values"""

        # For each column, find and impute
        n_rows_X, n_cols_X = X.shape
        for c in range(n_cols_X):
            if not np.any(mask[:, c], axis=0):
                continue

            # Row index for receivers and potential donors (pdonors)
            receivers_row_idx = np.where(mask[:, c])[0]
            pdonors_row_idx = np.where(~mask_fx[:, c])[0]

            # Impute using column mean if n_neighbors are not available
            if len(pdonors_row_idx) < self.n_neighbors:
                warnings.warn("Insufficient number of neighbors! "
                              "Filling in column mean.")
                X[receivers_row_idx, c] = self.statistics_[c]
                continue

            # Get distance from potential donors
            dist_pdonors = dist[receivers_row_idx][:, pdonors_row_idx]
            dist_pdonors = dist_pdonors.reshape(-1,
                                                len(pdonors_row_idx))
            # Argpartition to seperate actual donors from the rest
            pdonors_idx = np.argpartition(
                dist_pdonors, self.n_neighbors - 1, axis=1)

            # Get final donors row index from pdonors
            donors_idx = pdonors_idx[:, :self.n_neighbors]
            # Get weights or None
            dist_pdonors_rows = np.arange(len(donors_idx))[:, None]
            weight_matrix = _get_weights(
                dist_pdonors[
                    dist_pdonors_rows, donors_idx], self.weights)
            donor_row_idx_ravel = donors_idx.ravel()

            # Retrieve donor cells and calculate kNN score
            fitted_X_temp = fitted_X[pdonors_row_idx]
            donors = fitted_X_temp[donor_row_idx_ravel, c].reshape(
                (-1, self.n_neighbors))
            donors_mask = _get_mask(donors, self.missing_values)
            donors = np.ma.array(donors, mask=donors_mask)

            # Final imputation
            imputed = np.ma.average(donors, axis=1,
                                    weights=weight_matrix)
            X[receivers_row_idx, c] = imputed.data
        return X

    def fit(self, X, y=None):
        """Fit the imputer on X.

        Parameters
        ----------
        X : {array-like}, shape (n_samples, n_features)
            Input data, where ``n_samples`` is the number of samples and
            ``n_features`` is the number of features.

        Returns
        -------
        self : object
            Returns self.
        """

        # Check parameters
        force_all_finite = False if self.missing_values in ["NaN",
                                                            np.nan] else True
        X = check_array(X, accept_sparse=False, dtype=np.float64,
                        force_all_finite=force_all_finite, copy=self.copy)
        self.weights = _check_weights(self.weights)

        # Check for +/- inf
        if np.any(np.isinf(X)):
            raise ValueError("+/- inf values are not allowed.")

        # Check if % missing in any column > col_max_missing
        mask = _get_mask(X, self.missing_values)
        if np.any(mask.sum(axis=0) > (X.shape[0] * self.col_max_missing)):
            raise ValueError("Some column(s) have more than {}% missing values"
                             .format(self.col_max_missing * 100))
        X_col_means = np.ma.array(X, mask=mask).mean(axis=0).data

        # Check if % missing in any row > row_max_missing
        bad_rows = mask.sum(axis=1) > (mask.shape[1] * self.row_max_missing)
        if np.any(bad_rows):
            warnings.warn(
                "There are rows with more than {0}% missing values. These "
                "rows are not included as donor neighbors."
                    .format(self.row_max_missing * 100))

            # Remove rows that have more than row_max_missing % missing
            X = X[~bad_rows, :]

        # Check if sufficient neighboring samples available
        if X.shape[0] < self.n_neighbors:
            raise ValueError("There are only %d samples, but n_neighbors=%d."
                             % (X.shape[0], self.n_neighbors))

        # Instantiate NN object, get column means, and store in statistics_
        # neigh = NearestNeighbors(n_neighbors=self.n_neighbors,
        #                          metric=self.metric,
        #                          metric_params={"missing_values":
        #                                         self.missing_values})
        # self._fitted_neighbors = neigh.fit(X)
        self.fitted_X_ = X
        self.statistics_ = X_col_means

        return self

    def transform(self, X):
        """Impute all missing values in X.

        Parameters
        ----------
        X : {array-like}, shape = [n_samples, n_features]
            The input data to complete.

        Returns
        -------
        X : {array-like}, shape = [n_samples, n_features]
            The imputed dataset.
        """

        check_is_fitted(self, ["fitted_X_", "statistics_"])
        force_all_finite = False if self.missing_values in ["NaN",
                                                            np.nan] else True
        X = check_array(X, accept_sparse=False, dtype=FLOAT_DTYPES,
                        force_all_finite=force_all_finite, copy=self.copy)
        # Check for +/- inf
        if np.any(np.isinf(X)):
            raise ValueError("+/- inf values are not allowed in data to be "
                             "transformed.")

        # Get fitted data and ensure correct dimension
        # fitted_X = self._fitted_neighbors._fit_X
        n_rows_fit_X, n_cols_fit_X = self.fitted_X_.shape
        n_rows_X, n_cols_X = X.shape

        if n_cols_X != n_cols_fit_X:
            raise ValueError("Incompatible dimension between the fitted "
                             "dataset and the one to be transformed.")
        mask = _get_mask(X, self.missing_values)

        row_total_missing = mask.sum(axis=1)
        if not np.any(row_total_missing):
            return X

        # Check for excessive missingness in rows
        bad_rows = row_total_missing > (mask.shape[1] * self.row_max_missing)
        if np.any(bad_rows):
            warnings.warn(
                "There are rows with more than {0}% missing values. The "
                "missing features in these rows are imputed with column means."
                    .format(self.row_max_missing * 100))
            X_bad = X[bad_rows, :]
            X = X[~bad_rows, :]
            mask = mask[~bad_rows]
            row_total_missing = mask.sum(axis=1)
        row_has_missing = row_total_missing.astype(np.bool)

        if np.any(row_has_missing):

            # Mask for fitted_X
            mask_fx = _get_mask(self.fitted_X_, self.missing_values)

            # Get row index of missing and distance from donors
            dist_temp = pairwise_distances(X[row_has_missing],
                                           self.fitted_X_,
                                           metric=self.metric,
                                           squared=False,
                                           missing_values=self.missing_values)
            dist = np.empty((n_rows_X, n_rows_X))
            dist[row_has_missing] = dist_temp.copy()
            dist[~row_has_missing] = np.nan

            # Delete temp var binding
            del dist_temp

            # Find and impute missing
            X = self._impute(dist, X, self.fitted_X_, mask, mask_fx)

        # Merge bad rows to X and mean impute their missing values
        if np.any(bad_rows):
            bad_missing_index = np.where(_get_mask(X_bad, self.missing_values))
            X_bad[bad_missing_index] = np.take(self.statistics_,
                                               bad_missing_index[1])
            X_merged = np.empty((n_rows_X, n_cols_X))
            X_merged[bad_rows, :] = X_bad
            X_merged[~bad_rows, :] = X
            X = X_merged
        return X

    def fit_transform(self, X, y=None, **fit_params):
        """Fit KNNImputer and impute all missing values in X.

        Parameters
        ----------
        X : {array-like}, shape (n_samples, n_features)
            Input data, where ``n_samples`` is the number of samples and
            ``n_features`` is the number of features.

        Returns
        -------
        X : {array-like}, shape (n_samples, n_features)
            Returns imputed dataset.
        """
        return self.fit(X).transform(X)
