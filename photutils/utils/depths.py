# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module provides tools for calculating limiting fluxes.
"""

import warnings

import numpy as np
from astropy.stats import SigmaClip
from astropy.utils.exceptions import AstropyUserWarning
from scipy.spatial import KDTree

from ._optional_deps import HAS_TQDM  # pylint: disable=E0611  # noqa: F401
from .footprints import circular_footprint

__all__ = ['ImageDepth']


class ImageDepth:
    r"""
    Class to calculate the limiting flux and magnitude of an image.

    Parameters
    ----------
    aper_radius : float
        The radius (in pixels) of the circular apertures used to compute
        the image depth.

    nsigma : float, optional
        The number of standard deviations at which to compute the image
        depths.

    mask_pad : float, optional
        An additional padding (in pixels) to apply when dilating the
        input mask.

    napers : int, optional
        The number of circular apertures used to compute the image
        depth.

    niters : int, optional
        The number of iterations, each with randomly-generated
        apertures, for which the image depth will be calculated.

    overlap : bool, optional
        Whether to allow the aperture to overlap.

    overlap_maxiters : int, optional
        The maximum number of iterations that will be used when
        attempting to find additional non-overlapping apertures. This
        keyword has no effect unless ``overlap=False``. While increasing
        this number may generate more non-overlapping apertures in
        crowded cases, it will also run slower.

    seed : int, optional
        A seed to initialize the `numpy.random.BitGenerator`. If `None`,
        then fresh, unpredictable entropy will be pulled from the OS.
        Separate function calls with the same ``seed`` will generate the
        same results.

    zeropoint : float, optional
        The zeropoint used to calculate the magnitude limit from the
        flux limit:

        .. math:: m_{\mathrm{lim}} = -2.5 \log_{10} f_{\mathrm{lim}}
                  + \mathrm{zeropoint}

    sigma_clip : `astropy.stats.SigmaClip` instance, optional
        A `~astropy.stats.SigmaClip` object that defines the sigma
        clipping parameters to use when computing the limiting flux. If
        `None` then no sigma clipping will be performed.

    progress_bar : bool, optional
        Whether to display a progress bar. The progress bar requires
        that the `tqdm <https://tqdm.github.io/>`_ optional dependency
        be installed. Note that the progress bar does not currently work
        in the Jupyter console due to limitations in ``tqdm``.

    Notes
    -----
    The image depth is calculated by placing random circular apertures
    with the specified radius on blank regions of the image. The number
    of apertures is specified by the ``napers`` keyword. The blank
    regions are calculated from an input mask, which should mask both
    sources in the image and areas without image coverage. The input
    mask will be dilated with a circular footprint with a radius equal
    to the input ``aper_radius`` plus ``mask_pad``. The image border
    is also masked with the same radius.

    The flux limit is calculated as the standard deviation of the
    aperture fluxes times the input ``nsigma`` significance level. The
    aperture flux values can be sigma clipped prior to computing the
    standard deviation using the ``sigma_clip`` keyword.

    The flux limit is calculated ``niters`` times, each with a
    randomly-generated set of circular apertures. The returned flux
    limit is the average of these flux limits.

    The magnitude limit is calculated from flux limit using the input
    ``zeropoint`` keyword as:

    .. math:: m_{\mathrm{lim}} = -2.5 \log_{10} f_{\mathrm{lim}}
              + \mathrm{zeropoint}
    """

    def __init__(self, aper_radius, *, nsigma=5., mask_pad=5, napers=1000,
                 niters=10, overlap=True, overlap_maxiters=100, seed=None,
                 zeropoint=0., sigma_clip=SigmaClip(sigma=3.0, maxiters=10),
                 progress_bar=True):

        if aper_radius <= 0:
            raise ValueError('aper_radius must be > 0')
        if mask_pad <= 0:
            raise ValueError('mask_pad must be > 0')

        self.aper_radius = aper_radius
        self.nsigma = nsigma
        self.mask_pad = mask_pad
        self.napers = napers
        self.niters = niters
        self.overlap = overlap
        self.overlap_maxiters = overlap_maxiters
        self.seed = seed
        self.zeropoint = zeropoint
        self.sigma_clip = sigma_clip
        self.progress_bar = progress_bar

        self.rng = np.random.default_rng(self.seed)
        self.dilate_radius = int(np.ceil(self.aper_radius + self.mask_pad))
        self.dilate_footprint = circular_footprint(radius=self.dilate_radius)

        self.apertures = []
        self.napers_used = np.array([])
        self.fluxes = []
        self.flux_limits = np.array([])
        self.mag_limits = np.array([])

    def __call__(self, data, mask):
        """
        Calculate the limiting flux and magnitude of an image.

        Parameters
        ----------
        data : 2D `~numpy.ndarray`
            The 2D array, which should be in flux units (not surface
            brightness units).

        mask : 2D bool `~numpy.ndarray`
            A 2D mask array with the same shape as ``data`` where
            a `True` value indicates the corresponding element of
            ``data`` is masked. The input array should mask both sources
            (e.g., from a segmentation image) and regions without image
            coverage. If `None`, then the entire image will be used.

        Returns
        -------
        flux_limit, mag_limit : float
            The flux and magnitude limits. The flux limit is returned in
            the same units as the input ``data``. The magnitude limit is
            calculated from the flux limit and the input ``zeropoint``.
        """
        # prevent circular import
        from ..aperture import CircularAperture

        iter_range = range(self.niters)
        if self.progress_bar and HAS_TQDM:
            from tqdm.auto import tqdm  # pragma: no cover

            iter_range = tqdm(iter_range, desc='Image Depths')

        if mask is None or not np.any(mask):
            all_xycoords = self._make_all_coords_no_mask(data.shape)
        else:
            all_xycoords = self._make_all_coords(mask)

        napers = self.napers
        if not self.overlap:
            napers2 = 1.5 * self.napers
            napers = int(min(napers2, 0.1 * len(all_xycoords)))

        fluxes = []
        flux_limits = []
        apertures = []
        for _ in iter_range:
            if self.overlap:
                xycoords = self._make_coords(all_xycoords, napers)
            else:
                xycoords = self._make_nonoverlap_coords(all_xycoords, napers)

            apers = CircularAperture(xycoords, r=self.aper_radius)
            apertures.append(apers)
            fluxes, _ = apers.do_photometry(data)
            if self.sigma_clip is not None:
                fluxes = self.sigma_clip(fluxes, masked=False)  # ndarray
            self.fluxes.append(fluxes)
            flux_limits.append(self.nsigma * np.std(fluxes))

        self.apertures = apertures
        napers_used = np.array([len(apers) for apers in apertures])
        self.napers_used = napers_used
        if np.any(napers_used < self.napers):
            warnings.warn(f'Unable to generate {self.napers} non-overlapping '
                          'apertures in unmasked regions. The number of '
                          f'apertures used was less than {self.napers} (see '
                          'the "napers_used" ImageDepth object attribute). '
                          'To fix this, decrease the number of apertures '
                          'and/or aperture size, or increase '
                          '`overlap_maxiters`. Alternatively, you may set '
                          'overlap=True', AstropyUserWarning)

        self.flux_limits = np.array(flux_limits)
        self.mag_limits = -2.5 * np.log10(self.flux_limits) + self.zeropoint

        flux_limit = np.mean(self.flux_limits)
        mag_limit = -2.5 * np.log10(flux_limit) + self.zeropoint

        return flux_limit, mag_limit

    @staticmethod
    def _find_slice_axis(data, axis):
        """
        Calculate a slice for the minimal bounding box along an axis for
        the `True` values of a 2D boolean array.

        Parameters
        ----------
        data : 2D bool `~numpy.ndarray`
            The boolean array.
        axis : int
            The axis to use (0 or 1).

        Returns
        -------
        slice : slice object
            A slice object for the input axis. If the data values along
            the input axis are all `False`, then the slice object will
            include the entire axis range.
        """
        if data.ndim != 2:
            raise ValueError('data must be 2D')
        if axis not in (0, 1):
            raise ValueError('axis must be 0 or 1')

        xx = np.any(data, axis=axis)
        if np.all(~xx):
            idx = 0 if axis else 1
            slc = slice(0, data.shape[idx])
        else:
            x0, x1 = np.where(xx)[0][[0, -1]]
            slc = slice(x0, x1 + 1)

        return slc

    def _find_slices(self, data):
        """
        Calculate a tuple slice for the minimal bounding box for
        the `True` values of a 2D boolean array.

        Parameters
        ----------
        data : 2D bool `~numpy.ndarray`
            The boolean array.

        Returns
        -------
        slices : tuple of slices
            A tuple of slice objects for each axis of the array. If the
            data is all `False`, then the slice tuple will include the
            entire image range.
        """
        xslice = self._find_slice_axis(data, 0)
        yslice = self._find_slice_axis(data, 1)
        return yslice, xslice

    def _mask_border(self, mask):
        """
        Mask pixels around the image border.

        Parameters
        ----------
        mask : 2D bool `~numpy.ndarray`
            Boolean mask array.

        Returns
        -------
        mask : 2D bool `~numpy.ndarray`
            Boolean mask array.
        """
        mask[:self.dilate_radius, :] = True
        mask[-self.dilate_radius:, :] = True
        mask[:, :self.dilate_radius] = True
        mask[:, -self.dilate_radius:] = True

        return mask

    def _dilate_mask(self, mask):
        """
        Dilate the input mask to ensure that apertures do not overlap
        the mask.

        The mask is dilated with a circular footprint with a radius
        equal to the input ``aper_radius`` plus ``mask_pad``.

        Border pixels are also masked with the same radius.

        Parameters
        ----------
        mask : 2D bool `~numpy.ndarray`
            Boolean mask array.

        Returns
        -------
        mask : 2D bool `~numpy.ndarray`
            Dilated boolean mask array.
        """
        from scipy.ndimage import binary_dilation

        if np.any(mask):
            mask = binary_dilation(mask, structure=self.dilate_footprint)

        mask = self._mask_border(mask)

        return mask

    def _make_all_coords_no_mask(self, shape):
        """
        Return an array of all possible (x, y) coordinates.

        Border pixels will be excluded.

        Parameters
        ----------
        shape : 2 tuple of int
            The array shape.

        Returns
        -------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        """
        ny, nx = shape

        # remove the image borders
        border = self.dilate_radius
        border2 = 2 * border
        ny -= border2
        nx -= border2

        yi, xi = np.mgrid[0:ny, 0:nx]
        xi = xi.ravel()
        yi = yi.ravel()

        # shift back to coordinates to the original image
        xi += border
        yi += border

        return np.column_stack((xi, yi))

    def _make_all_coords(self, mask):
        """
        Return an array of all possible unmasked (x, y) coordinates.

        Border pixels will be excluded.

        Parameters
        ----------
        mask : 2D bool `~numpy.ndarray`
            The boolean source mask array.

        Returns
        -------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        """
        mask_inv = ~self._dilate_mask(mask)
        mask_slc = self._find_slices(mask_inv)
        yi, xi = np.nonzero(mask_inv[mask_slc])

        # shift back to coordinates to the original (unsliced) image
        xi += mask_slc[1].start
        yi += mask_slc[0].start

        return np.column_stack((xi, yi))

    def _make_coords(self, xycoords, napers):
        """
        Randomly choose ``napers`` (without replacement) coordinates
        from the input ``xycoords``.

        This function also adds < +/-0.5 pixel random shifts so that the
        coordinates are not all integers.

        Parameters
        ----------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        napers : int
            The number of aperture to make.

        Returns
        --------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        """
        if napers > xycoords.shape[0]:
            raise ValueError('Too many apertures for given unmasked area')

        idx = self.rng.choice(xycoords.shape[0], napers, replace=False)
        xycoords = xycoords[idx, :].astype(float)

        shift = self.rng.uniform(-0.5, 0.5, size=xycoords.shape)
        xycoords += shift

        return xycoords

    def _make_nonoverlap_coords(self, init_xycoords, napers):
        """
        Randomly choose ``napers`` (without replacement) coordinates
        from the input ``xycoords`` that do not overlap.

        Parameters
        ----------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        napers : int
            The number of aperture to make.

        Returns
        --------
        xycoords : 2xN `~numpy.ndarray`
            The (x, y) coordinates.
        """
        minsep = self.aper_radius * 2.0
        xycoords = np.zeros((0, 2))  # placeholder for while loop

        # attempt to generate all the coordinates at once; this will
        # work only for non-crowded blank areas
        niter = 1
        while xycoords.shape[0] < self.napers:
            if niter > 10:
                break

            new_xycoords = self._make_coords(init_xycoords, napers)
            if niter == 1:
                xycoords = new_xycoords
            else:
                xycoords = np.vstack((xycoords, new_xycoords))

            dist, _ = KDTree(xycoords).query(xycoords, k=[2])
            mask = (dist >= minsep).squeeze()
            xycoords = xycoords[mask]
            niter += 1

        # add new coordinates one by one (slower)
        napers = len(init_xycoords)
        if xycoords.shape[0] < self.napers:
            new_xycoords = self._make_coords(init_xycoords, napers)
            if xycoords.shape[0] == 0:
                xycoords = new_xycoords[0]
                new_xycoords = new_xycoords[1:]

            count = 1
            while (xycoords.shape[0] < self.napers
                   and count < self.overlap_maxiters):
                idx = self.rng.choice(new_xycoords.shape[0], 1, replace=False)
                new_xy = new_xycoords[idx, :]
                dist, _ = KDTree(new_xy).query(xycoords, 1)
                if np.min(dist) > minsep:
                    xycoords = np.vstack((xycoords, new_xy))
                    count = 0
                else:
                    count += 1

        xycoords = xycoords[0:self.napers]

        return xycoords
