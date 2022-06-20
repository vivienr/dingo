from functools import partial
from multiprocessing import Pool
from math import isclose

import numpy as np
from typing import Dict, List, Tuple, Union
from numbers import Number
import warnings
import lal
import lalsimulation as LS
import pandas as pd
from bilby.gw.conversion import (
    convert_to_lal_binary_black_hole_parameters,
    bilby_to_lalsimulation_spins,
)

import dingo.gw.waveform_generator.wfg_utils as wfg_utils
import dingo.gw.waveform_generator.frame_utils as frame_utils
from dingo.gw.domains import Domain, FrequencyDomain, TimeDomain


class WaveformGenerator:
    """Generate polarizations in the specified domain for a
    single GW coalescence given a set of waveform parameters.
    """

    def __init__(
        self,
        approximant: str,
        domain: Domain,
        f_ref: float,
        f_start: float = None,
        mode_list: List[Tuple] = None,
        transform=None,
        spin_conversion_phase=None,
    ):
        """
        Parameters
        ----------
        approximant : str
            Waveform "approximant" string understood by lalsimulation
            This is defines which waveform model is used.
        domain : Domain
            Domain object that specifies on which physical domain the
            waveform polarizations will be generated, e.g. Fourier
            domain, time domain.
        f_ref : float
            Reference frequency for the waveforms
        f_start : float
            Starting frequency for waveform generation. This is optional, and if not
            included, the starting frequency will be set to f_min. This exists so that
            EOB waveforms can be generated starting from a lower frequency than f_min.
        mode_list : List[Tuple]
            A list of waveform (ell, m) modes to include when generating
            the polarizations.
        spin_conversion_phase : float = None
            Value for phiRef when computing cartesian spins from bilby spins via
            bilby_to_lalsimulation_spins. The common convention is to use the value of
            the phase parameter here, which is also used in the spherical harmonics
            when combining the different modes. If spin_conversion_phase = None,
            this default behavior is adapted.
            For dingo, this convention for the phase parameter makes it impossible to
            treat the phase as an extrinsic parameter, since we can only account for
            the change of phase in the spherical harmonics when changing the phase (in
            order to also change the cartesian spins -- specifically, to rotate the spins
            by phase in the sx-sy plane -- one would need to recompute the modes,
            which is expensive).
            By setting spin_conversion_phase != None, we impose the convention to always
            use phase = spin_conversion_phase when computing the cartesian spins.
        """
        if not isinstance(approximant, str):
            raise ValueError("approximant should be a string, but got", approximant)
        else:
            self.approximant_str = approximant
            self.approximant = LS.GetApproximantFromString(approximant)

        if not issubclass(type(domain), Domain):
            raise ValueError(
                "domain should be an instance of a subclass of Domain, but got",
                type(domain),
            )
        else:
            self.domain = domain

        self.f_ref = f_ref
        self.f_start = f_start

        self.lal_params = None
        if mode_list is not None:
            self.lal_params = self.setup_mode_array(mode_list)

        self.transform = transform
        self._spin_conversion_phase = None
        self.spin_conversion_phase = spin_conversion_phase

    @property
    def spin_conversion_phase(self):
        return self._spin_conversion_phase

    @spin_conversion_phase.setter
    def spin_conversion_phase(self, value):
        if value is None:
            print(
                "Setting spin_conversion_phase = None. Using phase parameter for "
                "conversion to cartesian spins."
            )
        else:
            print(
                f"Setting spin_conversion_phase = {value}. Using this value for the "
                f"phase parameter for conversion to cartesian spins."
            )
        self._spin_conversion_phase = value

    def generate_hplus_hcross(
        self, parameters: Dict[str, float], catch_waveform_errors=True
    ) -> Dict[str, np.ndarray]:
        """Generate GW polarizations (h_plus, h_cross).

        If the generation of the lalsimulation waveform fails with an
        "Input domain error", we return NaN polarizations.

        Use the domain, approximant, and mode_list specified in the constructor
        along with the waveform parameters to generate the waveform polarizations.


        Parameters
        ----------
        parameters: Dict[str, float]
            A dictionary of parameter names and scalar values.
            The parameter dictionary must include the following keys.
            For masses, spins, and distance there are multiple options.

            Mass: (mass_1, mass_2) or a pair of quantities from
                ((chirp_mass, total_mass), (mass_ratio, symmetric_mass_ratio))
            Spin:
                (a_1, a_2, tilt_1, tilt_2, phi_12, phi_jl) if precessing binary or
                (chi_1, chi_2) if the binary has aligned spins
            Reference frequency: f_ref at which spin vectors are defined
            Extrinsic:
                Distance: one of (luminosity_distance, redshift, comoving_distance)
                Inclination: theta_jn
                Reference phase: phase
                Geocentric time: geocent_time (GPS time)
            The following parameters are not required:
                Sky location: ra, dec,
                Polarization angle: psi
            Units:
                Masses should be given in units of solar masses.
                Distance should be given in megaparsecs (Mpc).
                Frequencies should be given in Hz and time in seconds.
                Spins should be dimensionless.
                Angles should be in radians.

        catch_waveform_errors: bool
            Whether to catch lalsimulation errors

        Returns
        -------
        wf_dict:
            A dictionary of generated waveform polarizations
        """
        if not isinstance(parameters, dict):
            raise ValueError("parameters should be a dictionary, but got", parameters)
        elif not isinstance(list(parameters.values())[0], float):
            raise ValueError("parameters dictionary must contain floats", parameters)

        # Include reference frequency with the parameters. Copy the dict first for safety.
        parameters = parameters.copy()
        parameters["f_ref"] = self.f_ref

        # Convert to lalsimulation parameters according to the specified domain
        parameters_lal = self._convert_parameters_to_lal_frame(
            parameters, self.lal_params
        )

        # Generate GW polarizations
        if isinstance(self.domain, FrequencyDomain):
            wf_generator = self.generate_FD_waveform
        elif isinstance(self.domain, TimeDomain):
            wf_generator = self.generate_TD_waveform
        else:
            raise ValueError(f"Unsupported domain type {type(self.domain)}.")

        try:
            wf_dict = wf_generator(parameters_lal)
        except Exception as e:
            if not catch_waveform_errors:
                raise
            else:
                EDOM = e.args[0] == "Internal function call failed: Input domain error"
                if EDOM:
                    warnings.warn(
                        f"Evaluating the waveform failed with error: {e}\n"
                        f"The parameters were {parameters_lal}\n"
                    )
                    pol_nan = np.ones(len(self.domain)) * np.nan
                    wf_dict = {"h_plus": pol_nan, "h_cross": pol_nan}
                else:
                    raise

        if self.transform is not None:
            return self.transform(wf_dict)
        else:
            return wf_dict

    def _convert_to_scalar(self, x: Union[np.ndarray, float]) -> Number:
        """
        Convert a single element array to a number.

        Parameters
        ----------
        x:
            Array or number

        Returns
        -------
        A number
        """
        if isinstance(x, np.ndarray):
            if x.shape == () or x.shape == (1,):
                return x.item()
            else:
                raise ValueError(
                    f"Expected an array of length one, but shape = {x.shape}"
                )
        else:
            return x

    def _convert_parameters_to_lal_frame(
        self,
        parameter_dict: Dict,
        lal_params=None,
        lal_target_function=None,
    ) -> Tuple:
        """Convert to lal source frame parameters

        Parameters
        ----------
        parameter_dict : Dict
            A dictionary of parameter names and 1-dimensional prior distribution
            objects. If None, we use a default binary black hole prior.
        lal_params : (None, or Swig Object of type 'tagLALDict *')
            Extra parameters which can be passed to lalsimulation calls.
        lal_target_function: str = None
            Name of the lalsimulation function for which to prepare the parameters.
            If None, use SimInspiralFD if self.domain is FD, and SimInspiralTD if
            self.domain is TD.
            Choices:
                - SimInspiralFD (Also works for SimInspiralChooseFDWaveform)
                - SimInspiralTD (Also works for SimInspiralChooseTDWaveform)
                - SimInspiralChooseFDModes
                - SimInspiralChooseTDModes
        Returns
        -------
        lal_parameter_tuple:
            A tuple of parameters for the lalsimulation waveform generator
        """
        # check that the lal_target_function is valid
        if lal_target_function is None:
            if isinstance(self.domain, FrequencyDomain):
                lal_target_function = "SimInspiralFD"
            elif isinstance(self.domain, TimeDomain):
                lal_target_function = "SimInspiralTD"
            else:
                raise ValueError(f"Unsupported domain type {type(self.domain)}.")
        if lal_target_function not in [
            "SimInspiralFD",
            "SimInspiralTD",
            "SimInspiralChooseTDModes",
            "SimInspiralChooseFDModes",
            "SimIMRPhenomXPCalculateModelParametersFromSourceFrame",
        ]:
            raise ValueError(
                f"Unsupported lalsimulation waveform function {lal_target_function}."
            )

        # Transform mass, spin, and distance parameters
        p, _ = convert_to_lal_binary_black_hole_parameters(parameter_dict)

        # Convert to SI units
        p["mass_1"] *= lal.MSUN_SI
        p["mass_2"] *= lal.MSUN_SI
        p["luminosity_distance"] *= 1e6 * lal.PC_SI

        # Transform to lal source frame: iota and Cartesian spin components
        param_keys_in = (
            "theta_jn",
            "phi_jl",
            "tilt_1",
            "tilt_2",
            "phi_12",
            "a_1",
            "a_2",
            "mass_1",
            "mass_2",
            "f_ref",
            "phase",
        )
        param_values_in = [p[k] for k in param_keys_in]
        # if spin_conversion_phase is set, use this as fixed phiRef when computing the
        # cartesian spins instead of using the phase parameter
        if self.spin_conversion_phase is not None:
            param_values_in[-1] = self.spin_conversion_phase
        iota_and_cart_spins = bilby_to_lalsimulation_spins(*param_values_in)
        iota, s1x, s1y, s1z, s2x, s2y, s2z = [
            float(self._convert_to_scalar(x)) for x in iota_and_cart_spins
        ]

        # Construct argument list for FD and TD lal waveform generator wrappers
        spins_cartesian = s1x, s1y, s1z, s2x, s2y, s2z
        masses = (p["mass_1"], p["mass_2"])
        r = p["luminosity_distance"]
        phase = p["phase"]
        ecc_params = (0.0, 0.0, 0.0)  # longAscNodes, eccentricity, meanPerAno

        # Get domain parameters
        f_ref = p["f_ref"]
        if isinstance(self.domain, FrequencyDomain):
            delta_f = self.domain.delta_f
            f_max = self.domain.f_max
            if self.f_start is not None:
                f_min = self.f_start
            else:
                f_min = self.domain.f_min
            # parameters needed for TD waveforms
            delta_t = 0.5 / self.domain.f_max
        elif isinstance(self.domain, TimeDomain):
            raise NotImplementedError("Time domain not supported yet.")
            # FIXME: compute f_min from duration or specify it if SimInspiralTD
            #  is used for a native FD waveform
            f_min = 20.0
            delta_t = self.domain.delta_t
            # parameters needed for FD waveforms
            f_max = 1.0 / self.domain.delta_t
            delta_f = 1.0 / self.domain.duration
        else:
            raise ValueError(f"Unsupported domain type {type(self.domain)}.")

        if lal_target_function == "SimInspiralFD":
            # LS.SimInspiralFD takes parameters:
            #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
            #   distance, inclination, phiRef,
            #   longAscNodes, eccentricity, meanPerAno,
            #   deltaF, f_min, f_max, f_ref,
            #   lal_params, approximant
            domain_pars = (delta_f, f_min, f_max, f_ref)
            domain_pars = tuple(float(p) for p in domain_pars)
            lal_parameter_tuple = (
                masses
                + spins_cartesian
                + (r, iota, phase)
                + ecc_params
                + domain_pars
                + (lal_params, self.approximant)
            )

        elif lal_target_function == "SimInspiralTD":
            # LS.SimInspiralTD takes parameters:
            #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
            #   distance, inclination, phiRef,
            #   longAscNodes, eccentricity, meanPerAno,
            #   delta_t, f_min, f_ref
            #   lal_params, approximant
            domain_pars = (delta_t, f_min, f_ref)
            domain_pars = tuple(float(p) for p in domain_pars)
            lal_parameter_tuple = (
                masses
                + spins_cartesian
                + (r, iota, phase)
                + ecc_params
                + domain_pars
                + (lal_params, self.approximant)
            )
        elif lal_target_function == "SimInspiralChooseFDModes":
            domain_pars = (delta_f, f_min, f_max, f_ref)
            domain_pars = tuple(float(p) for p in domain_pars)
            lal_parameter_tuple = (
                masses
                + spins_cartesian
                + domain_pars
                + (phase, r, iota)
                + (lal_params, self.approximant)
            )

        elif (
            lal_target_function
            == "SimIMRPhenomXPCalculateModelParametersFromSourceFrame"
        ):
            lal_parameter_tuple = (
                masses + (f_ref,) + (phase, iota) + spins_cartesian + (lal_params,)
            )

        elif lal_target_function == "SimInspiralChooseTDModes":
            # LS.SimInspiralChooseTDModes takes parameters:
            #   phiRef=0 (for lal legacy reasons), delta_t,
            #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
            #   f_min, f_ref
            #   distance,
            #   lal_params, l_max, approximant
            domain_pars = (delta_t, f_min, f_ref)
            domain_pars = tuple(float(p) for p in domain_pars)
            if "l_max" not in parameter_dict:
                l_max = 5  # hard code l_max for now
            lal_parameter_tuple = (
                (
                    0.0,
                    domain_pars[0],
                )  # domain_pars[0] = delta_t
                + masses
                + spins_cartesian
                + domain_pars[1:]  # domain_pars[1:] = f_min, f_ref
                + (r,)
                + (lal_params, l_max, self.approximant)
            )
            # also pass iota, since this is needed for recombination of the modes
            lal_parameter_tuple = (lal_parameter_tuple, iota)

        return lal_parameter_tuple

    def setup_mode_array(self, mode_list: List[Tuple]) -> lal.Dict:
        """Define a mode array to select waveform modes
        to include in the polarizations from a list of modes.

        Parameters
        ----------
        mode_list : a list of (ell, m) modes

        Returns
        -------
        lal_params:
            A lal parameter dictionary
        """
        lal_params = lal.CreateDict()
        ma = LS.SimInspiralCreateModeArray()
        for (ell, m) in mode_list:
            LS.SimInspiralModeArrayActivateMode(ma, ell, m)
            # LS.SimInspiralModeArrayActivateMode(ma, ell, -m)
        LS.SimInspiralWaveformParamsInsertModeArray(lal_params, ma)
        return lal_params

    def generate_FD_waveform(self, parameters_lal: Tuple) -> Dict[str, np.ndarray]:
        """
        Generate Fourier domain GW polarizations (h_plus, h_cross).

        Parameters
        ----------
        parameters_lal:
            A tuple of parameters for the lalsimulation waveform generator

        Returns
        -------
        pol_dict:
            A dictionary of generated waveform polarizations
        """
        # Note: SEOBNRv4PHM does not support the specification of spins at a
        # reference frequency different from the starting frequency. In addition,
        # waveform generation will fail if the orbital distance is smaller than ~ 10M.
        # To avoid this, we can start at a sufficiently low and consistent starting frequency
        # for the entire dataset. If the number of generation failures is a very small
        # fraction over the prior distribution then the dataset should be good to use.
        #
        # Note: XLALSimInspiralFD() internally calls XLALSimInspiralTD() to generate
        # a conditioned time-domain waveform. In the past, this function lowered
        # the starting frequency, but this is thankfully no longer the case
        # for models such as SEOBNRv4PHM where the reference frequency is equal
        # to the starting frequency. So, the TD waveform will be generated by
        # calling XLALSimInspiralChooseTDWaveform().
        # See https://git.ligo.org/waveforms/reviews/lalsuite/-/commit/195f9127682de19f5fce19cc5828116dd2d23461
        #
        # LS.SimInspiralFD takes parameters:
        #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
        #   distance, inclination, phiRef,
        #   longAscNodes, eccentricity, meanPerAno,
        #   deltaF, f_min, f_max, f_ref,
        #   lal_params, approximant

        # Sanity check types of arguments
        check_floats = all(map(lambda x: isinstance(x, float), parameters_lal[:18]))
        check_int = isinstance(parameters_lal[19], int)
        # parameters_lal[18]  # lal_params could be None or a LALDict
        if not (check_floats and check_int):
            raise ValueError(
                "SimInspiralFD received invalid argument(s)", parameters_lal
            )

        # Depending on whether the domain is uniform or non-uniform call the appropriate wf generator
        hp, hc = LS.SimInspiralFD(*parameters_lal)
        # The check below filters for unphysical waveforms:
        # For IMRPhenomXPHM, the LS.SimInspiralFD result is numerically instable
        # for rare parameter configurations (~1 in 1M), leading to bins with very
        # numbers if multibanding is used. As a preliminary fix, we slightly perturb
        # the parameters.
        if max(np.max(np.abs(hp.data.data)), np.max(np.abs(hc.data.data))) > 1e-17:
            print(f"Perturbing parameters {parameters_lal} due to instability.")
            hp, hc = LS.SimInspiralFD(
                parameters_lal[0] * 1.0000001, *parameters_lal[1:]
            )

        # Ensure that the waveform agrees with the frequency grid defined in the domain.
        if not isclose(self.domain.delta_f, hp.deltaF, rel_tol=1e-6):
            raise ValueError(
                f"Waveform delta_f is inconsistent with domain: {hp.deltaF} vs {self.domain.delta_f}!"
                f"To avoid this, ensure that f_max = {self.domain.f_max} is a power of two"
                "when you are using a native time-domain waveform model."
            )

        frequency_array = self.domain()
        h_plus = np.zeros_like(frequency_array, dtype=complex)
        h_cross = np.zeros_like(frequency_array, dtype=complex)
        # Ensure that length of wf agrees with length of domain. Enforce by truncating frequencies beyond f_max
        if len(hp.data.data) > len(frequency_array):
            warnings.warn(
                "LALsimulation waveform longer than domain's `frequency_array`"
                f"({len(hp.data.data)} vs {len(frequency_array)}). Truncating lalsim array."
            )
            h_plus = hp.data.data[: len(h_plus)]
            h_cross = hc.data.data[: len(h_cross)]
        else:
            h_plus[: len(hp.data.data)] = hp.data.data
            h_cross[: len(hc.data.data)] = hc.data.data

        # Undo the time shift done in SimInspiralFD to the waveform
        dt = 1 / hp.deltaF + (hp.epoch.gpsSeconds + hp.epoch.gpsNanoSeconds * 1e-9)
        time_shift = np.exp(-1j * 2 * np.pi * dt * frequency_array)
        h_plus *= time_shift
        h_cross *= time_shift
        pol_dict = {"h_plus": h_plus, "h_cross": h_cross}
        return pol_dict

    def generate_hplus_hcross_m(
        self, parameters: Dict[str, float]
    ) -> Dict[tuple, Dict[str, np.ndarray]]:
        """
        Generate GW polarizations (h_plus, h_cross), separated into contributions from
        the different modes. This method is identical to self.generate_hplus_hcross,
        except that it generates the individual contributions of the modes to the
        polarizations and sorts these according to their transformation behavior (see
        below), instead of returning the overall sum.

        This is useful in order to treat the phase as an extrinsic parameter. Instead of
        {"h_plus": hp, "h_cross": hc}, this method returns a dict in the form of
        {m: {"h_plus": hp_m, "h_cross": hc_m} for m in [-l_max,...,0,...,l_max]}. Each
        key m contains the contribution to the polarization that transforms according
        to exp(-1j * m * phase) under phase transformations (due to the spherical
        harmonics).

        Note:
            - pol_m[m] contains contributions of the m modes *and* and the -m modes.
              This is because the frequency domain (FD) modes have a positive frequency
              part which transforms as exp(-1j * m * phase), while the negative
              frequency part transforms as exp(+1j * m * phase). Typically, one of these
              dominates [e.g., the (2,2) mode is dominated by the negative frequency
              part and the (-2,2) mode is dominated by the positive frequency part]
              such that the sum of (l,|m|) and (l,-|m|) modes transforms approximately as
              exp(1j * |m| * phase), which is e.g. used for phase marginalization in
              bilby/lalinference. However, this is not exact. In this method we account
              for this effect, such that each contribution pol_m[m] transforms
              *exactly* as exp(-1j * m * phase).
            - Phase shifts contribute in two ways: Firstly via the spherical harmonics,
              which we account for with the exp(-1j * m * phase) transformation.
              Secondly, the phase determines how the PE spins transform to cartesian
              spins, by rotating (sx,sy) by phase. This is *not* accounted for in this
              function. Instead, the phase for computing the cartesian spins is fixed
              to self.spin_conversion_phase (if not None). This effectively changes the
              PE parameters {phi_jl, phi_12} to parameters {phi_jl_prime, phi_12_prime}.
              For parameter estimation, a postprocessing operation can be applied to
              account for this, {phi_jl_prime, phi_12_prime} -> {phi_jl, phi_12}.
              See also documentation of __init__ method for more information on
              self.spin_conversion_phase.

        Differences to self.generate_hplus_hcross:
        - We don't catch errors yet TODO
        - We don't apply transforms yet TODO

        Parameters
        ----------
        parameters: dict
            Dictionary of parameters for the waveform.
            For details see see self.generate_hplus_hcross.

        Returns
        -------
        pol_m: dict
            Dictionary with contributions to h_plus and h_cross, sorted by their
            transformation behaviour under phase shifts:
            {m: {"h_plus": hp_m, "h_cross": hc_m} for m in [-l_max,...,0,...,l_max]}
            Each contribution h_m transforms as exp(-1j * m * phase) under phase shifts
            (for fixed self.spin_conversion_phase, see above).
        """
        if not isinstance(parameters, dict):
            raise ValueError("parameters should be a dictionary, but got", parameters)
        elif not isinstance(list(parameters.values())[0], float):
            raise ValueError("parameters dictionary must contain floats", parameters)

        if isinstance(self.domain, FrequencyDomain):
            # Generate FD modes in for frequencies [-f_max, ..., 0, ..., f_max].
            if LS.SimInspiralImplementedFDApproximants(self.approximant):
                # Step 1: generate waveform modes in L0 frame in native domain of
                # approximant (here: FD)
                hlm_fd, iota = self.generate_FD_modes_LO(parameters)

                # Step 2: Transform modes to target domain.
                # Not required here, as approximant domain and target domain are both FD.

            else:
                assert LS.SimInspiralImplementedTDApproximants(self.approximant)
                # Step 1: generate waveform modes in L0 frame in native domain of
                # approximant (here: TD)
                hlm_td, iota = self.generate_TD_modes_LO(parameters)

                # Step 2: Transform modes to target domain.
                # This requires tapering of TD modes, and FFT to transform to FD.
                wfg_utils.taper_td_modes_in_place(hlm_td)
                hlm_fd = wfg_utils.td_modes_to_fd_modes(hlm_td, self.domain)

            # Step 3: Separate negative and positive frequency parts of the modes,
            # and add contributions according to their transformation behavior under
            # phase shifts.
            pol_m = wfg_utils.get_polarizations_from_fd_modes_m(
                hlm_fd, iota, parameters["phase"]
            )

        else:
            raise NotImplementedError(
                f"Target domain of type {type(self.domain)} not yet implemented."
            )

        return pol_m

    def generate_FD_modes_LO(self, parameters):
        """
        Generate FD modes in the L0 frame.

        Parameters
        ----------
        parameters: dict
            Dictionary of parameters for the waveform.
            For details see see self.generate_hplus_hcross.

        Returns
        -------
        hlm_fd: dict
            Dictionary with (l,m) as keys and the corresponding FD modes in lal format as
            values.
        iota: float
        """
        # TD approximants that are implemented in J frame. Currently tested for:
        #   101: IMRPhenomXPHM
        if self.approximant in [101]:
            parameters_lal_fd_modes = self._convert_parameters_to_lal_frame(
                {**parameters, "f_ref": self.f_ref},
                lal_target_function="SimInspiralChooseFDModes",
            )
            iota = parameters_lal_fd_modes[14]
            hlm_fd = LS.SimInspiralChooseFDModes(*parameters_lal_fd_modes)
            # unpack linked list, convert lal objects to arrays
            hlm_fd = wfg_utils.linked_list_modes_to_dict_modes(hlm_fd)
            hlm_fd = {k: v.data.data for k, v in hlm_fd.items()}
            # For the waveform models considered here (e.g., IMRPhenomXPHM), the modes
            # are returned in the J frame (where the observer is at inclination=theta_JN,
            # azimuth=0). In this frame, the dependence on the reference phase enters
            # via the modes themselves. We need to convert to the L0 frame so that the
            # dependence on phase enters via the spherical harmonics.
            hlm_fd = frame_utils.convert_J_to_L0_frame(
                hlm_fd,
                parameters,
                self,
                spin_conversion_phase=self.spin_conversion_phase,
            )
            return hlm_fd, iota
        else:
            raise NotImplementedError(
                f"Approximant {LS.GetApproximantFromString(self.approximant)} not "
                f"implemented. When adding this approximant to this method, make sure "
                f"the the output dict hlm_td contains the TD modes in the *L0 frame*. "
                f"In particular, adding an approximant that is implemented in the same "
                f"domain and frame as one of the approximants should just be a matter of "
                f"adding the approximant number (here: {self.approximant}) to the "
                f"corresponding if statement. However, when doing this please make sure "
                f"to test that this works as intended! Ideally, add some unit tests."
            )

    def generate_TD_modes_LO(self, parameters):
        """
        Generate TD modes in the L0 frame.

        Parameters
        ----------
        parameters: dict
            Dictionary of parameters for the waveform.
            For details see see self.generate_hplus_hcross.

        Returns
        -------
        hlm_td: dict
            Dictionary with (l,m) as keys and the corresponding TD modes in lal format as
            values.
        iota: float
        """
        # TD approximants that are implemented in L0 frame. Currently tested for:
        #   52: SEOBNRv4PHM
        if self.approximant in [52]:
            parameters_lal_td_modes, iota = self._convert_parameters_to_lal_frame(
                {**parameters, "f_ref": self.f_ref},
                lal_target_function="SimInspiralChooseTDModes",
            )
            hlm_td = LS.SimInspiralChooseTDModes(*parameters_lal_td_modes)
            return wfg_utils.linked_list_modes_to_dict_modes(hlm_td), iota
        else:
            raise NotImplementedError(
                f"Approximant {LS.GetApproximantFromString(self.approximant)} not "
                f"implemented. When adding this approximant to this method, make sure "
                f"the the output dict hlm_td contains the TD modes in the *L0 frame*. "
                f"In particular, adding an approximant that is implemented in the same "
                f"domain and frame as one of the approximants should just be a matter of "
                f"adding the approximant number (here: {self.approximant}) to the "
                f"corresponding if statement. However, when doing this please make sure "
                f"to test that this works as intended! Ideally, add some unit tests."
            )

    # def generate_hplus_hcross_mode_contributions(
    def generate_hplus_hcross_modes(
        self,
        parameters: Dict[str, float],
        catch_waveform_errors=True,
    ) -> Dict[tuple, Dict[str, np.ndarray]]:
        """
        Generate GW polarizations (h_plus, h_cross) for individual modes.
        This method is identical to self.generate_hplus_hcross, except that it
        generates the individual contributions of the modes polarizations, not just
        their sum. Instead of {"h_plus": hp, "h_cross": hc}, it returns the individual
        contributions in the format {(2,2): {"h_plus": hp_22, "h_cross": hc_22}, ...}.

        This is useful in order to treat the phase as an extrinsic parameters,
        since the individual modes simply need to be transformed with exp(i*m*phase).

        Note that there is one caveat. The phase phiRef is not just used for the Euler
        angle, but also as a reference for the cartesian spins. While the phase does
        not directly enter the computation of the individual modes, it determines how
        the xy-spin splits into x and y components in cartesian coordinates
        (sx**2 + sy**2 = const.).
        This is currently *not* accounted for.

        Differences to self.generate_hplus_hcross:
        - We don't catch errors yet TODO
        - We don't apply transforms yet TODO

        # Todo Check different models XPHM/EOB (NRsurr?)
        # Todo Better name

        Parameters
        ----------
        parameters: dict
            Dictionary of parameters for the waveform.
            For details see see self.generate_hplus_hcross.

        Returns
        -------
        pol_dict_modes: dict
            Dictionary containing (l, m) modes as keys and dictionaries containing the
            h_plus and h_cross polarizations of the modes as values. E.g.,
            {(2,2): {"h_plus": hp_22, "h_cross": hc_22}, ...}
        """
        if not isinstance(parameters, dict):
            raise ValueError("parameters should be a dictionary, but got", parameters)
        elif not isinstance(list(parameters.values())[0], float):
            raise ValueError("parameters dictionary must contain floats", parameters)

        if LS.SimInspiralImplementedTDApproximants(self.approximant):
            domain_type = "TD"
        elif LS.SimInspiralImplementedFDApproximants(self.approximant):
            domain_type = "FD"
        else:
            raise ValueError(f"Unknown approximant {self.approximant}")

        # Include reference frequency with the parameters. Copy the dict first for safety.
        parameters = parameters.copy()
        parameters["f_ref"] = self.f_ref

        # Generate modes. We need to differentiate between time domain and frequency
        # domain waveform models. Note that this refers to the domain of the model,
        # and not the final domain we want for the data.
        if domain_type == "TD":
            # Generate the contributions to the polarizations of the modes.
            pol_dict_modes = self.generate_TD_waveform_modes(parameters)
            # pol_dict_modes corresponds to the mode-separated output of LS.SimInspiralTD

            # # Cross check that modes indeed combine to the result of LS.SimInspiralTD.
            # # This includes tapering.
            # parameters_lal_td = self._convert_parameters_to_lal_frame(
            #     parameters,
            #     self.lal_params,
            #     lal_target_function="SimInspiralTD",
            # )
            # hptd, hctd = LS.SimInspiralTD(*parameters_lal_td)
            # hp_sum, hc_sum = np.zeros_like(hptd.data.data), np.zeros_like(hptd.data.data)
            # for mode, pol_dict in pol_dict_modes.items():
            #     hp_sum += pol_dict["h_plus"].data.data
            #     hc_sum += pol_dict["h_cross"].data.data
            # import matplotlib.pyplot as plt
            # plt.plot(hp_sum)
            # plt.plot(hptd.data.data, ',')
            # plt.plot(hptd.data.data - hp_sum)
            # plt.show()
            # plt.plot(hc_sum)
            # plt.plot(hctd.data.data, ',')
            # plt.plot(hctd.data.data - hc_sum)
            # plt.show()

            if isinstance(self.domain, FrequencyDomain):
                # we are now in line 3019 of LS.SimInspiralFD,
                # retval = XLALSimInspiralTD(...), where, pol_dict_modes corresponds to
                # retval. All that's left to do now is to prepare the polarizations for
                # the FFT, and then apply the FFT.
                hp_sample = list(pol_dict_modes.values())[0]["h_plus"]
                delta_f = self.domain.delta_f
                delta_t = 0.5 / self.domain.f_max
                f_nyquist = self.domain.f_max  # use f_max as f_nyquist
                # check that nyquist frequency is power of two of delta_f
                n = round(f_nyquist / delta_f)
                if (n & (n - 1)) != 0:
                    raise NotImplementedError(
                        "Nyquist frequency is not a power of two of delta_f"
                    )
                chirplen = int(2 * f_nyquist / delta_f)
                if chirplen < hp_sample.data.length:
                    # warning from lalsimulation
                    print(
                        f"Specified frequency interval of {delta_f} Hz is too large "
                        f"for a chirp of duration {hp_sample.data.length * delta_t} s "
                        f"with Nyquist frequency {f_nyquist} Hz. The inspiral will be "
                        f"truncated."
                    )
                lal_fft_plan = lal.CreateForwardREAL8FFTPlan(chirplen, 0)
                pol_dict_modes_FD = {}

                # iterate through modes, apply lines 3040 - 3050 in LS.SimInspiralFD
                for mode, pol_dict in pol_dict_modes.items():
                    hp = pol_dict["h_plus"]
                    hc = pol_dict["h_cross"]
                    # resize waveforms to the required length
                    lal.ResizeREAL8TimeSeries(hp, hp.data.length - chirplen, chirplen)
                    lal.ResizeREAL8TimeSeries(hc, hc.data.length - chirplen, chirplen)
                    # put the waveform in the frequency domain
                    # (the units will correct themselves)
                    hp_FD = lal.CreateCOMPLEX16FrequencySeries(
                        # None should be lalDimensionlessUnit, how to get that in python?
                        "FD H_PLUS",
                        hp.epoch,
                        0.0,
                        delta_f,
                        None,
                        chirplen // 2 + 1,
                    )
                    hc_FD = lal.CreateCOMPLEX16FrequencySeries(
                        "FD H_CROSS", hc.epoch, 0.0, delta_f, None, chirplen // 2 + 1
                    )
                    lal.REAL8TimeFreqFFT(hp_FD, hp, lal_fft_plan)
                    lal.REAL8TimeFreqFFT(hc_FD, hc, lal_fft_plan)

                    pol_dict_modes_FD[mode] = {
                        "h_plus": hp_FD.data.data,
                        "h_cross": hc_FD.data.data,
                    }

                # # Cross check, that the FFT polarizations modes indeed combine to the
                # # same result as LS.SimInspiralFD.
                # parameters_lal_fd = self._convert_parameters_to_lal_frame(
                #     parameters,
                #     self.lal_params,
                #     lal_target_function="SimInspiralFD",
                # )
                # hpfd, hcfd = LS.SimInspiralFD(*parameters_lal_fd)
                # pol_dict_tmp = sum_polarization_modes(pol_dict_modes_FD, 0.0)
                # hp_sum, hc_sum = pol_dict_tmp["h_plus"], pol_dict_tmp["h_cross"]
                # import matplotlib.pyplot as plt
                # plt.plot(hp_sum.real)
                # plt.plot(hpfd.data.data.real, ',')
                # plt.plot(hpfd.data.data.real - hp_sum.real)
                # plt.xlim((0, 2000))
                # plt.show()
                # plt.plot(hc_sum.real)
                # plt.plot(hcfd.data.data.real, ',')
                # plt.plot(hcfd.data.data.real - hc_sum.real)
                # plt.xlim((0, 2000))
                # plt.show()

                # check that domain of the polarizations is consistent with self.domain
                if not isclose(self.domain.delta_f, hp_FD.deltaF, rel_tol=1e-6):
                    raise ValueError(
                        f"Waveform delta_f is inconsistent with domain: {hp.deltaF} vs "
                        f"{self.domain.delta_f}! To avoid this, ensure that f_max = "
                        f"{self.domain.f_max} is a power of two of delta_f = "
                        f"{self.domain.delta_f} when you are using a native time-domain "
                        f"waveform model."
                    )

                # Undo the time shift done by LS to the waveform
                dt = 1 / hp_FD.deltaF + (
                    hp_FD.epoch.gpsSeconds + hp_FD.epoch.gpsNanoSeconds * 1e-9
                )
                time_shift = np.exp(-1j * 2 * np.pi * dt * self.domain())
                for pol_dict in pol_dict_modes_FD.values():
                    pol_dict["h_plus"] *= time_shift
                    pol_dict["h_cross"] *= time_shift

                return pol_dict_modes_FD

            elif isinstance(self.domain, TimeDomain):
                raise NotImplementedError("Time domain not implemented yet.")
                # Time domain might require time shifting (not sure), but other than
                # that I think pol_dict_modes is essentially what one needs.
            else:
                raise NotImplementedError(
                    f"Unsupported domain type {type(self.domain)}"
                )

        elif domain_type == "FD":
            raise NotImplementedError("FD waveform models not implemented yet")

    def generate_TD_waveform(self, parameters_lal: Tuple) -> Dict[str, np.ndarray]:
        """
        Generate time domain GW polarizations (h_plus, h_cross)

        Parameters
        ----------
        parameters_lal:
            A tuple of parameters for the lalsimulation waveform generator

        Returns
        -------
        pol_dict:
            A dictionary of generated waveform polarizations
        """
        # Note: XLALSimInspiralTD() now calls XLALSimInspiralChooseTDWaveform()
        # for models such as SEOBNRv4PHM where the reference frequency is equal
        # to the starting frequency and thus leaves our choice of starting
        # frequency untouched.
        #
        # LS.SimInspiralTD takes parameters:
        #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
        #   distance, inclination, phiRef,
        #   longAscNodes, eccentricity, meanPerAno,
        #   deltaT, f_min, f_ref
        #   lal_params, approximant

        hp, hc = LS.SimInspiralTD(*parameters_lal)
        h_plus = hp.data.data
        h_cross = hc.data.data
        pol_dict = {"h_plus": h_plus, "h_cross": h_cross}
        return pol_dict

    def generate_TD_waveform_modes(
        self,
        parameters,
        # parameters_lal: Tuple,
        # iota,
        # phase,
        taper_individually: bool = False,
    ) -> Dict[tuple, Dict[str, np.ndarray]]:
        """
        Generate time domain GW polarizations (h_plus, h_cross) for the individual
        modes. Similar to generate_TD_waveform, but returns the polarizations for the
        individual modes, instead of only the sum.

        Step 1: Generate the modes
        Step 2: Combine the modes to polarizations using spherical harmonics
        Step 3: Apply tapering

        Parameters
        ----------
        parameters: dict
            parameters for waveform generation
        parameters_lal:
            A tuple of parameters for the lalsimulation waveform generator
        iota: float
            iota angle
        phase: float
            reference phase

        Returns
        -------
        pol_dict:
            A dictionary of generated waveform polarizations
        """
        # LS.SimInspiralChooseTDModes takes parameters:
        #   phiRef=0 (for lal legacy reasons), delta_t,
        #   m1, m2, S1x, S1y, S1z, S2x, S2y, S2z,
        #   f_min, f_ref
        #   distance,
        #   lal_params, l_max, approximant

        # This mimics the behavior of the LS function SimInspiralTD, see
        # https://lscsoft.docs.ligo.org/lalsuite/lalsimulation/_l_a_l_sim_inspiral_8c_source.html#l02730
        # but instead of SimInspiralChooseTDWaveform, we call SimInspiralChooseTDModes
        # to extract the individual modes. The rest should proceed analogously.

        spin_freq_flag = LS.SimInspiralGetSpinFreqFromApproximant(self.approximant)

        phase = parameters["phase"]
        parameters_lal_td_modes, iota = self._convert_parameters_to_lal_frame(
            parameters,
            self.lal_params,
            lal_target_function="SimInspiralChooseTDModes",
        )

        if spin_freq_flag in (1, 3):
            # Step 1: compute individual TD modes
            modes = LS.SimInspiralChooseTDModes(*parameters_lal_td_modes)

            # Step 2: recombine the modes to polarizations, but individually for each mode
            pol_dict_modes = {}
            p = modes.this
            while p is not None:
                l, m = p.l, p.m
                # temporarily set p.next = None, such that the polarization is only
                # computed for the present mode
                next_node = p.next
                p.next = None
                # compute contribution of present mode to polarizations
                hp_mode, hc_mode = LS.SimInspiralPolarizationsFromSphHarmTimeSeries(
                    p, iota, np.pi / 2.0 - phase
                )
                pol_dict_modes[(l, m)] = {"h_plus": hp_mode, "h_cross": hc_mode}
                # proceed with next mode
                p = next_node

            # Step 3: Apply tapering to the polarizations, as done in lines 2773-2777
            # https://lscsoft.docs.ligo.org/lalsuite/lalsimulation/_l_a_l_sim_inspiral_waveform_taper_8c_source.html#l00222
            taper = 1  # corresponds to LAL_SIM_INSPIRAL_TAPER_START
            # Tapering of individual modes can lead to slightly inconsistent results,
            # since the lal tapering functions adapt to the positions of the peeks.
            if taper_individually:
                for pol_dict in pol_dict_modes.values():
                    LS.SimInspiralREAL8WaveTaper(pol_dict["h_plus"].data, taper)
                    LS.SimInspiralREAL8WaveTaper(pol_dict["h_cross"].data, taper)
            else:
                windows = get_tapering_windows(pol_dict_modes, taper)
                for pol_dict in pol_dict_modes.values():
                    for pol, pol_data in pol_dict.items():
                        pol_data.data.data *= windows[pol]

        else:
            raise NotImplementedError(
                f"Spin frequency flag {spin_freq_flag} not implemented"
            )
            # Implementing this requires performing the equivalent of line 2779 in
            # https://lscsoft.docs.ligo.org/lalsuite/lalsimulation/_l_a_l_sim_inspiral_8c_source.html
            # which means we will have to reimplement LS.SimInspiralTDFromTD for
            # individual modes.

        return pol_dict_modes


