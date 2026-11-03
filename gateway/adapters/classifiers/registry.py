"""Classifier selection by config (spec section 3): `CLASSIFIER_BACKEND=rules|laya|jev`.

Jev is hosted, so it is available only with `JEV_API_KEY`; selecting it without one fails at
startup with a clear error and never falls back to sending text anywhere else.
"""

from typing import Literal

import httpx2

from gateway.adapters.classifiers.rules import RulesClassifier
from gateway.adapters.classifiers.systemone import SystemOneClassifier
from gateway.ports.classifiers import Classifier

Backend = Literal["rules", "laya", "jev"]


class ClassifierConfigError(RuntimeError):
    pass


def build_classifier(
    backend: Backend,
    *,
    http: httpx2.AsyncClient,
    laya_base_url: str,
    laya_api_key: str | None,
    jev_base_url: str,
    jev_api_key: str | None,
    jev_model: str,
    timeout_s: float,
) -> Classifier:
    if backend == "rules":
        return RulesClassifier()
    if backend == "laya":
        return SystemOneClassifier(
            name="laya",
            base_url=laya_base_url,
            api_key=laya_api_key,
            http=http,
            timeout_s=timeout_s,
        )
    if not jev_api_key:
        raise ClassifierConfigError(
            "CLASSIFIER_BACKEND=jev needs JEV_API_KEY; Jev is a hosted API and stays off without"
            " a key (set CLASSIFIER_BACKEND=laya or rules instead)"
        )
    return SystemOneClassifier(
        name="jev",
        base_url=jev_base_url,
        api_key=jev_api_key,
        model=jev_model,
        http=http,
        hosted=True,
        timeout_s=timeout_s,
    )
