from typing import Optional, Union

import numpy as np
import pandas as pd
from astropy.time import Time
from bilby.core.prior import Uniform, Interped
from bilby.gw.detector import InterferometerList
from torchvision.transforms import Compose

from dingo.core.samplers import Sampler, GNPESampler
from dingo.core.transforms import GetItem, RenameKey
from dingo.core.density import NaiveKDE
from dingo.gw.domains import build_domain
from dingo.gw.gwutils import get_window_factor, get_extrinsic_prior_dict
from dingo.gw.likelihood import (
    StationaryGaussianGWLikelihood,
    synthetic_phase_sample_and_log_prob_multi,
)
from dingo.gw.prior import build_prior_with_defaults
from dingo.gw.transforms import (
    WhitenAndScaleStrain,
    RepackageStrainsAndASDS,
    ToTorch,
    SelectStandardizeRepackageParameters,
    GNPECoalescenceTimes,
    TimeShiftStrain,
    GNPEChirp,
    GNPEBase,
    PostCorrectGeocentTime,
    CopyToExtrinsicParameters,
    GetDetectorTimes,
)


class GWSamplerMixin(object):
    """
    Mixin class designed to add gravitational wave functionality to Sampler classes:
        * builders for prior, domain, and likelihood
        * correction for fixed detector locations during training (t_ref)
    """

    def __init__(self, synthetic_phase_kwargs=None, **kwargs):
        """
        Parameters
        ----------
        synthetic_phase_kwargs : dict = None
            kwargs for synthetic phase generation.
        kwargs
            Keyword arguments that are forwarded to the superclass.
        """
        super().__init__(**kwargs)
        self.t_ref = self.base_model_metadata["train_settings"]["data"]["ref_time"]
        self._pesummary_package = "gw"
        self.synthetic_phase_kwargs = synthetic_phase_kwargs

    # _build_prior and _build_domain are called by Sampler.__init__, in that order.

    def _build_prior(self):
        """Build the prior based on model metadata. Called by __init__()."""
        intrinsic_prior = self.base_model_metadata["dataset_settings"][
            "intrinsic_prior"
        ]
        extrinsic_prior = get_extrinsic_prior_dict(
            self.base_model_metadata["train_settings"]["data"]["extrinsic_prior"]
        )
        self.prior = build_prior_with_defaults({**intrinsic_prior, **extrinsic_prior})

        # Split off prior over geocent_time if samples appear to be time-marginalized.
        # This needs to be saved to initialize the likelihood.
        if (
            "geocent_time" in self.prior.keys()
            and "geocent_time" not in self.inference_parameters
        ):
            self.geocent_time_prior = self.prior.pop("geocent_time")
        else:
            self.geocent_time_prior = None
        # Split off prior over phase if samples appear to be phase-marginalized.
        if "phase" in self.prior.keys() and "phase" not in self.inference_parameters:
            self.phase_prior = self.prior.pop("phase")
        else:
            self.phase_prior = None

    def _build_domain(self):
        """
        Construct the domain object based on model metadata. Includes the window factor
        needed for whitening data.

        Called by __init__() immediately after _build_prior().
        """
        self.domain = build_domain(
            self.base_model_metadata["dataset_settings"]["domain"]
        )

        data_settings = self.base_model_metadata["train_settings"]["data"]
        if "domain_update" in data_settings:
            self.domain.update(data_settings["domain_update"])

        self.domain.window_factor = get_window_factor(data_settings["window"])

    # _build_likelihood is called at the beginning of Sampler.importance_sample

    def _build_likelihood(
        self,
        time_marginalization_kwargs: Optional[dict] = None,
        phase_marginalization: bool = False,
        phase_grid: Optional[np.ndarray] = None,
    ):
        """
        Build the likelihood function based on model metadata. This is called at the
        beginning of importance_sample().

        Parameters
        ----------
        time_marginalization_kwargs: dict, optional
            kwargs for time marginalization. At this point the only kwarg is n_fft,
            which determines the number of FFTs used (higher n_fft means better
            accuracy, at the cost of longer computation time).
        phase_marginalization: bool = False
            Whether to marginalize over phase.
        """
        if time_marginalization_kwargs is not None:
            if self.geocent_time_prior is None:
                raise NotImplementedError(
                    "Time marginalization is not compatible with "
                    "non-marginalized network."
                )
            if type(self.geocent_time_prior) != Uniform:
                raise NotImplementedError(
                    "Only uniform time prior is supported for time marginalization."
                )
            time_marginalization_kwargs["t_lower"] = self.geocent_time_prior.minimum
            time_marginalization_kwargs["t_upper"] = self.geocent_time_prior.maximum

        if phase_marginalization:
            # check that phase prior is uniform [0, 2pi)
            if not (
                isinstance(self.phase_prior, Uniform)
                and (self.phase_prior._minimum, self.phase_prior._maximum)
                == (0, 2 * np.pi)
            ):
                raise ValueError(
                    f"Phase prior should be uniform [0, 2pi) for phase "
                    f"marginalization, but is {self.phase_prior}."
                )

        # The detector reference positions during likelihood evaluation should be based
        # on the event time, since any post-correction to account for the training
        # reference time has already been applied to the samples.

        if self.event_metadata is not None and "time_event" in self.event_metadata:
            t_ref = self.event_metadata["time_event"]
        else:
            t_ref = self.t_ref

        self.likelihood = StationaryGaussianGWLikelihood(
            wfg_kwargs=self.base_model_metadata["dataset_settings"][
                "waveform_generator"
            ],
            wfg_domain=build_domain(
                self.base_model_metadata["dataset_settings"]["domain"]
            ),
            data_domain=self.domain,
            event_data=self.context,
            t_ref=t_ref,
            time_marginalization_kwargs=time_marginalization_kwargs,
            phase_marginalization=phase_marginalization,
            phase_grid=phase_grid,
        )

    def _post_correct_for_tref(
        self, samples: Union[dict, pd.DataFrame], inverse: bool = False
    ):
        """
        Correct the sky position of an event based on the reference time of the model.
        This is necessary since the model was trained with with fixed detector (reference)
        positions. This transforms the right ascension based on the e difference between
        the time of the event and t_ref.

        The correction is only applied if the event time can be found in self.metadata[
        'event'].

        This method modifies the samples in place.

        Parameters
        ----------
        samples : dict or pd.DataFrame
        inverse : bool, default True
            Whether to apply instead the inverse transformation. This is used prior to
            calculating the log_prob.
        """
        if self.event_metadata is not None:
            t_event = self.event_metadata.get("time_event")
            if t_event is not None and t_event != self.t_ref:
                ra = samples["ra"]
                time_reference = Time(self.t_ref, format="gps", scale="utc")
                time_event = Time(t_event, format="gps", scale="utc")
                longitude_event = time_event.sidereal_time("apparent", "greenwich")
                longitude_reference = time_reference.sidereal_time(
                    "apparent", "greenwich"
                )
                delta_longitude = longitude_event - longitude_reference
                ra_correction = delta_longitude.rad
                if not inverse:
                    samples["ra"] = (ra + ra_correction) % (2 * np.pi)
                else:
                    samples["ra"] = (ra - ra_correction) % (2 * np.pi)

    def _sample_synthetic_phase(
        self, samples: Union[dict, pd.DataFrame], inverse: bool = False
    ):
        """
        Sample a synthetic phase for the waveform. This is a post-processing step
        applicable to samples theta in the full parameter space, except for the phase
        parameter (i.e., 14D samples). This step adds a theta parameter to the samples
        based on likelihood evaluations.

        A synthetic phase is sampled as follows.

            * Compute the waveform mu(theta, phase=0) for phase 0.
            * Compute the likelihood for mu(theta, phase=0) * exp(2i*phase) on a
              uniform grid of phase values in the range [0, 2pi).
              Note that this is *not* equivalent to the actual likelihood for
              (theta, phase) unless the waveform is fully dominated by the (2, 2) mode.
            * Build a synthetic conditional phase distribution based on this grid.
              We use a kde, such that we can sample and also evaluate the log_prob.
              We add a constant background with weight self.synthetic_phase_kwargs to the
              kde to make sure that we keep a mass-covering property. With this,
              the impportance sampling will yield exact results even if the synthetic
              phase conditional is just an approximation.

        Besides adding phase samples to samples['phase'], this method also modifies
        samples['log_prob'] by adding the log_prob of the synthetic phase conditional.

        This method modifies the samples in place.

        Parameters
        ----------
        samples : dict or pd.DataFrame
        inverse : bool, default True
            Whether to apply instead the inverse transformation. This is used prior to
            calculating the log_prob.

        """
        if inverse:
            pass
        else:
            if not (
                isinstance(self.phase_prior, Uniform)
                and (self.phase_prior._minimum, self.phase_prior._maximum)
                == (0, 2 * np.pi)
            ):
                raise ValueError(
                    f"Phase prior should be uniform [0, 2pi) to generate synthetic phase."
                    f" However, the prior is {self.phase_prior}."
                )
            # This builds on the Bilby approach to sampling the phase when using a
            # phase-marginalized likelihood.

            # Only sample synthetic phase for samples initially within the prior.
            theta = pd.DataFrame(samples).drop(columns="log_prob")
            log_prior = self.prior.ln_prob(theta, axis=0)
            constraints = self.prior.evaluate_constraints(theta)
            np.putmask(log_prior, constraints == 0, -np.inf)
            within_prior = log_prior != -np.inf

            self._build_likelihood()

            import time

            t0 = time.time()

            # Begin with phase set to zero and evaluate the complex inner product.
            theta["phase"] = 0.0
            d_inner_h_complex = self.likelihood.d_inner_h_complex_multi(
                theta.iloc[within_prior],
                self.synthetic_phase_kwargs.get("num_processes", 1),
            )

            # Evaluate the log posterior over the phase across the grid. This is
            # un-normalized, with a different normalization for each sample. (It will
            # eventually be normalized.)
            phases = np.linspace(0, 2 * np.pi, self.synthetic_phase_kwargs["n_grid"])
            phasor = np.exp(-2j * phases)
            phase_log_posterior = np.outer(d_inner_h_complex, phasor).real
            phase_posterior = np.exp(phase_log_posterior - max(phase_log_posterior))

            # Interpolate and sample a new phase.
            # new_phase = np.empty(len(phase_posterior))
            # delta_log_prob = np.empty(len(phase_posterior))
            # for i, p in enumerate(phase_posterior):
            #     interp = Interped(phases, p)
            #     new_phase[i] = interp.sample()
            #     delta_log_prob[i] = interp.ln_prob(new_phase[i])

            new_phase, delta_log_prob = synthetic_phase_sample_and_log_prob_multi(
                phases,
                phase_posterior,
                self.synthetic_phase_kwargs.get("num_processes", 1),
            )

            print(f"{time.time() - t0:.2f}s to sample synthetic phase.")

            phase_array = np.full(len(theta), 0.0)
            phase_array[within_prior] = new_phase
            delta_log_prob_array = np.full(len(theta), -np.inf)
            delta_log_prob_array[within_prior] = delta_log_prob

            samples["phase"] = phase_array
            samples["log_prob"] += delta_log_prob_array

            # set prior for phase, since now phase parameter is present
            self.prior["phase"] = self.phase_prior
            # reset likelihood for safety
            self.likelihood = None

            # # build grid
            # kde = NaiveKDE(0, np.pi, self.synthetic_phase_kwargs["n_grid"])
            # phase_grid = kde.grid
            #
            # self._build_likelihood(phase_grid=phase_grid)
            # theta = pd.DataFrame(samples).drop(columns="log_prob")
            #
            # # only evaluate in prior support
            # log_prior = self.prior.ln_prob(theta, axis=0)
            # constraints = self.prior.evaluate_constraints(theta)
            # np.putmask(log_prior, constraints == 0, -np.inf)
            # within_prior = log_prior != -np.inf
            #
            # import time
            #
            # t0 = time.time()
            #
            # # evaluate the likelihood on the phase grid (this is an approximation if
            # # higher modes are relevant)
            # log_likelihood_grid = self.likelihood.log_likelihood_multi(
            #     theta.iloc[within_prior],
            #     self.synthetic_phase_kwargs.get("num_processes", 1),
            # )
            #
            # # build density
            # kde.initialize_log_prob(
            #     log_likelihood_grid,
            #     self.synthetic_phase_kwargs.get("uniform_weight", 0.05),
            # )
            # phase, delta_log_prob = kde.sample_and_log_prob()
            # # the kde is uniform in [0, pi), we now modify it to a pi-periodic
            # # distribution in [0, 2pi).
            # phase = phase + np.random.choice((0,np.pi), size=len(phase))
            # delta_log_prob = delta_log_prob - np.log(2)
            # print(f"{time.time() - t0:.2f}s to sample synthetic phase")
            #
            # # unsqueeze phase and log_prob, fill samples outside prior support with -inf
            # phase_arr = np.full(len(theta), -np.inf)
            # phase_arr[within_prior] = phase
            # delta_log_prob_arr = np.full(len(theta), -np.inf)
            # delta_log_prob_arr[within_prior] = delta_log_prob
            #
            # # insert phase into theta and add log_prob
            # if isinstance(samples, pd.DataFrame):
            #     samples.insert(samples.shape[1] - 2, "phase", phase_arr)
            #     samples["log_prob"] += delta_log_prob_arr
            # else:
            #     samples["phase"] = phase_arr
            #     samples["log_prob"] += delta_log_prob_arr
            #
            # # set prior for phase, since now phase parameter is present
            # self.prior["phase"] = self.phase_prior
            # # reset likelihood for safety
            # self.likelihood = None

    def _post_process(self, samples: Union[dict, pd.DataFrame], inverse: bool = False):
        """
        Post processing of parameter samples.
        * Correct the sky position for a potentially fixed reference time.
          (see self._post_correct_for_tref)
        * Potentially sample a synthetic phase. (see self._sample_synthetic_phase)

        This method modifies the samples in place.

        Parameters
        ----------
        samples : dict or pd.DataFrame
        inverse : bool, default True
            Whether to apply instead the inverse transformation. This is used prior to
            calculating the log_prob.
        """
        self._post_correct_for_tref(samples, inverse)
        if self.synthetic_phase_kwargs is not None:
            self._sample_synthetic_phase(samples, inverse)


