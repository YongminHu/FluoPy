"""
Functions for EEM analysis
Author: Yongmin Hu (yongmin.hu@eawag.ch, yongminhu@outlook.com)
Last update: 2024-01-10
"""

from read_data import *
from plot import *
from utils import *
import scipy.stats as stats
import random
import pandas as pd
import numpy as np
import statistics
import itertools
import string
import warnings
import math
from sklearn.linear_model import LinearRegression
from math import sqrt
from sklearn.metrics import mean_squared_error, explained_variance_score, r2_score
from matplotlib.colors import LogNorm, Normalize
from scipy.ndimage import gaussian_filter
from scipy.interpolate import RegularGridInterpolator, interp1d, griddata
from datetime import datetime, timedelta
from tensorly.decomposition import parafac, non_negative_parafac
from tensorly.cp_tensor import cp_to_tensor
from tlviz.model_evaluation import core_consistency
from tlviz.outliers import compute_leverage
from tlviz.factor_tools import permute_cp_tensor
from IPython.display import display
from pandas.plotting import register_matplotlib_converters
from scipy.sparse.linalg import ArpackError
from sklearn.ensemble import IsolationForest
from sklearn import svm
from matplotlib.cm import get_cmap
from typing import Literal, Union, Optional

register_matplotlib_converters()

def process_eem_stack(eem_stack, f, *args, **kwargs):
    processed_eem_stack = []
    other_outputs = []
    for i in range(eem_stack.shape[0]):
        f_output = f(eem_stack[i, :, :], *args, **kwargs)
        if isinstance(f_output, tuple):
            processed_eem_stack.append(f_output[0])
            other_outputs.append(f_output[1:])
        else:
            processed_eem_stack.append(f_output)
    if len(set([eem.shape for eem in processed_eem_stack])) > 1:
        warnings.warn("Processed EEMs have different shapes")
    return np.array(processed_eem_stack), other_outputs


def eem_threshold_masking(intensity, ex_range, em_range, threshold, fill, mask_type='greater', plot=False,
                          autoscale=True, cmin=0, cmax=4000):
    mask = np.ones(intensity.shape)
    intensity_masked = intensity.astype(float)
    extent = (em_range.min(), em_range.max(), ex_range.min(), ex_range.max())
    if mask_type == 'smaller':
        mask[np.where(intensity < threshold)] = np.nan
    if mask_type == 'greater':
        mask[np.where(intensity > threshold)] = np.nan
    intensity_masked[np.isnan(mask)] = fill
    if plot:
        plt.figure(figsize=(8, 8))
        plot_eem(intensity_masked, em_range=em_range, ex_range=ex_range, auto_intensity_range=autoscale, vmin=cmin, vmax=cmax)
        plt.imshow(mask, extent=extent, alpha=0.9, cmap="binary")
        # plt.title('Relative STD<{threshold}'.format(threshold=threshold))
    return intensity_masked, mask


def eem_region_masking(intensity, ex_range, em_range, em_min=250, em_max=810, ex_min=230, ex_max=500,
                       fill_value='nan'):
    masked = intensity.copy()
    em_min_idx = dichotomy_search(em_range, em_min)
    em_max_idx = dichotomy_search(em_range, em_max)
    ex_min_idx = dichotomy_search(ex_range, ex_min)
    ex_max_idx = dichotomy_search(ex_range, ex_max)
    mask = np.ones(intensity.shape)
    mask[ex_range.shape[0] - ex_max_idx - 1:ex_range.shape[0] - ex_min_idx,
    em_min_idx:em_max_idx + 1] = 0
    if fill_value == 'nan':
        masked[mask == 0] = np.nan
    elif fill_value == 'zero':
        masked[mask == 0] = 0
    return masked, mask


def eem_gaussian_filter(intensity, sigma=1, truncate=3):
    intensity_filtered = gaussian_filter(intensity, sigma=sigma, truncate=truncate)
    return intensity_filtered


def eem_cutting(intensity, ex_range, em_range, em_min, em_max, ex_min, ex_max):
    em_min_idx = dichotomy_search(em_range, em_min)
    em_max_idx = dichotomy_search(em_range, em_max)
    ex_min_idx = dichotomy_search(ex_range, ex_min)
    ex_max_idx = dichotomy_search(ex_range, ex_max)
    intensity_cut = intensity[ex_range.shape[0] - ex_max_idx - 1:ex_range.shape[0] - ex_min_idx,
                    em_min_idx:em_max_idx + 1]
    em_range_cut = em_range[em_min_idx:em_max_idx + 1]
    ex_range_cut = ex_range[ex_min_idx:ex_max_idx + 1]
    return intensity_cut, ex_range_cut, em_range_cut


def eem_nan_imputing(intensity, ex_range, em_range, method: str = 'linear', fill_value: str ='linear_ex',
                     prior_mask=None):
    x, y = np.meshgrid(em_range, ex_range[::-1])
    xx = x[~np.isnan(intensity)].flatten()
    yy = y[~np.isnan(intensity)].flatten()
    zz = intensity[~np.isnan(intensity)].flatten()
    interpolated = None
    if isinstance(fill_value, float):
        interpolated = griddata((xx, yy), zz, (x, y), method=method, fill_value=fill_value)
    elif fill_value == 'linear_ex':
        interpolated = griddata((xx, yy), zz, (x, y), method=method)
        for i in range(interpolated.shape[1]):
            col = interpolated[:, i]
            mask = np.isnan(col)
            if np.any(mask):
                interp_func = interp1d(np.flatnonzero(~mask), col[~mask], kind='linear',
                                                   fill_value='extrapolate')
                col[mask] = interp_func(np.flatnonzero(mask))
            interpolated[:, i] = col
    elif fill_value == 'linear_em':
        interpolated = griddata((xx, yy), zz, (x, y), method=method)
        for j in range(interpolated.shape[0]):
            col = interpolated[j, :]
            mask = np.isnan(col)
            if np.any(mask):
                interp_func = interp1d(np.flatnonzero(~mask), col[~mask], kind='linear',
                                                   fill_value='extrapolate')
                col[mask] = interp_func(np.flatnonzero(mask))
            interpolated[j, :] = col
    if prior_mask is None:
        return interpolated
    else:
        intensity_imputed = intensity.copy()
        intensity_imputed[prior_mask == 0] = interpolated[prior_mask == 0]
        return intensity_imputed


def eem_raman_normalization(intensity, ex_range_blank=None, em_range_blank=None, blank=None, from_blank=False,
                            integration_time=1, ex_lb=349, ex_ub=351, bandwidth_type='wavenumber', bandwidth=1800,
                            rsu_standard=20000, manual_rsu=1):
    if not from_blank:
        return intensity / manual_rsu, manual_rsu
    else:
        ex_range_cut = ex_range_blank[(ex_range_blank >= ex_lb) & (ex_range_blank <= ex_ub)]
        rsu_tot = 0
        for ex in ex_range_cut.tolist():
            if bandwidth_type == 'wavenumber':
                em_target = -ex / (0.00036 * ex - 1)
                wn_target = 10000000 / em_target
                em_lb = 10000000 / (wn_target + bandwidth)
                em_rb = 10000000 / (wn_target - bandwidth)
                rsu, _, _ = eem_regional_integration(blank, ex_range_blank, em_range_blank,
                                                     [em_lb, em_rb],
                                                     [ex, ex])
            else:
                rsu, _, _ = eem_regional_integration(blank, ex_range_blank, em_range_blank,
                                                     [ex - bandwidth, ex + bandwidth],
                                                     [ex, ex])
            rsu_tot += rsu
        return intensity * rsu_standard / rsu_tot / integration_time, rsu_standard / rsu_tot / integration_time


def eem_raman_masking(intensity, ex_range, em_range, tolerance=5, method='linear', axis='grid'):
    intensity_masked = np.array(intensity)
    raman_mask = np.ones(intensity.shape)
    lambda_em = -ex_range / (0.00036 * ex_range - 1)
    tol_emidx = int(np.round(tolerance / (em_range[1] - em_range[0])))
    for s in range(0, intensity_masked.shape[0]):
        exidx = ex_range.shape[0] - s - 1
        if lambda_em[s] <= em_range[0] and lambda_em[s] + tolerance >= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em[s] + tolerance)
            raman_mask[exidx, 0: emidx + 1] = 0
        elif lambda_em[s] - tolerance <= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em[s])
            raman_mask[exidx, 0: emidx + tol_emidx + 1] = 0
        else:
            emidx = dichotomy_search(em_range, lambda_em[s] - tolerance)
            raman_mask[exidx, emidx: emidx + 2 * tol_emidx + 1] = 0

    if method == 'nan':
        intensity_masked[np.where(raman_mask == 0)] = np.nan
    else:
        if axis == 'ex':
            for j in range(0, intensity.shape[1]):
                try:
                    mask_start_idx = np.min(np.where(raman_mask[:, j] == 0)[0])
                    mask_end_idx = np.max(np.where(raman_mask[:, j] == 0)[0])
                    x = np.flipud(ex_range)[np.where(raman_mask[:, j] == 1)]
                    y = intensity_masked[:, j][np.where(raman_mask[:, j] == 1)]
                    f1 = interp1d(x, y, kind=method, fill_value='extrapolate')
                    y_predict = f1(np.flipud(ex_range))
                    intensity_masked[:, j] = y_predict
                except ValueError:
                    continue

        if axis == 'em':
            for i in range(0, intensity.shape[0]):
                try:
                    mask_start_idx = np.min(np.where(raman_mask[i, :] == 0)[0])
                    mask_end_idx = np.max(np.where(raman_mask[i, :] == 0)[0])
                    x = em_range[np.where(raman_mask[i, :] == 1)]
                    y = intensity_masked[i, :][np.where(raman_mask[i, :] == 1)]
                    f1 = interp1d(x, y, kind=method, fill_value='extrapolate')
                    y_predict = f1(em_range)
                    intensity_masked[i, :] = y_predict
                except ValueError:
                    continue

        if axis == 'grid':
            old_nan = np.isnan(intensity)
            intensity_masked[np.where(raman_mask == 0)] = np.nan
            intensity_masked = eem_nan_imputing(intensity_masked, method=method)
            # restore the nan values in non-raman-scattering region
            intensity_masked[old_nan] = np.nan
    return intensity_masked, raman_mask