def SEOBNRv4PHM_maximum_starting_frequency(
    total_mass: float, fudge: float = 0.99
) -> float:
    """
    Given a total mass return the largest possible starting frequency allowed
    for SEOBNRv4PHM and similar effective-one-body models.

    The intended use for this function is at the stage of designing
    a data set: after choosing a mass prior one can use it to figure out
    which prior samples would run into an issue when generating an EOB waveform,
    and tweak the parameters to reduce the number of failing configurations.

    Parameters
    ----------
    total_mass:
        Total mass in units of solar masses
    fudge:
        A fudge factor

    Returns
    -------
    f_max_Hz:
        The largest possible starting frequency in Hz
    """
    total_mass_sec = total_mass * lal.MTSUN_SI
    f_max_Hz = fudge * 10.5 ** (-1.5) / (np.pi * total_mass_sec)
    return f_max_Hz


def generate_waveforms_task_func(
    args: Tuple, waveform_generator: WaveformGenerator = None
) -> Dict[str, np.ndarray]:
    """
    Picklable wrapper function for parallel waveform generation.

    Parameters
    ----------
    args:
        A tuple of (index, pandas.core.series.Series)
    waveform_generator:
        A WaveformGenerator instance

    Returns
    -------
    The generated waveform polarization dictionary
    """
    parameters = args[1].to_dict()
    return waveform_generator.generate_hplus_hcross(parameters)


