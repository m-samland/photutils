# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module defines classes to estimate the 2D background and background
RMS in an image.
"""

from itertools import product
import warnings

from astropy.nddata import NDData
from astropy.stats import SigmaClip
import astropy.units as u
from astropy.utils import lazyproperty
from astropy.utils.exceptions import AstropyUserWarning
import numpy as np
from numpy.lib.index_tricks import index_exp

from .core import SExtractorBackground, StdBackgroundRMS
from .interpolators import BkgZoomInterpolator
from ..utils import ShepardIDWInterpolator

__all__ = ['Background2D']

__doctest_requires__ = {('Background2D'): ['scipy']}


class Background2D:
    """
    Class to estimate a 2D background and background RMS noise in an
    image.

    The background is estimated using sigma-clipped statistics in each
    box of a grid that covers the input ``data`` to create a
    low-resolution, and possibly irregularly-gridded, background map.

    The final background map is calculated by interpolating the
    low-resolution background map.

    Parameters
    ----------
    data : array_like or `~astropy.nddata.NDData`
        The 2D array from which to estimate the background and/or
        background RMS map.

    box_size : int or array_like (int)
        The box size along each axis.  If ``box_size`` is a scalar then
        a square box of size ``box_size`` will be used.  If ``box_size``
        has two elements, they should be in ``(ny, nx)`` order.  For
        best results, the box shape should be chosen such that the
        ``data`` are covered by an integer number of boxes in both
        dimensions.  When this is not the case, see the ``edge_method``
        keyword for more options.

    mask : array_like (bool), optional
        A boolean mask, with the same shape as ``data``, where a `True`
        value indicates the corresponding element of ``data`` is
        masked. Masked data are excluded from calculations. ``mask`` is
        intended to mask sources or bad pixels. Use ``coverage_mask``
        to mask blank areas of an image. ``mask`` and ``coverage_mask``
        differ only in that ``coverage_mask`` is applied to the output
        background and background RMS maps (see ``fill_value``).

    coverage_mask : array_like (bool), optional
        A boolean mask, with the same shape as ``data``, where a `True`
        value indicates the corresponding element of ``data`` is masked.
        ``coverage_mask`` should be `True` where there is no coverage
        (i.e., no data) for a given pixel (e.g., blank areas in a mosaic
        image). It should not be used for bad pixels (in that case use
        ``mask`` instead). ``mask`` and ``coverage_mask`` differ only in
        that ``coverage_mask`` is applied to the output background and
        background RMS maps (see ``fill_value``).

    fill_value : float, optional
        The value used to fill the output background and background RMS
        maps where the input ``coverage_mask`` is `True`.

    exclude_percentile : float in the range of [0, 100], optional
        The percentage of masked pixels in a box, used as a threshold
        for determining if the box is excluded. If a box has
        more than ``exclude_percentile`` percent of its pixels
        masked then it will be excluded from the low-resolution map.
        Masked pixels include those from the input ``mask`` and
        ``coverage_mask``, those resulting from the data padding
        (i.e., if ``edge_method='pad'``), and those resulting from
        any sigma clipping (i.e., if ``sigma_clip`` is used). Setting
        ``exclude_percentile=0`` will exclude boxes that have any
        masked pixels. Setting ``exclude_percentile=100`` will only
        exclude boxes that are completely masked. Note that completely
        masked boxes are *always* excluded. For best results,
        ``exclude_percentile`` should be kept as low as possible (as
        long as there are sufficient pixels for reasonable statistical
        estimates). The default is 10.0.

    filter_size : int or array_like (int), optional
        The window size of the 2D median filter to apply to the
        low-resolution background map.  If ``filter_size`` is a scalar
        then a square box of size ``filter_size`` will be used.  If
        ``filter_size`` has two elements, they should be in ``(ny, nx)``
        order.  A filter size of ``1`` (or ``(1, 1)``) means no
        filtering.

    filter_threshold : int, optional
        The threshold value for used for selective median filtering of
        the low-resolution 2D background map. The median filter will
        be applied to only the background boxes with values larger
        than ``filter_threshold``. Set to `None` to filter all boxes
        (default).

    edge_method : {'pad', 'crop'}, optional
        The method used to determine how to handle the case where the
        image size is not an integer multiple of the ``box_size`` in
        either dimension.  Both options will resize the image to give an
        exact multiple of ``box_size`` in both dimensions.

        * ``'pad'``: pad the image along the top and/or right edges.
          This is the default and recommended method.
        * ``'crop'``: crop the image along the top and/or right edges.

    sigma_clip : `astropy.stats.SigmaClip` instance, optional
        A `~astropy.stats.SigmaClip` object that defines the sigma
        clipping parameters.  If `None` then no sigma clipping will be
        performed.  The default is to perform sigma clipping with
        ``sigma=3.0`` and ``maxiters=10``.

    bkg_estimator : callable, optional
        A callable object (a function or e.g., an instance of any
        `~photutils.background.BackgroundBase` subclass) used to
        estimate the background in each of the boxes.  The callable
        object must take in a 2D `~numpy.ndarray` or
        `~numpy.ma.MaskedArray` and have an ``axis`` keyword
        (internally, the background will be calculated along
        ``axis=1``).  The callable object must return a 1D
        `~numpy.ma.MaskedArray`.  If ``bkg_estimator`` includes sigma
        clipping, it will be ignored (use the ``sigma_clip`` keyword to
        define sigma clipping).  The default is an instance of
        `~photutils.background.SExtractorBackground`.

    bkgrms_estimator : callable, optional
        A callable object (a function or e.g., an instance of any
        `~photutils.background.BackgroundRMSBase` subclass) used to
        estimate the background RMS in each of the boxes.  The callable
        object must take in a 2D `~numpy.ndarray` or
        `~numpy.ma.MaskedArray` and have an ``axis`` keyword
        (internally, the background RMS will be calculated along
        ``axis=1``).  The callable object must return a 1D
        `~numpy.ma.MaskedArray`.  If ``bkgrms_estimator`` includes sigma
        clipping, it will be ignored (use the ``sigma_clip`` keyword to
        define sigma clipping).  The default is an instance of
        `~photutils.background.StdBackgroundRMS`.

    interpolator : callable, optional
        A callable object (a function or object) used to interpolate the
        low-resolution background or background RMS image to the
        full-size background or background RMS maps.  The default is an
        instance of `BkgZoomInterpolator`.

    Notes
    -----
    If there is only one background box element (i.e., ``box_size`` is
    the same size as (or larger than) the ``data``), then the background
    map will simply be a constant image.
    """

    def __init__(self, data, box_size, *, mask=None, coverage_mask=None,
                 fill_value=0.0, exclude_percentile=10.0, filter_size=(3, 3),
                 filter_threshold=None, edge_method='pad',
                 sigma_clip=SigmaClip(sigma=3.0, maxiters=10),
                 bkg_estimator=SExtractorBackground(sigma_clip=None),
                 bkgrms_estimator=StdBackgroundRMS(sigma_clip=None),
                 interpolator=BkgZoomInterpolator()):

        if isinstance(data, (u.Quantity, NDData)):  # includes CCDData
            self.unit = data.unit
            data = data.data
        else:
            self.unit = None

        self.data = self._validate_array(data, 'data', shape=False)
        self.mask = self._validate_array(mask, 'mask')
        self.coverage_mask = self._validate_array(coverage_mask,
                                                  'coverage_mask')
        self.total_mask = self._combine_masks()

        box_size = self._process_size_input(box_size)
        # box_size cannot be larger than the data array size
        self.box_size = np.array((min(box_size[0], data.shape[0]),
                                  min(box_size[1], data.shape[1])))

        self.fill_value = fill_value
        if exclude_percentile < 0 or exclude_percentile > 100:
            raise ValueError('exclude_percentile must be between 0 and 100 '
                             '(inclusive).')
        self.exclude_percentile = exclude_percentile
        self.filter_size = self._process_size_input(filter_size)
        self.filter_threshold = filter_threshold
        self.edge_method = edge_method
        self.sigma_clip = sigma_clip
        bkg_estimator.sigma_clip = None
        bkgrms_estimator.sigma_clip = None
        self.bkg_estimator = bkg_estimator
        self.bkgrms_estimator = bkgrms_estimator
        self.interpolator = interpolator

        self.background_mesh = None
        self.background_rms_mesh = None

        self._prepare_data()
        self._reshape_data()
        self._select_initial_boxes()
        self._compute_box_statistics()
        self._make_meshes()
        self._filter_meshes()

    @staticmethod
    def _process_size_input(array):
        array = np.atleast_1d(array).astype(int)
        if len(array) == 1:
            array = np.repeat(array, 2)
            if len(array) != 2:
                raise ValueError('box_size and filter_size inputs must '
                                 'have only 1 or 2 elements')
        return array

    def _validate_array(self, array, name, shape=True):
        if name in ('mask', 'coverage_mask') and array is np.ma.nomask:
            array = None
        if array is not None:
            array = np.asanyarray(array)
            if array.ndim != 2:
                raise ValueError(f'{name} must be a 2D array.')
            if shape and array.shape != self.data.shape:
                raise ValueError(f'data and {name} must have the same shape.')
        return array

    def _combine_masks(self):
        if self.mask is None and self.coverage_mask is None:
            return None
        if self.mask is None:
            return self.coverage_mask
        elif self.coverage_mask is None:
            return self.mask
        else:
            return np.logical_or(self.mask, self.coverage_mask)

    def _prepare_data(self):
        """
        Prepare the data.

        This method:
          * converts the data to float dtype (and makes a copy)
          * automatically masks non-finite values
          * replaces all masked values with NaN
          * converts MaskedArray to ndarray using NaN as masked values
        """
        # float array type is needed to insert nans into the array
        self.data = self.data.astype(float)  # makes a copy

        # include non-finite values in the total mask
        bad_mask = ~np.isfinite(self.data)
        if np.any(bad_mask):
            if self.total_mask is None:
                self.total_mask = bad_mask
            else:
                self.total_mask |= bad_mask
            warnings.warn('Input data contains invalid values (NaNs or '
                          'infs), which were automatically masked.',
                          AstropyUserWarning)

        # replace all masked values with NaN
        if self.total_mask is not None:
            self.data[self.total_mask] = np.nan

        # convert MaskedArray to ndarray using np.nan as masked values
        if isinstance(self.data, np.ma.MaskedArray):
            self.data = self.data.filled(np.nan)

    def _reshape_data(self):
        """
        First, pad or crop the 2D data array so that there are an
        integer number of boxes in both dimensions.

        Then reshape into a different 2D array where each row represents
        the data in a single box.
        """
        self.nboxes = self.data.shape // self.box_size
        extra_size = self.data.shape % self.box_size

        if np.sum(extra_size) != 0:
            # pad or crop the data
            if self.edge_method == 'pad':
                pad_size = self.box_size - extra_size
                pad_width = ((0, pad_size[0]), (0, pad_size[1]))
                data = np.pad(self.data, pad_width, mode='constant',
                              constant_values=np.nan)
                self.nboxes = data.shape // self.box_size
            elif self.edge_method == 'crop':
                crop_size = self.nboxes * self.box_size
                crop_slc = index_exp[0:crop_size[0], 0:crop_size[1]]
                data = self.data[crop_slc]
            else:
                raise ValueError('edge_method must be "pad" or "crop"')
        else:
            data = self.data

        self.box_npixels = np.prod(self.box_size)
        self.nboxes_tot = np.prod(self.nboxes)

        # a reshaped 2D array with box data along the x axis
        self._box_data = np.swapaxes(data.reshape(
            self.nboxes[0], self.box_size[0],
            self.nboxes[1], self.box_size[1]),
            1, 2).reshape(self.nboxes_tot, self.box_npixels)

    @lazyproperty
    def _box_npixels_threshold(self):
        # boxes that are completely masked are always excluded.
        # boxes that contain more than ``exclude_percentile`` percent
        # masked pixels are also excluded:
        #   - for exclude_percentile=0, only boxes where nmasked=0 will
        #     be included
        #   - for exclude_percentile=100, all boxes will be included
        #     *unless* they are completely masked
        threshold = self.exclude_percentile / 100. * self.box_npixels

        # always exclude completely masked boxes
        if self.exclude_percentile == 100:
            threshold += 1
        return threshold

    def _get_box_indices(self):
        """
        Define the x and y indices of the low-resolution box image that
        are used to compute background statistics.

        The ``exclude_percentile`` keyword determines which boxes are
        not used for the background interpolation.
        """
        # the number of NaN pixels in each box
        nmasked = np.count_nonzero(np.isnan(self._box_data), axis=1)

        # define indices of good (included) boxes
        box_idx = np.where(nmasked <= self._box_npixels_threshold)[0]

        if box_idx.size == 0:
            raise ValueError('All boxes contain > {0} ({1} percent per '
                             'box) masked pixels (or all are completely '
                             'masked). Please check your data or increase '
                             '"exclude_percentile" to allow more boxes to '
                             'be included.'
                             .format(self._box_npixels_threshold,
                                     self.exclude_percentile))

        return box_idx

    def _select_initial_boxes(self):
        # perform a first cut on rejecting boxes
        self._box_idx = self._get_box_indices()
        if self._box_idx.size != self._box_data.shape[0]:
            self._box_data = self._box_data[self._box_idx, :]

    def _compute_box_statistics(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=AstropyUserWarning)
            if self.sigma_clip is not None:
                self._box_data = self.sigma_clip(self._box_data, axis=1,
                                                 masked=False)

        # perform box rejection on sigma-clipped data (i.e., for any
        # newly-masked pixels)
        idx = self._get_box_indices()
        self._box_idx = self._box_idx[idx]
        if self._box_idx.size != self._box_data.shape[0]:
            self._box_data = self._box_data[idx, :]

        import bottleneck
        self._bkg1d = bottleneck.nanmedian(self._box_data, axis=1)
        self._bkgrms1d = bottleneck.nanstd(self._box_data, axis=1)
        # TODO
        # self._bkg1d = self.bkg_estimator(self._box_data, axis=1)
        # self._bkgrms1d = self.bkgrms_estimator(self._box_data, axis=1)

    def _make_2d_array(self, data):
        """
        Convert a 1D array of values to a 2D array given the 1D indices
        in ``self._box_idx``.

        Parameters
        ----------
        data : 1D `~numpy.ndarray`
            A 1D array of values.

        Returns
        -------
        result : 2D `~numpy.ndarray`
            A 2D array. Pixels not defined in ``mesh_idx`` are assigned
            a value of np.nan.
        """
        data2d = np.full(self.nboxes, np.nan)
        data2d[self._mesh_idx] = data
        return data2d

    def _interpolate_meshes(self, data, n_neighbors=10, eps=0.0, power=1.0,
                            reg=0.0):
        """
        Use IDW interpolation to fill in any masked pixels in the
        low-resolution 2D mesh background and background RMS images.

        This is required to use a regular-grid interpolator to expand
        the low-resolution image to the full size image.

        Parameters
        ----------
        data : 1D `~numpy.ndarray`
            A 1D array of mesh values.

        n_neighbors : int, optional
            The maximum number of nearest neighbors to use during the
            interpolation.

        eps : float, optional
            Set to use approximate nearest neighbors; the kth neighbor
            is guaranteed to be no further than (1 + ``eps``) times the
            distance to the real *k*-th nearest neighbor. See
            `scipy.spatial.cKDTree.query` for further information.

        power : float, optional
            The power of the inverse distance used for the interpolation
            weights.  See the Notes section for more details.

        reg : float, optional
            The regularization parameter. It may be used to control the
            smoothness of the interpolator. See the Notes section for
            more details.

        Returns
        -------
        result : 2D `~numpy.ndarray`
            A 2D array of the mesh values where masked pixels have been
            filled by IDW interpolation.
        """
        yx = np.column_stack(self._mesh_idx)
        interp_func = ShepardIDWInterpolator(yx, data)

        coords = np.array(list(product(range(self.nboxes[0]),
                                       range(self.nboxes[1]))))
        img1d = interp_func(coords, n_neighbors=n_neighbors, power=power,
                            eps=eps, reg=reg)

        return img1d.reshape(self.nboxes)

    def _make_meshes(self):
        """
        Calculate the low-resolution background and background RMS
        "mesh" images.

        The ``background_mesh`` and ``background_rms_mesh`` images
        are equivalent to the low-resolution "MINIBACKGROUND" and
        "MINIBACK_RMS" background maps in SourceExtractor, respectively.
        """
        self._mesh_idx = np.unravel_index(self._box_idx, self.nboxes)

        # make the unfiltered 2D mesh arrays (these are not masked)
        if self._bkg1d.size == self.nboxes_tot:
            bkg = self._make_2d_array(self._bkg1d)
            bkgrms = self._make_2d_array(self._bkgrms1d)
        else:
            bkg = self._interpolate_meshes(self._bkg1d)
            bkgrms = self._interpolate_meshes(self._bkgrms1d)

        self.background_mesh = bkg
        self.background_rms_mesh = bkgrms

    def _selective_filter(self, data, indices):
        """
        Selectively filter only pixels above ``filter_threshold`` in the
        background mesh.

        The same pixels are filtered in both the background and
        background RMS meshes.

        Parameters
        ----------
        data : 2D `~numpy.ndarray`
            A 2D array of mesh values.

        indices : 2 tuple of int
            A tuple of the ``y`` and ``x`` indices of the pixels to
            filter.

        Returns
        -------
        filtered_data : 2D `~numpy.ndarray`
            The filtered 2D array of mesh values.
        """
        data_out = np.copy(data)
        for i, j in zip(*indices):
            yfs, xfs = self.filter_size
            hyfs, hxfs = yfs // 2, xfs // 2
            yidx0 = max(i - hyfs, 0)
            yidx1 = min(i - hyfs + yfs, data.shape[0])
            xidx0 = max(j - hxfs, 0)
            xidx1 = min(j - hxfs + xfs, data.shape[1])
            data_out[i, j] = np.median(data[yidx0:yidx1, xidx0:xidx1])

        return data_out

    def _filter_meshes(self):
        """
        Apply a 2D median filter to the low-resolution 2D meshes.

        Both the background and background RMS meshes are computed at
        the same time here because the filtering of both depends on the
        background mesh filter threshold.
        """
        if np.array_equal(self.filter_size, [1, 1]):
            return

        from scipy.ndimage import generic_filter

        if self.filter_threshold is None:
            # filter the entire arrays
            self.background_mesh = generic_filter(
                self.background_mesh, np.nanmedian, size=self.filter_size,
                mode='constant', cval=np.nan)
            self.background_rms_mesh = generic_filter(
                self.background_rms_mesh, np.nanmedian,
                size=self.filter_size, mode='constant', cval=np.nan)
        else:
            # selectively filter
            indices = np.nonzero(self.background_mesh > self.filter_threshold)
            self.background_mesh = self._selective_filter(
                self.background_mesh, indices)
            self.background_rms_mesh = self._selective_filter(
                self.background_rms_mesh, indices)

    @lazyproperty
    def _mesh_yxpos(self):
        box_cen = (self.box_size - 1) / 2.
        return (self._mesh_idx * self.box_size[:, None]) + box_cen[:, None]

    @lazyproperty
    def _mesh_xypos(self):
        return np.flipud(self._mesh_yxpos)

    @lazyproperty
    def mesh_nmasked(self):
        """
        A 2D array of the number of masked pixels in each mesh. NaN
        values indiciate where meshes were excluded.
        """
        return self._make_2d_array(
            np.count_nonzero(np.isnan(self._box_data), axis=1))

    @lazyproperty
    def background_mesh_ma(self):
        """
        The background 2D (masked) array mesh prior to any
        interpolation.  The array is masked only if meshes were
        excluded.
        """
        if len(self._bkg1d) == self.nboxes_tot:
            return self.background_mesh
        else:
            return self._make_2d_array(self._bkg1d)

    @lazyproperty
    def background_rms_mesh_ma(self):
        """
        The background RMS 2D (masked) array mesh prior to any
        interpolation.  The array is masked only if meshes were
        excluded.
        """
        if len(self._bkgrms1d) == self.nboxes_tot:
            return self.background_rms_mesh
        else:
            return self._make_2d_array(self._bkgrms1d)

    @lazyproperty
    def background_median(self):
        """
        The median value of the 2D low-resolution background map.

        This is equivalent to the value SourceExtractor prints to stdout
        (i.e., "(M+D) Background: <value>").
        """
        _median = np.median(self.background_mesh)
        if self.unit is not None:
            _median <<= self.unit
        return _median

    @lazyproperty
    def background_rms_median(self):
        """
        The median value of the low-resolution background RMS map.

        This is equivalent to the value SourceExtractor prints to stdout
        (i.e., "(M+D) RMS: <value>").
        """
        _rms_median = np.median(self.background_rms_mesh)
        if self.unit is not None:
            _rms_median <<= self.unit
        return _rms_median

    @lazyproperty
    def background(self):
        """A 2D `~numpy.ndarray` containing the background image."""
        bkg = self.interpolator(self.background_mesh, self)
        if self.coverage_mask is not None:
            bkg[self.coverage_mask] = self.fill_value
        if self.unit is not None:
            bkg <<= self.unit
        return bkg

    @lazyproperty
    def background_rms(self):
        """A 2D `~numpy.ndarray` containing the background RMS image."""
        bkg_rms = self.interpolator(self.background_rms_mesh, self)
        if self.coverage_mask is not None:
            bkg_rms[self.coverage_mask] = self.fill_value
        if self.unit is not None:
            bkg_rms <<= self.unit
        return bkg_rms

    def plot_meshes(self, axes=None, marker='+', color='blue', outlines=False,
                    **kwargs):
        """
        Plot the low-resolution mesh boxes on a matplotlib Axes
        instance.

        Parameters
        ----------
        axes : `matplotlib.axes.Axes` or `None`, optional
            The matplotlib axes on which to plot.  If `None`, then the
            current `~matplotlib.axes.Axes` instance is used.

        marker : str, optional
            The marker to use to mark the center of the boxes.  Default
            is '+'.

        color : str, optional
            The color for the markers and the box outlines.  Default is
            'blue'.

        outlines : bool, optional
            Whether or not to plot the box outlines in addition to the
            box centers.

        **kwargs : `dict`
            Any keyword arguments accepted by
            `matplotlib.patches.Patch`.  Used only if ``outlines`` is
            True.
        """
        import matplotlib.pyplot as plt

        kwargs['color'] = color
        if axes is None:
            axes = plt.gca()
        axes.scatter(*self._mesh_xypos, marker=marker, color=color)
        if outlines:
            from ..aperture import RectangularAperture
            xypos = np.column_stack(self._mesh_xypos)
            apers = RectangularAperture(xypos, self.box_size[1],
                                        self.box_size[0], 0.)
            apers.plot(axes=axes, **kwargs)