def eem_rayleigh_masking(intensity, ex_range, em_range, tolerance_o1=15, tolerance_o2=15,
                         axis_o1='grid', axis_o2='grid', method_o1='zero', method_o2='linear'):
    intensity_masked = np.array(intensity)
    rayleigh_mask_o1 = np.ones(intensity.shape)
    rayleigh_mask_o2 = np.ones(intensity.shape)
    lambda_em_o1 = ex_range
    tol_emidx_o1 = int(np.round(tolerance_o1 / (em_range[1] - em_range[0])))
    for s in range(0, intensity_masked.shape[0]):
        exidx = ex_range.shape[0] - s - 1
        if lambda_em_o1[s] <= em_range[0] and lambda_em_o1[s] + tolerance_o1 >= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em_o1[s] + tolerance_o1)
            rayleigh_mask_o1[exidx, 0:emidx + 1] = 0
        elif lambda_em_o1[s] - tolerance_o1 <= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em_o1[s])
            rayleigh_mask_o1[exidx, 0: emidx + tol_emidx_o1 + 1] = 0
        else:
            emidx = dichotomy_search(em_range, lambda_em_o1[s])
            rayleigh_mask_o1[exidx, emidx: emidx + tol_emidx_o1 + 1] = 0
            intensity_masked[exidx, 0: emidx] = 0
    lambda_em_o2 = ex_range * 2
    tol_emidx_o2 = int(np.round(tolerance_o2 / (em_range[1] - em_range[0])))
    for s in range(0, intensity_masked.shape[0]):
        exidx = ex_range.shape[0] - s - 1
        if lambda_em_o2[s] <= em_range[0] and lambda_em_o2[s] + tolerance_o2 >= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em_o2[s] + tolerance_o2)
            rayleigh_mask_o2[exidx, 0:emidx + 1] = 0
        elif lambda_em_o2[s] - tolerance_o2 <= em_range[0]:
            emidx = dichotomy_search(em_range, lambda_em_o2[s])
            rayleigh_mask_o2[exidx, 0: emidx + tol_emidx_o2 + 1] = 0
        else:
            emidx = dichotomy_search(em_range, lambda_em_o2[s] - tolerance_o2)
            rayleigh_mask_o2[exidx, emidx: emidx + 2 * tol_emidx_o2 + 1] = 0

    for axis, itp, mask in zip([axis_o1, axis_o2], [method_o1, method_o2], [rayleigh_mask_o1, rayleigh_mask_o2]):
        if itp == 'zero':
            intensity_masked[np.where(mask == 0)] = 0
        elif itp == 'nan':
            intensity_masked[np.where(mask == 0)] = np.nan
        else:
            if axis == 'ex':
                for j in range(0, intensity.shape[1]):
                    try:
                        mask_start_idx = np.min(np.where(mask[:, j] == 0)[0])
                        mask_end_idx = np.max(np.where(mask[:, j] == 0)[0])
                        x = np.flipud(ex_range)[np.where(mask[:, j] == 1)]
                        y = intensity_masked[:, j][np.where(mask[:, j] == 1)]
                        f1 = interp1d(x, y, kind=itp, fill_value='extrapolate')
                        y_predict = f1(np.flipud(ex_range))
                        intensity_masked[:, j] = y_predict
                    except ValueError:
                        continue
            if axis == 'em':
                for i in range(0, intensity.shape[0]):
                    try:
                        mask_start_idx = np.min(np.where(mask[i, :] == 0)[0])
                        mask_end_idx = np.max(np.where(mask[i, :] == 0)[0])
                        x = em_range[np.where(mask[i, :] == 1)]
                        y = intensity_masked[i, :][np.where(mask[i, :] == 1)]
                        f1 = interp1d(x, y, kind=itp, fill_value='extrapolate')
                        y_predict = f1(em_range)
                        intensity_masked[i, :] = y_predict
                    except ValueError:
                        continue
            if axis == 'grid':
                old_nan = np.isnan(intensity)
                old_nan_o1 = np.isnan(intensity_masked)
                intensity_masked[np.where(mask == 0)] = np.nan
                intensity_masked = eem_nan_imputing(intensity_masked, method=itp)
                # restore the nan values in non-raman-scattering region
                intensity_masked[old_nan] = np.nan
                intensity_masked[old_nan_o1] = np.nan
    return intensity_masked, (rayleigh_mask_o1, rayleigh_mask_o2)


def eem_ife_correction(intensity, ex_range, em_range, absorbance, ex_range_abs, cuvette_length=1, ex_lower_limit=200,
                       ex_upper_limit=825):
    ex_range2 = np.concatenate([[ex_upper_limit], ex_range_abs, [ex_lower_limit]])
    absorbance = np.concatenate([[0], absorbance, [max(absorbance)]])
    f1 = interp1d(ex_range2, absorbance, kind='linear', bounds_error=False, fill_value='extrapolate')
    absorbance_ex = np.fliplr(np.array([f1(ex_range)]))
    absorbance_em = np.array([f1(em_range)])
    ife_factors = 10 ** (cuvette_length * (absorbance_ex.T.dot(np.ones(absorbance_em.shape)) +
                                           np.ones(absorbance_ex.shape).T.dot(absorbance_em)))
    intensity_corrected = intensity * ife_factors
    return intensity_corrected


def eem_regional_integration(intensity, ex_range, em_range, em_boundary, ex_boundary):
    intensity_cut, em_range_cut, ex_range_cut = eem_cutting(intensity, ex_range, em_range,
                                                            em_min=em_boundary[0], em_max=em_boundary[1],
                                                            ex_min=ex_boundary[0], ex_max=ex_boundary[1])
    if intensity_cut.shape[0] == 1:
        integration = np.trapz(intensity_cut, em_range_cut, axis=1)
    elif intensity_cut.shape[1] == 1:
        integration = np.trapz(intensity_cut, ex_range_cut, axis=0)
    else:
        result_x = np.trapz(intensity_cut, np.flip(ex_range_cut), axis=0)
        integration = np.absolute(np.trapz(result_x, em_range_cut, axis=0))
    # number of effective pixels (i.e. pixels with positive intensity)
    num_pixels = intensity[intensity > 0].shape[0]
    avg_regional_intensity = integration / num_pixels
    return integration, avg_regional_intensity, num_pixels


def eem_interpolation(intensity, ex_range_old, em_range_old, ex_range_new, em_range_new, method: str = 'linear'):
    """
    Interpolate EEM on given ex/em ranges. This function is typically used for changing the ex/em ranges of an EEM
    (e.g., in order to synchronize EEMs to the same ex/em ranges). It may not be able to interpolate EEM containing nan
    values. For nan value imputation, please consider eem_nan_imputing().

    Parameters
    ----------
    intensity
    ex_range_old
    em_range_old
    ex_range_new
    em_range_new
    method

    Returns
    -------

    """
    interp = RegularGridInterpolator((ex_range_old[::-1], em_range_old), intensity, method=method)
    x, y = np.meshgrid(em_range_new, ex_range_new[::-1])
    xx = x.flatten()
    yy = y.flatten()
    coordinates_new = np.concatenate([xx[:, np.newaxis], yy[:, np.newaxis]], axis=1)
    intensity_interpolated = interp(coordinates_new).reshape(ex_range_new.shape[0], em_range_new.shape[0])
    return intensity_interpolated


def eems_tf_normalization(eem_stack):
    eem_stack_normalized = eem_stack.copy()
    tf_list = []
    for i in range(eem_stack.shape[0]):
        tf = eem_stack[i].sum()
        tf_list.append(tf)
    weights = tf_list / np.mean(tf_list)
    eem_stack_normalized = eem_stack / np.array(weights)[:, np.newaxis, np.newaxis]
    return eem_stack_normalized, np.array(weights)


def eems_outlier_detection_if(eem_stack, ex_range, em_range, tf_normalization=True, grid_size=(10, 10),
                              contamination=0.02):
    '''
    tells whether it should be considered as an inlier according to the fitted model. +1: inlier; -1: outlier
    :param eem_stack:
    :param ex_range:
    :param em_range:
    :param tf_normalization:
    :param grid_size:
    :param contamination:
    :return:
    '''
    if tf_normalization:
        eem_stack, _ = eems_tf_normalization(eem_stack)
    em_range_new = np.arange(em_range[0], em_range[-1], grid_size[1])
    ex_range_new = np.arange(ex_range[0], ex_range[-1], grid_size[0])
    eem_stack_interpolated = process_eem_stack(eem_stack, eem_interpolation, ex_range, em_range, ex_range_new,
                                               em_range_new)
    eem_stack_unfold = eem_stack_interpolated.reshape(eem_stack_interpolated.shape[0],
                                                      eem_stack_interpolated.shape[1] * eem_stack_interpolated.shape[2])
    eem_stack_unfold = np.nan_to_num(eem_stack_unfold)
    clf = IsolationForest(random_state=0, n_estimators=200, contamination=contamination).fit(eem_stack_unfold)
    label = clf.predict(eem_stack_unfold)
    return label