def generate_waveforms_parallel(
    waveform_generator: WaveformGenerator,
    parameter_samples: pd.DataFrame,
    pool: Pool = None,
) -> Dict[str, np.ndarray]:
    """Generate a waveform dataset, optionally in parallel.

    Parameters
    ----------
    waveform_generator: WaveformGenerator
        A WaveformGenerator instance
    parameter_samples: pd.DataFrame
        Intrinsic parameter samples
    pool: multiprocessing.Pool
        Optional pool of workers for parallel generation

    Returns
    -------
    polarizations:
        A dictionary of all generated polarizations stacked together
    """
    # logger.info('Generating waveform polarizations ...')

    task_func = partial(
        generate_waveforms_task_func, waveform_generator=waveform_generator
    )
    task_data = parameter_samples.iterrows()
    if pool is not None:
        polarizations_list = pool.map(task_func, task_data)
    else:
        polarizations_list = list(map(task_func, task_data))
    polarizations = {
        pol: np.stack([wf[pol] for wf in polarizations_list])
        for pol in polarizations_list[0].keys()
    }
    return polarizations


def generate_waveform_and_catch_errors(
    wf_generator_func, parameters_lal, len_domain, catch_waveform_errors=True
):
    """
    Wraps wf_generator_func.

    If lal does not hit an error, this simply returns
    wf_dict = wf_generator_func(parameters_lal).

    If lal does hit an error:
        * If catch_waveform_errors is False, this raises the error.
        * If catch_waveform_errors is True, this returns a dictionary of nan's.

    Parameters
    ----------
    wf_generator_func: callable
        Function that computes wf_dict = wf_generator_func(parameters_lal)
    parameters_lal
        parameters for lal routine
    len_domain: int
        length of domain, required for nan initialization of the polarizations
    catch_waveform_errors: bool=Tru
        If True, catch lal errors and return nan's.

    Returns
    -------

    """
    try:
        wf_dict = wf_generator_func(parameters_lal)
    except Exception as e:
        if not catch_waveform_errors:
            raise
        else:
            EDOM = e.args[0] == "Internal function call failed: Input domain error"
            if EDOM:
                warnings.warn(
                    f"Evaluating the waveform failed with error: {e}\n"
                    f"The parameters were {parameters_lal}\n"
                )
                pol_nan = np.ones(len(self.domain)) * np.nan
                wf_dict = {"h_plus": pol_nan, "h_cross": pol_nan}
            else:
                raise
    return wf_dict


