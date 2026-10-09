"""Two-objective MC EHVI with exact areas and shared deterministic normal draws."""
from __future__ import annotations
import math
from typing import Any
import numpy as np

from blade_shape_runtime import stream_seed

#: The hypervolume reference sits this fraction of each objective's observed
#: range below the worst observed value, so the worst points still enclose a
#: nonzero area. The output directory's hypervolume_reference.json keeps the
#: value it was first written with, so changing this does not move the
#: reference (or the hv_gain history) of an existing run.
HV_REFERENCE_MARGIN = 0.05


def hv_reference_point(observed: np.ndarray) -> np.ndarray:
    """Reference point below the observed objectives (both maximised)."""
    observed = np.asarray(observed, dtype=float).reshape(-1, 2)
    return observed.min(axis=0)-HV_REFERENCE_MARGIN*np.maximum(np.ptp(observed, axis=0), 1e-6)


def normal_samples(count: int, seed: int) -> np.ndarray:
    if count < 1:
        raise ValueError('EHVI sample count must be positive.')
    try:
        from scipy.stats import qmc
        from scipy.special import ndtri
        unit = qmc.Sobol(d=2, scramble=True, seed=seed).random_base2(int(math.ceil(math.log2(count))))[:count]
        return ndtri(np.clip(unit, np.finfo(float).eps, 1-np.finfo(float).eps))
    except ImportError:
        return np.random.default_rng(seed).standard_normal((count, 2))


def improvement_2d(samples: np.ndarray, observed: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Candidate rectangle minus its overlap with disjoint observed horizontal strips."""
    samples = np.asarray(samples, dtype=float)
    gain = np.prod(np.maximum(samples-reference, 0), axis=-1)
    observed = np.asarray(observed, dtype=float).reshape(-1, 2)
    valid = observed[np.isfinite(observed).all(axis=1) & np.all(observed>reference, axis=1)]
    top = float(reference[1])
    for x,y in valid[np.argsort(valid[:,0])[::-1]]:
        if y > top:
            width = np.maximum(np.minimum(samples[...,0],x)-reference[0],0)
            height = np.maximum(np.minimum(samples[...,1],y)-top,0)
            gain -= width*height
            top = y
    return np.maximum(gain, 0)


def expected_hvi(config: dict[str,Any], observed: np.ndarray, mean: np.ndarray, std: np.ndarray) -> dict[str,Any]:
    mean, std = np.asarray(mean,float), np.asarray(std,float)
    if mean.ndim != 2 or mean.shape[1] != 2 or mean.shape != std.shape:
        raise ValueError('EHVI expects two objectives and matching mean/std arrays.')
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std<0):
        raise ValueError('EHVI requires finite predictions and nonnegative standard deviations.')
    observed=np.asarray(observed,float).reshape(-1,2)
    observed=observed[np.isfinite(observed).all(axis=1)]
    settings=config.get('surrogate',{})
    base=int(settings.get('ehvi_y_samples',256))
    count=max(base,int(settings.get('ehvi_validation_samples',base)))
    reference=config.get('_ehvi_reference',settings.get('ehvi_reference'))
    if reference is None:
        if not len(observed):
            raise ValueError('An explicit reference is required without observations.')
        reference=hv_reference_point(observed)
    reference=np.asarray(reference,float)
    if reference.shape!=(2,) or not np.isfinite(reference).all():
        raise ValueError('Invalid EHVI reference point.')
    z=normal_samples(count,stream_seed(int(config['runtime'].get('seed',42)),'ehvi_normals'))
    # Evaluate every candidate with identical base samples. No ordering-dependent top-k refinement.
    draws=mean[:,None,:]+std[:,None,:]*z[None,:,:]
    gains=improvement_2d(draws,observed,reference)
    coarse=gains[:,:base].mean(axis=1)
    scores=gains.mean(axis=1)
    return {'scores':scores,'base_scores':coarse,'sampling_change':np.abs(scores-coarse),
            'samples':count,'base_samples':base,'reference':reference}