def eems_outlier_detection_ocs(eem_stack, ex_range, em_range, tf_normalization=True, grid_size=(10, 10), nu=0.02,
                               kernel="rbf", gamma=10000):
    '''

    :param eem_stack:
    :param ex_range:
    :param em_range:
    :param tf_normalization:
    :param grid_size:
    :param nu:
    :param kernel:
    :param gamma:
    :return:
    '''
    if tf_normalization:
        eem_stack, _ = eems_tf_normalization(eem_stack)
    em_range_new = np.arange(em_range[0], em_range[-1], grid_size[1])
    ex_range_new = np.arange(ex_range[0], ex_range[-1], grid_size[0])
    eem_stack_interpolated = process_eem_stack(eem_stack, eem_interpolation, ex_range, em_range, ex_range_new,
                                               em_range_new)
    eem_stack_unfold = eem_stack_interpolated.reshape(eem_stack_interpolated.shape[0],
                                                      eem_stack_interpolated.shape[1] * eem_stack_interpolated.shape[2])
    eem_stack_unfold = np.nan_to_num(eem_stack_unfold)
    clf = svm.OneClassSVM(nu=nu, kernel=kernel, gamma=gamma).fit(eem_stack_unfold)
    label = clf.predict(eem_stack_unfold)
    return label


def eems_fit_components(eem_stack, component_stack, fit_intercept=False):
    assert eem_stack.shape[1:] == component_stack.shape, "EEM and component have different shapes"
    score_sample = []
    fmax_sample = []
    max_values = np.amax(component_stack, axis=(1, 2))
    eem_stack_pred = np.zeros(eem_stack.shape)
    for i in range(eem_stack.shape[0]):
        y_true = eem_stack[i].reshape([-1])
        x = component_stack.reshape([component_stack.shape[0], -1]).T
        reg = LinearRegression(fit_intercept=fit_intercept).fit(x, y_true)
        y_pred = reg.predict(x)
        eem_stack_pred[i, :, :] = y_pred.reshape((eem_stack.shape[1], eem_stack.shape[2]))
        score_sample.append(reg.coef_)
        fmax_sample.append(reg.coef_ * max_values)
    return score_sample, fmax_sample, eem_stack_pred


def eems_error(eem_stack_true, eem_stack_pred, metric: str = 'mse'):
    assert eem_stack_true.shape == eem_stack_pred.shape, "eem_stack_true and eem_stack_pred have different shapes"
    error = []
    for i in range(eem_stack_true.shape[0]):
        y_true = eem_stack_true[i].reshape([-1])
        y_pred = eem_stack_pred[i].reshape([-1])
        if metric == 'mse':
            error.append(mean_squared_error(y_true, y_pred))
        elif metric == 'explained_variance':
            error.append(explained_variance_score(y_true, y_pred))
        elif metric == 'r2':
            error.append(r2_score(y_true, y_pred))
    return np.array(error)


