"""Zero Trust security (Module 5).

- ``identity``     device identities from topology + config/identities.yaml
- ``records``      flows, logins, anomalies
- ``traffic``      simulated traffic and synthetic attacks
- ``detectors``    behaviour detectors and the Eq. 3 threat index
- ``trust``        Eq. 2 trust scores and trust levels
- ``policy``       default-deny policy decisions (config/zero_trust_policy.yaml)
- ``enforcement``  switch rules and a simulated enforcer
- ``engine``       per-step decide / enforce / detect / update-trust loop
- ``evaluation``   CLI: ``python -m network.security.evaluation``
- ``live``         background simulation for the viewer

Standard library + PyYAML; no Mininet. All traffic is SIMULATED and all
attacks SYNTHETIC until live flow telemetry exists.
"""