def sum_polarizations_m(pol_m, phase_shift=0.0):
    """
    Sum the polarizations over the m components, optionally introducing a phase shift.
    """
    polarizations = ["h_plus", "h_cross"]
    result = {pol: 0.0 for pol in polarizations}
    for pol in polarizations:
        for m, h in pol_m.items():
            result[pol] += h[pol] * np.exp(-1j * m * phase_shift)
    return result


def sum_fd_mode_contributions(fd_modearray_dict, delta_phi=0.0):
    """
    Sums the contributions of individual FrequencyDomain (FD) modes in fd_modearray_dict.
    This assumes exp(i * |m| * delta_phi) transformations to account for delta_phi.

    Typically the arrays in fd_modearray_dict would be FD polarizations, but they could
    also be FD waveforms in the individual detectors.

    Parameters
    ----------
    fd_modearray_dict: dict
        Dictionary of frequency domain mode arrays. These could e.g. be the
        polarizations, in which case the structure would be
        {
            {(2, 2): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            {(2, 1): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            ...
        }
    delta_phi: float = 0.0
        delta phi. Each mode (l, m) will be multiplied with exp(i * |m| * delta_phi)
        before the sum.

    Returns
    -------
    summed_dict: dict
        Dictionary of summed FD arrays.
        In case of polarizations: {"h_plus": hp_sum, "h_cross": hc_sum}
    """
    sample = list(fd_modearray_dict.values())[0]
    keys = sample.keys()
    # initialized summed dicts
    summed_dict = {k: np.zeros_like(sample[k]) for k in keys}
    for mode, array_dict in fd_modearray_dict.items():
        if isinstance(mode, tuple):
            _, m = mode
        else:
            m = mode
        for k in keys:
            summed_dict[k] += array_dict[k] * np.exp(1j * abs(m) * delta_phi)
    return summed_dict