class EEMDataset:
    """
    Build an EEM dataset.
    """
    def __init__(self, eem_stack, ex_range, em_range, ref=None, index=None):
        """
        Parameters
        ----------
        eem_stack: np.ndarray (3d)
            A stack of EEM. It should have a shape of (N, I, J), where N is the number of samples, I is the number of
            excitation wavelengths, and J is the number of emission wavelengths.
        ex_range: np.ndarray (1d)
            The excitation wavelengths.
        em_range: np.ndarray (1d)
            The emission wavelengths.
        ref: np.ndarray (1d) or None
            Optional. The reference data, e.g., the COD of each sample. It should have a length equal to the number of
            samples in the eem_stack.
        index: list or None
            Optional. The index used to label each sample. The number of elements in the list should equal the number
            of samples in the eem_stack.
        """
        # ------------------parameters--------------------
        # The Em/Ex ranges should be sorted in ascending order
        self.eem_stack = eem_stack
        self.ex_range = ex_range
        self.em_range = em_range
        self.ref = ref
        self.index = index
        self.extent = (self.em_range.min(), self.em_range.max(), self.ex_range.min(), self.ex_range.max())

    # --------------------EEM dataset features--------------------
    def zscore(self):
        transformed_data = stats.zscore(self.eem_stack, axis=0)
        return transformed_data

    def mean(self):
        mean = np.mean(self.eem_stack, axis=0)
        return mean

    def variance(self):
        variance = np.var(self.eem_stack, axis=0)
        return variance

    def rel_std(self, threshold=0.05):
        coef_variation = stats.variation(self.eem_stack, axis=0)
        rel_std = abs(coef_variation)
        if threshold:
            mean = np.mean(self.eem_stack, axis=0)
            qualified_pixel_proportion = np.count_nonzero(rel_std < threshold) / np.count_nonzero(~np.isnan(rel_std))
            print("The proportion of pixels with relative STD < {t}: ".format(t=threshold),
                  qualified_pixel_proportion)
        return rel_std

    def std(self):
        return np.std(self.eem_stack, axis=0)

    def total_fluorescence(self):
        return self.eem_stack.sum(axis=(1, 2))

    def regional_integration(self, em_boundary, ex_boundary):
        integrations, _ = process_eem_stack(self.eem_stack, eem_regional_integration, ex_range=self.ex_range,
                                            em_range=self.em_range, em_boundary=em_boundary, ex_boundary=ex_boundary)
        return integrations

    def peak_picking(self, ex, em):
        """
        Return the fluorescence intensities at the location closest the given (ex, em)

        Parameters
        ----------
        ex: float or int
            excitation wavelength of the wanted location
        em: float or int
            emission wavelength of the wanted location

        Returns
        -------
        fi: pandas.DataFrame
            table of fluorescence intensities at the wanted location for all samples
        ex_actual:
            the actual ex of the extracted fluorescence intensities
        em_actual:
            the actual em of the extracted fluorescence intensities
        """
        em_idx = dichotomy_search(self.em_range, em)
        ex_idx = dichotomy_search(self.ex_range, ex)
        fi = self.eem_stack[:, - ex_idx - 1, em_idx]
        if self.index:
            fi = pd.DataFrame(fi, index=self.index)
        else:
            fi = pd.DataFrame(fi, index=np.arange(fi.shape[0]))
        ex_actual = self.ex_range[ex_idx]
        em_actual = self.em_range[em_idx]
        return fi, ex_actual, em_actual

    def correlation(self):
        """
        Analyze the correlation between reference and fluorescence intensity at each pair of ex/em.

        Returns
        -------
        corr_dict: dict
            A dictionary containing multiple correlation evaluation metrics.

        """
        m = self.eem_stack
        x = self.ref
        x = x.reshape(m.shape[0], 1)
        w, b, r2, pc, pc_p, sc, sc_p = [np.full((m.shape[1], m.shape[2]), fill_value=np.nan)]*7
        e = np.full(m.shape, fill_value=np.nan)
        for i in range(m.shape[1]):
            for j in range(m.shape[2]):
                try:
                    y = (m[:, i, j])
                    reg = LinearRegression().fit(x, y)
                    w[i, j] = reg.coef_
                    b[i, j] = reg.intercept_
                    r2[i, j] = reg.score(x, y)
                    e[:, i, j] = reg.predict(x) - y
                    pc[i, j], pc_p[i, j] = stats.pearsonr(x, y)
                    sc[i, j], sc_p[i, j] = stats.spearmanr(x, y)
                except:
                    pass
        corr_dict = {'slope': w, 'intercept': b, 'r_square': r2, 'linear regression residual': e,
                     'Pearson corr. coef.': pc, 'Pearson corr. coef. p-value': pc_p, 'Spearman corr. coef.': sc,
                     'Spearman corr. coef. p-value': sc_p}
        return corr_dict

    # -----------------EEM dataset processing methods-----------------

    def threshold_masking(self, threshold, mask_type='greater', plot=False, autoscale=True, cmin=0, cmax=4000,
                          copy=True):
        eem_stack_masked, masks = process_eem_stack(self.eem_stack, eem_threshold_masking, ex_range=self.ex_range,
                                                    em_range=self.em_range, threshold=threshold, mask_type=mask_type,
                                                    plot=plot, autoscale=autoscale, cmin=cmin, cmax=cmax)
        if not copy:
            self.eem_stack = eem_stack_masked
        return eem_stack_masked, masks

    def gaussian_filter(self, sigma=1, truncate=3, copy=True):
        eem_stack_filtered = process_eem_stack(self.eem_stack, eem_gaussian_filter, sigma=sigma, truncate=truncate)
        if not copy:
            self.eem_stack = eem_stack_filtered
        return eem_stack_filtered

    def region_masking(self, ex_min, ex_max, em_min, em_max, fill_value='nan', copy=True):
        eem_stack_masked, _ = process_eem_stack(self.eem_stack, eem_region_masking, ex_range=self.ex_range,
                                                em_range=self.em_range, ex_min=ex_min, ex_max=ex_max, em_min=em_min,
                                                em_max=em_max, fill_value=fill_value)
        if not copy:
            self.eem_stack = eem_stack_masked
        return eem_stack_masked

    def cutting(self, ex_min, ex_max, em_min, em_max, copy=True):
        eem_stack_cut, new_ranges = process_eem_stack(self.eem_stack, eem_cutting, ex_range=self.ex_range,
                                                      em_range=self.em_range,
                                                      ex_min=ex_min, ex_max=ex_max, em_min=em_min, em_max=em_max)
        if not copy:
            self.eem_stack = eem_stack_cut
            self.ex_range = new_ranges[0][0]
            self.em_range = new_ranges[0][1]
        return eem_stack_cut, new_ranges[0][0], new_ranges[0][1]

    def nan_imputing(self, method='linear', fill_value='linear_ex', prior_mask=None, copy=True):
        eem_stack_imputed = process_eem_stack(self.eem_stack, eem_nan_imputing, ex_range=self.ex_range,
                                              em_range=self.em_range, method=method, fill_value=fill_value,
                                              prior_mask=prior_mask)
        if not copy:
            self.eem_stack = eem_stack_imputed
        return eem_stack_imputed

    def raman_normalization(self, ex_range_blank=None, em_range_blank=None, blank=None, from_blank=False,
                            integration_time=1, ex_lb=349, ex_ub=351, bandwidth_type='wavenumber', bandwidth=1800,
                            rsu_standard=20000, manual_rsu=1, copy=True):
        eem_stack_normalized = process_eem_stack(self.eem_stack, eem_raman_normalization, ex_range_blank=ex_range_blank,
                                                 em_range_blank=em_range_blank, blank=blank, from_blank=from_blank,
                                                 integration_time=integration_time, ex_lb=ex_lb, ex_ub=ex_ub,
                                                 bandwidth_type=bandwidth_type, bandwidth=bandwidth,
                                                 rsu_standard=rsu_standard, manual_rsu=manual_rsu)
        if not copy:
            self.eem_stack = eem_stack_normalized
        return eem_stack_normalized

    def tf_normalization(self, copy=True):
        eem_stack_normalized, weights = eems_tf_normalization(self.eem_stack)
        if not copy:
            self.eem_stack = eem_stack_normalized
        return eem_stack_normalized, weights

    def raman_masking(self, tolerance=5, method='linear', axis='grid', copy=True):
        eem_stack_masked, _ = process_eem_stack(self.eem_stack, eem_raman_masking, ex_range=self.ex_range,
                                                em_range=self.em_range, tolerance=tolerance, method=method, axis=axis)
        if not copy:
            self.eem_stack = eem_stack_masked
        return eem_stack_masked

    def rayleigh_masking(self, tolerance_o1=15, tolerance_o2=15, axis_o1='grid', axis_o2='grid', method_o1='zero',
                         method_o2='linear', copy=True):
        eem_stack_masked, _ = process_eem_stack(self.eem_stack, eem_rayleigh_masking, ex_range=self.ex_range,
                                                em_range=self.em_range, tolerance_o1=tolerance_o1,
                                                tolerance_o2=tolerance_o2, axis_o1=axis_o1, axis_o2=axis_o2,
                                                method_o1=method_o1, method_o2=method_o2)
        if not copy:
            self.eem_stack = eem_stack_masked
        return eem_stack_masked

    def ife_correction(self, absorbance, ex_range_abs, cuvette_length=1, ex_lower_limit=200, ex_upper_limit=825,
                       copy=True):
        eem_stack_corrected = process_eem_stack(self.eem_stack, eem_ife_correction, ex_range=self.ex_range,
                                                em_range=self.em_range, absorbance=absorbance,
                                                ex_range_abs=ex_range_abs, cuvette_length=cuvette_length,
                                                ex_lower_limit=ex_lower_limit, ex_upper_limit=ex_upper_limit)
        if not copy:
            self.eem_stack = eem_stack_corrected
        return eem_stack_corrected

    def interpolation(self, ex_range_new, em_range_new, copy=True):
        eem_stack_interpolated = process_eem_stack(self.eem_stack, eem_interpolation, ex_range_old=self.ex_range,
                                                   em_range_old=self.em_range, ex_range_new=ex_range_new,
                                                   em_range_new=em_range_new)
        if not copy:
            self.eem_stack = eem_stack_interpolated
            self.ex_range = ex_range_new
            self.em_range = em_range_new
        return eem_stack_interpolated, ex_range_new, em_range_new

    def outlier_detection_if(self, tf_normalization=True, grid_size=(10, 10), contamination=0.02, deletion=False):
        labels = eems_outlier_detection_if(eem_stack=self.eem_stack, ex_range=self.ex_range, em_range=self.em_range,
                                           tf_normalization=tf_normalization, grid_size=grid_size,
                                           contamination=contamination)
        if deletion:
            self.eem_stack = self.eem_stack[labels != -1]
            self.ref = self.ref[labels != -1]
            self.index = [idx for i, idx in enumerate(self.index) if labels[i] != -1]
        return labels

    def outlier_detection_ocs(self, tf_normalization=True, grid_size=(10, 10), nu=0.02, kernel='rbf', gamma=10000,
                              deletion=False):
        labels = eems_outlier_detection_ocs(eem_stack=self.eem_stack, ex_range=self.ex_range, em_range=self.em_range,
                                            tf_normalization=tf_normalization, grid_size=grid_size, nu=nu,
                                            kernel=kernel, gamma=gamma)
        if deletion:
            self.eem_stack = self.eem_stack[labels != -1]
            self.ref = self.ref[labels != -1]
            self.index = [idx for i, idx in enumerate(self.index) if labels[i] != -1]
        return labels

    def splitting(self, n_split, rule: str = 'random'):
        """
        To split the EEM dataset and form multiple sub-datasets.

        Parameters
        ----------
        n_split: int
            The number of splits.
        rule: str, {'random', 'sequential'}
            If 'random' is passed, the split will be generated randomly. If 'sequential' is passed, the dataset will be
            split according to index order.

        Returns
        -------
        model_list: list
            A list of sub-datasets. Each of them is an EEMDataset object.
        """
        idx_eems = [i for i in range(self.eem_stack.shape[0])]
        model_list = []
        if rule == 'random':
            random.shuffle(idx_eems)
            idx_splits = np.array_split(idx_eems, n_split)
        elif rule == 'sequential':
            idx_splits = np.array_split(idx_eems, n_split)
        for split in idx_splits:
            m = EEMDataset(eem_stack=np.array([self.eem_stack[i] for i in split]), ex_range=self.ex_range,
                           em_range=self.em_range, ref=np.array([self.ref[i] for i in split]),
                           index=[self.index[i] for i in split])
            model_list.append(m)
        return model_list


def combine_eem_datasets(list_eem_datasets):
    eem_stack_combined = []
    ref_combined = []
    index_combined = []
    ex_range_0 = list_eem_datasets[0].ex_range
    em_range_0 = list_eem_datasets[0].em_range
    for d in list_eem_datasets:
        eem_stack_combined.append(d.eem_stack)
        ref_combined.append(d.ref)
        index_combined = index_combined + d.index
        if not np.array_equal(d.ex_range, ex_range_0) or not np.array_equal(d.em_range, em_range_0):
            Warning('ex_range and em_range of the datasets must be identical. If you want to combine EEM datasets '
                    'having different ex/em ranges, please consider unify the ex/em ranges using the interpolation() '
                    'method of EEMDataset object')
    eem_stack_combined = np.concatenate(eem_stack_combined, axis=0)
    ref_combined = np.concatenate(ref_combined, axis=0)
    eem_dataset_combined = EEMDataset(eem_stack=eem_stack_combined, ex_range=ex_range_0, em_range=em_range_0,
                                      ref=ref_combined, index=index_combined)
    return eem_dataset_combined


