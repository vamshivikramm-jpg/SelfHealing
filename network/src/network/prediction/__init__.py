"""AI-based failure prediction (Module 4).

- ``data``        prediction datasets and the train/validation/test suite
- ``models``      rule, z-score, Isolation Forest and scikit-learn models
- ``registry``    versioned models with champion/challenger and rollback
- ``evaluation``  row- and event-level metrics, threshold selection
- ``experiment``  CLI: ``python -m network.prediction.experiment``
- ``online``      live early warning (FAILURE_PREDICTED events)

Requires numpy and scikit-learn. Every result so far comes from
SIMULATED data with designed (synthetic) warning patterns.

Submodules are imported directly (``from network.prediction.models import
...``) so that the live predictor does not pull in the scenario tools.
"""