def sum_over_l(fd_modearray_dict):
    """
    Sums the contributions of individual FrequencyDomain (FD) modes in
    fd_modearray_dict for different l but identical |m|. This can be useful, since only
    |m| determines the transformation behaviour under the spherical harmonics when
    changing the phase.

    Typically the arrays in fd_modearray_dict would be FD polarizations, but they could
    also be FD waveforms in the individual detectors.

    Parameters
    ----------
    fd_modearray_dict: dict
        Dictionary of frequency domain mode arrays. These could e.g. be the
        polarizations, in which case the structure would be
        {
            {(2, 2): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            {(2, 1): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            ...
        }

    Returns
    -------
    summed_dict: dict
        Dictionary of summed FD arrays for different modes |m|.
        In case of polarizations:
        {
            {(-1, 2): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            {(-1, 1): {"h_plus": np.ndarray, "h_cross": np.ndarray}},
            ...
        }

    """
    summed_dict = {}
    for mode, array_dict in fd_modearray_dict.items():
        _, m = mode
        mode_key = (-1, abs(m))
        if mode_key not in summed_dict:
            summed_dict[mode_key] = array_dict
        else:
            for array_key, v in array_dict.items():
                summed_dict[mode_key][array_key] += v
    return summed_dict


def get_tapering_windows(pol_dict_modes, taper):
    """
    Get lal tapering windows for the different polarizations, by summing up the modes
    of pol_dict_modes, applying lal tapering, and settings

        windows[pol] = modes_summed_tapered[pol] / modes_summed.

    This is useful since the lal tapering functions are adaptive to the input data,
    so applying them to the modes individually yields different windows. Computing the
    window with summed modes leads to coherent tapering between the modes.

    Parameters
    ----------
    pol_dict_modes: dict
        dict of mode contributions to the different polarizations
    taper: int
        lal flag for tapering

    Returns
    -------
    windows: dict
        dict with windows for the different polarization keys
    """
    pols = next(iter(pol_dict_modes.values())).keys()

    # sum up mode contributions to different polarizations
    pol_sums = {
        k: sum(pol_dict_mode[k].data.data for pol_dict_mode in pol_dict_modes.values())
        for k in pols
    }

    # To interface the lal tapering functions we need lal objects. As a bit of a hack,
    # we temporarily use one of the modes to insert summed polarizations for tapering,
    # and reset the data in the end
    k_tmp, v_tmp = next(iter(pol_dict_modes.items()))
    v_tmp = v_tmp["h_plus"]
    data_tmp = np.array(v_tmp.data.data, copy=True)

    # get windows
    windows = {}
    for pol, pol_sum in pol_sums.items():
        v_tmp.data.data = pol_sum
        LS.SimInspiralREAL8WaveTaper(v_tmp.data, taper)
        eps = 1e-10 * np.max(np.abs(pol_sum))
        windows[pol] = (np.abs(v_tmp.data.data) + eps) / (np.abs(pol_sum) + eps)
    # reset value for tmp vector
    pol_dict_modes[k_tmp]["h_plus"].data.data = data_tmp

    return windows