class GWSampler(GWSamplerMixin, Sampler):
    """
    Sampler for gravitational-wave inference using neural posterior estimation. Wraps a
    PosteriorModel instance.

    This is intended for use either as a standalone sampler, or as a sampler producing
    initial sample points for a GNPE sampler.
    """

    def __init__(self, **kwargs):
        """
        Parameters
        ----------
        model : PosteriorModel
        """
        super().__init__(**kwargs)
        self._initialize_transforms()

    def _initialize_transforms(self):

        data_settings = self.metadata["train_settings"]["data"]

        # preprocessing transforms:
        #   * whiten and scale strain (since the inference network expects standardized
        #   data)
        #   * repackage strains and asds from dicts to an array
        #   * convert array to torch tensor on the correct device
        #   * extract only strain/waveform from the sample
        self.transform_pre = Compose(
            [
                WhitenAndScaleStrain(self.domain.noise_std),
                RepackageStrainsAndASDS(
                    data_settings["detectors"],
                    first_index=self.domain.min_idx,
                ),
                ToTorch(device=self.model.device),
                GetItem("waveform"),
            ]
        )

        # postprocessing transforms:
        #   * de-standardize data and extract inference parameters
        self.transform_post = SelectStandardizeRepackageParameters(
            {"inference_parameters": self.inference_parameters},
            data_settings["standardization"],
            inverse=True,
            as_type="dict",
        )