class PARAFAC:
    """
    PARAFAC model
    """

    def __init__(self, rank, non_negativity=True, init='svd', tf_normalization=True,
                 loadings_normalization: Optional[str] = 'sd', sort_em=True):
        """
        Parameters
        ----------
        rank: int
            The number of components
        non_negativity: bool
            Whether to apply the non-negativity constraint
        init: str or tensorly.CPTensor, {‘svd’, ‘random’, CPTensor}
            Type of factor matrix initialization
        tf_normalization: bool
            Whether to normalize the EEMs by the total fluorescence in PARAFAC model establishment
        loadings_normalization: str or None, {'sd', 'maximum', None}
            Type of normalization applied to loadings. if 'sd' is passed, the standard deviation will be normalized
            to 1. If 'maximum' is passed, the maximum will be normalized to 1. The scores will be adjusted accordingly.
        sort_em: bool
            Whether to sort components by emission peak position from lowest to highest. If False is passed, the
            components will be sorted by the contribution to the total variance.

        Attributes
        ----------
        score
        ex_loadings
        em_loadings
        fmax
        component_stack
        cptensors
        eem_stack_train
        eem_stack_reconstructed
        ex_range
        em_range
        """
        # ----------parameters--------------
        self.rank = rank
        self.non_negativity = non_negativity
        self.init = init
        self.tf_normalization = tf_normalization
        self.loadings_normalization = loadings_normalization
        self.sort_em = sort_em

        # -----------attributes---------------
        self.score = None
        self.ex_loadings = None
        self.em_loadings = None
        self.fmax = None
        self.component_stack = None
        self.cptensors = None
        self.eem_stack_train = None
        self.eem_stack_reconstructed = None
        self.ex_range = None
        self.em_range = None

    # --------------methods------------------
    def establish(self, eem_dataset: EEMDataset):
        """
        Establish a PARAFAC model based on a given EEM dataset

        Parameters
        ----------
        eem_dataset: EEMDataset
            The EEM dataset that the PARAFAC model establishes on.

        Returns
        -------
        self: object
            The established PARAFAC model
        """
        if self.tf_normalization:
            _, tf_weights = eem_dataset.tf_normalization(copy=False)
        try:
            if not self.non_negativity:
                if np.isnan(eem_dataset.eem_stack).any():
                    mask = np.where(np.isnan(eem_dataset.eem_stack), 0, 1)
                    cptensors = parafac(eem_dataset.eem_stack, rank=self.rank, mask=mask, init=self.init)
                else:
                    cptensors = parafac(eem_dataset.eem_stack, rank=self.rank, init=self.init)
            else:
                if np.isnan(eem_dataset.eem_stack).any():
                    mask = np.where(np.isnan(eem_dataset.eem_stack), 0, 1)
                    cptensors = non_negative_parafac(eem_dataset.eem_stack, rank=self.rank, mask=mask, init=self.init)
                else:
                    cptensors = non_negative_parafac(eem_dataset.eem_stack, rank=self.rank, init=self.init)
        except ArpackError:
            print(
                "PARAFAC failed possibly due to the presence of patches of nan values. Please consider cut or "
                "interpolate the nan values.")
        a, b, c = cptensors[1]
        component_stack = np.zeros([self.rank, b.shape[0], c.shape[0]])
        for r in range(self.rank):

            # when non_negativity is not applied, ensure the scores are generally positive
            if not self.non_negativity:
                if a[:, r].sum() < 0:
                    a[:, r] = -a[:, r]
                    if abs(b[:, r].min()) > b[:, r].max():
                        b[:, r] = -b[:, r]
                    else:
                        c[:, r] = -c[:, r]
                elif abs(b[:, r].min()) > b[:, r].max() and abs(c[:, r].min()) > c[:, r].max():
                    b[:, r] = -b[:, r]
                    c[:, r] = -c[:, r]

            if self.loadings_normalization == 'sd':
                stdb = b[:, r].std()
                stdc = c[:, r].std()
                b[:, r] = b[:, r] / stdb
                c[:, r] = c[:, r] / stdc
                a[:, r] = a[:, r] * stdb * stdc
            elif self.loadings_normalization == 'maximum':
                maxb = b[:, r].max()
                maxc = c[:, r].min()
                b[:, r] = b[:, r] / maxb
                c[:, r] = c[:, r] / maxc
                a[:, r] = a[:, r] * maxb * maxc
            component = np.array([b[:, r]]).T.dot(np.array([c[:, r]]))
            component_stack[r, :, :] = component

        if self.tf_normalization:
            a = np.multiply(a, tf_weights[:, np.newaxis])
        score = pd.DataFrame(a)
        fmax = a * component_stack.max(axis=(1, 2))
        ex_loadings = pd.DataFrame(np.flipud(b), index=eem_dataset.ex_range)
        em_loadings = pd.DataFrame(c, index=eem_dataset.em_range)
        if self.sort_em:
            em_peaks = [c[1] for c in em_loadings.idxmax()]
            peak_rank = list(enumerate(stats.rankdata(em_peaks)))
            order = [i[0] for i in sorted(peak_rank, key=lambda x: x[1])]
            component_stack = component_stack[order]
            ex_loadings = pd.DataFrame({'component {r} ex loadings'.format(r=i+1): ex_loadings.iloc[:, order[i]]
                                        for i in range(self.rank)})
            em_loadings = pd.DataFrame({'component {r} em loadings'.format(r=i+1): em_loadings.iloc[:, order[i]]
                                        for i in range(self.rank)})
            score = pd.DataFrame({'component {r} score'.format(r=i+1): score.iloc[:, order[i]]
                                  for i in range(self.rank)})
            fmax = pd.DataFrame({'component {r} fmax'.format(r=i+1): fmax[:, order[i]]
                                 for i in range(self.rank)})
        else:
            column_labels = ['component {r}'.format(r=i+1) for i in range(self.rank)]
            ex_loadings.columns = column_labels
            em_loadings.columns = column_labels
            score.columns = column_labels
            fmax = pd.DataFrame(fmax, columns=['component {r}'.format(r=i+1) for i in range(self.rank)])

        ex_loadings.index = eem_dataset.ex_range.tolist()
        em_loadings.index = eem_dataset.em_range.tolist()

        if eem_dataset.index:
            score.index = eem_dataset.index
            fmax.index = eem_dataset.index
        else:
            score.index = [i + 1 for i in range(a.shape[0])]
            fmax.index = [i + 1 for i in range(a.shape[0])]

        self.score = score
        self.ex_loadings = ex_loadings
        self.em_loadings = em_loadings
        self.fmax = fmax
        self.component_stack = component_stack
        self.cptensors = cptensors
        self.eem_stack_train = eem_dataset.eem_stack
        self.ex_range = eem_dataset.ex_range
        self.em_range = eem_dataset.em_range
        self.eem_stack_reconstructed = cp_to_tensor(cptensors)
        return self

    def fit(self, eem_dataset: EEMDataset, fit_intercept=False):
        """
        Fit a given EEM dataset with the established PARAFAC components by linear regression. This method can be used
        to fit a new EEM dataset independent of the one used in model establishment.

        Parameters
        ----------
        eem_dataset: EEMDataset
            The EEM.
        fit_intercept: bool
            Whether to calculate the intercept.

        Returns
        -------
        score_sample: np.ndarray (1d)
            The fitted score.
        fmax_sample: np.ndarray (1d)
            The fitted Fmax.
        eem_stack_pred: np.ndarray (3d)
            The EEM dataset reconstructed.
        """
        score_sample, fmax_sample, eem_stack_pred = eems_fit_components(eem_dataset.eem_stack, self.component_stack,
                                                                        fit_intercept=fit_intercept)
        return score_sample, fmax_sample, eem_stack_pred

    def component_peak_locations(self):
        """
        Get the ex/em of component peaks

        Returns
        -------
        max_exem: list
            A List of (ex, em) of component peaks.
        """
        max_exem = []
        for r in range(self.rank):
            max_index = np.unravel_index(np.argmax(self.component_stack[r, :, :]), self.component_stack[r, :, :].shape)
            max_exem.append((self.ex_range[-(max_index[0] + 1)], self.em_range[max_index[1]]))
        return max_exem

    def residual(self):
        """
        Get the residual of the established PARAFAC model, i.e., the difference between the original EEM dataset and
        the reconstructed EEM dataset.

        Returns
        -------
        res: np.ndarray (3d)
            the residual
        """
        res = self.eem_stack_train - self.eem_stack_reconstructed
        return res

    def explained_variance(self):
        """
        Calculate the explained variance of the established PARAFAC model

        Returns
        -------
        ev: float
            the explained variance
        """
        y_train = self.eem_stack_train.reshape(-1)
        y_pred = self.eem_stack_reconstructed.reshape(-1)
        ev = 100 * (1 - np.var(y_pred - y_train) / np.var(y_train))
        return ev

    def core_consistency(self):
        """
        Calculate the core consistency of the established PARAFAC model

        Returns
        -------
        ev: float
            core consistency
        """
        cc = core_consistency(self.cptensors, self.eem_stack_train)
        return cc

    def leverage(self, mode: str = 'sample'):
        """
        Calculate the leverage of a selected mode.

        Parameters
        ----------
        mode: str, {'ex', 'em', 'sample'}
            The mode of which the leverage is calculated.

        Returns
        -------
        lvr: pandas.DataFrame
            The table of leverage

        """
        if mode == 'ex':
            lvr = compute_leverage(self.ex_loadings)
        elif mode == 'em':
            lvr = compute_leverage(self.em_loadings)
        elif mode == 'sample':
            lvr = compute_leverage(self.score)
        lvr.index = lvr.index.set_levels(['leverage of {m}'.format(m=mode)] * len(lvr.index.levels[0]), level=0)
        return lvr

    def sample_rmse(self):
        """
        Calculate the root mean squared error (RMSE) of EEM of each sample.

        Returns
        -------
        sse: pandas.DataFrame
            Table of RMSE
        """
        res = self.residual()
        n_pixels = self.eem_stack_train.shape[1] * self.eem_stack_train.shape[2]
        rmse = pd.DataFrame(sqrt(np.sum(res**2, axis=(1,2)) / n_pixels), index=self.score.index)
        return rmse

    def sample_normalized_rmse(self):
        """
        Calculate the normalized root mean squared error (normalized RMSE) of EEM of each sample. It is defined as the
        RMSE divided by the mean of original signal.

        Returns
        -------
        normalized_sse: pandas.DataFrame
            Table of normalized RMSE
        """
        res = self.residual()
        n_pixels = self.eem_stack_train.shape[1] * self.eem_stack_train.shape[2]
        normalized_sse = pd.DataFrame(sqrt(np.sum(res**2, axis=(1,2)) / n_pixels) /
                                      np.average(self.eem_stack_train, axis=(1,2)),
                                      index=self.score.index)
        return normalized_sse

    def sample_summary(self):
        """
        Get a table showing the score, Fmax, leverage, RMSE and normalized RMSE for each sample.

        Returns
        -------
        summary: pandas.DataFrame
            Table of samples' score, Fmax, leverage, RMSE and normalized RMSE.
        """
        lvr = self.leverage()
        rmse = self.sample_rmse()
        normalized_rmse = self.sample_normalized_rmse()
        summary = pd.concat([self.score, self.fmax, lvr, rmse, normalized_rmse], axis=1)
        return summary