def apply_frequency_mask(h, domain):
    return {k: v * domain.frequency_mask for k, v in h.items()}


def inner(h1, h2):
    return np.sum([h1[pol].conj() * h2[pol] for pol in h1]).real


def mismatch(h1, h2):
    return 1 - inner(h1, h2) / np.sqrt(inner(h1, h1) * inner(h2, h2))


if __name__ == "__main__":
    import pandas as pd
    import numpy as np
    from dingo.gw.domains import build_domain
    from dingo.gw.prior import build_prior_with_defaults

    domain_settings = {
        "type": "FrequencyDomain",
        "f_min": 10.0,
        "f_max": 2048.0,
        "delta_f": 0.125,
    }
    domain = build_domain(domain_settings)
    intrinsic_dict = {
        "mass_1": "bilby.core.prior.Constraint(minimum=10.0, maximum=80.0)",
        "mass_2": "bilby.core.prior.Constraint(minimum=10.0, maximum=80.0)",
        "mass_ratio": "bilby.gw.prior.UniformInComponentsMassRatio(minimum=0.125, maximum=1.0)",
        "chirp_mass": "bilby.gw.prior.UniformInComponentsChirpMass(minimum=25.0, maximum=100.0)",
        "luminosity_distance": 1000.0,
        "theta_jn": "bilby.core.prior.Sine(minimum=0.0, maximum=np.pi)",
        "phase": 'bilby.core.prior.Uniform(minimum=0.0, maximum=2*np.pi, boundary="periodic")',
        "a_1": "bilby.core.prior.Uniform(minimum=0.0, maximum=0.99)",
        "a_2": "bilby.core.prior.Uniform(minimum=0.0, maximum=0.99)",
        "tilt_1": "bilby.core.prior.Sine(minimum=0.0, maximum=np.pi)",
        "tilt_2": "bilby.core.prior.Sine(minimum=0.0, maximum=np.pi)",
        "phi_12": 'bilby.core.prior.Uniform(minimum=0.0, maximum=2*np.pi, boundary="periodic")',
        "phi_jl": 'bilby.core.prior.Uniform(minimum=0.0, maximum=2*np.pi, boundary="periodic")',
        "geocent_time": 0.0,
    }
    prior = build_prior_with_defaults(intrinsic_dict)
    p = prior.sample()
    p = {
        "mass_ratio": 0.3501852584069329,
        "chirp_mass": 31.709276525188667,
        "luminosity_distance": 1000.0,
        "theta_jn": 1.3663250108421872,
        "phase": 2.3133395191342094,
        "a_1": 0.9082488389607664,
        "a_2": 0.23195443013657285,
        "tilt_1": 2.2991912365076708,
        "tilt_2": 2.2878677821511086,
        "phi_12": 2.3726027637572384,
        "phi_jl": 1.5356479043406908,
        "geocent_time": 0.0,
    }

    wfg = WaveformGenerator(
        # "SEOBNRv4PHM",
        "IMRPhenomXPHM",
        domain,
        20.0,
        f_start=10.0,
        spin_conversion_phase=0.0,
    )

    pol_m = wfg.generate_hplus_hcross_m(p)

    phase_shift = np.random.uniform(high=2 * np.pi)
    print(f"{phase_shift:.2f}")
    pol = sum_polarizations_m(pol_m, phase_shift=phase_shift)

    pol_ref = wfg.generate_hplus_hcross({**p, "phase": p["phase"] + phase_shift})
    m = mismatch(
        apply_frequency_mask(pol, wfg.domain), apply_frequency_mask(pol_ref, wfg.domain)
    )
    print(f"mismatch {m:.1e}")

    import matplotlib.pyplot as plt

    x = wfg.domain()
    plt.xlim((10, 512))
    plt.xscale("log")
    plt.plot(x, pol_ref["h_plus"].real)
    plt.plot(x, pol["h_plus"].real)
    plt.plot(x, (pol_ref["h_plus"] - pol["h_plus"]).real)
    plt.show()
