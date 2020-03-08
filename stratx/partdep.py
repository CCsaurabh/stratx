"""
MIT License

Copyright (c) 2019 Terence Parr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import numpy as np
from numpy import nan, where
import pandas as pd
from typing import Mapping, List, Tuple
import matplotlib.pyplot as plt
import matplotlib as mpl
from  matplotlib.collections import LineCollection
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
from scipy.stats import binned_statistic
import warnings
import collections
from timeit import default_timer as timer
from sklearn.utils import resample

from dtreeviz.trees import *
from numba import jit, prange
import numba


def leaf_samples(rf, X_not_col:np.ndarray) -> Sequence:
    """
    Return a list of arrays where each array is the set of X sample indexes
    residing in a single leaf of some tree in rf forest. For example, if there
    are 4 leaves (in one or multiple trees), we might return:

        array([array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
           array([10, 11, 12, 13, 14, 15]), array([16, 17, 18, 19, 20]),
           array([21, 22, 23, 24, 25, 26, 27, 28, 29]))
    """
    n_trees = len(rf.estimators_)
    leaf_samples = []
    leaf_ids = rf.apply(X_not_col)  # which leaf does each X_i go to for sole tree?
    for t in range(n_trees):
        # Group by id and return sample indexes
        uniq_ids = np.unique(leaf_ids[:,t])
        sample_idxs_in_leaves = [np.where(leaf_ids[:, t] == id)[0] for id in uniq_ids]
        leaf_samples.extend(sample_idxs_in_leaves)
    return leaf_samples


def collect_point_betas(X, y, colname, leaves, nbins:int):
    ignored = 0
    leaf_xranges = []
    leaf_slopes = []
    point_betas = np.full(shape=(len(X),), fill_value=np.nan)

    for samples in leaves: # samples is set of obs indexes that live in a single leaf
        leaf_all_x = X.iloc[samples]
        leaf_x = leaf_all_x[colname].values
        leaf_y = y.iloc[samples].values
        # Right edge of last bin is max(leaf_x) but that means we ignore the last value
        # every time. Tweak domain right edge a bit so max(leaf_x) falls in last bin.
        last_bin_extension = 0.0000001
        domain = (np.min(leaf_x), np.max(leaf_x)+last_bin_extension)
        bins = np.linspace(*domain, num=nbins+1, endpoint=True)
        binned_idx = np.digitize(leaf_x, bins) # bin number for values in leaf_x
        for b in range(1, len(bins)+1):
            bin_x = leaf_x[binned_idx == b]
            bin_y = leaf_y[binned_idx == b]
            if len(bin_x) < 2: # could be none or 1 in bin
                ignored += len(bin_x)
                continue
            r = (np.min(bin_x), np.max(bin_x))
            if len(bin_x)<2 or np.isclose(r[0], r[1]):
    #             print(f'ignoring {bin_x} -> {bin_y} for same range')
                ignored += len(bin_x)
                continue
            lm = LinearRegression()
            leaf_obs_idx_for_bin = np.nonzero((leaf_x>=bins[b-1]) &(leaf_x<bins[b]))
            obs_idx = samples[leaf_obs_idx_for_bin]
            lm.fit(bin_x.reshape(-1, 1), bin_y)
            point_betas[obs_idx] = lm.coef_[0]
            leaf_slopes.append(lm.coef_[0])
            leaf_xranges.append(r)

    leaf_slopes = np.array(leaf_slopes)
    return leaf_xranges, leaf_slopes, point_betas, ignored


def partial_dependence(X:pd.DataFrame, y:pd.Series, colname:str,
                       min_slopes_per_x=5,
                       # ignore pdp y values derived from too few slopes (usually at edges)
                       # important for getting good starting point of PD so AUC isn't skewed.
                       parallel_jit=True,
                       n_trees=1, min_samples_leaf=10, bootstrap=False, max_features=1.0,
                       supervised=True,
                       verbose=False):
    """
    Internal computation of partial dependence information about X[colname]'s effect on y.
    Also computes partial derivative of y with respect to X[colname].

    :param X: 
    :param y: 
    :param colname: 
    :param min_slopes_per_x:   ignore pdp y values derived from too few slopes (less than .3% of num records)
                            tried percentage of max slope count but was too variable; this is same count across all features
    :param n_trees:
    :param min_samples_leaf: 
    :param bootstrap: 
    :param max_features: 
    :param supervised: 
    :param verbose: 

    Returns:
        leaf_xranges    The ranges of X[colname] partitions


        leaf_slopes     Associated slope for each leaf xrange

        dx              The change in x from one non-NaN unique X[colname] to the next

        dydx            The slope at each non-NaN unique X[colname]

        pdpx            The non-NaN unique X[colname] values

        pdpy            The effect of each non-NaN unique X[colname] on y; effectively
                        the cumulative sum (integration from X[colname] x to z for all
                        z in X[colname]). The first value is always 0.

        ignored         How many samples from len(X) total records did we have to
                        ignore because of samples in leaves with identical X[colname]
                        values.
    """
    X_not_col = X.drop(colname, axis=1).values
    X_col = X[colname]
    if supervised:
        rf = RandomForestRegressor(n_estimators=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   bootstrap=bootstrap,
                                   max_features=max_features)
        rf.fit(X_not_col, y)
        if verbose:
            print(f"Strat Partition RF: dropping {colname} training R^2 {rf.score(X_not_col, y):.2f}")

    else:
        """
        Wow. Breiman's trick works in most cases. Falls apart on Boston housing MEDV target vs AGE
        """
        if verbose: print("USING UNSUPERVISED MODE")
        X_synth, y_synth = conjure_twoclass(X)
        rf = RandomForestClassifier(n_estimators=n_trees,
                                   min_samples_leaf=int(min_samples_leaf * 2),  # there are 2x as many samples (X,X') so must double leaf size
                                   bootstrap=bootstrap,
                                   max_features=max_features,
                                   oob_score=False)
        rf.fit(X_synth.drop(colname, axis=1), y_synth)

    if verbose:
        leaves = leaf_samples(rf, X_not_col)
        nnodes = rf.estimators_[0].tree_.node_count
        print(f"Partitioning 'x not {colname}': {nnodes} nodes in (first) tree, "
              f"{len(rf.estimators_)} trees, {len(leaves)} total leaves")

    leaf_xranges, leaf_slopes, ignored = \
        collect_discrete_slopes(rf, X, y, colname)

    # print('leaf_xranges', leaf_xranges)
    # print('leaf_slopes', leaf_slopes)

    real_uniq_x = np.unique(X_col) # comes back sorted
    if verbose:
        print(f"discrete StratPD num samples ignored {ignored}/{len(X)} for {colname}")

    #print("uniq x =", len(real_uniq_x), "slopes.shape =", leaf_slopes.shape, "x ranges.shape", leaf_xranges.shape)
    if parallel_jit:
        slope_at_x, slope_counts_at_x = \
            avg_values_at_x_jit(real_uniq_x, leaf_xranges, leaf_slopes)
    else:
        slope_at_x, slope_counts_at_x = \
            avg_values_at_x_nonparallel_jit(real_uniq_x, leaf_xranges, leaf_slopes)

    # Drop any nan slopes; implies we have no reliable data for that range
    # Last slope is nan since no data after last x value so that will get dropped too
    # Also cut out any pdp x for which we don't have enough support (num slopes avg'd together)
    # Make sure to drop slope_counts_at_x, uniq_x values too :)
    if min_slopes_per_x <= 0:
        min_slopes_per_x = 1 # must have at least one slope value
    notnan_idx = ~np.isnan(slope_at_x)
    relevant_slopes = slope_counts_at_x >= min_slopes_per_x
    idx = notnan_idx & relevant_slopes
    slope_at_x = slope_at_x[idx]
    slope_counts_at_x = slope_counts_at_x[idx]
    pdpx = real_uniq_x[idx]

    # Integrate the partial derivative estimate in slope_at_x across pdpx to get dependence
    dx = np.diff(pdpx)
    dydx = slope_at_x[:-1] # ignore last point as dx is always one smaller

    '''
    # Weight slopes by mass ratio at each x location for which we have a slope
    # Mass ratio is (count at x)/(max count at x) giving 0..1
    if len(pdpx)>1:
        _, pdpx_counts = np.unique(X_col[np.isin(X_col, pdpx)], return_counts=True)
        x_counts = [np.sum(X_col == x) for x in pdpx]
        weighted_dydx = dydx * x_counts[:-1]/np.max(x_counts[:-1])
    '''

    y_deltas = dydx * dx   # change in y from dx[i] to dx[i+1]
    # print(f"y_deltas: {y_deltas}")
    pdpy = np.cumsum(y_deltas)                    # we lose one value here
    pdpy = np.concatenate([np.array([0]), pdpy])  # add back the 0 we lost

    return leaf_xranges, leaf_slopes, slope_counts_at_x, dx, slope_at_x, pdpx, pdpy, ignored


def plot_stratpd(X:pd.DataFrame, y:pd.Series, colname:str, targetname:str,
                 min_slopes_per_x=5,  # ignore pdp y values derived from too few slopes (usually at edges)
                 # important for getting good starting point of PD so AUC isn't skewed.
                 n_trials=5, # how many pd curves to show (subsampling by 2/3 to get diff X sets)
                 n_trees=1,
                 min_samples_leaf=10,
                 bootstrap=False,
                 subsample_size=.75,
                 max_features=1.0,
                 supervised=True,
                 ax=None,
                 xrange=None,
                 yrange=None,
                 title=None,
                 show_xlabel=True,
                 show_ylabel=True,
                 show_pdp_line=False,
                 show_all_pdp=True,
                 show_slope_lines=True,
                 show_slope_counts=False,
                 show_x_counts=True,
                 show_impact=False,
                 show_impact_dots=True,
                 show_impact_line=True,
                 pdp_marker_size=2,
                 pdp_marker_alpha=.5,
                 pdp_line_width=.5,
                 slope_line_color='#2c7fb8',
                 slope_line_width=.5,
                 slope_line_alpha=.3,
                 pdp_line_color='black',
                 pdp_marker_color='black',
                 pdp_marker_cmap='coolwarm',
                 impact_fill_color='#FFE091',
                 impact_pdp_color='#D73028',
                 impact_marker_size=3,
                 fontname='Arial',
                 title_fontsize=11,
                 label_fontsize=10,
                 ticklabel_fontsize=10,
                 barchart_size=0.20,
                 # if show_slope_counts, what ratio of vertical space should barchart use at bottom?
                 barchar_alpha=0.7,
                 verbose=False,
                 figsize=None
                 ):
    """
    Plot the partial dependence of X[colname] on y.

    Returns:
        leaf_xranges    The ranges of X[colname] partitions


        leaf_slopes     Associated slope for each leaf xrange

        dx              The change in x from one non-NaN unique X[colname] to the next

        dydx            The slope at each non-NaN unique X[colname]

        pdpx            The non-NaN unique X[colname] values

        pdpy            The effect of each non-NaN unique X[colname] on y; effectively
                        the cumulative sum (integration from X[colname] x to z for all
                        z in X[colname]). The first value is always 0.

        ignored         How many samples from len(X) total records did we have to
                        ignore because of samples in leaves with identical X[colname]
                        values.
    """
    def avg_pd_curve(all_pdpx, all_pdpy):
        m = defaultdict(float)
        c = defaultdict(int)
        for i in range(n_trials):
            for px, py in zip(all_pdpx, all_pdpy):
                for x, y in zip(px, py):
                    m[x] += y
                    c[x] += 1
        for x in m.keys():
            m[x] /= c[x]

        # We now have dict with average pdpy for each pdpx found in any curve
        # but we need to ensure we get it back in pdpx order
        pdpx = np.array(sorted(m.keys()))
        pdpy = np.empty(shape=(len(m),))
        for i,x in enumerate(pdpx):
            pdpy[i] = m[x]
        return pdpx, pdpy

    all_pdpx = []
    all_pdpy = []
    n = len(X)
    ignored = 0
    for i in range(n_trials):
        if n_trials>1:
            if bootstrap:
                idxs = resample(range(n), n_samples=n, replace=True) # bootstrap
            else: # subsample
                idxs = resample(range(n), n_samples=int(n*subsample_size), replace=False)
            X_, y_ = X.iloc[idxs], y.iloc[idxs]
        else:
            X_, y_ = X, y

        leaf_xranges, leaf_slopes, slope_counts_at_x, dx, slope_at_x, pdpx, pdpy, ignored_ = \
            partial_dependence(X=X_, y=y_, colname=colname,
                               min_slopes_per_x=min_slopes_per_x,
                               n_trees=n_trees, min_samples_leaf=min_samples_leaf,
                               bootstrap=bootstrap, max_features=max_features,
                               supervised=supervised,
                               verbose=verbose)
        ignored += ignored_
        all_pdpx.append(pdpx)
        all_pdpy.append(pdpy)

    ignored /= n_trials # average number of x values ignored across trials

    if ax is None:
        if figsize is not None:
            fig, ax = plt.subplots(1, 1, figsize=figsize)
        else:
            fig, ax = plt.subplots(1, 1)

    avg_pdp_marker_size = pdp_marker_size
    if show_all_pdp and n_trials>1:
        sorted_by_imp = np.argsort([np.mean(np.abs(v)) for v in all_pdpy])
        cmap = plt.get_cmap(pdp_marker_cmap)
        ax.set_prop_cycle(color=cmap(np.linspace(0,1,num=n_trials)))
        for i in range(n_trials):
            ax.plot(all_pdpx[sorted_by_imp[i]], all_pdpy[sorted_by_imp[i]],
                    '.', markersize=pdp_marker_size, alpha=pdp_marker_alpha)
        avg_pdp_marker_size += 1

    # Get avg curve, reset pdpx and pdpy to the average
    pdpx, pdpy = avg_pd_curve(all_pdpx, all_pdpy)
    ax.plot(pdpx, pdpy, '.', c=pdp_marker_color, markersize=avg_pdp_marker_size, label=colname)

    if show_pdp_line:
        ax.plot(pdpx, pdpy, lw=pdp_line_width, c=pdp_line_color)

    domain = (np.min(X[colname]), np.max(X[colname]))  # ignores any max(x) points as no slope info after that

    min_y = min(pdpy)
    max_y = max(pdpy)
    if n_trials==1 and show_slope_lines:
        segments = []
        for xr, slope in zip(leaf_xranges, leaf_slopes):
            w = np.abs(xr[1] - xr[0])
            delta_y = slope * w
            closest_x_i = np.abs(pdpx - xr[0]).argmin() # find curve point for xr[0]
            closest_x = pdpx[closest_x_i]
            closest_y = pdpy[closest_x_i]
            slope_line_endpoint_y = closest_y + delta_y
            one_line = [(closest_x, closest_y), (closest_x + w, slope_line_endpoint_y)]
            segments.append( one_line )
            if slope_line_endpoint_y < min_y:
                min_y = slope_line_endpoint_y
            elif slope_line_endpoint_y > max_y:
                max_y = slope_line_endpoint_y

        lines = LineCollection(segments, alpha=slope_line_alpha, color=slope_line_color, linewidths=slope_line_width)
        ax.add_collection(lines)

    if xrange is not None:
        ax.set_xlim(*xrange)
    else:
        ax.set_xlim(*domain)
    if yrange is not None:
        ax.set_ylim(*yrange)
    else:
        ax.set_ylim(min_y, max_y)

    X_col = X[colname]
    _, pdpx_counts = np.unique(X_col[np.isin(X_col, pdpx)], return_counts=True)

    leave_room_scaler = 1.3
    x_width = max(pdpx) - min(pdpx) + 1
    count_bar_width = x_width / len(pdpx)
    if count_bar_width/x_width < 0.002:
        count_bar_width = x_width * 0.002 # don't make them so skinny they're invisible
    # print(f"x_width={x_width:.2f}, count_bar_width={count_bar_width}")
    if show_x_counts:
        ax2 = ax.twinx()
        # scale y axis so the max count height is 10% of overall chart
        ax2.set_ylim(0, max(pdpx_counts) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(pdpx_counts)]))
        ax2.bar(x=pdpx, height=pdpx_counts, width=count_bar_width,
                facecolor='#BABABA', align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"$x$ point count", labelpad=-12, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0] - (yrange[1]-yrange[0]) * barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size * leave_room_scaler, max_y)
        ax2.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)
        for tick in ax2.get_xticklabels():
            tick.set_fontname(fontname)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)

    if n_trials==1 and show_slope_counts:
        ax2 = ax.twinx()
        # scale y axis so the max count height is barchart_size of overall chart
        ax2.set_ylim(0, max(slope_counts_at_x) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(slope_counts_at_x)]))
        ax2.bar(x=pdpx, height=slope_counts_at_x, width=count_bar_width,
                facecolor='#BABABA', align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"slope count", labelpad=-12, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0]-(yrange[1]-yrange[0])*barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size, max_y)
        ax2.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)
        for tick in ax2.get_xticklabels():
            tick.set_fontname(fontname)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)

    if show_impact:
        # r = max_y - min_y
        # if max(weighted_pdpy) > 0:
        #     verticalalignment = 'bottom'
        #     y_text_shift = r*.01
        # else:
        #     verticalalignment = 'top'
        #     y_text_shift = -r*.02 # drop a bit to avoid collision with 0 line
        # ax.text(0.5, .98, f"Impact {impact:.2f}", horizontalalignment='center',
        #         verticalalignment='top', transform=ax.transAxes,
        #         fontsize=label_fontsize, fontname=fontname)
        # ax.text((max(pdpx)+1+min(pdpx))/2, 0+y_text_shift, f"Impact {impact:.2f}",
        #         horizontalalignment='center', verticalalignment=verticalalignment,
        #         fontsize=label_fontsize, fontname=fontname)
        ax.fill_between(pdpx, pdpy, [0] * len(pdpx), color=impact_fill_color)
        if show_impact_dots:
            ax.scatter(pdpx, pdpy, s=impact_marker_size, c=impact_pdp_color)
        if show_impact_line:
            ax.plot(pdpx, pdpy, lw=.3, c='grey')

    if show_xlabel:
        xl = colname
        # if show_impact:
        #     impact, importance = compute_importance(X_col, pdpx, pdpy)
        #     xl += f" (Impact {impact:.2f}, importance {importance:.2f})"
        ax.set_xlabel(xl, fontsize=label_fontsize, fontname=fontname)
    if show_ylabel:
        ax.set_ylabel(targetname, fontsize=label_fontsize, fontname=fontname)
    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    ax.spines['top'].set_linewidth(.5)
    ax.spines['right'].set_linewidth(.5)
    ax.spines['left'].set_linewidth(.5)
    ax.spines['bottom'].set_linewidth(.5)

    for tick in ax.get_xticklabels():
        tick.set_fontname(fontname)
    for tick in ax.get_yticklabels():
        tick.set_fontname(fontname)
    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    return pdpx, pdpy, ignored


@jit(nopython=True)
def discrete_xc_space(x: np.ndarray, y: np.ndarray):
    """
    Use the unique x values within a leaf to dynamically compute the bins,
    rather then using a fixed nbins hyper parameter. Group the leaf x,y by x
    and collect the average y.  The unique x and y averages are the new x and y pairs.
    The slope for each x is:

        (y_{i+1} - y_i) / (x_{i+1} - x_i)

    If the ordinal/ints are exactly one unit part, then it's just y_{i+1} - y_i. If
    they are not consecutive, we do not ignore isolated x_i as it ignores too much data.
    E.g., if x is [1,3,4] and y is [9,8,10] then the x=2 coordinate is spanned as part
    of 1 to 3. The two slopes are [(8-9)/(3-1), (10-8)/(4-3)] and bin widths are [2,1].

    If there is exactly one unique x value in the leaf, the leaf provides no information
    about how x_c contributes to changes in y. We have to ignore this leaf.
    """
    ignored = 0

    # Group by x, take mean of all y with same x value (they come back sorted too)
    uniq_x = np.unique(x)
    avg_y = np.array([y[x==ux].mean() for ux in uniq_x])

    if len(uniq_x)==1:
        # print(f"ignore {len(x)} in discrete_xc_space")
        ignored += len(x)
        return np.array([[0]],dtype=x.dtype), np.array([0.0]), ignored

    bin_deltas = np.diff(uniq_x)
    y_deltas = np.diff(avg_y)
    leaf_slopes = y_deltas / bin_deltas  # "rise over run"
    leaf_xranges = np.array(list(zip(uniq_x, uniq_x[1:])))

    return leaf_xranges, leaf_slopes, ignored

def collect_discrete_slopes(rf, X, y, colname):
    """
    For each leaf of each tree of the random forest rf (trained on all features
    except colname), get the leaf samples then isolate the column of interest X values
    and the target y values. Perform piecewise linear regression of X[colname] vs y
    to get the slopes in various regions of X[colname].  We don't need to subtract
    the minimum y value before regressing because the slope won't be different.
    (We are ignoring the intercept of the regression line).

    Return for each leaf, the ranges of X[colname] partitions,
    associated slope for each range

    Only does discrete now after doing pointwise continuous slopes differently.
    """
    # start = timer()
    leaf_slopes = []   # drop or rise between discrete x values
    leaf_xranges = []  # drop is from one discrete value to next

    ignored = 0

    X_col = X[colname].values
    X_not_col = X.drop(colname, axis=1)
    leaves = leaf_samples(rf, X_not_col)
    y = y.values

    if False:
        nnodes = rf.estimators_[0].tree_.node_count
        print(f"Partitioning 'x not {colname}': {nnodes} nodes in (first) tree, "
              f"{len(rf.estimators_)} trees, {len(leaves)} total leaves")

    for samples in leaves:
        leaf_x = X_col[samples]
        # leaf_x = one_leaf_samples[]#.reshape(-1,1)
        leaf_y = y[samples]

        if np.abs(np.min(leaf_x) - np.max(leaf_x)) < 1.e-8: # faster than np.isclose()
            # print(f"ignoring xleft=xright @ {r[0]}")
            ignored += len(leaf_x)
            continue

        leaf_xranges_, leaf_slopes_, ignored_ = \
            discrete_xc_space(leaf_x, leaf_y)

        leaf_slopes.extend(leaf_slopes_)
        leaf_xranges.extend(leaf_xranges_)
        ignored += ignored_

    if len(leaf_xranges)==0:
        # make sure empty list has same shape (jit complains)
        leaf_xranges = np.array([]).reshape(0, 0)
    else:
        leaf_xranges = np.array(leaf_xranges)
    leaf_slopes = np.array(leaf_slopes)

    # stop = timer()
    # if verbose: print(f"collect_discrete_slopes {stop - start:.3f}s")
    return leaf_xranges, leaf_slopes, ignored


'''
Currently not needed
def avg_values_at_x_nojit(uniq_x, leaf_ranges, leaf_slopes):
    """
    Compute the weighted average of leaf_slopes at each uniq_x.

    Value at max(x) is NaN since we have no data beyond that point.
    """
    nx = len(uniq_x)
    nslopes = len(leaf_slopes)
    slopes = np.zeros(shape=(nx, nslopes))
    # collect the slope for each range (taken from a leaf) as collection of
    # flat lines across the same x range
    i = 0
    for xr, slope in zip(leaf_ranges, leaf_slopes):
        s = np.full(nx, slope, dtype=float)
        # now trim line so it's only valid in range xr;
        # don't set slope on right edge
        s[np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]) )] = np.nan
        slopes[:, i] = s
        i += 1

    # The value could be genuinely zero so we use nan not 0 for out-of-range
    # Now average horiz across the matrix, averaging within each range
    # Wrap nanmean() in catcher to avoid "Mean of empty slice" warning, which
    # comes from some rows being purely NaN; I should probably look at this sometime
    # to decide whether that's hiding a bug (can there ever be a nan for an x range)?
    # Oh right. We might have to ignore some leaves (those with single unique x values)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        avg_value_at_x = np.nanmean(slopes, axis=1)
        # how many slopes avg'd together to get avg
        slope_counts_at_x = nslopes - np.isnan(slopes).sum(axis=1)

    # return average slope at each unique x value and how many slopes included in avg at each x
    return avg_value_at_x, slope_counts_at_x
'''


# We get about 20% boost from parallel but limits use of other parallelism it seems;
# i get crashes when using multiprocessing package on top of this.
# If using n_jobs=1 all the time for importances, then turn jit=False so this
# method is not used
@jit(nopython=True, parallel=True) # use prange not range.
def avg_values_at_x_jit(uniq_x, leaf_ranges, leaf_slopes):
    """
    Compute the weighted average of leaf_slopes at each uniq_x.

    Value at max(x) is NaN since we have no data beyond that point.
    """
    nx = uniq_x.shape[0]
    nslopes = leaf_slopes.shape[0]
    slopes = np.empty(shape=(nx, nslopes), dtype=np.double)
    # collect the slope for each range (taken from a leaf) as collection of
    # flat lines across the same x range

    '''
    for j in prange(nslopes):
        xl = leaf_ranges[j,0]
        xr = leaf_ranges[j,1]
        slope = leaf_slopes[j]
        # s = np.full(nx, slope)#, dtype=double)
        # s[np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]) )] = np.nan
        # slopes[:, i] = s

        # Compute slope all the way across uniq_x but then trim line so
        # slope is only valid in range xr; don't set slope on right edge
        for i in prange(nx):
            if (uniq_x[i] >= xl) or (uniq_x[i] < xr):
                slopes[i, j] = slope
            else:
                slopes[i, j] = np.nan
    '''

    for i in prange(nslopes):
        xr, slope = leaf_ranges[i], leaf_slopes[i]
        # Compute slope all the way across uniq_x but then trim line so
        # slope is only valid in range xr; don't set slope on right edge
        slopes[:, i] = np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]), np.nan, slope)

    # The value could be genuinely zero so we use nan not 0 for out-of-range
    # Now average horiz across the matrix, averaging within each range
    # Wrap nanmean() in catcher to avoid "Mean of empty slice" warning, which
    # comes from some rows being purely NaN; I should probably look at this sometime
    # to decide whether that's hiding a bug (can there ever be a nan for an x range)?
    # Oh right. We might have to ignore some leaves (those with single unique x values)

    # Compute:
    #   avg_value_at_x = np.mean(slopes[good], axis=1)  (numba doesn't allow axis arg)
    #   slope_counts_at_x = nslopes - np.isnan(slopes).sum(axis=1)
    avg_value_at_x = np.zeros(shape=nx)
    slope_counts_at_x = np.zeros(shape=nx)
    for i in prange(nx):
        row = slopes[i, :]
        n_nan = np.sum(np.isnan(row))
        avg_value_at_x[i] = np.nan if n_nan==nslopes else np.nanmean(row)
        slope_counts_at_x[i] = nslopes - n_nan

    # return average slope at each unique x value and how many slopes included in avg at each x
    return avg_value_at_x, slope_counts_at_x


# Hideous copying to get different kinds of jit'ing. This is slower by 20%
# than other version but can run in parallel with multiprocessing package.
@jit(nopython=True)
def avg_values_at_x_nonparallel_jit(uniq_x, leaf_ranges, leaf_slopes):
    """
    Compute the weighted average of leaf_slopes at each uniq_x.

    Value at max(x) is NaN since we have no data beyond that point.
    """
    nx = len(uniq_x)
    nslopes = len(leaf_slopes)
    slopes = np.zeros(shape=(nx, nslopes))
    # collect the slope for each range (taken from a leaf) as collection of
    # flat lines across the same x range

    for i in range(nslopes):
        xr, slope = leaf_ranges[i], leaf_slopes[i]

        # s = np.full(nx, slope)#, dtype=float)
        # s[np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]) )] = np.nan
        # slopes[:, i] = s

        # Compute slope all the way across uniq_x but then trim line so
        # slope is only valid in range xr; don't set slope on right edge
        slopes[:, i] = np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]), np.nan, slope)


    # The value could be genuinely zero so we use nan not 0 for out-of-range
    # Now average horiz across the matrix, averaging within each range
    # Wrap nanmean() in catcher to avoid "Mean of empty slice" warning, which
    # comes from some rows being purely NaN; I should probably look at this sometime
    # to decide whether that's hiding a bug (can there ever be a nan for an x range)?
    # Oh right. We might have to ignore some leaves (those with single unique x values)

    # Compute:
    #   avg_value_at_x = np.mean(slopes[good], axis=1)  (numba doesn't allow axis arg)
    #   slope_counts_at_x = nslopes - np.isnan(slopes).sum(axis=1)
    avg_value_at_x = np.zeros(shape=nx)
    slope_counts_at_x = np.zeros(shape=nx)
    for i in range(nx):
        row = slopes[i, :]
        n_nan = np.sum(np.isnan(row))
        avg_value_at_x[i] = np.nan if n_nan==nslopes else np.nanmean(row)
        slope_counts_at_x[i] = nslopes - n_nan

    # return average slope at each unique x value and how many slopes included in avg at each x
    return avg_value_at_x, slope_counts_at_x


def plot_stratpd_gridsearch(X, y, colname, targetname,
                            min_samples_leaf_values=(2,5,10,20,30),
                            min_slopes_per_x_values=(5,), # Show default count only by default
                            n_trials=5,
                            nbins_values=(1,2,3,4,5),
                            nbins_smoothing=None,
                            binned=False,
                            yrange=None,
                            xrange=None,
                            show_regr_line=False,
                            show_slope_lines=True,
                            show_impact=False,
                            show_slope_counts=False,
                            show_x_counts=True,
                            marginal_alpha=.05,
                            slope_line_alpha=.1,
                            title_fontsize=8,
                            label_fontsize=7,
                            ticklabel_fontsize=7,
                            cellwidth=2.5,
                            cellheight=2.5):
    ncols = len(min_samples_leaf_values)
    if not binned:
        fig, axes = plt.subplots(len(min_slopes_per_x_values), ncols + 1,
                                 figsize=((ncols + 1) * cellwidth, len(min_slopes_per_x_values)*cellheight))
        if len(min_slopes_per_x_values)==1:
            axes = axes.reshape(1,-1)
        for row,min_slopes_per_x in enumerate(min_slopes_per_x_values):
            marginal_plot_(X, y, colname, targetname, ax=axes[row][0],
                           show_regr_line=show_regr_line, alpha=marginal_alpha,
                           label_fontsize=label_fontsize,
                           ticklabel_fontsize=ticklabel_fontsize)
            col = 1
            axes[row][0].set_title("Marginal", fontsize=title_fontsize)
            for msl in min_samples_leaf_values:
                #print(f"---------- min_samples_leaf={msl} ----------- ")
                try:
                    pdpx, pdpy, ignored = \
                        plot_stratpd(X, y, colname, targetname, ax=axes[row][col],
                                     min_samples_leaf=msl,
                                     min_slopes_per_x=min_slopes_per_x,
                                     n_trials=n_trials,
                                     xrange=xrange,
                                     yrange=yrange,
                                     n_trees=1,
                                     show_ylabel=False,
                                     slope_line_alpha=slope_line_alpha,
                                     show_slope_lines=show_slope_lines,
                                     show_impact=show_impact,
                                     show_slope_counts=show_slope_counts,
                                     show_x_counts=show_x_counts,
                                     label_fontsize=label_fontsize,
                                     ticklabel_fontsize=ticklabel_fontsize)
                    # print(f"leafsz {msl} avg abs curve value: {np.mean(np.abs(pdpy)):.2f}, mean {np.mean(pdpy):.2f}, min {np.min(pdpy):.2f}, max {np.max(pdpy)}")
                except ValueError as e:
                    print(e)
                    axes[row][col].set_title(f"Can't gen: leafsz={msl}", fontsize=8)
                else:
                    title = f"leafsz={msl}, min_slopes={min_slopes_per_x}"
                    if ignored>0:
                        title = f"leafsz={msl}, min_slopes={min_slopes_per_x},\nignored={100 * ignored / len(X):.2f}%"
                    axes[row][col].set_title(title, fontsize=title_fontsize)
                col += 1

    else:
        # more or less ignoring this branch these days
        nrows = len(nbins_values)
        fig, axes = plt.subplots(nrows, ncols + 1,
                                 figsize=((ncols + 1) * 2.5, nrows * 2.5))

        row = 0
        for i, nbins in enumerate(nbins_values):
            marginal_plot_(X, y, colname, targetname, ax=axes[row, 0], show_regr_line=show_regr_line)
            if row==0:
                axes[row,0].set_title("Marginal", fontsize=10)
            col = 1
            for msl in min_samples_leaf_values:
                #print(f"---------- min_samples_leaf={msl}, nbins={nbins:.2f} ----------- ")
                try:
                    leaf_xranges, leaf_slopes, Xbetas, plot_x, plot_y, ignored = \
                        plot_stratpd_binned(X, y, colname, targetname, ax=axes[row, col],
                                            nbins=nbins,
                                            min_samples_leaf=msl,
                                            nbins_smoothing=nbins_smoothing,
                                            yrange=yrange,
                                            show_ylabel=False,
                                            n_trees=1)
                except ValueError:
                    axes[row, col].set_title(
                        f"Can't gen: leafsz={msl}, nbins={nbins}",
                        fontsize=8)
                else:
                    axes[row, col].set_title(
                        f"leafsz={msl}, nbins={nbins},\nignored={100*ignored/len(X):.2f}%",
                        fontsize=9)
                col += 1
            row += 1


def marginal_plot_(X, y, colname, targetname, ax, alpha=.1, show_regr_line=True,
                   label_fontsize=7,
                   ticklabel_fontsize=7):
    ax.scatter(X[colname], y, alpha=alpha, label=None, s=10)
    ax.set_xlabel(colname, fontsize=label_fontsize)
    ax.set_ylabel(targetname, fontsize=label_fontsize)
    col = X[colname]

    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    if show_regr_line:
        r = LinearRegression()
        r.fit(X[[colname]], y)
        xcol = np.linspace(np.min(col), np.max(col), num=100)
        yhat = r.predict(xcol.reshape(-1, 1))
        ax.plot(xcol, yhat, linewidth=1, c='orange', label=f"$\\beta_{{{colname}}}$")
        ax.text(min(xcol) * 1.02, max(y) * .95, f"$\\beta_{{{colname}}}$={r.coef_[0]:.3f}")


def marginal_catplot_(X, y, colname, targetname, ax, catnames, alpha=.1, show_xticks=True):
    catcodes, catnames_, catcode2name = getcats(X, colname, catnames)

    ax.scatter(X[colname].values, y.values, alpha=alpha, label=None, s=10)
    ax.set_xlabel(colname)
    ax.set_ylabel(targetname)
    # col = X[colname]
    # cats = np.unique(col)

    if show_xticks:
        ax.set_xticks(catcodes)
        ax.set_xticklabels(catnames_)
    else:
        ax.set_xticks([])

def plot_catstratpd_gridsearch(X, y, colname, targetname,
                               n_trials=3,
                               min_samples_leaf_values=(2, 5, 10, 20, 30),
                               min_y_shifted_to_zero=True,  # easier to read if values are relative to 0 (usually); do this for high cardinality cat vars
                               show_xticks=True,
                               show_all_cat_deltas=True,
                               catnames=None,
                               yrange=None,
                               cellwidth=2.5,
                               cellheight=2.5):

    ncols = len(min_samples_leaf_values)
    fig, axes = plt.subplots(1, ncols + 1,
                             figsize=((ncols + 1) * cellwidth, cellheight))

    marginal_catplot_(X, y, colname, targetname, catnames=catnames, ax=axes[0], alpha=0.05,
                      show_xticks=show_xticks)
    axes[0].set_title("Marginal", fontsize=10)

    col = 1
    for msl in min_samples_leaf_values:
        #print(f"---------- min_samples_leaf={msl} ----------- ")
        if yrange is not None:
            axes[col].set_ylim(yrange)
        try:
            uniq_catcodes, combined_avg_per_cat, ignored, merge_ignored = \
                plot_catstratpd(X, y, colname, targetname, ax=axes[col],
                                n_trials=n_trials,
                                min_samples_leaf=msl,
                                catnames=catnames,
                                yrange=yrange,
                                n_trees=1,
                                show_xticks=show_xticks,
                                show_all_deltas=show_all_cat_deltas,
                                show_ylabel=False,
                                min_y_shifted_to_zero=min_y_shifted_to_zero)
        except ValueError:
            axes[col].set_title(f"Can't gen: leafsz={msl}", fontsize=8)
        else:
            axes[col].set_title(f"leafsz={msl}, ign'd={ignored / len(X):.1f}%", fontsize=9)
        col += 1


def catwise_leaves(rf, X_not_col, X_col, y, max_catcode):
    """
    Return a 2D array with the average y value for each category in each leaf
    normalized by subtracting the overall avg y value from all categories.

    The columns are the y avg value changes per cat found in a single leaf as
    they differ from the overall y average. Each row represents a category level. E.g.,

    row           leaf0       leaf1
     0       166.430176  186.796956
     1       219.590349  176.448626

    Cats are possibly noncontiguous with nan rows for cat codes not present. Not all
    values in a leaf column will be non-nan.  Only those categories mentioned in
    a leaf have values.
    Shape is (max cat + 1, num leaves).

    As reference cat, use the smallest cat in the leaf. Previously, we
    subtracted the average of the leaf y not the overall y avg,  but this failed
    to capture the relationship between categories when there are many levels. Tried
    subtracting mean(y) but that was harder to interpret later in noisy environment;
    couldn't get the proper zero PDP y value on left edge of PDP.

    Within a single leave, there will typically only be a few categories
    represented.
    """
    leaves = leaf_samples(rf, X_not_col)

    leaf_deltas = np.full(shape=(max_catcode+1, len(leaves)), fill_value=np.nan)
    leaf_counts = np.zeros(shape=(max_catcode+1, len(leaves)), dtype=int)
    refcats = np.empty(shape=(len(leaves),), dtype=int)

    ignored = 0
    for leaf_i in range(len(leaves)):
        sample = leaves[leaf_i]
        leaf_cats = X_col[sample]
        leaf_y = y[sample]
        # perform a groupby(catname).mean()
        uniq_leaf_cats, count_leaf_cats = np.unique(leaf_cats, return_counts=True) # comes back sorted
        avg_y_per_cat = np.array([leaf_y[leaf_cats==cat].mean() for cat in uniq_leaf_cats])
        # print("uniq_leaf_cats",uniq_leaf_cats,"count_y_per_cat",count_leaf_cats)

        if len(uniq_leaf_cats) < 2:
            # print(f"ignoring {len(sample)} obs for {len(avg_y_per_cat)} cat(s) in leaf")
            ignored += len(sample)
            refcats[leaf_i] = -1 # cat codes are assumed to be positive integers
            continue

        # Use random cat code as refcat
        idx_of_random_cat_in_leaf = np.random.randint(0, len(uniq_leaf_cats), size=1)
        refcats[leaf_i] = uniq_leaf_cats[idx_of_random_cat_in_leaf]
        delta_y_per_cat = avg_y_per_cat - avg_y_per_cat[idx_of_random_cat_in_leaf]
        # print("delta_y_per_cat",delta_y_per_cat)

        # Store into leaf i vector just those deltas we have data for
        # leave cats w/o representation as nan
        leaf_deltas[uniq_leaf_cats, leaf_i] = delta_y_per_cat
        leaf_counts[uniq_leaf_cats, leaf_i] = count_leaf_cats

    # refcat[i]=-1 for all leaves i we ignored so remove those and return
    # See unit test test_catwise_leaves:test_two_leaves_with_2nd_ignored()
    keep_leaves_idxs = np.where(refcats>=0)[0]
    leaf_deltas = leaf_deltas[:,keep_leaves_idxs]
    leaf_counts = leaf_counts[:,keep_leaves_idxs]
    refcats = refcats[keep_leaves_idxs]
    return leaf_deltas, leaf_counts, refcats, ignored


def cat_partial_dependence(X, y,
                           colname,  # X[colname] expected to be numeric codes
                           max_catcode=None, # if we're bootstrapping, might see diff max's so normalize to one max
                           n_trees=1,
                           min_samples_leaf=5,
                           max_features=1.0,
                           bootstrap=False,
                           supervised=True,
                           verbose=False):
    X_not_col = X.drop(colname, axis=1).values
    X_col = X[colname].values
    if (X_col<0).any():
        raise ValueError(f"Category codes must be > 0 in column {colname}")
    if max_catcode is None:
        max_catcode = np.max(X_col)
    if supervised:
        rf = RandomForestRegressor(n_estimators=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   bootstrap = bootstrap,
                                   max_features = max_features,
                                   oob_score=False)
        rf.fit(X_not_col, y)
        if verbose:
            print(f"CatStrat Partition RF: dropping {colname} training R^2 {rf.score(X_not_col, y):.2f}")
    else:
        print("USING UNSUPERVISED MODE")
        X_synth, y_synth = conjure_twoclass(X)
        rf = RandomForestClassifier(n_estimators=n_trees,
                                    min_samples_leaf=min_samples_leaf * 2,
                                    # there are 2x as many samples (X,X') so must double leaf size
                                    bootstrap=bootstrap,
                                    max_features=max_features,
                                    oob_score=False)
        rf.fit(X_synth.drop(colname,axis=1), y_synth)

    rf.fit(X_not_col, y)

    leaf_deltas, leaf_counts, refcats, ignored = \
        catwise_leaves(rf, X_not_col, X_col, y.values, max_catcode)

    USE_MEAN_Y=False
    if USE_MEAN_Y:
        count_per_cat = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            avg_per_cat = np.nanmean(leaf_deltas, axis=1)
            merge_ignored = 0
            # slope_counts_at_cat = leaf_histos.shape[1] - np.isnan(leaf_histos).sum(axis=1)
    else:
        avg_per_cat, count_per_cat, merge_ignored = \
            avg_values_at_cat(leaf_deltas, leaf_counts, refcats, verbose=verbose)

    if verbose:
        print(f"CatStratPD Num samples ignored {ignored} for {colname}")

    return leaf_deltas, leaf_counts, avg_per_cat, count_per_cat, ignored, merge_ignored

# currently unused
'''
@jit(nopython=True)
def avg_values_at_cat_jit(leaf_deltas, leaf_counts, refcats, max_iter=3, verbose=False):
    """
    In leaf_deltas, we have information from the leaves indicating how much
    above or below each category was from the reference category of that leaf.
    The reference category is the one with the minimum cat code (not y value), so the
    refcat's relative value in the leaf column will be 0. Categories not mentioned
    in the leaf, will have nan values in that column.

    The goal is to combine all of these relative category bumps and drops,
    despite the fact that they do not have the same reference category. We
    collect all of the leaves with a reference category level i and average them
    together (for all unique categories mentioned in min_cats).  Now we have
    a list of relative value vectors, one per category level used as a reference.

    The list is sorted in order of unique reference category. (Hopefully this
    will be much smaller than the number of categories total for speed.) Note these
    sum vectors might have np.nan values to represent unknown category info.
    I set all refcat values to np.nan to ease computation then set the smallest
    refcat relative value to 0 right before function exit.  This sorting is important
    so data can feed forward; refcat i uses the value of refcat i in the running sum
    of refcat vectors. (see 2nd loop)

    Now we have to create a result vector, sums_per_cat, that combines the
    relative vectors. The problem is of course the different reference categories.
    We initialize sums_per_cat to be the average relative to the first unique
    reference category. Let's assume that the first refcat is 0, which means we take
    the first element from the avg_for_refcats list to initialize sums_per_cat. To add
    in the next vector, we first have to compensate for the difference in
    reference category. refcats[i] tells us which category the vector is
    relative to so we take the corresponding value from the running sum, sums_per_cat,
    at position refcats[i] and add that to all elements of the avg_for_refcats[i]
    vector.

    BTW, it's possible that more than a single value within a leaf_deltas vector will be 0.
    I.e., the reference category value is always 0 in the vector, but there might be
    another category whose value was the same y, giving a 0 relative value. I set them
    to nan, however, when combining histos for same refcat.

    Example:

    refcats: [0,1]

    sums_for_refcats
     [[nan nan]
     [ 1. nan]
     [ 2.  3.]
     [nan  2.]
     [ 0. nan]
     [nan nan]]

    counts
     [[0 0]
     [1 0]
     [1 1]
     [0 1]
     [1 0]
     [0 0]]

    Then to combine, we see a loop with an iteration per unique min cat:

    0 : initial  = [nan  1.  2. nan  0. nan] 	sums_per_cat = [nan  1.  2. nan  0. nan]
    1 : adjusted = [nan nan  4.  3. nan nan] 	sums_per_cat = [nan  1.  6.  3.  0. nan]

    Then divide by

    So we get a final avg per cat of:  [ 0.  1.  3.  3.  0. nan]

    Notes:

    * 2 diff ignores
    * num values per leaf isn't super important; want min to remove effects of
      other vars but big enough not to find just one cat in the leaf
    * refcat choice isn't big deal except for efficiency. want to merge
      leaves with same refcat quickly; more with same refcat reduces vectors
      to process
    * if unlucky and refcat y value in leaf is outlier, it biases all
      cat deltas in that leaf. Gotta rotate and get more estimates.
      imagine people heights with same height but ref person is sitting.
      all deltas will look huge instead of 0.
    * number of values we average for each cat matters; more values means
      noise should cancel out or we get better estimate one way or another
    * might need min_values_per_cat hyperparameter akin to min_slopes_per_x
    * wow. choosing random refcat helps avoid focusing on some outliers by accident

    :param leaf_deltas: A 2D matrix where rows are category levels/values and
                        columns hold y values for categories.
    :param refcats: For each leaf, we must know what category was used as the reference.
                     I.e., which category had the smallest y value in the leaf?
    :return:
    """
    # FIRST LOOP COMBINES LEAF VECTORS WITH SAME REFCAT FOR EFFICIENCY
    uniq_refcats = np.array(sorted(np.unique(refcats)), dtype=np.int32)

    if verbose:
        print("refcats =", refcats)
        print("uniq_refcats =", uniq_refcats)
        print("leaf_deltas\n", leaf_deltas[0:30])
        print("leaf_counts\n", leaf_counts[0:30])
        # print("leaf_deltas reordered by refcat order\n", leaf_deltas[0:30,np.argsort(refcats)])

    avg_for_refcats = np.empty(shape=(len(leaf_deltas), len(uniq_refcats)))
    counts_for_refcats = np.empty(shape=(len(leaf_deltas), len(uniq_refcats)), dtype=np.int32)

    for j,cat in enumerate(uniq_refcats):
        # collect and add up vectors from all leaves with cat as the reference category
        idxs_of_same_cat = np.where(refcats == cat)[0]
        leaves_with_same_refcat = leaf_deltas[:, idxs_of_same_cat]
        counts_with_same_refcat = leaf_counts[:, idxs_of_same_cat]
        s = nanmerge_matrix_cols(leaves_with_same_refcat*counts_with_same_refcat)
        # count how many non-nan values values across all leaves with cat as ref category
        c = np.sum(counts_with_same_refcat, axis=1)
        avg_for_refcats[:,j] = s / zero_as_one(c)
        counts_for_refcats[:,j] = c

    # print("unsorted counts\n", counts_for_refcats[0:30])
    # We want to initial group to be one with most weight in hopes of merging
    # more vectors in a single pass
    weight_for_refcats = np.sum(counts_for_refcats, axis=0)

    # Sort to get most populated vectors to the left of matrix; more chance of intersection
    uniq_refcats_by_weight_idxs = np.argsort(weight_for_refcats)[::-1]
    avg_for_refcats = avg_for_refcats[:,uniq_refcats_by_weight_idxs]
    weight_for_refcats = weight_for_refcats[uniq_refcats_by_weight_idxs]
    uniq_refcats = uniq_refcats[uniq_refcats_by_weight_idxs]
    counts_for_refcats = counts_for_refcats[:,uniq_refcats_by_weight_idxs]

    if verbose:
        print("counts\n", counts_for_refcats[0:30])
        cats_with_values_count = np.sum(counts_for_refcats, axis=1)
        nonzero_idx = np.where(cats_with_values_count>0)[0]
        print("counts per cat>0 (",len(cats_with_values_count[nonzero_idx]),"/",len(cats_with_values_count),
              "): ", cats_with_values_count[nonzero_idx])
        # print("counts per cat\n", counts_for_refcats[np.where(np.sum(counts_for_refcats, axis=1)>0)[0]])
        print("refcat weights\n", weight_for_refcats)
        print("sorted refcats", uniq_refcats)
        # print("sums_for_refcats (reordered by weight)\n", sums_for_refcats[:30])
        print("avgs per refcat\n", avg_for_refcats[0:30])


    # SECOND LOOP SUMS COMBINED VECTORS USING RELATIVE VALUE FROM RUNNING SUM
    """
    [[nan nan nan nan nan nan nan nan  0. nan]
     [nan nan nan nan  0. nan nan nan nan nan]
     [nan  0. nan nan nan nan nan nan nan nan]
     [ 0. nan nan nan nan nan nan nan nan nan]
     [ 1. nan nan nan nan nan  0. nan nan nan]
     [ 2.  3.  0.  6.  7. nan  1. nan 13. nan]
     [nan nan  1. nan nan nan nan  5. nan nan]
     [ 4. nan nan  7.  8. nan nan nan nan nan]
     [ 5.  6. nan nan nan  0.  4. nan nan nan]
     [nan nan nan nan nan nan nan  8. 17.  0.]
     [ 7. nan  5. nan nan  2. nan nan nan nan]
    """
    # catavg is the running sum vector
    catavg = avg_for_refcats[:,0] # init with first ref category (column)
    catavg_weight = counts_for_refcats[:,0]
    merge_ignored = 0
    work = set(range(1,len(uniq_refcats)))
    completed = set([-1]) # init to any nonempty set to enter loop #numba.typed.List()
    # completed.append(-1) # init to any nonempty set to enter loop
    iteration = 1
    while len(work)>0 and len(completed)>0 and iteration<=max_iter:
        #print("PASS", iteration, "len(work)", len(work))
        completed.clear()
        for j in work:      # for each refcat, avg in the vectors
            cat = uniq_refcats[j]
            v = avg_for_refcats[:,j]
            intersection_idx = np.where(~np.isnan(catavg) & ~np.isnan(v))[0]

            # print(intersection_idx)
            if len(intersection_idx)==0: # found something to merge into catavg?
                continue

            # pick random category in intersection to use as common refcat
            ix = np.random.choice(intersection_idx, size=1)[0]

            # modifying columns in place to be mergeable
            shifted_v = v - v[ix]        # make ix the reference cat in common
            relative_to_value = catavg[ix]
            adjusted_v = shifted_v + relative_to_value     # now v is mergeable with catavg
            cur_weight  = counts_for_refcats[:,j]
            prev_catavg = catavg
            catavg = nanavg_vectors(catavg, adjusted_v, catavg_weight, cur_weight)
            # Update weight of running sum to incorporate "mass" from v
            catavg_weight += cur_weight
            """
            if verbose:
                print(f"{cat:-2d} : vec to add =", parray(v), f"- {v[ix]:.2f}")
                print("     shifted    =", parray(shifted_v), "+ %.2f" % (relative_to_value,))
                print("     adjusted   =", parray(adjusted_v), "*", cur_weight)
                print("     prev avg   =", parray(prev_catavg),"*",catavg_weight-cur_weight)
                print("     new avg    =", parray(catavg))
                print()
            """
            completed.add(j)
        iteration += 1
        work = work - completed

    if len(work)>0:
        #print("Left", len(work), "leaves in work list")
        # hmm..couldn't merge some vectors; total up the samples we ignored
        for j in work:
            merge_ignored += weight_for_refcats[j]
        """
        if verbose: print("cats", uniq_refcats[list(work)], "couldn't be merged into running sum; ignored=",merge_ignored)
        """

    """
    if verbose: print("final cat avgs", parray3(catavg))
        """
    return catavg, catavg_weight, merge_ignored # last one is count of values per cat actually incorporated
'''

def avg_values_at_cat(leaf_deltas, leaf_counts, refcats, max_iter=3, verbose=False):
    """
    In leaf_deltas, we have information from the leaves indicating how much
    above or below each category was from the reference category of that leaf.
    The reference category is randomly selected, so the
    refcat's relative y value in the leaf will be 0. Categories not mentioned
    in the leaf, will have NAN values. refcats[leaf] tells us which category
    is the reference category for leaf.

    The goal is to merge all of the columns in leaf_deltas,
    despite the fact that they do not have the same reference category. We init a
    running average vector to be the first column of category deltas. Then we attempt
    to merge each of the other columns into the running average. We make multiple passes
    over the columns of leaf_deltas until nothing changes, we hit the maximum number
    of iterations, or everything has merged.

    To merge vector v (column j of leaf_deltas) into catavg, select a category, index ix, in common at random.
    Subtract v[ix] from v so that ix is v's new reference and v[ix]=0. Add catavg[ix] to
    the adjusted v so that v is now comparable to catavg. We can now do a weighted
    average of catavg and v, paying careful attention of NaN.

    It's possible that more than a single value within a leaf_deltas vector is 0.
    I.e., the reference category value is always 0 in the vector, but there might be
    another category whose value was the same y, giving a 0 relative value.

    Example:

    leaf_deltas

        [[   nan  26.85  -2.47]
         [-28.67   0.   -30.37]
         [ 10.3     nan   0.  ]
         [  4.72  23.99  -6.03]
         [  0.    21.47 -11.  ]]

    leaf_counts

        [[0 2 1]
         [1 1 1]
         [1 0 2]
         [1 2 2]
         [2 3 1]]

    refcats

        [4, 1, 2]

    Init catavg to [7.58  -23.97  10.30   4.72   1.32]
    work = {1, 2} as catavg is index 0.

    Then we loop until nothing changes, combining columns of leaf_deltas. We have to do
    weighted average so keep track of the count for each category, catavg_weight, for
    the running sum in catavg.

     1 : vec to add = [ 26.85   0.00    nan  23.99  21.47 ] - 23.99
         shifted    = [ 2.86  -23.99    nan   0.00  -2.52 ] + 4.72
         adjusted   = [ 7.58  -19.27    nan   4.72   2.20 ] * [2 1 0 2 3]
         prev avg   = [ nan   -28.67  10.30   4.72   0.00 ] * [0 1 1 1 2]
         new avg    = [ 7.58  -23.97  10.30   4.72   1.32 ]

     2 : vec to add = [ -2.47 -30.37   0.00  -6.03 -11.00 ] - -6.03
         shifted    = [ 3.56  -24.34   6.03   0.00  -4.97 ] + 4.72
         adjusted   = [ 8.28  -19.62  10.75   4.72  -0.25 ] * [1 1 2 2 1]
         prev avg   = [ 7.58  -23.97  10.30   4.72   1.32 ] * [2 2 1 3 5]
         new avg    = [ 7.81  -22.52  10.60   4.72   1.06 ]

    final cat avgs [ 7.813 -22.520 10.600  4.720  1.058 ]

    Then divide by

    So we get a final avg per cat of:  [ 0.  1.  3.  3.  0. nan]

    Choosing random refcat helps avoid focusing on some outliers by accident
    and after merging same (random) refcat, use random refcat to merge in the loop.
    even less likely to hit outlier 2x in row.
    """
    # catavg is the running average vector and starts out as the first column
    catavg = leaf_deltas[:,0] # init with first ref category (column)
    catavg_weight = leaf_counts[:,0]
    merge_ignored = 0
    weight_for_refcats = np.sum(leaf_counts, axis=0)

    work = set(range(1,leaf_deltas.shape[1]))
    completed = {-1} # init to any nonempty set to enter loop
    iteration = 1
    while len(work)>0 and len(completed)>0 and iteration<=max_iter:
        # print(f"PASS {iteration} len(work)", len(work))
        completed = set()
        for j in work:      # for each refcat, avg in the vectors
            cat = refcats[j]
            v = leaf_deltas[:,j]
            intersection_idx = np.where(~np.isnan(catavg) & ~np.isnan(v))[0]

            # print(intersection_idx)
            if len(intersection_idx)==0: # found something to merge into catavg?
                continue

            # pick random category in intersection to use as common refcat
            ix = np.random.choice(intersection_idx, size=1)[0]

            # Merge column j into catavg vector
            shifted_v = v - v[ix]                       # make ix the reference cat in common
            relative_to_value = catavg[ix]              # corresponding value in catavg
            adjusted_v = shifted_v + relative_to_value  # adjust so v is mergeable with catavg
            cur_weight  = leaf_counts[:,j]
            prev_catavg = catavg                        # track only for verbose/debugging purposes
            catavg = nanavg_vectors(catavg, adjusted_v, catavg_weight, cur_weight)
            # Update weight of running avg to incorporate "mass" from v
            catavg_weight += cur_weight
            if verbose:
                print(f"{cat:-2d} : vec to add =", parray(v), f"- {v[ix]:.2f}")
                print("     shifted    =", parray(shifted_v), f"+ {relative_to_value:.2f}")
                print("     adjusted   =", parray(adjusted_v), "*", cur_weight)
                print("     prev avg   =", parray(prev_catavg),"*",catavg_weight-cur_weight)
                print("     new avg    =", parray(catavg))
                print()
            completed.add(j)
        iteration += 1
        work = work - completed

    if len(work)>0:
        print(f"Left {len(work)} leaves/unique cats in work list")
        # hmm..couldn't merge some vectors; total up the samples we ignored
        for j in work:
            merge_ignored += weight_for_refcats[j]
        if verbose: print(f"cats {refcats[list(work)]} couldn't be merged into running sum; ignored={merge_ignored}")

    if verbose: print("final cat avgs", parray3(catavg))
    return catavg, catavg_weight, merge_ignored # last one is count of values per cat actually incorporated


def plot_catstratpd(X, y,
                    colname,  # X[colname] expected to be numeric codes
                    targetname,
                    catnames=None,  # map of catcodes to catnames; converted to map if sequence passed
                    # must pass dict or series if catcodes are not 1..n contiguous
                    # None implies use np.unique(X[colname]) values
                    # Must be 0-indexed list of names if list
                    n_trials=5,
                    subsample_size = .75,
                    bootstrap=False,
                    ax=None,
                    n_trees=1,
                    min_samples_leaf=5,
                    max_features=1.0,
                    yrange=None,
                    title=None,
                    show_x_counts=True,
                    pdp_marker_lw=1,
                    pdp_marker_size=6,
                    pdp_marker_alpha=.6,
                    pdp_color='black',
                    fontname='Arial',
                    title_fontsize=11,
                    label_fontsize=10,
                    barchart_size=0.20,
                    barchar_alpha=0.9,
                    ticklabel_fontsize=10,
                    min_y_shifted_to_zero=True,
                    # easier to read if values are relative to 0 (usually); do this for high cardinality cat vars
                    show_xlabel=True,
                    show_xticks=True,
                    show_ylabel=True,
                    show_impact=False,
                    verbose=False,
                    figsize=(5,3)):
    """
    Warning: cat columns are assumed to be label encoded as unique integers. This
    function uses the cat code as a raw index internally. So if you have two cat
    codes 1 and 1000, this function allocates internal arrays of size 1000+1.

    only works for ints, not floats
    """
    if ax is None:
        if figsize is not None:
            fig, ax = plt.subplots(1, 1, figsize=figsize)
        else:
            fig, ax = plt.subplots(1, 1)

    uniq_catcodes = np.unique(X[colname])
    max_catcode = max(uniq_catcodes)

    X_col = X[colname]
    n = len(X_col)

    def avg_pd_catvalues(all_avg_per_cat):
        m = np.zeros(shape=(max_catcode+1,))
        c = np.zeros(shape=(max_catcode+1,), dtype=int)

        # For each unique catcode, sum and count avg_per_cat values found among trials
        for i in range(n_trials):
            avg_per_cat = all_avg_per_cat[i]
            catcodes = np.where(~np.isnan(avg_per_cat))[0]
            for code in catcodes:
                m[code] += avg_per_cat[code]
                c[code] += 1
        # Convert to average value per cat
        for code in np.where(m!=0)[0]:
            m[code] /= c[code]
        m = np.where(c==0, np.nan, m) # cats w/o values should be nan, not 0
        return m

    impacts = []
    all_avg_per_cat = []
    ignored = 0
    merge_ignored = 0
    for i in range(n_trials):
        if n_trials>1:
            if bootstrap:
                idxs = resample(range(n), n_samples=n, replace=True)
            else: # use subsetting
                idxs = resample(range(n), n_samples=int(n * subsample_size), replace=False)
            X_, y_ = X.iloc[idxs], y.iloc[idxs]
        else:
            X_, y_ = X, y

        leaf_deltas, leaf_counts, avg_per_cat, count_per_cat, ignored_, merge_ignored_ = \
            cat_partial_dependence(X_, y_,
                                   max_catcode=np.max(X_col),
                                   colname=colname,
                                   n_trees=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   max_features=max_features,
                                   bootstrap=False,
                                   verbose=verbose)
        impacts.append(np.nanmean(np.abs(avg_per_cat)))
        if min_y_shifted_to_zero:
            avg_per_cat -= np.nanmin(avg_per_cat)
        ignored += ignored_
        merge_ignored += merge_ignored_
        all_avg_per_cat.append( avg_per_cat )

    ignored /= n_trials # average number of x values ignored across trials
    merge_ignored /= n_trials # average number of x values ignored across trials

    combined_avg_per_cat = avg_pd_catvalues(all_avg_per_cat)
    print("mean(pdpy)", np.nanmean(combined_avg_per_cat))

    impact_order = np.argsort(impacts)
    print("impacts", impacts)
    print("avg impact", np.mean(impacts))

    cmap = plt.get_cmap('coolwarm')
    colors=cmap(np.linspace(0, 1, num=n_trials))
    min_y = 9999999999999
    max_y = -min_y

    for i in range(0,n_trials): # find min/max from all trials
        avg_per_cat = all_avg_per_cat[i]
        if np.nanmin(avg_per_cat) < min_y:
            min_y = np.nanmin(avg_per_cat)
        if np.nanmax(avg_per_cat) > max_y:
            max_y = np.nanmax(avg_per_cat)

    # Show a dot for each cat in all trials
    for i in range(1,n_trials): # only do if > 1 trial
        ax.plot(range(len(uniq_catcodes)), all_avg_per_cat[i][uniq_catcodes], '.', c=mpl.colors.rgb2hex(colors[impact_order[i]]),
                markersize=pdp_marker_size, alpha=pdp_marker_alpha)

    # Show avg line
    segments = []
    for cat, delta in zip(range(len(uniq_catcodes)), combined_avg_per_cat[uniq_catcodes]):
        one_line = [(cat-0.5, delta), (cat+0.5, delta)]
        segments.append(one_line)
        # ax.plot([cat-0.5,cat+0.5], [delta,delta], '-',
        #         lw=1.0, c=pdp_color, alpha=pdp_marker_alpha)
        # ax.plot(range(len(uniq_catcodes)), avg_delta, '.', c='k', markersize=pdp_marker_size + 1)
    lines = LineCollection(segments, alpha=pdp_marker_alpha, color=pdp_color, linewidths=pdp_marker_lw)
    ax.add_collection(lines)

    leave_room_scaler = 1.3

    if yrange is not None:
        ax.set_ylim(*yrange)
    else:
        ax.set_ylim(min_y*1.05, max_y*1.05)

    if show_x_counts:
        # Only show cat counts for those which are present in X[colname] (unlike stratpd plot)
        _, cat_counts = np.unique(X_col[np.isin(X_col, uniq_catcodes)], return_counts=True)
        count_bar_width=1
        ax2 = ax.twinx()
        # scale y axis so the max count height is 10% of overall chart
        ax2.set_ylim(0, max(cat_counts) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(cat_counts)]))
        ax2.bar(x=range(len(uniq_catcodes)), height=cat_counts, width=count_bar_width,
                facecolor='#BABABA', align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"$x$ point count", labelpad=-12, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0]-(yrange[1]-yrange[0])*barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size * leave_room_scaler, max_y)
        plt.setp(ax2.get_xticklabels(), visible=False)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)

    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    # leave .8 on either size of graph
    ax.set_xlim(0-.8,len(uniq_catcodes)-1+0.8)
    if show_xticks:
        ax.set_xticks(range(len(uniq_catcodes)))
        if catnames is not None:
            labels = [catnames[c] for c in uniq_catcodes]
            ax.set_xticklabels(labels)
        else:
            ax.set_xticklabels(uniq_catcodes)
        for tick in ax.get_xticklabels():
            tick.set_fontname(fontname)
    else:
        ax.set_xticks([])
        ax.set_xticklabels([])

    if show_xlabel:
        label = colname
        if show_impact:
            label += f" (Impact {np.mean(impacts):.1f}, importance {np.mean(weighted_impacts):.1f})"
        ax.set_xlabel(label, fontsize=label_fontsize, fontname=fontname)
    if show_ylabel:
        ax.set_ylabel(targetname, fontsize=label_fontsize, fontname=fontname)
    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    for tick in ax.get_yticklabels():
        tick.set_fontname(fontname)

    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    ax.spines['top'].set_linewidth(.5)
    ax.spines['right'].set_linewidth(.5)
    ax.spines['left'].set_linewidth(.5)
    ax.spines['bottom'].set_linewidth(.5)

    return uniq_catcodes, combined_avg_per_cat, ignored, merge_ignored


def getcats(X, colname, incoming_cats):
    if incoming_cats is None or isinstance(incoming_cats, pd.Series):
        catcodes = np.unique(X[colname])
        catcode2name = [None] * (max(catcodes) + 1)
        for c in catcodes:
            catcode2name[c] = c
        catcode2name = np.array(catcode2name)
        catnames = catcodes
    elif isinstance(incoming_cats, dict):
        catnames_ = [None] * (max(incoming_cats.keys()) + 1)
        catcodes = []
        catnames = []
        for code, name in incoming_cats.items():
            catcodes.append(code)
            catnames.append(name)
            catnames_[code] = name
        catcodes = np.array(catcodes)
        catnames = np.array(catnames)
        catcode2name = np.array(catnames_)
    elif not isinstance(incoming_cats, dict):
        # must be a list of names then
        catcodes = []
        catnames_ = [None] * len(incoming_cats)
        for cat, c in enumerate(incoming_cats):
            if c is not None:
                catcodes.append(cat)
            catnames_[cat] = c
        catcodes = np.array(catcodes)
        catcode2name = np.array(catnames_)
        catnames = np.array(incoming_cats)
    else:
        raise ValueError("catnames must be None, 0-indexed list, or pd.Series")
    return catcodes, catnames, catcode2name


# -------------- S U P P O R T ---------------

def scramble(X : np.ndarray) -> np.ndarray:
    """
    From Breiman: https://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm
    "...the first coordinate is sampled from the N values {x(1,n)}. The second
    coordinate is sampled independently from the N values {x(2,n)}, and so forth."
    """
    X_rand = X.copy()
    ncols = X.shape[1]
    for col in range(ncols):
        X_rand[:,col] = np.random.choice(X[:,col], len(X), replace=True)
    return X_rand


def df_scramble(X : pd.DataFrame) -> pd.DataFrame:
    """
    From Breiman: https://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm
    "...the first coordinate is sampled from the N values {x(1,n)}. The second
    coordinate is sampled independently from the N values {x(2,n)}, and so forth."
    """
    X_rand = X.copy()
    for colname in X:
        X_rand[colname] = X_rand[colname].sample(frac=1.0)
    return X_rand


def conjure_twoclass(X):
    """
    Make new data set 2x as big with X and scrambled version of it that
    destroys structure between features. Old is class 0, scrambled is class 1.
    """
    if isinstance(X, pd.DataFrame):
        X_rand = df_scramble(X)
        X_synth = pd.concat([X, X_rand], axis=0)
    else:
        X_rand = scramble(X)
        X_synth = np.concatenate([X, X_rand], axis=0)
    y_synth = np.concatenate([np.zeros(len(X)),
                              np.ones(len(X_rand))], axis=0)
    return X_synth, pd.Series(y_synth)


def compress_catcodes(X, catcolnames, inplace=False):
    "Compress categorical integers if less than 90% dense"
    X_ = X if inplace else X.copy()
    for colname in catcolnames:
        uniq_x = np.unique(X_[colname])
        if len(uniq_x) < 0.90 * len(X_):  # sparse? compress into contiguous range of x cat codes
            X_[colname] = X_[colname].rank(method='min').astype(int)
    return X_


def nanavg_vectors(a, b, wa=1.0, wb=1.0):
    "Add two vectors a+b but support nan+x==x and nan+nan=nan"
    a_nan = np.isnan(a)
    b_nan = np.isnan(b)
    # both_nan = a_nan & b_nan
    c = a*wa + b*wb # weighted average where both are non-nan
    c /= zero_as_one(wa+wb) # weighted avg
    # c = np.where(a_nan, 0, a) * wa + np.where(b_nan, 0, b) * wb
    # if adding nan to nan, leave as nan
    c[a_nan] = b[a_nan]   # copy any stuff where b has only value (unweighted into result)
    in_a_not_b = (~a_nan) & b_nan
    c[in_a_not_b] = a[in_a_not_b] # copy stuff where a has only value
    return c


def nanmerge_matrix_cols(A):
    """
    Add all vertical vectors in A but support nan+x==x and nan+nan=nan.
    """
    s = np.nansum(A, axis=1)
    all_nan_entries = np.isnan(A)
    # if all entries for a cat are nan, make sure sum s is nan for that cat
    s[all_nan_entries.all(axis=1)] = np.nan
    return s


def zero_as_one(a):
    return np.where(a == 0, 1, a)


def parray(a):
    if type(a[0])==np.int64:
        return '[ ' + (' '.join([f"{x:6d}" for x in a])).strip() + ' ]'
    else:
        return '[ ' + (' '.join([f"{x:6.2f}" for x in a])).strip() + ' ]'


def parray3(a):
    return '[ ' + (' '.join([f"{x:6.3f}" for x in a])).strip() + ' ]'