def loadings_similarity(loadings1: pd.DataFrame, loadings2: pd.DataFrame, wavelength_alignment=False, dtw=False):
    """
    Calculate the Tucker's congruence between each pair of components of two loadings (of excitation or emission).

    Parameters
    ----------
    loadings1: pandas.DataFrame
        The first loadings. Each column of the table corresponds to one component.
    loadings2: pandas.DataFrame
        The second loadings. Each column of the table corresponds to one component.
    wavelength_alignment: bool
        Align the ex/em ranges of the components. This is useful if the PARAFAC models have different ex/em wavelengths.
        Note that ex/em will be aligned according to the ex/em ranges with the lower intervals between the two PARAFAC
        models.
    dtw: bool
        Apply dynamic time warping (DTW) to align the component loadings before calculating the similarity. This is
        useful for matching loadings with similar but shifted shapes.

    Returns
    -------
    m_sim: pandas.DataFrame
        The table of loadings similarities between each pair of components.
    """
    wl_range1, wl_range2 = (loadings1.index, loadings2.index)
    if wavelength_alignment:
        wl_interval1 = (wl_range1.max() - wl_range1.min()) / (wl_range1.shape[0] - 1)
        wl_interval2 = (wl_range2.max() - wl_range2.min()) / (wl_range2.shape[0] - 1)
        if wl_interval2 > wl_interval1:
            f2 = interp1d(wl_range2, loadings2.to_numpy(), axis=0)
            loadings2 = f2(wl_range1)
        elif wl_interval1 > wl_interval2:
            f1 = interp1d(wl_range1, loadings1.to_numpy(), axis=0)
            loadings1 = f1(wl_range2)
    else:
        loadings1, loadings2 = (loadings1.to_numpy(), loadings2.to_numpy())
    m_sim = np.zeros([loadings1.shape[1], loadings2.shape[1]])
    for n2 in range(loadings2.shape[1]):
        for n1 in range(loadings1.shape[1]):
            if dtw:
                ex1_aligned, ex2_aligned = dynamic_time_warping(loadings1[:, n1], loadings2[:, n2])
            else:
                ex1_aligned, ex2_aligned = [loadings1[:, n1], loadings2[:, n2]]
            m_sim[n1, n2] = stats.pearsonr(ex1_aligned, ex2_aligned)[0]
    m_sim = pd.DataFrame(m_sim, index=['model1 C{i}'.format(i=i+1) for i in range(loadings1.shape[1])],
                         columns=['model2 C{i}'.format(i=i+1) for i in range(loadings2.shape[1])])
    return m_sim


def align_parafac_components(models_dict: dict, ex_ref: pd.DataFrame, em_ref: pd.DataFrame, wavelength_alignment=False):
    """
    Align the components of PARAFAC models according to given reference ex/em loadings so that similar components
    are labelled by the same name.

    Parameters
    ----------
    models_dict: dict
        Dictionary of PARAFAC object. The models to be aligned.
    ex_ref: pandas.DataFrame
        Ex loadings of the reference
    em_ref: pandas.DataFrame
        Em loadings of the reference
    wavelength_alignment: bool
        Align the ex/em ranges of the components. This is useful if the PARAFAC models have different ex/em wavelengths.
        Note that ex/em will be aligned according to the ex/em ranges with the lower intervals between the two PARAFAC
        models.

    Returns
    -------
    models_dict_new: dict
        Dictionary of the aligned PARAFAC object.
    """
    component_labels_ref = ex_ref.columns
    models_dict_new = {}
    for model_label, model in models_dict.items():
        m_sim_ex = loadings_similarity(model, ex_ref, wavelength_alignment=wavelength_alignment)
        m_sim_em = loadings_similarity(model, em_ref, wavelength_alignment=wavelength_alignment)
        m_sim = (m_sim_ex + m_sim_em)/2
        ex_var, em_var = (model.ex_loadings, model.em_loadings)
        matched_index = []
        m_sim_copy = m_sim.copy()
        if ex_var.shape[1] <= ex_ref.shape[1]:
            for n_var in range(ex_var.shape[1]):
                max_index = np.argmax(m_sim[n_var, :])
                while max_index in matched_index:
                    m_sim_copy[n_var, max_index] = 0
                    max_index = np.argmax(m_sim_copy[n_var, :])
                matched_index.append(max_index)
            component_labels_var = [component_labels_ref[i] for i in matched_index]
            permutation = get_indices_smallest_to_largest(matched_index)
        else:
            for n_ref in range(ex_ref.shape[1]):
                max_index = np.argmax(m_sim[:, n_ref])
                while max_index in matched_index:
                    m_sim_copy[max_index, n_ref] = 0
                    max_index = np.argmax(m_sim_copy[:, n_ref])
                matched_index.append(max_index)
            non_ordered_index = list(set([i for i in range(ex_var.shape[1])]) - set(matched_index))
            permutation = matched_index + non_ordered_index
            component_labels_ref_extended = component_labels_ref + ['O{i}'.format(i=i+1) for i in
                                                                    range(len(non_ordered_index))]
            component_labels_var = [0] * len(permutation)
            for i, nc in enumerate(permutation):
                component_labels_var[nc] = component_labels_ref_extended[i]
        model.score.columns, model.ex_loadings.columns, model.em_loadings.columns, model.fmax.columns = (
                [component_labels_var] * 4)
        model.score = model.score.iloc[:, permutation]
        model.ex_loadings = model.ex_loadings.iloc[:, permutation]
        model.em_loadings = model.em_loadings.iloc[:, permutation]
        model.fmax = model.fmax.iloc[:, permutation]
        model.component_stack = model.component_stack[permutation, :, :]
        model.cptensor = permute_cp_tensor(model.cptensor, permutation)
        models_dict_new[model_label] = model
        return models_dict_new


def align_parafac_components(models_dict, model_ref, model1: PARAFAC, model2: PARAFAC, m_sim):
    """
    Sort the order of components of two PARAFAC models so that similar components are labelled by the same number.

    Parameters
    ----------
    model1: PARAFAC
        PARAFAC model 1.
    model2: PARAFAC
        PARAFAC model 2.
    m_sim: pandas.DataFrame
        The component similarity matrix. It can be obtained with parafac_components_similarity().

    Returns
    -------
    model1: PARAFAC
        The sorted PARAFAC model 1.
    model2: PARAFAC
        The sorted PARAFAC model 2.
    """
    ex1, em1, ex2, em2 = (model1.ex_loadings, model1.em_loadings, model2.ex_loadings, model2.em_loadings)
    if ex1.shape[1] >= ex2.shape[1]:
        ex_ref, em_ref = (ex2, em2)
        ex_var, em_var = (ex1, ex1)
        m_sim = m_sim.to_numpy()
    else:
        ex_ref, em_ref = (ex1, em1)
        ex_var, em_var = (ex2, em2)
        m_sim = m_sim.to_numpy().T
    matched_index = []
    memory = []
    m_sim_copy = m_sim.copy()
    for n_ref in range(ex_ref.shape[1]):
        max_index = np.argmax(m_sim[:, n_ref])
        while max_index in memory:
            m_sim_copy[max_index, n_ref] = 0
            max_index = np.argmax(m_sim_copy[:, n_ref])
        memory.append(max_index)
        matched_index.append((max_index, n_ref))
    non_ordered_idx = list(set([i for i in range(ex_var.shape[1])]) - set(memory))
    order = [o[0] for o in matched_index] + non_ordered_idx
    ex_var_sorted, em_var_sorted = (ex_var.copy(), em_var.copy())
    for i, o in enumerate(order):
        ex_var_sorted['component {i}'.format(i=i+1)] = ex1['component {i}'.format(i=o+1)]
        em_var_sorted['component {i}'.format(i=i+1)] = em1['component {i}'.format(i=o+1)]
    if ex1.shape[1] >= ex2.shape[1]:
        model1.ex_loadings = ex_var_sorted
        model1.em_loadings = em_var_sorted
    else:
        model2.ex_loadings = ex_var_sorted
        model2.em_loadings = em_var_sorted
    return model1, model2


# def explained_variance(eem_stack, rank=[1, 2, 3, 4, 5], decomposition_method='non_negative_parafac', init='svd',
#                        dataset_normalization=False, plot_ve=True):
#     if isinstance(rank, int):
#         rank = [rank]
#     if dataset_normalization:
#         eem_stack, tf = eems_tf_normalization(eem_stack)
#     ev_list = []
#     for r in rank:
#         if decomposition_method == 'parafac':
#             weight, factors = parafac(eem_stack, r, init=init)
#         elif decomposition_method == 'non_negative_parafac':
#             weight, factors = non_negative_parafac(eem_stack, r, init=init)
#         eem_stack_reconstruct = cp_to_tensor((weight, factors))
#         y_train = eem_stack.reshape(-1)
#         y_pred = eem_stack_reconstruct.reshape(-1)
#         ev_list.append(round(100 * (1 - np.var(y_pred - y_train) / np.var(y_train)), 2))
#     if plot_ve:
#         plt.close()
#         plt.figure(figsize=(10, 5))
#         for i in range(len(rank)):
#             plt.plot(rank[i], ev_list[i], '-o')
#             plt.annotate(ev_list[i], (rank[i] + 0.2, ev_list[i]))
#         plt.xlabel("Rank")
#         plt.xticks(rank)
#         plt.ylabel("Variance explained [%]")
#     return ev_list


