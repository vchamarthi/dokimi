# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Only the known SDK TypedDict advisories bypass the project's warnings-as-errors policy."""

import warnings

import pytest

SCHEMA_MODULE = "pydantic._internal._generate_schema"
DEVICE_ADVISORY = ("Items 'device_code', 'user_code', 'verification_uri' on TypedDict class '_DeviceCode' "
                   "are using the `ReadOnly` qualifier. Pydantic will not protect items from any mutation "
                   "on dictionary instances.")
ACCESS_ADVISORY = ("Items 'access_token', 'error' on TypedDict class '_AccessTokenPoll' are using the `ReadOnly` "
                   "qualifier. Pydantic will not protect items from any mutation on dictionary instances.")
THINKING_ADVISORY = ("Item 'index' on TypedDict class 'ChatCompletionThinkingBlock' is using the `ReadOnly` "
                     "qualifier. Pydantic will not protect items from any mutation on dictionary instances.")


@pytest.mark.parametrize("message", [DEVICE_ADVISORY, THINKING_ADVISORY, ACCESS_ADVISORY])
def test_known_sdk_readonly_advisory_does_not_interrupt_model_import(message):
    with warnings.catch_warnings(record=True) as emitted:
        warnings.warn_explicit(message, UserWarning, filename="sdk.py", lineno=1, module=SCHEMA_MODULE)
    assert emitted == []


@pytest.mark.parametrize(("message", "module"), [
    (DEVICE_ADVISORY.replace("_DeviceCode", "UnknownClass"), SCHEMA_MODULE),
    (THINKING_ADVISORY.replace("'index'", "'new_field'"), SCHEMA_MODULE),
    (DEVICE_ADVISORY.replace("'verification_uri'", "'new_field'"), SCHEMA_MODULE),
    (ACCESS_ADVISORY.replace("'error'", "'new_field'"), SCHEMA_MODULE),
    ("A different warning from the same schema generator", SCHEMA_MODULE),
    (DEVICE_ADVISORY, "dokimi.example"),
])
def test_unknown_sdk_warnings_still_fail_the_test(message, module):
    with warnings.catch_warnings(), pytest.raises(UserWarning):
        warnings.warn_explicit(message, UserWarning, filename="sdk.py", lineno=1, module=module)
