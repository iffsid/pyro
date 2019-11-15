import math
import warnings

import torch

import pyro
import pyro.poutine as poutine
from pyro.infer.elbo import ELBO
from pyro.infer.enum import get_importance_trace_detached
from pyro.infer.util import is_validation_enabled
from pyro.poutine.util import prune_subsample_sites
from pyro.util import check_if_enumerated, warn_if_nan, check_model_guide_match


class ReweightedWakeSleep(ELBO):
    r"""
    An implementation of Reweighted Wake Sleep following reference [1].

    .. note:: This is particularly useful for models with stochastic branching,
        as described in [2].

    .. note:: This returns _two_ losses, one each for the model and the guide.

    .. warning:: Mini-batch training might have issues.

    :param num_particles: The number of particles/samples used to form the objective
        (gradient) estimator. Default is 2.
    :param insomnia: The scaling between the wake-phi and sleep-phi terms. Default is 1.0 [wake-phi]
    :param num_sleep_particles: The number of particles used to form the sleep-phi estimator.
        Default is 1 [matching the batch size].
    :param int max_plate_nesting: Bound on max number of nested
        :func:`pyro.plate` contexts. Default is infinity.
    :param bool strict_enumeration_warning: Whether to warn about possible
        misuse of enumeration, i.e. that
        :class:`~pyro.infer.traceenum_elbo.TraceEnum_ELBO` is used iff there
        are enumerated sample sites.

    References:

    [1] `Reweighted Wake-Sleep`,
        Jörg Bornschein, Yoshua Bengio

    [2] `Revisiting Reweighted Wake-Sleep for Models with Stochastic Control Flow`,
        Tuan Anh Le, Adam R. Kosiorek, N. Siddharth, Yee Whye Teh, Frank Wood
    """

    def __init__(self,
                 num_particles=2,
                 insomnia=1.,
                 num_sleep_particles=None,
                 max_plate_nesting=float('inf'),
                 max_iarange_nesting=None,  # DEPRECATED
                 vectorize_particles=True,
                 strict_enumeration_warning=True):
        if max_iarange_nesting is not None:
            warnings.warn("max_iarange_nesting is deprecated; use max_plate_nesting instead",
                          DeprecationWarning)
            max_plate_nesting = max_iarange_nesting

        # force K > 1 otherwise SNIS not possible
        assert(num_particles > 1), \
            "Reweighted Wake Sleep needs to be run with more than one particle"

        super(ReweightedWakeSleep, self).__init__(num_particles=num_particles,
                                                  max_plate_nesting=max_plate_nesting,
                                                  vectorize_particles=vectorize_particles,
                                                  strict_enumeration_warning=strict_enumeration_warning)
        self.insomnia = insomnia
        if num_sleep_particles is None:
            num_sleep_particles = num_particles
        self.num_sleep_particles = num_sleep_particles
        assert(insomnia >= 0 and insomnia <= 1), \
            "insomnia should be in [0, 1]"

    def _get_trace(self, model, guide, *args, **kwargs):
        """
        Returns a single trace from the guide, and the model that is run against it.
        """
        model_trace, guide_trace = get_importance_trace_detached(
            "flat", self.max_plate_nesting, model, guide, *args, **kwargs)
        if is_validation_enabled():
            check_if_enumerated(guide_trace)
        return model_trace, guide_trace

    def _loss(self, model, guide, *args, **kwargs):
        """
        :returns: returns model loss and guide loss
        :rtype: float, float

        Computes the re-weighted wake-sleep estimators for the model (wake-theta) and the
          guide (insomnia * wake-phi + (1 - insomnia) * sleep-phi).
        Performs backward as appropriate on both, over the specified number of particles.
        """

        # COMPUTE QUANTITIES FOR WAKE THETA AND WAKE PHI
        log_joints = []
        log_qs = []

        for model_trace, guide_trace in self._get_traces(model, guide, *args, **kwargs):
            log_joint = 0.
            log_q = 0.

            for name, site in model_trace.nodes.items():
                if site["type"] == "sample":
                    log_p_site = site["log_prob"]
                    log_joint = log_joint + log_p_site

            for name, site in guide_trace.nodes.items():
                if site["type"] == "sample":
                    log_q_site = site["log_prob"]
                    log_q = log_q + log_q_site

            log_joints.append(log_joint)
            log_qs.append(log_q)

        log_joints = log_joints[0] if self.vectorize_particles else torch.stack(log_joints)
        log_qs = log_qs[0] if self.vectorize_particles else torch.stack(log_qs)
        log_weights = log_joints - log_qs.detach()

        # COMPUTE WAKE THETA LOSS
        log_sum_weight = torch.logsumexp(log_weights, dim=0)
        # TODO: check whether sum or mean over batch dim (currently sum)
        #   the following line sums IWAE ELBO over all plate dims in the model
        wake_theta_loss = -(log_sum_weight - math.log(self.num_particles)).sum()  # <-- TA: I think this is right
        #   the following line means IWAE ELBO over all plate dims in the model
        # wake_theta_loss = -(log_sum_weight - math.log(self.num_particles)).mean()
        warn_if_nan(wake_theta_loss, "wake theta loss")

        if self.insomnia > 0:
            # COMPUTE WAKE PHI LOSS
            normalised_weights = (log_weights - log_sum_weight).exp().detach()
            # TODO: check whether sum or mean over batch dim (currently sum)
            #   the following line sums over the particle dimension
            #   and sums over all other plate dimensions in the model
            wake_phi_loss = -(normalised_weights * log_qs).sum(0).sum()  # <-- TA: I think this is right
            #   the following line sums over the particle dimension
            #   and means over all other plate dimensions in the model
            # wake_phi_loss = -(normalised_weights * log_qs).sum(0).mean()
            warn_if_nan(wake_phi_loss, "wake phi loss")

        if self.insomnia < 1:
            # sleep phi = csis
            _model = pyro.poutine.uncondition(model)
            _guide = guide
            _log_q = 0.
            should_vectorize = self.vectorize_particles and self.num_sleep_particles > 1
            if should_vectorize:
                old_num_particles = self.num_particles
                self.num_particles = self.num_sleep_particles
                _model = self._vectorized_num_particles(_model)
                _guide = self._vectorized_num_particles(guide)
            for _ in range(1 if should_vectorize else self.num_sleep_particles):
                _model_trace = poutine.trace(_model).get_trace(*args, **kwargs)
                for site in _model_trace.nodes.values():
                    if site["type"] == "sample":
                        site["value"] = site["value"].detach()
                _guide_trace = self._get_matched_trace(_model_trace, _guide, *args, **kwargs)
                # TODO: check whether sum or mean over batch dim (currently sum)
                _log_q += _guide_trace.log_prob_sum()
            if should_vectorize:
                self.num_particles = old_num_particles

            # TODO: check whether sum or mean over batch dim (currently sum)
            #   the following line sums over particles and
            #   sums over all other plate dims in the model
            sleep_phi_loss = -_log_q
            #   the following line means over particles and
            #   sums over all other plate dims in the model
            # sleep_phi_loss = -_log_q / self.num_sleep_particles <-- TA: I think this is right
            warn_if_nan(sleep_phi_loss, "sleep phi loss")

        phi_loss = sleep_phi_loss if self.insomnia == 0 \
            else wake_phi_loss if self.insomnia == 1 \
            else self.insomnia * wake_phi_loss + (1. - self.insomnia) * sleep_phi_loss

        return wake_theta_loss, phi_loss

    def loss(self, model, guide, *args, **kwargs):
        """
        :returns: returns model loss and guide loss
        :rtype: float, float

        Computes the re-weighted wake-sleep estimators for the model (wake-theta) and the
          guide (insomnia * wake-phi + (1 - insomnia) * sleep-phi).
        """
        with torch.no_grad():
            wake_theta_loss, phi_loss = self._loss(model, guide, *args, **kwargs)

        return wake_theta_loss, phi_loss

    def loss_and_grads(self, model, guide, *args, **kwargs):
        """
        :returns: returns model loss and guide loss
        :rtype: float

        Computes the RWS estimators for the model (wake-theta) and the guide (wake-phi).
        Performs backward as appropriate on both, using num_particle many samples/particles.
        """
        wake_theta_loss, phi_loss = self._loss(model, guide, *args, **kwargs)
        try:
            wake_theta_loss.backward(retain_graph=True)
        except RuntimeError:
            pass
        phi_loss.backward()

        return wake_theta_loss.detach(), phi_loss.detach()

    @staticmethod
    def _get_matched_trace(model_trace, guide, *args, **kwargs):
        # TODO: hardcoded kwarg 'observations'?
        kwargs["observations"] = {}
        for node in model_trace.stochastic_nodes + model_trace.observation_nodes:
            if "was_observed" in model_trace.nodes[node]["infer"]:
                model_trace.nodes[node]["is_observed"] = True
                kwargs["observations"][node] = model_trace.nodes[node]["value"]

        guide_trace = poutine.trace(poutine.replay(guide, model_trace)).get_trace(*args, **kwargs)
        check_model_guide_match(model_trace, guide_trace)
        guide_trace = prune_subsample_sites(guide_trace)
        return guide_trace