# def parafac_pixel_error(eem_stack, em_range, ex_range, rank,
#                         decomposition_method='non_negative_parafac', init='svd',
#                         dataset_normalization=False):
#     if dataset_normalization:
#         eem_stack_nor, tf = eems_tf_normalization(eem_stack)
#         if decomposition_method == 'parafac':
#             weight, factors = parafac(eem_stack_nor, rank, init=init)
#         elif decomposition_method == 'non_negative_parafac':
#             weight, factors = non_negative_parafac(eem_stack_nor, rank, init=init)
#         eem_stack_reconstruct = cp_to_tensor((weight, factors)) * tf[:, np.newaxis, np.newaxis]
#     else:
#         if decomposition_method == 'parafac':
#             weight, factors = parafac(eem_stack, rank, init=init)
#         elif decomposition_method == 'non_negative_parafac':
#             weight, factors = non_negative_parafac(eem_stack, rank, init=init)
#         eem_stack_reconstruct = cp_to_tensor((weight, factors))
#     res_abs = eem_stack - eem_stack_reconstruct
#     with np.errstate(divide='ignore', invalid='ignore'):
#         res_ratio = 100 * (eem_stack - eem_stack_reconstruct) / eem_stack
#     return res_abs, res_ratio


# def parafac_sample_error(eem_stack, index, rank, error_type='MSE',
#                          decomposition_method='non_negative_parafac', init='svd',
#                          dataset_normalization=False, plot_error=True):
#     def ssim(eem1, eem2, k1=0.01, k2=0.03, l=255):
#         c1 = (k1 * l) ** 2
#         c2 = (k2 * l) ** 2
#         mu1 = np.mean(eem1)
#         mu2 = np.mean(eem2)
#         sigma1 = np.std(eem1)
#         sigma2 = np.std(eem2)
#         sigma12 = np.cov(eem1.flat, eem2.flat)[0, 1]
#         numerator = (2 * mu1 * mu2 + c1) * (2 * sigma12 + c2)
#         denominator = (mu1 ** 2 + mu2 ** 2 + c1) * (sigma1 ** 2 + sigma2 ** 2 + c2)
#         ssim_index = numerator / denominator
#         return ssim_index
#
#     err_list = []
#     if dataset_normalization:
#         eem_stack_nor, tf = eems_tf_normalization(eem_stack)
#
#         if decomposition_method == 'parafac':
#             weight, factors = parafac(eem_stack_nor, rank, init=init)
#         elif decomposition_method == 'non_negative_parafac':
#             weight, factors = non_negative_parafac(eem_stack_nor, rank, init=init)
#         eem_stack_reconstruct = cp_to_tensor((weight, factors))
#         eem_stack_reconstruct = eem_stack_reconstruct * tf[:, np.newaxis, np.newaxis]
#     else:
#         if decomposition_method == 'parafac':
#             weight, factors = parafac(eem_stack, rank, init=init)
#         elif decomposition_method == 'non_negative_parafac':
#             weight, factors = non_negative_parafac(eem_stack, rank, init=init)
#         eem_stack_reconstruct = cp_to_tensor((weight, factors))
#
#     for i in range(eem_stack.shape[0]):
#         if error_type == 'MSE':
#             err_list.append(np.mean(np.square(eem_stack[i] - eem_stack_reconstruct[i])))
#         if error_type == 'PSNR':
#             mse = np.mean(np.square(eem_stack[i] - eem_stack_reconstruct[i]))
#             err_list.append(20 * np.log10(eem_stack[i].max() / np.sqrt(mse)))
#         if error_type == 'SSIM':
#             err_list.append(ssim(matrix_dtype_to_uint8(eem_stack[i]), matrix_dtype_to_uint8(eem_stack_reconstruct[i])))
#     if plot_error:
#         plt.figure(figsize=(10, 5))
#         plt.plot(index, err_list)
#         plt.xlabel("Sample")
#         plt.xticks(rotation=90)
#         plt.ylabel(error_type)
#     return err_list


# def eem_stack_spliting(eem_stack, datlist, n_split=4, rule='random'):
#     idx_eems = [i for i in range(eem_stack.shape[0])]
#     split_set = []
#     datlist_set = []
#     if rule == 'random':
#         random.shuffle(idx_eems)
#         idx_splits = np.array_split(idx_eems, n_split)
#     if rule == 'chronological':
#         idx_splits = np.array_split(idx_eems, n_split)
#     for split in idx_splits:
#         split_set.append(np.array([eem_stack[i] for i in split]))
#         datlist_set.append([datlist[i] for i in split])
#     return split_set, datlist_set


class SplitValidation:
    """
    Conduct PARAFAC model validation by evaluating the consistency of PARAFAC models established on EEM sub-datasets.
    """
    def __init__(self, rank, n_split, combination_size, n_test, rule, similarity_metric='TCC', non_negativity=True,
                 tf_normalization=True):
        """
        Parameters
        ----------
        rank: int
            Number of components in PARAFAC.
        n_split: int
            Number of splits.
        combination_size: int or str, {int, 'half'}
            The number of splits assembled into one combination. If 'half' is passed, each combination will include
            half of the splits (i.e., the split-half validation).
        n_test: int or str, {int, 'max'}
            The number of tests conducted. If 'max' is passed, all possible combination will be tested. Otherwise, a
            specified number of combinations will be randomly selected for testing.
        rule: str, {'random', 'sequential'}
            Whether to split the EEM dataset randomly. If 'sequential' is passed, the dataset will be split according
            to index order.
        non_negativity: bool
            Whether to apply non-negativity constraint in PARAFAC.
        tf_normalization: bool
            Whether to normalize the EEM by total fluorescence in PARAFAC.
        """
        # ---------------Parameters-------------------
        self.rank = rank
        self.n_split = n_split
        self.combination_size = combination_size
        self.n_test = n_test
        self.rule = rule
        self.similarity_metric = similarity_metric
        self.non_negativity = non_negativity
        self.tf_normalization = tf_normalization

        # ----------------Attributes------------------
        self.eem_subsets = None
        self.subset_specific_models = None
        self.similarities_ex = None
        self.similarities_em = None

    def run(self, eem_dataset: EEMDataset):
        split_set = eem_dataset.splitting(n_split=self.n_split, rule=self.rule)
        if self.combination_size == 'half':
            cs = int(self.n_split) / 2
        else:
            cs = int(self.combination_size)
        combos = []
        combo_labels = []
        for i, j in zip(itertools.combinations([i for i in range(self.n_split)], int(cs * 2)),
                        itertools.combinations(list(string.ascii_uppercase)[0:self.n_split], int(cs * 2))):
            elements = list(itertools.combinations(i, int(cs)))
            codes = list(itertools.combinations(j, int(cs)))
            for k in range(int(len(elements) / 2)):
                combos.append([elements[k], elements[-1 - k]])
                combo_labels.append([''.join(codes[k]), ''.join(codes[-1 - k])])
        if self.n_test == 'max':
            n_t = len(combos)
        elif isinstance(self.n_test, int):
            if self.n_test > len(combos):
                n_t = len(combos)
            else:
                n_t = self.n_test
        idx = random.sample(range(len(combos)), n_t)
        model_complete = PARAFAC(rank=self.rank, non_negativity=self.non_negativity,
                                 tf_normalization=self.tf_normalization)
        model_complete.establish(eem_dataset=eem_dataset)
        sims_ex, sims_em, models, subsets = ({}, {}, {}, {})

        for test_count in range(n_t):
            c1 = combos[idx[test_count]][0]
            c2 = combos[idx[test_count]][1]
            label = combo_labels[idx[test_count]]
            eem_dataset_c1 = combine_eem_datasets([split_set[i] for i in c1])
            eem_dataset_c2 = combine_eem_datasets([split_set[i] for i in c2])
            model_c1 = PARAFAC(rank=self.rank, non_negativity=self.non_negativity,
                               tf_normalization=self.tf_normalization)
            model_c1.establish(eem_dataset_c1)
            model_c2 = PARAFAC(rank=self.rank, non_negativity=self.non_negativity,
                               tf_normalization=self.tf_normalization)
            model_c2.establish(eem_dataset_c2)

            m_pair = []
            for m in (model_c1, model_c2):
                m_sim_ex, m_sim_em = loadings_similarity(m, model_complete)
                m_sorted, _ = align_parafac_components(m, model_complete, m_sim_ex + m_sim_em)
                m_pair.append(m_sorted)

            key = '{l1} vs. {l2}'.format(l1=label[0], l2=label[1])
            models[key] = m_pair
            sim_ex_all, sim_em_all = loadings_similarity(model_c1, model_c2)
            sims_ex[key], sims_em[key] = sim_ex_all.to_numpy().diagonal(), sim_em_all.to_numpy().diagonal()
            subsets[key] = [eem_dataset_c1, eem_dataset_c2]

        self.eem_subsets = subsets
        self.subset_specific_models = models
        self.similarities_ex = pd.DataFrame(sims_ex)
        self.similarities_em = pd.DataFrame(sims_em)
        return self

    def plot(self):
        return