class GWSamplerGNPE(GWSamplerMixin, GNPESampler):
    """
    Sampler for graviational-wave inference using group-equivariant neural posterior
    estimation (GNPE). Wraps a PosteriorModel instance.

    This sampler also contains an NPE sampler, which is used to generate initial
    samples for the GNPE loop.
    """

    def __init__(self, **kwargs):
        """

        Parameters
        ----------
        model : PosteriorModel
            GNPE model.
        init_model : PosteriodModel
            Used to produce samples for initializing the GNPE loop.
        num_iterations :
            Number of GNPE iterations to be performed by sampler.
        """
        super().__init__(**kwargs)
        self._initialize_transforms()

    def _initialize_transforms(self):
        """
        Builds the transforms that are used in the GNPE loop.
        """
        data_settings = self.metadata["train_settings"]["data"]
        ifo_list = InterferometerList(data_settings["detectors"])

        gnpe_time_settings = data_settings.get("gnpe_time_shifts")
        gnpe_chirp_settings = data_settings.get("gnpe_chirp")
        if not gnpe_time_settings and not gnpe_chirp_settings:
            raise KeyError(
                "GNPE inference requires network trained for either chirp mass "
                "or coalescence time GNPE."
            )

        # transforms for gnpe loop, to be applied prior to sampling step:
        #   * reset the sample (e.g., clone non-gnpe transformed waveform)
        #   * blurring detector times to obtain gnpe proxies
        #   * shifting the strain by - gnpe proxies
        #   * repackaging & standardizing proxies to sample['context_parameters']
        #     for conditioning of the inference network
        transform_pre = [RenameKey("data", "waveform")]
        if gnpe_time_settings:
            transform_pre.append(
                GNPECoalescenceTimes(
                    ifo_list,
                    gnpe_time_settings["kernel"],
                    gnpe_time_settings["exact_equiv"],
                    inference=True,
                )
            )
            transform_pre.append(TimeShiftStrain(ifo_list, self.domain))
        if gnpe_chirp_settings:
            transform_pre.append(
                GNPEChirp(
                    gnpe_chirp_settings["kernel"],
                    self.domain,
                    gnpe_chirp_settings.get("order", 0),
                )
            )
        transform_pre.append(
            SelectStandardizeRepackageParameters(
                {"context_parameters": data_settings["context_parameters"]},
                data_settings["standardization"],
                device=self.model.device,
            )
        )
        transform_pre.append(RenameKey("waveform", "data"))

        self.gnpe_parameters = []
        for transform in transform_pre:
            if isinstance(transform, GNPEBase):
                self.gnpe_parameters += transform.input_parameter_names
        print("GNPE parameters: ", self.gnpe_parameters)

        self.transform_pre = Compose(transform_pre)

        # transforms for gnpe loop, to be applied after sampling step:
        #   * de-standardization of parameters
        #   * post correction for geocent time (required for gnpe with exact equivariance)
        #   * computation of detectortimes from parameters (required for next gnpe
        #       iteration)
        self.transform_post = Compose(
            [
                SelectStandardizeRepackageParameters(
                    {"inference_parameters": self.inference_parameters},
                    data_settings["standardization"],
                    inverse=True,
                    as_type="dict",
                ),
                PostCorrectGeocentTime(),
                CopyToExtrinsicParameters(
                    "ra", "dec", "geocent_time", "chirp_mass", "mass_ratio"
                ),
                GetDetectorTimes(ifo_list, data_settings["ref_time"]),
            ]
        )


class GWSamplerUnconditional(GWSampler):
    def _initialize_transforms(self):
        # Postprocessing transform only:
        #   * De-standardize data and extract inference parameters. Be careful to use
        #   the standardization of the correct model, not the base model.
        self.transform_post = SelectStandardizeRepackageParameters(
            {"inference_parameters": self.inference_parameters},
            self.metadata["train_settings"]["data"]["standardization"],
            inverse=True,
            as_type="dict",
        )

    def _post_correct_for_tref(
        self, samples: Union[dict, pd.DataFrame], inverse: bool = False
    ):
        # We do not want to correct for t_ref because we assume that the unconditional
        # model will have been trained on samples for which this correction was already
        # implemented.
        pass