def split_validation_interact(eem_stack, em_range, ex_range, rank, datlist, decomposition_method,
                              n_split=4, combination_size='half', n_test='max', rule='random', index=[],
                              criteria='TCC', plot_all_combos=True, dataset_normalization=False,
                              init='svd'):
    split_set, _ = eem_stack_spliting(eem_stack, datlist, n_split=n_split, rule=rule)
    if combination_size == 'half':
        cs = int(n_split) / 2
    else:
        cs = int(combination_size)
    combos = []
    combo_labels = []
    for i, j in zip(itertools.combinations([i for i in range(n_split)], int(cs * 2)),
                    itertools.combinations(list(string.ascii_uppercase)[0:n_split], int(cs * 2))):
        elements = list(itertools.combinations(i, int(cs)))
        codes = list(itertools.combinations(j, int(cs)))
        for k in range(int(len(elements) / 2)):
            combos.append([elements[k], elements[-1 - k]])
            combo_labels.append([''.join(codes[k]), ''.join(codes[-1 - k])])
    if n_test == 'max':
        n_t = len(combos)
    elif isinstance(n_test, int):
        if n_test > len(combos):
            n_t = len(combos)
        else:
            n_t = n_test
    idx = random.sample(range(len(combos)), n_t)
    test_count = 0
    sims = {}
    models = []
    while test_count < n_t:
        c1 = combos[idx[test_count]][0]
        c2 = combos[idx[test_count]][1]
        label = combo_labels[idx[test_count]]
        eem_stack_c1 = np.concatenate([split_set[i] for i in c1], axis=0)
        eem_stack_c2 = np.concatenate([split_set[i] for i in c2], axis=0)
        score1_df, exl1_df, eml1_df, _, _, _, _ = decomposition_interact(eem_stack_c1, em_range, ex_range, rank,
                                                                         index=index,
                                                                         decomposition_method=decomposition_method,
                                                                         dataset_normalization=dataset_normalization,
                                                                         score_normalization=False,
                                                                         loadings_normalization=True,
                                                                         component_normalization=False,
                                                                         plot_loadings=False,
                                                                         plot_components=False, display_score=False,
                                                                         component_autoscale=True, sort_em=True,
                                                                         init=init, plot_fmax=False
                                                                         )
        score2_df, exl2_df, eml2_df, _, _, _, _ = decomposition_interact(eem_stack_c2, em_range, ex_range, rank,
                                                                         index=index,
                                                                         decomposition_method=decomposition_method,
                                                                         dataset_normalization=dataset_normalization,
                                                                         score_normalization=False,
                                                                         loadings_normalization=True,
                                                                         component_normalization=False,
                                                                         plot_loadings=False,
                                                                         plot_components=False, display_score=False,
                                                                         component_autoscale=True, sort_em=True,
                                                                         init=init, plot_fmax=False
                                                                         )
        if test_count > 0:
            _, matched_index_prev, _, _ = align_parafac_components(models[test_count - 1][0][1],
                                                                   models[test_count - 1][0][2], exl1_df, eml1_df,
                                                                   similarity_metric=criteria,
                                                                   wavelength_alignment=False, criteria='mean')
            order = [o[1] for o in matched_index_prev]
            exl1_df = pd.DataFrame({'component {r}'.format(r=i + 1): exl1_df.iloc[:, order[i]] for i in range(rank)})
            eml1_df = pd.DataFrame({'component {r}'.format(r=i + 1): eml1_df.iloc[:, order[i]] for i in range(rank)})
            score1_df = pd.DataFrame(
                {'component {r}'.format(r=i + 1): score1_df.iloc[:, order[i]] for i in range(rank)})

        m_sim, matched_index, max_sim, _ = align_parafac_components(exl1_df, eml1_df, exl2_df, eml2_df,
                                                                    similarity_metric=criteria,
                                                                    wavelength_alignment=False, criteria='mean')
        for l in matched_index:
            if l[0] != l[1]:
                warnings.warn('Component {c1} of model {m1} does not match with '
                              'component {c1} of model {m2}, which is replaced by Component {c2} of model {m2}'
                              .format(c1=l[0] + 1, c2=l[1] + 1, m1=label[0], m2=label[1]))

        order = [o[1] for o in matched_index]
        exl2_df = pd.DataFrame({'component {r}'.format(r=i + 1): exl2_df.iloc[:, order[i]] for i in range(rank)})
        eml2_df = pd.DataFrame({'component {r}'.format(r=i + 1): eml2_df.iloc[:, order[i]] for i in range(rank)})
        score2_df = pd.DataFrame({'component {r}'.format(r=i + 1): score2_df.iloc[:, order[i]] for i in range(rank)})
        models.append([[score1_df, exl1_df, eml1_df, label[0]], [score2_df, exl2_df, eml2_df, label[1]]])
        sims['test {n}: {l1} vs. {l2}'.format(n=test_count + 1, l1=label[0], l2=label[1])] = max_sim
        test_count += 1
    sims_df = pd.DataFrame(sims, index=['component {c}'.format(c=c + 1) for c in range(rank)])
    if plot_all_combos:
        cmap = get_cmap('tab20')
        colors = [cmap(i) for i in np.linspace(0, 1, 2 * len(models))]
        for r in range(rank):
            plt.figure()
            for i in range(len(models)):
                score1_df, exl1_df, eml1_df, label1 = models[i][0]
                score2_df, exl2_df, eml2_df, label2 = models[i][1]
                plt.plot(exl1_df.index.get_level_values(1), exl1_df.iloc[:, r],
                         color=colors[2 * i], linewidth=1, label=label1 + '-ex')
                plt.plot(exl2_df.index.get_level_values(1), exl2_df.iloc[:, r],
                         color=colors[2 * i + 1], linewidth=1, label=label2 + '-ex')
                plt.plot(eml1_df.index.get_level_values(1), eml1_df.iloc[:, r],
                         color=colors[2 * i], linewidth=1, linestyle='dashed', label=label1 + '-em')
                plt.plot(eml2_df.index.get_level_values(1), eml2_df.iloc[:, r],
                         color=colors[2 * i + 1], linewidth=1, linestyle='dashed', label=label2 + '-em')
                plt.xlabel('Wavelength [nm]', fontsize=15)
                plt.xticks(np.arange(min(ex_range), max(em_range), 50), fontsize=12)
                plt.ylabel('Loadings', fontsize=15)
                plt.yticks(fontsize=12)
                plt.title('component {rank}'.format(rank=r + 1))
            plt.legend(fontsize=12, bbox_to_anchor=(1.05, 1), loc='upper left')
        print('Similarity of each test:')
        display(sims_df)
    return models, sims_df


def decomposition_reconstruction_interact(I, J, K, intensity, em_range, ex_range, datlist, data_to_view,
                                          crange=[0, 1000], manual_component=[], rmse=True, plot=True):
    idx = datlist.index(data_to_view)
    rank = I.shape[1]
    if not manual_component:
        num_component = np.arange(0, rank, 1)
    else:
        num_component = [i - 1 for i in manual_component]
    for r in num_component:
        component = np.array([J[:, r]]).T.dot(np.array([K[:, r]]))
        if r == 0:
            sample_r = I[idx, r] * component
        else:
            sample_r += I[idx, r] * component
        # reconstruction_error = np.linalg.norm(sample_r - eem_stack[idx])
        if plot:
            plot_eem(sample_r, em_range, ex_range, auto_intensity_range=False, vmin=crange[0], vmax=crange[1], figure_size=(8, 8),
                     title='Accumulate to component {rank}'.format(rank=r + 1))
    if rmse:
        error = np.sqrt(np.mean((sample_r - intensity) ** 2))
        # print("MSE of the final reconstructed EEM: ", error)
    if plot:
        plot_eem(intensity, em_range, ex_range, auto_intensity_range=False, vmin=crange[0], vmax=crange[1],
                 title='Original footprint')
        plot_eem(intensity - sample_r, em_range, ex_range, auto_intensity_range=False, vmin=crange[0], vmax=crange[1],
                 title='Residuals')
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = np.divide(intensity - sample_r, intensity)
        ratio[ratio == np.inf] = np.nan
        plot_eem(ratio, em_range, ex_range, auto_intensity_range=False, vmin=crange[0], vmax=crange[1], title='Error [%]')
    return sample_r, error


def export_parafac_interact(filepath, score_df, exl_df, eml_df, name, creator, date, email='', doi='', reference='', unit='',
                            toolbox='', fluorometer='', nSample='', decomposition_method='', validation='',
                            dataset_calibration='', preprocess='', sources='', description=''):
    info_dict = {'name': name, 'creator': creator, 'email': email, 'doi': doi, 'reference': reference,
                 'unit': unit, 'toolbox': toolbox, 'date': date, 'fluorometer': fluorometer, 'nSample': nSample,
                 'dateset_calibration': dataset_calibration, 'preprocess': preprocess,
                 'decomposition_method': decomposition_method,
                 'validation': validation, 'sources': sources, 'description': description}
    with open(filepath, 'w') as f:
        f.write('# \n# Fluorescence Model \n# \n')
        for key, value in info_dict.items():
            f.write(key + '\t' + value)
            f.write('\n')
        f.write('# \n# Excitation/Emission (Ex, Em), wavelength [nm], component_n [loading] \n# \n')
        f.close()
    with pd.option_context('display.multi_sparse', False):
        exl_df.to_csv(filepath, mode='a', sep="\t", header=None)
        eml_df.to_csv(filepath, mode='a', sep="\t", header=None)
    with open(filepath, 'a') as f:
        f.write('# \n# timestamp, component_n [Score] \n# \n')
        f.close()
    score_df.to_csv(filepath, mode='a', sep="\t", header=None)
    with open(filepath, 'a') as f:
        f.write('# end #')
    return info_dict
